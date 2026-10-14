# -*- coding: utf-8 -*-
"""高抛低吸助手 · P3 周度复盘（FR-11）

每周六推送一份「周度复盘」报告，覆盖需求 FR-11.1~FR-11.4：

| 板块 | 需求 | 数据来源 |
| --- | --- | --- |
| ① 本周信号回顾与胜率 | FR-11.1 | `decision_signals` + `decision_signal_outcomes`（四端同源 NFR-9） |
| ② 持仓盈亏与执行情况 | FR-11.2 | `portfolio_positions` + `execution_feedback`（人工回填 / FR-14.3.5） |
| ③ 大盘 / 板块周度总结 | FR-11.3 | 指数日线（同 P0 取数口径）+ 板块热点（尽力而为） |
| ④ 下周关注与关键价位 | FR-11.4 | 自选股 + 最新有效信号的买入/止损/目标位 |

设计约束：
- **依赖全可注入**（信号/持仓/取数/大盘/热点/价位）→ 离线可测；
- **板块级异常隔离**：任一板块取数失败只标注「数据暂缺」，不阻断整份报告（NFR-8）；
- **诚实口径**：胜率按 `outcome.direction_correct` 统计，样本为 0 时明确标注「暂无样本」；
- **免责声明**固定尾注（NFR-5）。

用法::

    python -m src.services.weekly_review_service --dry-run        # 生成不发送
    python -m src.services.weekly_review_service --json           # 输出结构化 JSON
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "DISCLAIMER",
    "WEEKLY_PUSH_TITLE",
    "WEEKLY_REVIEW_H5_PATH",
    "DEFAULT_INDEX_CODES",
    "SignalReview",
    "SignalReviewSummary",
    "PositionReview",
    "MarketReviewItem",
    "FocusItem",
    "WeeklyReview",
    "WeeklyReviewResult",
    "WeeklyReviewService",
    "week_window",
    "week_key_of",
]

DISCLAIMER = (
    "本内容由程序基于历史数据自动生成，仅供学习参考，不构成任何投资建议，据此操作风险自负。"
)
WEEKLY_PUSH_TITLE = "🗓 周度复盘"
WEEKLY_REVIEW_H5_PATH = "/review"

# 与 P1 日推同源：信号落库口径（NFR-9 四端同源）
_SOURCE_TYPE = "analysis"
_TRIGGER_SOURCE = "p0_engine"

DEFAULT_INDEX_CODES: List[tuple] = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
]

_ACTION_LABELS: Dict[str, str] = {
    "build": "建仓",
    "dip_buy": "抄底",
    "add": "加仓",
    "reduce": "减仓",
    "sell": "清仓",
    "watch": "观望",
    "hold": "持有",
    "clear": "清仓",
}

_EXECUTION_LABELS: Dict[str, str] = {
    "executed": "已执行",
    "ignored": "忽略",
    "later": "稍后",
}

_FEEDBACK_UNKNOWN = "未回填"


# --------------------------------------------------------------------------- #
# 时间窗口
# --------------------------------------------------------------------------- #
def week_window(now: Optional[datetime] = None) -> tuple:
    """返回``(本周一 00:00, 当前时刻)``；用于「本周」口径（周一~周六）。"""
    current = now or datetime.now()
    monday = (current - timedelta(days=current.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday, current


def week_key_of(now: Optional[datetime] = None) -> str:
    """本周标识 = 该周周一的 ``YYYY-MM-DD``（与 ``execution_feedback.week_key`` 对齐）。"""
    monday, _ = week_window(now)
    return monday.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class SignalReview:
    """FR-11.1 单条信号的回顾。"""

    signal_id: Optional[int] = None
    symbol: str = ""
    name: str = ""
    action: str = ""
    action_label: str = ""
    scenario: str = ""
    created_at: str = ""
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None
    outcome: str = ""              # hit | miss | neutral | ''
    direction_correct: Optional[bool] = None
    return_pct: Optional[float] = None
    eval_status: str = ""          # completed | unable | ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "name": self.name,
            "action": self.action,
            "action_label": self.action_label,
            "scenario": self.scenario,
            "created_at": self.created_at,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "target_price": self.target_price,
            "outcome": self.outcome,
            "direction_correct": self.direction_correct,
            "return_pct": self.return_pct,
            "eval_status": self.eval_status,
        }


@dataclass
class SignalReviewSummary:
    """FR-11.1 汇总（胜率口径见模块 docstring）。"""

    total: int = 0
    evaluated: int = 0
    correct: int = 0
    unevaluated: int = 0
    win_rate: Optional[float] = None
    avg_return_pct: Optional[float] = None
    by_action: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "evaluated": self.evaluated,
            "correct": self.correct,
            "unevaluated": self.unevaluated,
            "win_rate": self.win_rate,
            "avg_return_pct": self.avg_return_pct,
            "by_action": dict(self.by_action),
        }


@dataclass
class PositionReview:
    """FR-11.2 单只持仓的周度情况 + 执行回填。"""

    symbol: str = ""
    name: str = ""
    quantity: float = 0.0
    avg_cost: float = 0.0
    last_price: Optional[float] = None
    week_open_price: Optional[float] = None
    week_return_pct: Optional[float] = None
    week_pnl_amount: Optional[float] = None
    total_return_pct: Optional[float] = None
    execution_status: str = ""
    execution_label: str = _FEEDBACK_UNKNOWN
    note: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "quantity": self.quantity,
            "avg_cost": self.avg_cost,
            "last_price": self.last_price,
            "week_open_price": self.week_open_price,
            "week_return_pct": self.week_return_pct,
            "week_pnl_amount": self.week_pnl_amount,
            "total_return_pct": self.total_return_pct,
            "execution_status": self.execution_status,
            "execution_label": self.execution_label,
            "note": self.note,
        }


@dataclass
class MarketReviewItem:
    """FR-11.3 大盘 / 板块周度条目。"""

    kind: str = "index"            # index | sector
    name: str = ""
    week_return_pct: Optional[float] = None
    value: Optional[float] = None
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "week_return_pct": self.week_return_pct,
            "value": self.value,
            "note": self.note,
        }


@dataclass
class FocusItem:
    """FR-11.4 下周关注标的与关键价位。"""

    symbol: str = ""
    name: str = ""
    action_label: str = ""
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None
    note: str = ""

    def entry_zone(self) -> str:
        if self.entry_low is None and self.entry_high is None:
            return "—"
        low = "—" if self.entry_low is None else f"{self.entry_low:.2f}"
        high = "—" if self.entry_high is None else f"{self.entry_high:.2f}"
        if low == high:
            return low
        return f"{low} ~ {high}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "action_label": self.action_label,
            "entry_low": self.entry_low,
            "entry_high": self.entry_high,
            "entry_zone": self.entry_zone(),
            "stop_loss": self.stop_loss,
            "target_price": self.target_price,
            "note": self.note,
        }


@dataclass
class WeeklyReview:
    """FR-11 周度复盘报告（纯数据结构，可序列化 / 可渲染）。"""

    week_key: str = ""
    week_start: str = ""
    week_end: str = ""
    generated_at: str = ""
    signals: List[SignalReview] = field(default_factory=list)
    signal_summary: SignalReviewSummary = field(default_factory=SignalReviewSummary)
    positions: List[PositionReview] = field(default_factory=list)
    market: List[MarketReviewItem] = field(default_factory=list)
    next_week: List[FocusItem] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    h5_base_url: str = ""

    # ---------------- 汇总 ---------------- #
    @property
    def position_week_pnl(self) -> Optional[float]:
        values = [p.week_pnl_amount for p in self.positions if p.week_pnl_amount is not None]
        return sum(values) if values else None

    def execution_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for item in self.positions:
            key = item.execution_status or "unknown"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def detail_url(self) -> str:
        base = str(self.h5_base_url or "").rstrip("/")
        return f"{base}{WEEKLY_REVIEW_H5_PATH}" if base else ""

    def headline(self) -> str:
        summary = self.signal_summary
        if summary.evaluated > 0 and summary.win_rate is not None:
            signal_part = f"本周信号 {summary.total} 条，胜率 {summary.win_rate * 100:.0f}%"
        else:
            signal_part = f"本周信号 {summary.total} 条（暂无胜率样本）"
        return f"{WEEKLY_PUSH_TITLE}｜{signal_part}"

    # ---------------- 序列化 ---------------- #
    def to_dict(self) -> Dict[str, Any]:
        return {
            "week_key": self.week_key,
            "week_start": self.week_start,
            "week_end": self.week_end,
            "generated_at": self.generated_at,
            "signal_summary": self.signal_summary.to_dict(),
            "signals": [s.to_dict() for s in self.signals],
            "positions": [p.to_dict() for p in self.positions],
            "position_week_pnl": self.position_week_pnl,
            "execution_counts": self.execution_counts(),
            "market": [m.to_dict() for m in self.market],
            "next_week": [f.to_dict() for f in self.next_week],
            "notes": list(self.notes),
            "headline": self.headline(),
            "detail_url": self.detail_url(),
        }

    # ---------------- 渲染 ---------------- #
    def to_markdown(self) -> str:
        lines: List[str] = [WEEKLY_PUSH_TITLE, ""]
        lines.append(f"周期：{self.week_start} ~ {self.week_end}")
        lines.append("")

        # ① 本周信号回顾与胜率
        summary = self.signal_summary
        lines.append("【① 本周信号回顾与胜率】")
        if not self.signals:
            lines.append("本周未产生新信号。")
        else:
            if summary.evaluated > 0 and summary.win_rate is not None:
                lines.append(
                    f"共 {summary.total} 条信号，已评估 {summary.evaluated} 条，"
                    f"方向正确 {summary.correct} 条，胜率 {summary.win_rate * 100:.1f}%"
                )
            else:
                lines.append(f"共 {summary.total} 条信号，暂无已评估样本（胜率待观察）。")
            if summary.avg_return_pct is not None:
                lines.append(f"信号后平均涨跌：{summary.avg_return_pct:+.2f}%")
            lines.append("")
            lines.append("| 标的 | 建议 | 场景 | 信号日 | 结果 | 信号后涨跌% |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            for item in self.signals:
                result = self._outcome_label(item)
                ret = "—" if item.return_pct is None else f"{item.return_pct:+.2f}"
                name = f"{item.name}({item.symbol})" if item.name else item.symbol
                lines.append(
                    f"| {name} | {item.action_label or item.action} | "
                    f"{item.scenario or '—'} | {item.created_at} | {result} | {ret} |"
                )
        lines.append("")

        # ② 持仓盈亏与执行情况
        lines.append("【② 持仓盈亏与执行情况】")
        if not self.positions:
            lines.append("当前无持仓。")
        else:
            total = self.position_week_pnl
            if total is not None:
                lines.append(f"本周持仓浮动盈亏合计：{total:+,.2f} 元")
            lines.append("")
            lines.append("| 标的 | 数量 | 成本 | 现价 | 本周涨跌% | 本周盈亏 | 累计盈亏% | 是否执行 |")
            lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
            for pos in self.positions:
                name = f"{pos.name}({pos.symbol})" if pos.name else pos.symbol
                week_ret = "—" if pos.week_return_pct is None else f"{pos.week_return_pct:+.2f}"
                week_pnl = "—" if pos.week_pnl_amount is None else f"{pos.week_pnl_amount:+,.0f}"
                total_ret = "—" if pos.total_return_pct is None else f"{pos.total_return_pct:+.2f}"
                lines.append(
                    f"| {name} | {pos.quantity:.0f} | {pos.avg_cost:.3f} | "
                    f"{'—' if pos.last_price is None else format(pos.last_price, '.3f')} | "
                    f"{week_ret} | {week_pnl} | {total_ret} | {pos.execution_label} |"
                )
            counts = self.execution_counts()
            executed = counts.get("executed", 0)
            pending = counts.get("unknown", 0)
            lines.append("")
            lines.append(
                f"执行回填：已执行 {executed} 只｜待回填 {pending} 只"
                "（可在 H5 复盘页或飞书对话回执「已执行 / 忽略 / 稍后」）"
            )
        lines.append("")

        # ③ 大盘 / 板块周度总结
        lines.append("【③ 大盘 / 板块周度总结】")
        if not self.market:
            lines.append("大盘数据暂缺。")
        else:
            for item in self.market:
                pct = "—" if item.week_return_pct is None else f"{item.week_return_pct:+.2f}%"
                value = "" if item.value is None else f"（{item.value:.2f}）"
                note = f" {item.note}" if item.note else ""
                prefix = "·" if item.kind == "sector" else "▪"
                lines.append(f"{prefix} {item.name}{value} 周涨跌 {pct}{note}")
        lines.append("")

        # ④ 下周关注与关键价位
        lines.append("【④ 下周关注与关键价位】")
        if not self.next_week:
            lines.append("暂无关注标的。")
        else:
            lines.append("| 标的 | 建议 | 买入区间 | 止损 | 目标 / 减仓 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for focus in self.next_week:
                name = f"{focus.name}({focus.symbol})" if focus.name else focus.symbol
                stop = "—" if focus.stop_loss is None else f"{focus.stop_loss:.2f}"
                target = "—" if focus.target_price is None else f"{focus.target_price:.2f}"
                lines.append(
                    f"| {name} | {focus.action_label or '—'} | {focus.entry_zone()} | "
                    f"{stop} | {target} |"
                )
        lines.append("")

        for note in self.notes:
            lines.append(f"· {note}")
        if self.notes:
            lines.append("")
        lines.append(DISCLAIMER)
        return "\n".join(lines)

    @staticmethod
    def _outcome_label(item: SignalReview) -> str:
        if item.direction_correct is True:
            return "✅ 方向正确"
        if item.direction_correct is False:
            return "❌ 方向相反"
        if item.eval_status == "unable":
            return "⚪ 无法评估"
        return "— 待观察"


@dataclass
class WeeklyReviewResult:
    """``run`` 的返回：报告 + 发送情况。"""

    review: Optional[WeeklyReview] = None
    sent_text: bool = False
    skipped_reason: Optional[str] = None
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sent_text": self.sent_text,
            "skipped_reason": self.skipped_reason,
            "errors": list(self.errors),
            "markdown": self.review.to_markdown() if self.review else "",
            "report": self.review.to_dict() if self.review else None,
        }


# --------------------------------------------------------------------------- #
# 服务
# --------------------------------------------------------------------------- #
class WeeklyReviewService:
    """P3 周度复盘编排器。所有外部依赖均可注入，便于离线测试。"""

    def __init__(
        self,
        *,
        signal_service: Any = None,
        outcome_service: Any = None,
        position_service: Any = None,
        watchlist_service: Any = None,
        feedback_repo: Any = None,
        fetch: Optional[Callable[[str, int], Any]] = None,
        market_provider: Optional[Callable[[Any], Sequence[Any]]] = None,
        hotspot_provider: Optional[Callable[[], Sequence[Any]]] = None,
        level_provider: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
        notifier: Any = None,
        h5_base_url: Optional[str] = None,
        refresh_outcomes: bool = True,
        max_next_week: int = 20,
        market_fetch_days: int = 20,
    ):
        self._signal_service = signal_service
        self._outcome_service = outcome_service
        self._position_service = position_service
        self._watchlist_service = watchlist_service
        self._feedback_repo = feedback_repo
        self._fetch = fetch
        self._market_provider = market_provider
        self._hotspot_provider = hotspot_provider
        self._level_provider = level_provider
        self._notifier = notifier
        self._h5_base_url = h5_base_url
        self.refresh_outcomes = bool(refresh_outcomes)
        self.max_next_week = int(max_next_week)
        self.market_fetch_days = int(market_fetch_days)

    # ---------------- 懒加载依赖 ---------------- #
    @property
    def signal_service(self):
        if self._signal_service is None:
            from src.services.decision_signal_service import DecisionSignalService

            self._signal_service = DecisionSignalService()
        return self._signal_service

    @property
    def outcome_service(self):
        if self._outcome_service is None:
            from src.services.decision_signal_outcome_service import DecisionSignalOutcomeService

            self._outcome_service = DecisionSignalOutcomeService()
        return self._outcome_service

    @property
    def position_service(self):
        if self._position_service is None:
            from src.services.position_service import PositionService

            self._position_service = PositionService()
        return self._position_service

    @property
    def watchlist_service(self):
        if self._watchlist_service is None:
            from src.services.watchlist_service import WatchlistService

            self._watchlist_service = WatchlistService()
        return self._watchlist_service

    @property
    def feedback_repo(self):
        if self._feedback_repo is None:
            from src.repositories.execution_feedback_repo import ExecutionFeedbackRepository

            self._feedback_repo = ExecutionFeedbackRepository()
        return self._feedback_repo

    @property
    def notifier(self):
        if self._notifier is None:
            from src.services.p1_push_service import FeishuNotifier

            self._notifier = FeishuNotifier()
        return self._notifier

    def _resolve_h5_base_url(self) -> str:
        if self._h5_base_url is not None:
            return str(self._h5_base_url).strip()
        try:
            from src.config import get_config

            configured = getattr(get_config(), "hslb_h5_base_url", None)
            if configured:
                return str(configured).strip()
        except Exception:  # pragma: no cover - 配置不可用不阻断
            pass
        import os

        return str(os.getenv("HSLB_H5_BASE_URL", "") or "").strip()

    def _fetch_daily(self, symbol: str, days: int):  # pragma: no cover - 依赖网络
        if self._fetch is not None:
            return self._fetch(symbol, days)
        from src.services.alert_indicators import normalize_ohlcv
        from src.services.p0_signal_builder import P0SignalBuilder

        raw = P0SignalBuilder._fetch_daily(symbol, days)  # noqa: SLF001 - 复用同口径
        if raw is None:
            return None
        return normalize_ohlcv(raw, required_columns=("open", "high", "low", "close", "volume"))

    # ---------------- 主流程 ---------------- #
    def build(
        self,
        *,
        now: Optional[datetime] = None,
        symbols: Optional[Sequence[str]] = None,
    ) -> WeeklyReview:
        """组装一份周度复盘（纯读取，不发送）。"""
        now = now or datetime.now()
        week_start, week_end = week_window(now)
        review = WeeklyReview(
            week_key=week_start.strftime("%Y-%m-%d"),
            week_start=week_start.strftime("%Y-%m-%d"),
            week_end=week_end.strftime("%Y-%m-%d"),
            generated_at=now.strftime("%Y-%m-%d %H:%M:%S"),
            h5_base_url=self._resolve_h5_base_url(),
        )

        review.signals = self._collect_signals(week_start, week_end, review)
        review.signal_summary = _summarize_signals(review.signals)
        review.positions = self._collect_positions(week_start, review)
        review.market = self._collect_market(week_start, review)
        review.next_week = self._collect_next_week(symbols, review)
        return review

    def run(
        self,
        *,
        now: Optional[datetime] = None,
        symbols: Optional[Sequence[str]] = None,
        dry_run: bool = False,
    ) -> WeeklyReviewResult:
        """组装并（可选）推送飞书。"""
        result = WeeklyReviewResult()
        try:
            result.review = self.build(now=now, symbols=symbols)
        except Exception as exc:  # 组装失败也要留下痕迹
            logger.exception("周度复盘组装失败：%s", exc)
            result.errors.append(f"组装失败：{type(exc).__name__}: {exc}")
            return result

        if dry_run:
            result.skipped_reason = "dry_run"
            return result
        if not getattr(self.notifier, "is_available", lambda: True)():
            result.skipped_reason = "no_channel_configured"
            result.errors.append("未配置飞书渠道（FEISHU_* 均为空），仅生成内容未发送")
            return result

        try:
            result.sent_text = bool(self.notifier.send_text(result.review.to_markdown()))
        except Exception as exc:
            result.errors.append(f"文本推送失败：{type(exc).__name__}: {exc}")
        return result

    # ---------------- ① 信号回顾（FR-11.1） ---------------- #
    def _collect_signals(self, week_start: datetime, week_end: datetime, review: WeeklyReview) -> List[SignalReview]:
        try:
            page = self.signal_service.list_signals(
                source_type=_SOURCE_TYPE,
                trigger_source=_TRIGGER_SOURCE,
                created_from=week_start,
                created_to=week_end,
                page=1,
                page_size=100,
            )
        except Exception as exc:
            logger.warning("读取本周信号失败，信号板块降级为空：%s", exc)
            review.notes.append(f"信号板块数据暂缺（{type(exc).__name__}）")
            return []

        items = list((page or {}).get("items") or [])
        reviews: List[SignalReview] = []
        for item in items:
            try:
                reviews.append(self._review_one_signal(item))
            except Exception as exc:  # 单条失败不影响其余
                logger.warning("信号 %s 回顾失败：%s", item.get("id"), exc)
        reviews.sort(key=lambda r: (r.created_at, r.symbol), reverse=True)
        return reviews

    def _review_one_signal(self, item: Dict[str, Any]) -> SignalReview:
        signal_id = item.get("id")
        action = str(item.get("action") or "").strip()
        entry_low = _to_float(item.get("entry_low"))
        entry_high = _to_float(item.get("entry_high"))
        entry_price = None
        if entry_low is not None and entry_high is not None:
            entry_price = round((entry_low + entry_high) / 2.0, 4)
        elif entry_low is not None:
            entry_price = entry_low
        elif entry_high is not None:
            entry_price = entry_high

        review = SignalReview(
            signal_id=int(signal_id) if signal_id else None,
            symbol=str(item.get("stock_code") or "").strip().upper(),
            name=str(item.get("stock_name") or "").strip(),
            action=action,
            action_label=_ACTION_LABELS.get(action, action),
            scenario=_scenario_of(item),
            created_at=_as_date(item.get("created_at")),
            entry_price=entry_price,
            stop_loss=_to_float(item.get("stop_loss")),
            target_price=_to_float(item.get("target_price")),
        )

        outcome = self._latest_outcome(review.signal_id)
        if outcome is not None:
            review.outcome = str(outcome.get("outcome") or "")
            review.eval_status = str(outcome.get("eval_status") or "")
            correct = outcome.get("direction_correct")
            review.direction_correct = bool(correct) if correct is not None else None
            review.return_pct = _to_float(outcome.get("stock_return_pct"))
        return review

    def _latest_outcome(self, signal_id: Optional[int]) -> Optional[Dict[str, Any]]:
        if not signal_id:
            return None
        try:
            if self.refresh_outcomes:
                try:
                    self.outcome_service.run_outcomes(signal_id=int(signal_id))
                except Exception as exc:  # 刷新失败退化为读取已有结果
                    logger.debug("刷新信号 %s 结果失败，改用既有：%s", signal_id, exc)
            payload = self.outcome_service.list_signal_outcomes(int(signal_id))
        except Exception as exc:
            logger.warning("读取信号 %s 结果失败：%s", signal_id, exc)
            return None
        items = list((payload or {}).get("items") or [])
        if not items:
            return None
        completed = [i for i in items if str(i.get("eval_status")) == "completed"]
        return (completed or items)[0]

    # ---------------- ② 持仓与执行（FR-11.2） ---------------- #
    def _collect_positions(self, week_start: datetime, review: WeeklyReview) -> List[PositionReview]:
        try:
            positions = self.position_service.list_positions() or {}
        except Exception as exc:
            logger.warning("读取持仓失败，持仓板块降级为空：%s", exc)
            review.notes.append(f"持仓板块数据暂缺（{type(exc).__name__}）")
            return []

        week_key = week_start.strftime("%Y-%m-%d")
        reviews: List[PositionReview] = []
        for symbol, view in positions.items():
            try:
                reviews.append(self._review_one_position(symbol, view, week_start, week_key))
            except Exception as exc:  # 单只失败不影响其余
                logger.warning("持仓 %s 复盘失败：%s", symbol, exc)
        reviews.sort(key=lambda p: p.symbol)
        return reviews

    def _review_one_position(
        self,
        symbol: str,
        view: Any,
        week_start: datetime,
        week_key: str,
    ) -> PositionReview:
        quantity = _to_float(getattr(view, "quantity", None)) or 0.0
        avg_cost = _to_float(getattr(view, "avg_cost", None)) or 0.0
        item = PositionReview(symbol=str(symbol).upper(), quantity=quantity, avg_cost=avg_cost)

        bars = self._safe_fetch(symbol)
        if bars is not None and len(bars) > 0:
            last_price = _to_float(_last_value(bars, "close"))
            week_open = _price_at_or_before(bars, week_start)
            if week_open is None:
                week_open = _to_float(_first_value(bars, "close"))
            item.last_price = last_price
            item.week_open_price = week_open
            if last_price is not None and week_open:
                item.week_return_pct = round((last_price - week_open) / week_open * 100.0, 3)
                item.week_pnl_amount = round((last_price - week_open) * quantity, 2)
            if last_price is not None and avg_cost:
                item.total_return_pct = round((last_price - avg_cost) / avg_cost * 100.0, 3)

        feedback = self._safe_feedback(symbol, week_key)
        if feedback:
            item.execution_status = str(feedback.get("execution_status") or "")
            item.execution_label = _EXECUTION_LABELS.get(item.execution_status, _FEEDBACK_UNKNOWN)
            item.note = feedback.get("note")
        return item

    def _safe_feedback(self, symbol: str, week_key: str) -> Optional[Dict[str, Any]]:
        try:
            return self.feedback_repo.get(symbol, week_key)
        except Exception as exc:
            logger.warning("读取执行回填失败 %s：%s", symbol, exc)
            return None

    # ---------------- ③ 大盘 / 板块（FR-11.3） ---------------- #
    def _collect_market(self, week_start: datetime, review: WeeklyReview) -> List[MarketReviewItem]:
        items: List[MarketReviewItem] = []
        provider = self._market_provider or self._default_market_provider
        try:
            for raw in provider(week_start) or []:
                items.append(_coerce_market_item(raw))
        except Exception as exc:
            logger.warning("大盘板块取数失败，降级为空：%s", exc)
            review.notes.append(f"大盘板块数据暂缺（{type(exc).__name__}）")

        hotspot_provider = self._hotspot_provider or self._default_hotspot_provider
        try:
            for raw in hotspot_provider() or []:
                items.append(_coerce_market_item(raw))
        except Exception as exc:
            logger.warning("板块热点取数失败，已跳过：%s", exc)
        return items

    def _default_market_provider(self, week_start: datetime) -> List[MarketReviewItem]:
        out: List[MarketReviewItem] = []
        for code, name in DEFAULT_INDEX_CODES:
            bars = self._safe_fetch(code, days=self.market_fetch_days)
            if bars is None or len(bars) == 0:
                continue
            last_price = _to_float(_last_value(bars, "close"))
            week_open = _price_at_or_before(bars, week_start) or _to_float(_first_value(bars, "close"))
            pct = None
            if last_price is not None and week_open:
                pct = round((last_price - week_open) / week_open * 100.0, 3)
            out.append(
                MarketReviewItem(kind="index", name=name, week_return_pct=pct, value=last_price)
            )
        return out

    def _default_hotspot_provider(self) -> List[MarketReviewItem]:
        from src.services.market_hotspot_service import MarketHotspotService

        payload = MarketHotspotService().get_hotspots(market="cn", limit=5) or {}
        items: List[MarketReviewItem] = []
        for key in ("leading_industries", "leading_concepts"):
            for entry in payload.get(key) or []:
                if not isinstance(entry, dict):
                    continue
                name = (
                    entry.get("name")
                    or entry.get("theme")
                    or entry.get("label")
                    or entry.get("industry")
                    or entry.get("concept")
                )
                if not name:
                    continue
                items.append(
                    MarketReviewItem(
                        kind="sector",
                        name=str(name),
                        week_return_pct=_to_float(
                            entry.get("change_pct", entry.get("change"))
                        ),
                        note="领涨" if key == "leading_industries" else "题材活跃",
                    )
                )
        return items[:6]

    # ---------------- ④ 下周关注（FR-11.4） ---------------- #
    def _collect_next_week(
        self, symbols: Optional[Sequence[str]], review: WeeklyReview
    ) -> List[FocusItem]:
        watch_symbols = self._resolve_watch_symbols(symbols, review)
        provider = self._level_provider or self._default_level_provider
        items: List[FocusItem] = []
        for symbol in watch_symbols[: self.max_next_week]:
            try:
                focus = self._build_focus(symbol, provider)
            except Exception as exc:
                logger.warning("下周关注 %s 失败：%s", symbol, exc)
                continue
            if focus is not None:
                items.append(focus)
        return items

    def _resolve_watch_symbols(
        self, symbols: Optional[Sequence[str]], review: WeeklyReview
    ) -> List[str]:
        if symbols:
            return [str(s).strip().upper() for s in symbols if str(s).strip()]
        try:
            rows = self.watchlist_service.list_watchlist(enabled_only=True) or []
        except Exception as exc:
            logger.warning("读取自选股失败，下周关注降级为空：%s", exc)
            review.notes.append(f"自选股读取失败（{type(exc).__name__}）")
            return []
        out: List[str] = []
        for row in rows:
            symbol = str((row or {}).get("symbol") or "").strip().upper()
            if symbol:
                out.append(symbol)
        return out

    def _build_focus(self, symbol: str, provider: Callable[[str], Optional[Dict[str, Any]]]) -> Optional[FocusItem]:
        raw = provider(symbol) or {}
        focus = FocusItem(
            symbol=symbol,
            name=str(raw.get("name") or "").strip(),
            action_label=str(raw.get("action_label") or "").strip(),
            entry_low=_to_float(raw.get("entry_low")),
            entry_high=_to_float(raw.get("entry_high")),
            stop_loss=_to_float(raw.get("stop_loss")),
            target_price=_to_float(raw.get("target_price")),
            note=str(raw.get("note") or "").strip(),
        )
        if (
            focus.entry_low is None
            and focus.entry_high is None
            and focus.stop_loss is None
            and focus.target_price is None
        ):
            focus.note = focus.note or "暂无有效价位"
        return focus

    def _default_level_provider(self, symbol: str) -> Optional[Dict[str, Any]]:
        """默认价位来源：该标的**最新有效信号**（四端同源 NFR-9）。"""
        try:
            payload = self.signal_service.get_latest_active(stock_code=symbol, limit=1)
        except Exception as exc:
            logger.debug("读取 %s 最新信号失败：%s", symbol, exc)
            return None
        items = list((payload or {}).get("items") or [])
        if not items:
            return None
        signal = items[0]
        action = str(signal.get("action") or "").strip()
        return {
            "name": str(signal.get("stock_name") or "").strip(),
            "action_label": _ACTION_LABELS.get(action, action),
            "entry_low": signal.get("entry_low"),
            "entry_high": signal.get("entry_high"),
            "stop_loss": signal.get("stop_loss"),
            "target_price": signal.get("target_price"),
        }

    # ---------------- 工具 ---------------- #
    def _safe_fetch(self, symbol: str, days: int = 15) -> Any:
        try:
            return self._fetch_daily(symbol, days)
        except Exception as exc:
            logger.warning("取数失败 %s：%s", symbol, exc)
            return None


# --------------------------------------------------------------------------- #
# 纯函数工具
# --------------------------------------------------------------------------- #
def _summarize_signals(reviews: Sequence[SignalReview]) -> SignalReviewSummary:
    summary = SignalReviewSummary(total=len(reviews))
    correct = 0
    evaluated = 0
    returns: List[float] = []
    for item in reviews:
        if item.action:
            summary.by_action[item.action] = summary.by_action.get(item.action, 0) + 1
        if item.direction_correct is not None:
            evaluated += 1
            if item.direction_correct:
                correct += 1
        else:
            summary.unevaluated += 1
        if item.return_pct is not None:
            returns.append(item.return_pct)
    summary.evaluated = evaluated
    summary.correct = correct
    summary.win_rate = (correct / evaluated) if evaluated > 0 else None
    summary.avg_return_pct = round(sum(returns) / len(returns), 3) if returns else None
    return summary


def _to_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _as_date(value: Any) -> str:
    text = str(value or "")
    if "T" in text:
        text = text.split("T")[0]
    elif " " in text:
        text = text.split(" ")[0]
    return text


def _scenario_of(item: Dict[str, Any]) -> str:
    """从信号条目里取场景（metadata.scenario 优先，退化到 market_phase）。"""
    meta = item.get("metadata")
    if isinstance(meta, dict):
        scenario = str(meta.get("scenario") or "").strip()
        if scenario:
            return scenario
    raw_json = item.get("metadata_json")
    if isinstance(raw_json, str) and raw_json.strip():
        import json

        try:
            payload = json.loads(raw_json)
            if isinstance(payload, dict):
                scenario = str(payload.get("scenario") or "").strip()
                if scenario:
                    return scenario
        except (TypeError, ValueError):
            pass
    return str(item.get("market_phase") or "").strip()


def _last_value(bars: Any, column: str) -> Any:
    try:
        return bars[column].iloc[-1]
    except Exception:
        return None


def _first_value(bars: Any, column: str) -> Any:
    try:
        return bars[column].iloc[0]
    except Exception:
        return None


def _price_at_or_before(bars: Any, when: datetime) -> Optional[float]:
    """取 ``when`` 当日或之前**最后一根** K 线的收盘价（无则 None）。"""
    try:
        if "date" in bars.columns:
            dates = [_as_date(v) for v in bars["date"]]
        else:
            dates = [_as_date(v) for v in bars.index]
    except Exception:
        return None
    target = when.strftime("%Y-%m-%d")
    chosen = None
    for index, date_text in enumerate(dates):
        if date_text and date_text <= target:
            chosen = index
        else:
            break
    if chosen is None:
        return None
    try:
        return _to_float(bars["close"].iloc[chosen])
    except Exception:
        return None


def _coerce_market_item(raw: Any) -> MarketReviewItem:
    if isinstance(raw, MarketReviewItem):
        return raw
    if isinstance(raw, dict):
        return MarketReviewItem(
            kind=str(raw.get("kind") or "index"),
            name=str(raw.get("name") or ""),
            week_return_pct=_to_float(raw.get("week_return_pct")),
            value=_to_float(raw.get("value")),
            note=str(raw.get("note") or ""),
        )
    return MarketReviewItem(name=str(raw))


def _main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover
    import argparse
    import json

    parser = argparse.ArgumentParser(description="高抛低吸助手 · P3 周度复盘（FR-11）")
    parser.add_argument("--symbols", nargs="*", default=None, help="下周关注标的；缺省为全部启用自选股")
    parser.add_argument("--dry-run", action="store_true", help="只生成不发送")
    parser.add_argument("--no-refresh-outcomes", action="store_true", help="不刷新信号结果（只用既有）")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    service = WeeklyReviewService(refresh_outcomes=not args.no_refresh_outcomes)
    result = service.run(symbols=args.symbols, dry_run=args.dry_run)

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(result.review.to_markdown() if result.review else "（未生成报告）")
        print("")
        print(f"[发送情况] 文本={result.sent_text} 跳过原因={result.skipped_reason or '-'}")
        for err in result.errors:
            print(f"  · {err}")
    return 0 if not result.errors else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
