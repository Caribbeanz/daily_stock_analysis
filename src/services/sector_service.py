# -*- coding: utf-8 -*-
"""高抛低吸助手 · 板块归属与行业排行（M2-B）

职责：

1. 取个股**所属行业**（东财口径，与行业板块同源）；
2. 取**全量行业板块快照**（涨跌幅 / 涨跌家数），计算该股所属板块的当日排名；
3. 计算「个股 vs 板块」相对强弱，并产出一句人话结论。

设计约束（对应 `docs/requirements/hslb-mobile-h5-iteration2-requirements-v1.0.md` §4.3）：

- **同源**：行业归属与板块排行都用东财口径；Baostock 申万分类与东财板块名不是一套，
  不参与匹配（本轮也不接入，避免登录开销）；
- **缓存带交易日**：快照缓存 key 含日期，跨日自动失效，避免次日仍用昨日排行；
- **失败隔离**：取数失败只降级板块块，不阻断整屏；排名匹配不到时显式
  ``ranking_available=false``，不猜、不硬凑；
- **不做投资建议**：相对强弱未剔除市值因子，文案限定为"仅作参考"。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "SectorService",
    "normalize_industry_name",
    "build_sector_verdict",
]

# 板块名称归一化：去"板块"后缀、统一括号、去空白
_BOARD_SUFFIXES = ("板块", "行业", "指数")
_PAREN_MAP = {"（": "(", "）": ")", "【": "(", "】": ")"}


def normalize_industry_name(name: Any) -> str:
    """把行业 / 板块名归一化到可比形式（东财个股行业 vs 板块名之间常见的差异）。"""

    text = str(name or "").strip()
    if not text:
        return ""
    for source, target in _PAREN_MAP.items():
        text = text.replace(source, target)
    text = re.sub(r"\s+", "", text)
    for suffix in _BOARD_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    return text


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # 过滤 NaN


def build_sector_verdict(detail: Dict[str, Any]) -> str:
    """由结构化板块数据生成一句结论（唯一文案来源，前端不再二次拼接）。"""

    industry = str(detail.get("industry") or "").strip()
    change = _finite(detail.get("change_pct"))
    relative = _finite(detail.get("relative_pct"))
    rank = detail.get("rank")
    total = detail.get("total")

    if not industry:
        return "行业归属暂不可用"
    if change is None:
        return f"所属行业：{industry}；板块行情暂不可用"

    head = f"{industry}板块 {change:+.2f}%"
    if detail.get("ranking_available") and rank and total:
        head += f"（{rank}/{total}）"

    parts = [head]
    if relative is not None:
        if relative > 1:
            parts.append(f"个股 {relative:+.2f}%，强于板块（有独立逻辑）")
        elif relative < -1:
            parts.append(f"个股 {relative:+.2f}%，弱于板块（跑输行业）")
        else:
            parts.append(f"个股 {relative:+.2f}%，与板块基本同步")

    if change > 0:
        rank_ratio = (rank / total) if (detail.get("ranking_available") and rank and total) else None
        parts.append("板块强势" if rank_ratio is not None and rank_ratio <= 0.2 else "板块温和跟涨")
    elif change < 0:
        rank_ratio = (rank / total) if (detail.get("ranking_available") and rank and total) else None
        parts.append("板块走弱" if rank_ratio is not None and rank_ratio > 0.8 else "板块回调")
    else:
        parts.append("板块持平")

    verdict = "；".join(parts) + "。"
    if not detail.get("ranking_available"):
        verdict += "板块排名暂不可用。"
    verdict += "相对强弱未剔除市值因子，仅作参考，不构成换股建议。"
    return verdict


class SectorService:
    """板块归属 + 行业排行。依赖可注入，便于离线单测。"""

    def __init__(
        self,
        *,
        snapshot_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None,
        industry_loader: Optional[Callable[[str], Optional[str]]] = None,
        snapshot_ttl_seconds: int = 900,
        industry_ttl_seconds: int = 604800,
        now: Optional[Callable[[], Any]] = None,
    ):
        self._snapshot_provider = snapshot_provider
        self._industry_loader = industry_loader
        self.snapshot_ttl_seconds = max(0, int(snapshot_ttl_seconds))
        self.industry_ttl_seconds = max(0, int(industry_ttl_seconds))
        self._now = now or (lambda: date.today())
        self._lock = threading.Lock()
        self._snapshot_cache: Dict[str, Any] = {}
        self._industry_cache: Dict[str, Any] = {}

    # ---------------- 依赖 ---------------- #
    def _default_snapshot_provider(self) -> List[Dict[str, Any]]:
        from data_provider.base import DataFetcherManager

        return list(DataFetcherManager().get_sector_quotes() or [])

    def _default_industry_loader(self, symbol: str) -> Optional[str]:
        """东财个股信息里的「行业」字段。失败返回 None（不阻断整屏）。"""
        try:
            import akshare as ak
        except Exception as exc:  # pragma: no cover - 依赖缺失
            logger.warning("akshare 不可用，行业归属跳过：%s", exc)
            return None

        normalized = str(symbol or "").strip().split(".")[0]
        for candidate in (normalized, f"sh{normalized}", f"sz{normalized}"):
            try:
                df = ak.stock_individual_info_em(symbol=candidate)
            except Exception as exc:  # pragma: no cover - 依赖外部网络
                logger.debug("行业归属取数失败 %s：%s", candidate, exc)
                continue
            if df is None or getattr(df, "empty", True):
                continue
            try:
                item_col = "item" if "item" in df.columns else df.columns[0]
                value_col = "value" if "value" in df.columns else df.columns[1]
                for _, row in df.iterrows():
                    if str(row.get(item_col) or "").strip() in {"行业", "所属行业"}:
                        industry = str(row.get(value_col) or "").strip()
                        if industry:
                            return industry
            except Exception as exc:  # pragma: no cover - 数据格式异常
                logger.debug("行业归属解析失败 %s：%s", candidate, exc)
        return None

    # ---------------- 缓存 ---------------- #
    def _today_key(self) -> str:
        moment = self._now()
        if hasattr(moment, "strftime"):
            return moment.strftime("%Y-%m-%d")
        return str(moment)

    def _cache_read(self, cache: Dict[str, Any], key: str, ttl: int) -> Optional[Any]:
        if ttl <= 0:
            return None
        hit = cache.get(key)
        if not hit:
            return None
        stamp, value = hit
        if (time.time() - stamp) > ttl:
            cache.pop(key, None)
            return None
        return value

    def _cache_write(self, cache: Dict[str, Any], key: str, value: Any, ttl: int) -> Any:
        if ttl > 0:
            cache[key] = (time.time(), value)
        return value

    # ---------------- 取数 ---------------- #
    def snapshot(self) -> List[Dict[str, Any]]:
        """全量行业板块快照（缓存 key 含交易日）。"""
        key = self._today_key()
        with self._lock:
            cached = self._cache_read(self._snapshot_cache, key, self.snapshot_ttl_seconds)
        if cached is not None:
            return list(cached)

        provider = self._snapshot_provider or self._default_snapshot_provider
        try:
            rows = list(provider() or [])
        except Exception as exc:
            logger.warning("板块全量快照获取失败：%s", exc)
            rows = []

        cleaned: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = normalize_industry_name(row.get("name"))
            change = _finite(row.get("change_pct"))
            if not name or change is None:
                continue
            cleaned.append(
                {
                    "name": name,
                    "raw_name": str(row.get("name") or "").strip(),
                    "change_pct": round(change, 3),
                    "advancers": row.get("advancers"),
                    "decliners": row.get("decliners"),
                }
            )

        with self._lock:
            return list(self._cache_write(self._snapshot_cache, key, cleaned, self.snapshot_ttl_seconds))

    def industry(self, symbol: str) -> Optional[str]:
        """个股所属行业（缓存 7 天）。"""
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            return None
        with self._lock:
            cached = self._cache_read(self._industry_cache, normalized, self.industry_ttl_seconds)
        if cached is not None:
            return cached or None

        loader = self._industry_loader or self._default_industry_loader
        try:
            industry = loader(normalized)
        except Exception as exc:
            logger.warning("行业归属取数失败 %s：%s", normalized, exc)
            industry = None
        industry = str(industry or "").strip() or None
        with self._lock:
            self._cache_write(self._industry_cache, normalized, industry or "", self.industry_ttl_seconds)
        return industry

    # ---------------- 分析 ---------------- #
    def analyze(self, symbol: str, stock_change_pct: Optional[float]) -> Dict[str, Any]:
        """返回板块结构化数据 + 一句结论；任何失败都降级为 available=False。"""

        industry = self.industry(symbol)
        if not industry:
            detail = {
                "industry": "",
                "change_pct": None,
                "rank": None,
                "total": None,
                "relative_pct": None,
                "advancers": None,
                "decliners": None,
                "ranking_available": False,
            }
            return {"available": False, "detail": detail, "verdict": "行业归属暂不可用"}

        normalized_industry = normalize_industry_name(industry)
        snapshot = self.snapshot()
        quoted = sorted(snapshot, key=lambda item: item["change_pct"], reverse=True)
        total = len(quoted)
        matched = next((item for item in quoted if item["name"] == normalized_industry), None)

        if matched is None:
            detail = {
                "industry": industry,
                "change_pct": None,
                "rank": None,
                "total": total or None,
                "relative_pct": None,
                "advancers": None,
                "decliners": None,
                "ranking_available": False,
            }
            return {
                "available": False,
                "detail": detail,
                "verdict": f"所属行业：{industry}；行业归属已获取，但板块行情暂未匹配到该行业",
            }

        rank = quoted.index(matched) + 1
        sector_change = matched["change_pct"]
        relative = None
        if stock_change_pct is not None:
            relative = round(float(stock_change_pct) - sector_change, 2)

        detail = {
            "industry": industry,
            "change_pct": sector_change,
            "rank": rank,
            "total": total,
            "relative_pct": relative,
            "advancers": matched.get("advancers"),
            "decliners": matched.get("decliners"),
            "ranking_available": True,
        }
        return {"available": True, "detail": detail, "verdict": build_sector_verdict(detail)}
