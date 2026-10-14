# -*- coding: utf-8 -*-
"""高抛低吸助手 · P4 大屏看板（FR-14.1）

收盘后把「自选股结论 + 行情 + 持仓 + 指数 + 事件 + 本周复盘摘要」聚合成**一屏快照**，
供浏览器全屏、免登录常驻展示：

| 区块 | 内容 | 需求 |
| --- | --- | --- |
| 指数条 | 上证 / 深证 / 创业板（当日涨跌） | FR-14.1.2 |
| 自选股卡片 | 人话结论 + 四要素 + 评分 + 涨跌 + 距止损/目标 | FR-14.1.2 |
| 持仓汇总 | 总成本 / 市值 / 盈亏 / 盈亏率 | FR-14.1.2 |
| 事件提醒 | 事件源聚合（尽力而为） | FR-14.1.2 / FR-20 |
| 复盘摘要 | 本周胜率 / 本周持仓盈亏（复用 P3 服务） | FR-14.1.2 |
| 数据截止 | ``market_date`` + ``quote_as_of`` 显著标注 | FR-14.1.3 |

设计约束：

1. **只读**：不改动任何业务数据，不引入写通道；**不做盘中实时监控与分析**
   （NG4 / FR-14.1.1，仅展示收盘后数据与报告）；
2. **四端同源（NFR-9）**：个股结论直接复用 H5 的 :class:`StockOverviewService`
   （其数据源为已落库 ``decision_signals``），**不重算指标、不另立口径**；
3. **分块异常隔离（NFR-8）**：指数 / 事件 / 复盘任一失败只记入 ``degraded``，
   不阻断整屏；单只标的失败只影响该卡片；
4. **数据截止可见（FR-14.1.3）**：显式给出 ``market_date``（最新交易日）与
   ``quote_as_of``（行情时间），前端显著标注，避免"看起来实时"；
5. **可选快照缓存**：``cache_ttl_seconds > 0`` 时对相同入参做短暂缓存，
   避免大屏手动刷新打爆数据源（阈值可配置，NFR-6）。

用法::

    python -m src.services.screen_service --json
    python -m src.services.screen_service --symbols 601919 513100
"""

from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.services.daily_digest import ACTION_LABELS, DISCLAIMER

logger = logging.getLogger(__name__)

__all__ = [
    "DISCLAIMER",
    "ACTION_ORDER",
    "SCREEN_PATH",
    "WEEKLY_REVIEW_PATH",
    "ScreenCard",
    "ScreenIndex",
    "ScreenPortfolio",
    "ScreenSnapshot",
    "ScreenService",
    "summarize_portfolio",
]

#: 大屏页面路由（前端 SPA）与复盘页路由
SCREEN_PATH = "/screen"
WEEKLY_REVIEW_PATH = "/review"

#: 动作展示顺序（稳定排序；未知动作排在最后）
ACTION_ORDER: Tuple[str, ...] = (
    "build",
    "buy",
    "dip_buy",
    "add",
    "reduce",
    "sell",
    "clear",
    "hold",
    "watch",
    "avoid",
)


# --------------------------------------------------------------------------- #
# 数值工具（与推送 / H5 同口径的宽松解析）
# --------------------------------------------------------------------------- #
def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: Optional[float], digits: int = 3) -> Optional[float]:
    return None if value is None else round(value, digits)


def _as_date(value: Any) -> str:
    """把日期对象 / 字符串归一成 ``YYYY-MM-DD``（失败返回空串）。"""
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%Y-%m-%d")
        except Exception:  # pragma: no cover - 极端自定义对象
            return ""
    text = str(value).strip()
    return text[:10] if len(text) >= 10 else text


def _last_value(bars: Any, column: str) -> Optional[float]:
    try:
        return _to_float(bars[column].iloc[-1])
    except Exception:
        return None


