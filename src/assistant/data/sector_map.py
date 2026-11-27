# -*- coding: utf-8 -*-
"""个股 → 行业板块映射（D-22，需求 §3.5.2 Tab A 下「涉及板块」）。

为什么单独一个模块：板块区要展示的是「**持仓/自选所属**板块处于什么结构位置」，而板块
强度来自同花顺。两家对同一只股票给的行业名并不完全相同：

- 东财 F10（`RPT_F10_CORETHEME_BOARDTYPE`）给三级行业，且带罗马数字后缀：``银行`` / ``银行Ⅱ``
  / ``股份制银行Ⅲ``；
- 同花顺板块名不带后缀，且大致相当于**二级**（``银行`` / ``白酒`` / ``电池`` / ``保险``）。

所以这里做两件事：① 取东财三级行业 ② 按「二级 → 一级 → 三级」顺序、去掉 ``Ⅱ/Ⅲ`` 后缀后与
同花顺板块名精确匹配。匹配不上的标的**如实算未映射**（需求 SEC-3 `unmapped_holdings`），
不猜、不按名字相似度硬凑——板块错配会直接误导个股判断。

数据源可用性（2026-11-26 实测）：东财 `push2` 系列被风控，而 `datacenter-web` 的 F10 接口正常，
因此这里走 datacenter-web，不碰 push2。

同一个 F10 响应里还带 ``SECURITY_NAME_ABBR``（实测 600000 → 浦发银行），所以顺手承担
D-14 的股票名称来源：**不额外发请求**，名称随板块映射一起缓存（TTL 同 12h）。
名称只用于展示，不参与任何判定；取不到时由调用方退回代码，不编造。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

__all__ = [
    "DEFAULT_INDUSTRY_CACHE",
    "IndustryResolver",
    "SECTOR_ALIASES",
    "SectorRef",
    "normalize_industry_name",
    "to_secucode",
]

logger = logging.getLogger(__name__)

#: 行业缓存落盘位置（`data/` 已被 .gitignore 忽略）
DEFAULT_INDUSTRY_CACHE = Path("data/cache/assistant_industry_map.json")

_EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
#: 匹配优先级：同花顺板块更接近东财二级，二级优先；三级过窄、一级过粗，作后备
_LEVEL_ORDER = ("2", "1", "3")
_SUFFIX = re.compile(r"(Ⅱ|Ⅲ|Ⅱ|Ⅲ|II|III|Ⅰ|I)$")

#: 东财行业名 → 同花顺板块名的**显式**别名（同一行业、两家叫法不同）。
#:
#: 这里刻意用穷举表而不是相似度匹配：`difflib`/编辑距离会把「煤炭开采」错配到
#: 「煤炭开采加工」之外的板块，而板块错配会直接误导个股判断。表里每一条都得是
#: 人工核对过的同一行业——**新加一条要有依据，不是让代码去猜**。
#: 实测来源（2026-11-27，20 只红利 ETF 成分）：5 只未映射全部是「同词异序/带后缀」，
#: 加上这三条后 20/20 全部命中。
SECTOR_ALIASES: dict[str, str] = {
    # 东财二级「煤炭开采」= 同花顺「煤炭开采加工」（600188 兖矿能源、601101 昊华能源）
    "煤炭开采": "煤炭开采加工",
    # 东财二级「铁路公路」= 同花顺「公路铁路运输」（600012 皖通高速）
    "铁路公路": "公路铁路运输",
    # 东财二级「航运港口」= 同花顺「港口航运」（601872 招商轮船、603565 中谷物流）
    "航运港口": "港口航运",
}


def normalize_industry_name(name: str) -> str:
    """去掉东财的三级后缀与空白，得到可与同花顺板块名比较的裸名。"""
    cleaned = (name or "").strip()
    cleaned = _SUFFIX.sub("", cleaned).strip()
    return cleaned


def to_secucode(code: str) -> str:
    """A 股代码 → 东财 ``SECUCODE``（``600000.SH`` / ``000001.SZ``）。"""
    text = (code or "").strip().upper()
    if "." in text:
        return text
    if text.startswith(("6", "5", "9")):
        return f"{text}.SH"
    if text.startswith(("0", "2", "3")):
        return f"{text}.SZ"
    if text.startswith(("4", "8")):
        return f"{text}.BJ"
    return f"{text}.SH"


@dataclass(frozen=True)
class SectorRef:
    """一只标的对应的板块（同花顺口径），带上匹配依据便于排查。"""

    code: str
    sector_name: str
    sector_code: str
    matched_level: str
    raw_industry: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "sector_name": self.sector_name,
            "sector_code": self.sector_code,
            "matched_level": self.matched_level,
            "raw_industry": self.raw_industry,
        }


class IndustryResolver:
    """个股 → 行业板块解析器（内存 + 可选磁盘缓存，失败不抛给主流程）。

    同时是 D-14 的**名称来源**：默认取数路径会在同一次 F10 请求里捕获
    ``SECURITY_NAME_ABBR``，因此 `name()` 与 `industries()` 共用一份缓存，不重复联网。
    """

    def __init__(
        self,
        *,
        fetch_industries: Callable[[str], Sequence[tuple[str, str]]] | None = None,
        fetch_name: Callable[[str], str] | None = None,
        cache_path: Path | None = DEFAULT_INDUSTRY_CACHE,
        ttl_hours: float = 12.0,
        timeout: float = 8.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._fetch = fetch_industries or self._fetch_from_eastmoney
        #: 名称单独取数入口（仅测试/特殊源用）；None 表示「跟着行业请求一起拿」
        self._fetch_name = fetch_name
        self.cache_path = cache_path
        self.ttl_seconds = max(0.0, ttl_hours) * 3600
        self.timeout = timeout
        self._clock = clock or time.time
        self._memory: dict[str, tuple[float, list[tuple[str, str]]]] = {}
        self._names: dict[str, tuple[float, str]] = {}
        self._disk: dict[str, dict] | None = None
        self._disk_dirty = False

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------
    def industries(self, code: str) -> list[tuple[str, str]]:
        """返回 ``[(board_level, industry_name), ...]``；取不到返回空列表（不编造）。"""
        now = self._clock()
        hit = self._memory.get(code)
        if hit and (self.ttl_seconds <= 0 or now - hit[0] < self.ttl_seconds):
            return list(hit[1])

        disk = self._load_disk().get(code)
        if disk and self.ttl_seconds > 0:
            age = now - float(disk.get("ts") or 0)
            if age < self.ttl_seconds:
                rows = [(str(lvl), str(name)) for lvl, name in disk.get("rows") or []]
                self._memory[code] = (now, rows)
                if disk.get("name"):
                    self._names[code] = (now, str(disk["name"]))
                return list(rows)

        try:
            rows = [(str(level), str(name)) for level, name in self._fetch(code) if name]
        except Exception as exc:  # noqa: BLE001 - 单只失败只算未映射
            logger.warning("个股 %s 行业取数失败：%s: %s", code, type(exc).__name__, exc)
            return []
        self._memory[code] = (now, rows)
        self._store_disk(code, rows, now)
        return list(rows)

    def name(self, code: str) -> str:
        """股票简称（D-14）。取不到返回空串，由调用方退回代码，**不编造**。

        缓存顺序：内存 → 磁盘 → 取数。默认取数路径与 :meth:`industries` 共用一次 F10
        请求，所以先用行业（板块映射）再用名称时不会多打一次网络。
        """
        now = self._clock()
        if self._fresh(self._names.get(code)):
            return self._names[code][1]

        disk = self._load_disk().get(code)
        if disk and disk.get("name") and self._fresh((float(disk.get("ts") or 0), "")):
            text = str(disk["name"]).strip()
            self._names[code] = (now, text)
            return text

        if self._fetch_name is None:
            # 默认路径：一次 F10 请求同时拿行业与名称（捕获在 _fetch_from_eastmoney 里）
            self.industries(code)
            hit = self._names.get(code)
            return hit[1] if hit and self._fresh(hit) else ""

        try:
            fetched = (self._fetch_name(code) or "").strip()
        except Exception as exc:  # noqa: BLE001 - 名称拿不到只退回代码
            logger.warning("个股 %s 名称取数失败：%s: %s", code, type(exc).__name__, exc)
            return ""
        if fetched:
            self._remember_name(code, fetched, now)
        return fetched

    def _fresh(self, hit: tuple[float, str] | None) -> bool:
        if not hit:
            return False
        return self.ttl_seconds <= 0 or self._clock() - hit[0] < self.ttl_seconds

    def resolve(self, codes: Sequence[str], boards: Mapping[str, str]) -> dict[str, SectorRef]:
        """把代码映射到同花顺板块；``boards`` 是 ``板块名 → 板块代码``。

        一个板块名匹配不上就换下一个候选层级；精确名不中时再查一次
        :data:`SECTOR_ALIASES`（东财叫法与同花顺不同但确属同一行业），全不中即视为
        未映射（不出现在返回值里）。别名命中会留日志——**这是人工对照的结果，
        不是相似度猜的**，出问题时能一眼看出是哪条规则把它接上的。
        """
        resolved: dict[str, SectorRef] = {}
        for code in codes:
            rows = self.industries(code)
            if not rows:
                continue
            by_level: dict[str, list[str]] = {}
            for level, name in rows:
                by_level.setdefault(level, []).append(name)
            for level in _LEVEL_ORDER:
                for raw in by_level.get(level, []):
                    name = normalize_industry_name(raw)
                    target = name
                    sector_code = boards.get(name)
                    via_alias = False
                    if not sector_code:
                        alias = SECTOR_ALIASES.get(name)
                        if alias and boards.get(alias):
                            target, sector_code, via_alias = alias, boards[alias], True
                    if sector_code:
                        if via_alias:
                            logger.info(
                                "个股 %s 的行业 %r 按别名映射到同花顺板块 %r", code, name, target
                            )
                        resolved[code] = SectorRef(
                            code=code,
                            sector_name=target,
                            sector_code=sector_code,
                            matched_level=level,
                            raw_industry=raw,
                        )
                        break
                if code in resolved:
                    break
            if code not in resolved:
                logger.info("个股 %s 的行业未能匹配到同花顺板块：%s（按未映射处理）", code, rows)
        return resolved

    # ------------------------------------------------------------------
    # 数据源
    # ------------------------------------------------------------------
    def _fetch_from_eastmoney(self, code: str) -> list[tuple[str, str]]:
        rows = self._get_f10(code)
        name = next(
            (
                str(row.get("SECURITY_NAME_ABBR")).strip()
                for row in rows
                if row.get("SECURITY_NAME_ABBR")
            ),
            "",
        )
        if name:
            # 同一份响应里就有名称（D-14），顺手缓存，避免为展示再多打一次请求
            self._remember_name(code, name, self._clock())
        return [
            (str(row.get("BOARD_LEVEL")), str(row.get("BOARD_NAME")))
            for row in rows
            if row.get("BOARD_TYPE") == "行业"
        ]

    def _get_f10(self, code: str) -> list[dict]:
        import requests  # noqa: PLC0415 - 仅真实取数时需要

        params = {
            "reportName": "RPT_F10_CORETHEME_BOARDTYPE",
            "columns": "ALL",
            "filter": f'(SECUCODE="{to_secucode(code)}")',
            "pageNumber": 1,
            "pageSize": 60,
            "source": "HSF10",
            "client": "PC",
        }
        response = requests.get(
            _EM_URL,
            params=params,
            timeout=self.timeout,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://emweb.securities.eastmoney.com/",
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise RuntimeError(f"东财 F10 返回失败：{payload.get('message')}")
        return ((payload.get("result") or {}).get("data")) or []

    # ------------------------------------------------------------------
    # 磁盘缓存
    # ------------------------------------------------------------------
    def _load_disk(self) -> dict[str, dict]:
        if self._disk is not None:
            return self._disk
        self._disk = {}
        if self.cache_path and self.cache_path.is_file():
            try:
                raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._disk = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
            except (OSError, ValueError) as exc:
                logger.warning("行业缓存读不出来，按空缓存处理：%s", exc)
        return self._disk

    def _store_disk(self, code: str, rows: Sequence[tuple[str, str]], now: float) -> None:
        cache = self._load_disk()
        entry = cache.get(code) or {}
        cache[code] = {
            "ts": now,
            "updated_at": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
            "rows": [[level, name] for level, name in rows],
            # 名称与行业同一次请求拿到，写行时不能把它抹掉
            "name": entry.get("name") or self._name_of(code),
        }
        self._persist(cache)

    def _remember_name(self, code: str, name: str, now: float) -> None:
        """缓存股票简称：内存 + 磁盘（保留已有行业行，不触发额外请求）。"""
        text = (name or "").strip()
        if not text:
            return
        self._names[code] = (now, text)
        if not self.cache_path:
            return
        cache = self._load_disk()
        entry = dict(cache.get(code) or {})
        entry.setdefault("ts", now)
        entry["name"] = text
        entry.setdefault("rows", [])
        cache[code] = entry
        self._persist(cache)

    def _name_of(self, code: str) -> str:
        hit = self._names.get(code)
        return hit[1] if hit else ""

    def _persist(self, cache: Mapping[str, dict]) -> None:
        self._disk = dict(cache)
        self._disk_dirty = True
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
            )
            self._disk_dirty = False
        except OSError as exc:  # 缓存写失败不影响本次结果
            logger.warning("行业缓存写入失败（不影响本次映射）：%s", exc)
