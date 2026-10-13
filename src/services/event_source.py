# -*- coding: utf-8 -*-
"""高抛低吸助手 · 事件与风险事件源（FR-20）

把每日推送的「事件与风险」板块从**注入位**接到**真实事件源**：

- **主源**：``IntelligenceService``（RSS / 新闻源条目，按自选股名称 / 代码命中）
- **副源**：``AlertService`` 的价格 / 风险型预警触发（可选，缺省不启用）

设计原则（与 P1 一致）：
1. **纯读取**：不写入任何表；
2. **异常隔离**：任何来源失败都降级为空列表，绝不阻断推送（NFR-8）；
3. **无源即静默**：未配置情报源 / 无命中 → 返回 ``[]``，推送侧显示"今日无重大事件提醒"；
4. **可注入**：测试可注入 stub 的 intelligence / alert 服务。

用法::

    from src.services.event_source import EventSource

    provider = EventSource().as_provider()   # 可直接喂给 P1PushService(event_provider=provider)
    events = provider()                      # -> ["【中远海控】...（来源）", ...]

自检::

    python -m src.services.event_source --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "EventSource",
    "EventEntry",
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_MAX_EVENTS",
    "RISK_KEYWORDS",
]

DEFAULT_LOOKBACK_DAYS = 3
DEFAULT_MAX_EVENTS = 8
_TITLE_MAX_LEN = 60
_MAX_ALERTS = 4

# 突发型 / 风险类关键词（FR-20.2）：命中即标记为"风险"，用于文案前缀与排序加权
RISK_KEYWORDS: Tuple[str, ...] = (
    "风险", "警示", "问询", "处罚", "罚款", "立案", "减持", "退市", "st", "商誉",
    "变脸", "预亏", "亏损", "下调", "违约", "监管", "停牌", "爆雷", "暴雷",
)

# 日历型关键词（FR-20.1）：财报、分红、解禁、股东大会等
CALENDAR_KEYWORDS: Tuple[str, ...] = (
    "财报", "业绩", "年报", "季报", "中报", "预约披露", "分红", "派息", "送转",
    "除权", "除息", "解禁", "股东会", "股东大会", "限售",
)


@dataclass
class EventEntry:
    """单条事件（内部结构）。"""

    symbol: str = ""
    name: str = ""
    title: str = ""
    source: str = ""
    published_at: str = ""
    is_risk: bool = False
    is_calendar: bool = False

    @property
    def dedup_key(self) -> str:
        return (self.title or "").strip()

    def to_line(self) -> str:
        who = self.name or self.symbol or "市场"
        title = _truncate(self.title)
        if self.is_risk:
            prefix = f"【风险｜{who}】"
        elif self.is_calendar:
            prefix = f"【{who}·日历】"
        else:
            prefix = f"【{who}】"
        suffix = f"（{self.source}）" if self.source else ""
        return f"{prefix}{title}{suffix}"


def _truncate(text: str, limit: int = _TITLE_MAX_LEN) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _contains_any(text: str, keywords: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in keywords)


class EventSource:
    """把情报 / 预警服务聚合成「事件文案列表」的事件源。"""

    def __init__(
        self,
        *,
        watchlist_service: Any = None,
        intelligence_service: Any = None,
        alert_service: Any = None,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        max_events: int = DEFAULT_MAX_EVENTS,
        risk_only: bool = False,
    ):
        self._watchlist = watchlist_service
        self._intelligence = intelligence_service
        self._alert = alert_service
        self.lookback_days = max(1, int(lookback_days))
        self.max_events = max(1, int(max_events))
        self.risk_only = risk_only

    # ---------------- 懒加载 ---------------- #
    @property
    def watchlist(self):
        if self._watchlist is None:
            from src.services.watchlist_service import WatchlistService

            self._watchlist = WatchlistService()
        return self._watchlist

    @property
    def intelligence(self):
        if self._intelligence is None:
            from src.services.intelligence_service import IntelligenceService

            self._intelligence = IntelligenceService()
        return self._intelligence

    @property
    def alert(self):
        if self._alert is None:
            from src.services.alert_service import AlertService

            self._alert = AlertService()
        return self._alert

    # ---------------- 公共入口 ---------------- #
    def as_provider(self) -> Callable[[], List[str]]:
        """返回可直接注入 ``P1PushService(event_provider=...)`` 的无参可调用对象。"""

        def _provider() -> List[str]:
            return self.collect()

        return _provider

    def collect(self, symbols: Optional[Sequence[str]] = None) -> List[str]:
        """收集事件文案（已排序、已去重、已截断）。任何异常都降级为 ``[]``。"""
        targets = self._resolve_targets(symbols)
        entries: List[EventEntry] = []
        entries.extend(self._collect_intelligence(targets))
        entries.extend(self._collect_alerts(targets))
        return self._render(entries)

    # ---------------- 内部：标的 ---------------- #
    def _resolve_targets(self, symbols: Optional[Sequence[str]]) -> List[Dict[str, str]]:
        """返回 ``[{symbol, name}]``；按需过滤。"""
        try:
            items = self.watchlist.list_watchlist(enabled_only=True)
        except Exception as exc:
            logger.warning("事件源读取自选股失败，降级为空：%s", exc)
            return []
        wanted = None
        if symbols:
            wanted = {str(s).strip().upper() for s in symbols if str(s).strip()}
        targets: List[Dict[str, str]] = []
        for it in items or []:
            symbol = str(it.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            if wanted is not None and symbol not in wanted:
                continue
            targets.append({"symbol": symbol, "name": str(it.get("name") or "").strip()})
        return targets

    # ---------------- 内部：情报源 ---------------- #
    def _collect_intelligence(self, targets: List[Dict[str, str]]) -> List[EventEntry]:
        if not targets:
            return []
        index = self._build_index(targets)
        try:
            payload = self.intelligence.list_items(
                days=self.lookback_days,
                page=1,
                page_size=100,
            )
        except Exception as exc:  # 情报源异常不得阻断
            logger.warning("情报源读取失败，已降级：%s", exc)
            return []

        rows = (payload or {}).get("items") or []
        entries: List[EventEntry] = []
        for row in rows:
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            text = f"{title} {row.get('summary') or ''}"
            hit = self._match_symbol(text, index, row)
            if hit is None:
                continue
            symbol, name = hit
            is_risk = _contains_any(text, RISK_KEYWORDS)
            if self.risk_only and not is_risk:
                continue
            entries.append(
                EventEntry(
                    symbol=symbol,
                    name=name,
                    title=title,
                    source=str(row.get("source_name") or row.get("source") or "").strip(),
                    published_at=str(row.get("published_at") or row.get("fetched_at") or ""),
                    is_risk=is_risk,
                    is_calendar=_contains_any(text, CALENDAR_KEYWORDS),
                )
            )
        return entries

    # ---------------- 内部：预警源 ---------------- #
    def _collect_alerts(self, targets: List[Dict[str, str]]) -> List[EventEntry]:
        if not targets:
            return []
        index = self._build_index(targets)
        try:
            payload = self.alert.list_triggers(page=1, page_size=50)
        except Exception as exc:
            logger.warning("预警源读取失败，已降级：%s", exc)
            return []
        rows = (payload or {}).get("items") or []
        entries: List[EventEntry] = []
        for row in rows:
            symbol = str(row.get("target") or row.get("symbol") or "").strip().upper()
            name = index.get(symbol, "")
            if not symbol or symbol not in index:
                continue
            message = str(row.get("message") or row.get("title") or "").strip()
            if not message:
                continue
            entries.append(
                EventEntry(
                    symbol=symbol,
                    name=name,
                    title=message,
                    source="预警",
                    published_at=str(row.get("created_at") or row.get("triggered_at") or ""),
                    is_risk=True,
                )
            )
            if len(entries) >= _MAX_ALERTS:
                break
        return entries

    # ---------------- 内部：匹配与渲染 ---------------- #
    @staticmethod
    def _build_index(targets: List[Dict[str, str]]) -> Dict[str, str]:
        """``symbol -> name`` 映射（index 的键也用于归属判断）。"""
        return {t["symbol"]: t["name"] for t in targets}

    @staticmethod
    def _match_symbol(
        text: str,
        index: Dict[str, str],
        row: Dict[str, Any],
    ) -> Optional[Tuple[str, str]]:
        """判断一条情报归属哪只标的：先看 scope_value，再按名称 / 代码关键词。"""
        scope_value = str(row.get("scope_value") or "").strip().upper()
        scope_type = str(row.get("scope_type") or "").strip().lower()
        if scope_type == "symbol" and scope_value in index:
            return scope_value, index[scope_value]

        lowered = (text or "").lower()
        for symbol, name in index.items():
            if symbol and symbol.lower() in lowered:
                return symbol, name
        for symbol, name in index.items():
            if name and name in text:
                return symbol, name
        return None

    def _render(self, entries: List[EventEntry]) -> List[str]:
        if not entries:
            return []
        # 1) 去重（标题）
        unique: Dict[str, EventEntry] = {}
        for entry in entries:
            key = entry.dedup_key
            if not key or key in unique:
                continue
            unique[key] = entry
        # 2) 先按发布时间倒序，再稳定分组：风险类优先（组内保持时间倒序）
        ordered = sorted(unique.values(), key=lambda e: e.published_at or "", reverse=True)
        ordered.sort(key=lambda e: 0 if e.is_risk else 1)
        return [e.to_line() for e in ordered[: self.max_events]]


def _main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(description="高抛低吸助手 · 事件源自检（FR-20）")
    parser.add_argument("--symbols", nargs="*", default=None, help="限定标的；缺省为全部启用自选股")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--max-events", type=int, default=DEFAULT_MAX_EVENTS)
    parser.add_argument("--risk-only", action="store_true", help="只要风险类事件")
    parser.add_argument("--dry-run", action="store_true", help="仅采集并打印，不发送")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    source = EventSource(lookback_days=args.lookback_days, max_events=args.max_events, risk_only=args.risk_only)
    events = source.collect(args.symbols)
    if args.json:
        print(json.dumps({"count": len(events), "events": events}, ensure_ascii=False, indent=2))
    else:
        print(f"事件数：{len(events)}")
        for line in events:
            print(f"  · {line}")
        if not events:
            print("  （无命中：未配置情报源，或近期无涉及自选股的事件）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