def _prev_value(bars: Any, column: str) -> Optional[float]:
    try:
        if len(bars) < 2:
            return None
        return _to_float(bars[column].iloc[-2])
    except Exception:
        return None


def _last_date(bars: Any) -> str:
    try:
        if "date" in bars.columns:
            return _as_date(bars["date"].iloc[-1])
        return _as_date(bars.index[-1])
    except Exception:
        return ""


def _normalize_stamp(value: Any) -> str:
    """把行情时间归一成 ``YYYY-MM-DD HH:MM:SS``（容忍 ISO 的 ``T`` 与微秒）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    return text.replace("T", " ").split(".")[0][:19]


def _normalize_symbols(symbols: Optional[Sequence[str]]) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in symbols or []:
        symbol = str(raw or "").strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ScreenIndex:
    """指数条的一项。"""

    code: str = ""
    name: str = ""
    value: Optional[float] = None
    change_pct: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "value": self.value,
            "change_pct": self.change_pct,
        }


@dataclass
class ScreenCard:
    """自选股卡片（大屏一块）。"""

    symbol: str = ""
    name: str = ""

    # 行情
    price: Optional[float] = None
    change_pct: Optional[float] = None
    quote_time: Optional[str] = None

    # 结论（人话 + 四要素）
    action: str = "watch"
    action_label: str = "观望"
    scenario_label: str = "趋势不明"
    conclusion: str = ""
    is_urgent: bool = False
    score: Optional[int] = None
    entry_zone: str = "-"
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None
    position_ratio_label: str = "-"
    distance_to_stop_pct: Optional[float] = None
    distance_to_target_pct: Optional[float] = None
    signal_created_at: Optional[str] = None

    # 持仓（有持仓时填充）
    has_position: bool = False
    quantity: float = 0.0
    avg_cost: Optional[float] = None
    pnl_pct: Optional[float] = None
    position_value: Optional[float] = None

    degraded: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "price": self.price,
            "change_pct": self.change_pct,
            "quote_time": self.quote_time,
            "action": self.action,
            "action_label": self.action_label,
            "scenario_label": self.scenario_label,
            "conclusion": self.conclusion,
            "is_urgent": self.is_urgent,
            "score": self.score,
            "entry_zone": self.entry_zone,
            "stop_loss": self.stop_loss,
            "target_price": self.target_price,
            "position_ratio_label": self.position_ratio_label,
            "distance_to_stop_pct": self.distance_to_stop_pct,
            "distance_to_target_pct": self.distance_to_target_pct,
            "signal_created_at": self.signal_created_at,
            "has_position": self.has_position,
            "quantity": self.quantity,
            "avg_cost": self.avg_cost,
            "pnl_pct": self.pnl_pct,
            "position_value": self.position_value,
            "degraded": list(self.degraded),
        }


@dataclass
class ScreenPortfolio:
    """持仓汇总（大屏顶部条）。"""

    position_count: int = 0
    priced_count: int = 0
    total_cost: Optional[float] = None
    market_value: Optional[float] = None
    pnl_amount: Optional[float] = None
    pnl_pct: Optional[float] = None
    winners: int = 0
    losers: int = 0
    complete: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "position_count": self.position_count,
            "priced_count": self.priced_count,
            "total_cost": self.total_cost,
            "market_value": self.market_value,
            "pnl_amount": self.pnl_amount,
            "pnl_pct": self.pnl_pct,
            "winners": self.winners,
            "losers": self.losers,
            "complete": self.complete,
        }


@dataclass
class ScreenSnapshot:
    """一屏快照（大屏唯一数据入口）。"""

    generated_at: str = ""
    market_date: str = ""
    quote_as_of: str = ""
    refresh_seconds: int = 300

    indices: List[ScreenIndex] = field(default_factory=list)
    cards: List[ScreenCard] = field(default_factory=list)
    portfolio: ScreenPortfolio = field(default_factory=ScreenPortfolio)
    events: List[str] = field(default_factory=list)
    weekly: Dict[str, Any] = field(default_factory=dict)

    action_counts: Dict[str, int] = field(default_factory=dict)
    urgent_count: int = 0
    degraded: List[str] = field(default_factory=list)
    disclaimer: str = DISCLAIMER

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "market_date": self.market_date,
            "quote_as_of": self.quote_as_of,
            "refresh_seconds": self.refresh_seconds,
            "indices": [item.to_dict() for item in self.indices],
            "cards": [card.to_dict() for card in self.cards],
            "portfolio": self.portfolio.to_dict(),
            "events": list(self.events),
            "weekly": dict(self.weekly),
            "action_counts": dict(self.action_counts),
            "urgent_count": self.urgent_count,
            "degraded": list(self.degraded),
            "disclaimer": self.disclaimer,
        }


# --------------------------------------------------------------------------- #
# 纯函数组装
# --------------------------------------------------------------------------- #
def summarize_portfolio(
    positions: Mapping[str, Any],
    cards: Sequence[ScreenCard],
) -> Tuple[ScreenPortfolio, List[str]]:
    """由持仓视图 + 已取到价格的卡片汇总总成本 / 市值 / 盈亏（纯函数）。

    返回 ``(汇总, 降级说明)``；**部分持仓无行情时不臆造盈亏**，只标 ``complete=False``。
    """

    prices = {card.symbol: card.price for card in cards if card.price is not None}
    summary = ScreenPortfolio()
    notes: List[str] = []

    total_cost = 0.0
    market_value = 0.0
    unpriced: List[str] = []

    for raw_symbol, view in (positions or {}).items():
        symbol = str(raw_symbol or "").strip().upper()
        quantity = _to_float(getattr(view, "quantity", None)) or 0.0
        avg_cost = _to_float(getattr(view, "avg_cost", None)) or 0.0
        if not symbol or quantity <= 0:
            continue
        summary.position_count += 1
        total_cost += avg_cost * quantity

        price = prices.get(symbol)
        if price is None:
            unpriced.append(symbol)
            continue

        summary.priced_count += 1
        market_value += price * quantity
        if avg_cost > 0:
            if price > avg_cost:
                summary.winners += 1
            elif price < avg_cost:
                summary.losers += 1

    if summary.position_count == 0:
        return summary, notes

    summary.total_cost = _round(total_cost, 2)
    if unpriced:
        summary.complete = False
        notes.append(f"部分持仓缺少行情（{', '.join(unpriced[:5])}），盈亏暂不展示")
        return summary, notes

    summary.market_value = _round(market_value, 2)
    if summary.total_cost:
        summary.pnl_amount = _round(market_value - total_cost, 2)
        summary.pnl_pct = _round((market_value - total_cost) / total_cost * 100.0, 3)
    return summary, notes


def _rank_action(action: str) -> int:
    try:
        return ACTION_ORDER.index(action)
    except ValueError:
        return len(ACTION_ORDER)


# --------------------------------------------------------------------------- #
# 编排服务（依赖全可注入，离线可测）
# --------------------------------------------------------------------------- #
class ScreenService:
    """大屏快照聚合编排。"""

    def __init__(
        self,
        *,
        overview_service: Any = None,
        watchlist_service: Any = None,
        position_service: Any = None,
        event_source: Any = None,
        review_service: Any = None,
        fetch: Any = None,
        max_workers: int = 6,
        fetch_days: int = 20,
        refresh_seconds: int = 300,
        cache_ttl_seconds: int = 0,
        now: Any = None,
    ):
        self._overview_service = overview_service
        self._watchlist_service = watchlist_service
        self._position_service = position_service
        self._event_source = event_source
        self._review_service = review_service
        self._fetch = fetch
        self.max_workers = max(1, int(max_workers))
        self.fetch_days = max(2, int(fetch_days))
        self.refresh_seconds = max(0, int(refresh_seconds))
        self.cache_ttl_seconds = max(0, int(cache_ttl_seconds))
        self._now = now
        self._cache: Dict[Any, Tuple[float, Dict[str, Any]]] = {}

    # ---------------- 依赖（惰性） ---------------- #
    @property
    def overview_service(self):
        if self._overview_service is None:
            from src.services.stock_overview_service import StockOverviewService

            self._overview_service = StockOverviewService()
        return self._overview_service

    @property
    def watchlist_service(self):
        if self._watchlist_service is None:
            from src.services.watchlist_service import WatchlistService

            self._watchlist_service = WatchlistService()
        return self._watchlist_service

    @property
    def position_service(self):
        if self._position_service is None:
            from src.services.position_service import PositionService

            self._position_service = PositionService()
        return self._position_service

    @property
    def event_source(self):
        if self._event_source is None:
            from src.services.event_source import EventSource

            self._event_source = EventSource()
        return self._event_source

    @property
    def review_service(self):
        """本周复盘摘要来源：**不做大盘/热点取数**（大屏已有指数条），并关闭结果刷新。"""
        if self._review_service is None:
            from src.services.weekly_review_service import WeeklyReviewService

            self._review_service = WeeklyReviewService(
                refresh_outcomes=False,
                max_next_week=0,
                market_provider=lambda _week_start: [],
                hotspot_provider=lambda: [],
            )
        return self._review_service

    # ---------------- 取数 ---------------- #
    def _fetch_daily(self, symbol: str, days: int):  # pragma: no cover - 依赖网络
        if self._fetch is not None:
            return self._fetch(symbol, days)
        from src.services.alert_indicators import normalize_ohlcv
        from src.services.p0_signal_builder import P0SignalBuilder

        raw = P0SignalBuilder._fetch_daily(symbol, days)  # noqa: SLF001 - 复用同口径
        if raw is None:
            return None
        return normalize_ohlcv(raw, required_columns=("open", "high", "low", "close", "volume"))

    def _safe_fetch(self, symbol: str) -> Any:
        try:
            return self._fetch_daily(symbol, self.fetch_days)
        except Exception as exc:
            logger.warning("大屏取数失败 %s：%s", symbol, exc)
            return None

    # ---------------- 各区块 ---------------- #
    def _load_positions(self, degraded: List[str]) -> Dict[str, Any]:
        try:
            return dict(self.position_service.list_positions() or {})
        except Exception as exc:
            logger.warning("读取持仓失败，大屏按无持仓展示：%s", exc)
            degraded.append(f"持仓读取失败（{type(exc).__name__}），持仓区块暂缺")
            return {}

    def _load_watchlist(self, degraded: List[str]) -> Tuple[List[str], Dict[str, str]]:
        symbols: List[str] = []
        names: Dict[str, str] = {}
        try:
            rows = self.watchlist_service.list_watchlist(enabled_only=True) or []
        except Exception as exc:
            logger.warning("读取自选股失败：%s", exc)
            degraded.append(f"自选股读取失败（{type(exc).__name__}），仅展示持仓标的")
            return symbols, names
        for row in rows:
            data = row if isinstance(row, dict) else {}
            symbol = str(data.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            symbols.append(symbol)
            name = str(data.get("name") or "").strip()
            if name:
                names[symbol] = name
        return _normalize_symbols(symbols), names

    def _resolve_symbols(
        self,
        explicit: Optional[Sequence[str]],
        positions: Mapping[str, Any],
        degraded: List[str],
    ) -> Tuple[List[str], Dict[str, str]]:
        requested = _normalize_symbols(explicit)
        watch_symbols, names = self._load_watchlist(degraded)
        if requested:
            return requested, names
        merged = list(watch_symbols)
        seen = set(merged)
        for raw_symbol in positions:
            symbol = str(raw_symbol or "").strip().upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                merged.append(symbol)
        return merged, names

    def _build_card(
        self,
        symbol: str,
        position: Any,
        names: Mapping[str, str],
        with_quote: bool,
    ) -> ScreenCard:
        payload = self.overview_service.load(symbol, with_quote=with_quote) or {}
        quote = payload.get("quote") if isinstance(payload.get("quote"), dict) else {}

        card = ScreenCard(
            symbol=symbol,
            name=str(payload.get("name") or names.get(symbol) or "").strip(),
            price=_to_float(quote.get("current_price")),
            change_pct=_to_float(quote.get("change_percent")),
            quote_time=str(quote.get("update_time") or "").strip() or None,
            action=str(payload.get("action") or "watch").strip().lower() or "watch",
            action_label=str(payload.get("action_label") or "").strip() or "观望",
            scenario_label=str(payload.get("scenario_label") or "").strip() or "趋势不明",
            conclusion=str(payload.get("conclusion") or "").strip(),
            is_urgent=bool(payload.get("is_urgent")),
            score=int(payload["score"]) if isinstance(payload.get("score"), (int, float)) else None,
            entry_zone=str(payload.get("entry_zone") or "-").strip() or "-",
            stop_loss=_to_float(payload.get("stop_loss")),
            target_price=_to_float(payload.get("target_price")),
            position_ratio_label=str(payload.get("position_ratio_label") or "-").strip() or "-",
            distance_to_stop_pct=_to_float(payload.get("distance_to_stop_pct")),
            distance_to_target_pct=_to_float(payload.get("distance_to_target_pct")),
            signal_created_at=(
                str(payload.get("signal_created_at")) if payload.get("signal_created_at") else None
            ),
            degraded=[str(item) for item in (payload.get("degraded") or [])],
        )

        quantity = _to_float(getattr(position, "quantity", None)) or 0.0
        avg_cost = _to_float(getattr(position, "avg_cost", None)) or 0.0
        if position is not None and quantity > 0:
            card.has_position = True
            card.quantity = quantity
            card.avg_cost = _round(avg_cost, 4) if avg_cost else None
            if card.price is not None:
                card.position_value = _round(card.price * quantity, 2)
                if avg_cost > 0:
                    card.pnl_pct = _round((card.price - avg_cost) / avg_cost * 100.0, 3)
        return card

    def _load_cards(
        self,
        symbols: Sequence[str],
        positions: Mapping[str, Any],
        names: Mapping[str, str],
        with_quote: bool,
        degraded: List[str],
    ) -> List[ScreenCard]:
        if not symbols:
            return []

        def worker(symbol: str) -> ScreenCard:
            return self._build_card(symbol, positions.get(symbol), names, with_quote)

        results: Dict[str, ScreenCard] = {}
        workers = max(1, min(self.max_workers, len(symbols)))
        if workers == 1:
            for symbol in symbols:
                results[symbol] = self._guarded_card(worker, symbol, degraded)
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="screen-card") as pool:
                futures = {pool.submit(worker, symbol): symbol for symbol in symbols}
                for future in as_completed(futures):
                    symbol = futures[future]
                    try:
                        results[symbol] = future.result()
                    except Exception as exc:  # 单只失败不影响整屏
                        logger.warning("大屏卡片 %s 聚合失败：%s", symbol, exc)
                        degraded.append(f"{symbol} 聚合失败（{type(exc).__name__}）")
                        results[symbol] = ScreenCard(
                            symbol=symbol,
                            name=names.get(symbol) or "",
                            degraded=[f"聚合失败（{type(exc).__name__}）"],
                        )
        return [results[symbol] for symbol in symbols if symbol in results]

    def _guarded_card(self, worker, symbol: str, degraded: List[str]) -> ScreenCard:
        try:
            return worker(symbol)
        except Exception as exc:  # pragma: no cover - 单线程分支的防御
            logger.warning("大屏卡片 %s 聚合失败：%s", symbol, exc)
            degraded.append(f"{symbol} 聚合失败（{type(exc).__name__}）")
            return ScreenCard(symbol=symbol, degraded=[f"聚合失败（{type(exc).__name__}）"])

    def _collect_indices(self, degraded: List[str]) -> Tuple[List[ScreenIndex], str]:
        from src.services.weekly_review_service import DEFAULT_INDEX_CODES

        items: List[ScreenIndex] = []
        dates: List[str] = []
        for code, name in DEFAULT_INDEX_CODES:
            bars = self._safe_fetch(code)
            if bars is None or len(bars) == 0:
                items.append(ScreenIndex(code=code, name=name))
                continue
            last = _last_value(bars, "close")
            prev = _prev_value(bars, "close")
            change_pct = None
            if last is not None and prev:
                change_pct = _round((last - prev) / prev * 100.0, 3)
            items.append(ScreenIndex(code=code, name=name, value=last, change_pct=change_pct))
            date_text = _last_date(bars)
            if date_text:
                dates.append(date_text)

        if not items or all(item.value is None for item in items):
            degraded.append("指数行情暂不可用（数据源未就绪或非交易时段无数据）")
        return items, (max(dates) if dates else "")

    def _collect_events(self, symbols: Sequence[str], limit: int, degraded: List[str]) -> List[str]:
        if not symbols or limit <= 0:
            return []
        try:
            events = list(self.event_source.collect(list(symbols)) or [])
        except Exception as exc:
            logger.warning("事件源不可用，大屏省略事件板块：%s", exc)
            degraded.append("事件源不可用，已省略事件提醒")
            return []
        cleaned = [str(item).strip() for item in events if str(item or "").strip()]
        return cleaned[:limit]

    def _collect_weekly(self, with_weekly: bool, degraded: List[str]) -> Dict[str, Any]:
        if not with_weekly:
            return {}
        try:
            review = self.review_service.build()
        except Exception as exc:
            logger.warning("复盘摘要不可用，大屏省略复盘区块：%s", exc)
            degraded.append(f"本周复盘摘要暂不可用（{type(exc).__name__}）")
            return {}

        summary = getattr(review, "signal_summary", None)
        execution_counts = {}
        try:
            execution_counts = dict(review.execution_counts() or {})
        except Exception:  # pragma: no cover - 汇总函数不应失败
            execution_counts = {}
        base_url = str(getattr(review, "h5_base_url", "") or "").rstrip("/")
        payload: Dict[str, Any] = {
            "week_key": str(getattr(review, "week_key", "") or ""),
            "headline": str(review.headline() if callable(getattr(review, "headline", None)) else ""),
            "evaluated": getattr(summary, "evaluated", None),
            "correct": getattr(summary, "correct", None),
            "unevaluated": getattr(summary, "unevaluated", None),
            "win_rate": getattr(summary, "win_rate", None),
            "avg_return_pct": getattr(summary, "avg_return_pct", None),
            "position_week_pnl": getattr(review, "position_week_pnl", None),
            "execution_counts": execution_counts,
            "detail_url": f"{base_url}{WEEKLY_REVIEW_PATH}" if base_url else WEEKLY_REVIEW_PATH,
        }
        return payload

    # ---------------- 时间 ---------------- #
    def _resolve_now(self) -> datetime:
        """``now`` 可注入 ``datetime`` 或返回 ``datetime`` 的可调用对象（便于离线测试）。"""
        if callable(self._now):
            try:
                injected = self._now()
            except Exception:  # pragma: no cover - 注入对象异常时退回真实时间
                injected = None
            if isinstance(injected, datetime):
                return injected
        elif isinstance(self._now, datetime):
            return self._now
        return datetime.now()

    # ---------------- 公共入口 ---------------- #
    def build(
        self,
        symbols: Optional[Sequence[str]] = None,
        *,
        with_quote: bool = True,
        with_weekly: bool = True,
        limit_events: int = 6,
    ) -> Dict[str, Any]:
        """组装一屏快照（``to_dict()`` 形态，便于直接返回给 API）。"""

        cache_key = (
            tuple(_normalize_symbols(symbols)),
            bool(with_quote),
            bool(with_weekly),
            int(limit_events),
        )
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        moment = self._resolve_now()
        degraded: List[str] = []

        positions = self._load_positions(degraded)
        resolved, names = self._resolve_symbols(symbols, positions, degraded)
        cards = self._load_cards(resolved, positions, names, with_quote, degraded)
        indices, market_date = self._collect_indices(degraded)
        portfolio, portfolio_notes = summarize_portfolio(positions, cards)
        degraded.extend(note for note in portfolio_notes if note not in degraded)
        events = self._collect_events(resolved, limit_events, degraded)
        weekly = self._collect_weekly(with_weekly, degraded)

        action_counts: Dict[str, int] = {}
        for card in cards:
            action_counts[card.action] = action_counts.get(card.action, 0) + 1
        ordered_counts = {
            action: action_counts[action]
            for action in sorted(action_counts, key=_rank_action)
        }

        quote_times = [card.quote_time for card in cards if card.quote_time]
        snapshot = ScreenSnapshot(
            generated_at=moment.strftime("%Y-%m-%d %H:%M:%S"),
            market_date=market_date,
            quote_as_of=_normalize_stamp(max(quote_times)) if quote_times else "",
            refresh_seconds=self.refresh_seconds,
            indices=indices,
            cards=cards,
            portfolio=portfolio,
            events=events,
            weekly=weekly,
            action_counts=ordered_counts,
            urgent_count=sum(1 for card in cards if card.is_urgent),
            degraded=degraded,
            disclaimer=DISCLAIMER,
        )
        payload = snapshot.to_dict()
        self._cache_put(cache_key, payload)
        return payload

    # ---------------- 极简快照缓存（防手动刷新打爆数据源） ---------------- #
    def _cache_get(self, key: Any) -> Optional[Dict[str, Any]]:
        if self.cache_ttl_seconds <= 0:
            return None
        entry = self._cache.get(key)
        if entry is None:
            return None
        created_at, payload = entry
        if time.monotonic() - created_at > self.cache_ttl_seconds:
            self._cache.pop(key, None)
            return None
        return payload

    def _cache_put(self, key: Any, payload: Dict[str, Any]) -> None:
        if self.cache_ttl_seconds <= 0:
            return
        self._cache[key] = (time.monotonic(), payload)

    def clear_cache(self) -> None:
        """清空快照缓存（测试 / 强制刷新用）。"""
        self._cache.clear()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover - CLI
    import argparse
    import json

    parser = argparse.ArgumentParser(description="高抛低吸助手 · P4 大屏快照（FR-14.1）")
    parser.add_argument("--symbols", nargs="*", default=None, help="标的；缺省为自选股 + 持仓")
    parser.add_argument("--no-quote", action="store_true", help="跳过实时行情（离线）")
    parser.add_argument("--no-weekly", action="store_true", help="不取本周复盘摘要")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    service = ScreenService()
    payload = service.build(
        symbols=args.symbols,
        with_quote=not args.no_quote,
        with_weekly=not args.no_weekly,
    )

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"数据截止：{payload['market_date'] or '-'}  行情：{payload['quote_as_of'] or '-'}")
    for item in payload["indices"]:
        pct = item["change_pct"]
        print(f"  {item['name']}  {item['value']}  {'' if pct is None else f'{pct:+.2f}%'}")
    for card in payload["cards"]:
        pct = card["change_pct"]
        print(f"  {card['symbol']} {card['name']} {card['action_label']} {card['conclusion']}")
    for note in payload["degraded"]:
        print(f"  ! {note}")
    print(payload["disclaimer"])
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(_main())
