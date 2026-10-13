# -*- coding: utf-8 -*-
"""高抛低吸助手 · P2 个股详情聚合（H5 主入口 · FR-14.2.1）

把「最新决策信号 + 实时行情 + 事件与风险」聚合成**一屏可读**的个股详情：
`人话结论（顶部大字号）` + `四要素（建仓区间 / 止损 / 目标 / 仓位建议）`
+ `多因子明细` + `事件与风险`。

设计约束：

1. **四端同源（NFR-9）**：结论与四要素一律**取自已落库的 `decision_signals`**，
   本模块**不重算**指标、不落地新口径，避免"推送说减仓、H5 说持有"；
2. **纯函数组装**：`build_overview()` 不触网、不读库，便于离线单测；
3. **失败隔离（NFR-8）**：行情 / 事件任一失败只体现在 `degraded` 列表，不阻断整屏；
4. **降级可解释**：无信号、行情不可用、事件源未配置都会显式说明，**不伪装成正常**。

用法::

    python -m src.services.stock_overview_service 601919
    python -m src.services.stock_overview_service 601919 --no-quote --json
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

from src.services.daily_digest import ACTION_LABELS, DISCLAIMER, SCENARIO_LABELS

logger = logging.getLogger(__name__)

__all__ = [
    "FactorRow",
    "StockOverview",
    "StockOverviewService",
    "build_factors",
    "build_overview",
    "format_price",
    "format_ratio",
]

TREND_LABELS = {"up": "向上", "down": "向下", "sideways": "震荡"}


# --------------------------------------------------------------------------- #
# 格式化（与推送同口径：价格 >=1 保留两位，<1 保留三位）
# --------------------------------------------------------------------------- #
def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def format_price(value: Any) -> str:
    number = _finite(value)
    if number is None:
        return "-"
    return f"{number:.2f}" if abs(number) >= 1 else f"{number:.3f}"


def format_ratio(value: Any) -> str:
    number = _finite(value)
    if number is None:
        return "-"
    return f"{number * 100:.0f}%"


def _signed_pct(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:+.2f}%"


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FactorRow:
    """多因子明细的一行（H5 可展开区）。"""

    key: str
    label: str
    value: str
    tone: str = "neutral"  # positive | negative | warning | neutral

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "label": self.label, "value": self.value, "tone": self.tone}


@dataclass
class StockOverview:
    """一屏个股详情。字段缺失一律用 ``None`` / 空列表，由前端决定隐藏。"""

    symbol: str
    name: str = ""
    as_of: str = ""
    generated_at: str = ""

    # 结论区（顶部大字号）
    action: str = "watch"
    action_label: str = "观望"
    scenario: str = "unknown"
    scenario_label: str = "趋势不明"
    conclusion: str = ""
    reason: str = ""
    score: Optional[int] = None
    confidence: Optional[float] = None
    is_urgent: bool = False

    # 四要素（FR-14.2.1）
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    entry_zone: str = "-"
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None
    position_ratio: Optional[float] = None
    position_ratio_label: str = "-"
    position_reason: str = ""
    discipline: str = ""

    # 依据区
    watch_conditions: str = ""
    risk_summary: str = ""
    factors: List[FactorRow] = field(default_factory=list)

    # 事件与风险（FR-20）
    events: List[str] = field(default_factory=list)

    # 行情（可缺失）与相对价位
    quote: Optional[Dict[str, Any]] = None
    distance_to_stop_pct: Optional[float] = None
    distance_to_target_pct: Optional[float] = None

    # 溯源与降级
    signal_id: Optional[int] = None
    signal_created_at: Optional[str] = None
    degraded: List[str] = field(default_factory=list)
    disclaimer: str = DISCLAIMER

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "as_of": self.as_of,
            "generated_at": self.generated_at,
            "action": self.action,
            "action_label": self.action_label,
            "scenario": self.scenario,
            "scenario_label": self.scenario_label,
            "conclusion": self.conclusion,
            "reason": self.reason,
            "score": self.score,
            "confidence": self.confidence,
            "is_urgent": self.is_urgent,
            "entry_low": self.entry_low,
            "entry_high": self.entry_high,
            "entry_zone": self.entry_zone,
            "stop_loss": self.stop_loss,
            "target_price": self.target_price,
            "position_ratio": self.position_ratio,
            "position_ratio_label": self.position_ratio_label,
            "position_reason": self.position_reason,
            "discipline": self.discipline,
            "watch_conditions": self.watch_conditions,
            "risk_summary": self.risk_summary,
            "factors": [row.to_dict() for row in self.factors],
            "events": list(self.events),
            "quote": self.quote,
            "distance_to_stop_pct": self.distance_to_stop_pct,
            "distance_to_target_pct": self.distance_to_target_pct,
            "signal_id": self.signal_id,
            "signal_created_at": self.signal_created_at,
            "degraded": list(self.degraded),
            "disclaimer": self.disclaimer,
        }


# --------------------------------------------------------------------------- #
# 纯函数组装
# --------------------------------------------------------------------------- #
def _truthy(value: Any) -> bool:
    return bool(value) if isinstance(value, bool) else str(value or "").strip().lower() in {
        "true",
        "1",
        "yes",
        "是",
    }


def build_factors(signal: Optional[Dict[str, Any]]) -> List[FactorRow]:
    """从信号记录推导多因子明细（H5 可展开区）。无信号返回空列表。"""

    if not isinstance(signal, dict) or not signal:
        return []

    metadata = signal.get("metadata") if isinstance(signal.get("metadata"), dict) else {}
    evidence = signal.get("evidence") if isinstance(signal.get("evidence"), dict) else {}

    rows: List[FactorRow] = []

    scenario = str(signal.get("scenario") or metadata.get("scenario") or "").strip()
    if scenario:
        rows.append(
            FactorRow(
                key="scenario",
                label="场景判定",
                value=SCENARIO_LABELS.get(scenario, scenario),
                tone="neutral",
            )
        )

    trend = str(metadata.get("trend") or "").strip()
    if trend:
        rows.append(
            FactorRow(
                key="trend",
                label="日线趋势",
                value=TREND_LABELS.get(trend, trend),
                tone={"up": "positive", "down": "negative"}.get(trend, "neutral"),
            )
        )

    weekly = str(metadata.get("weekly_trend") or "").strip()
    if weekly:
        rows.append(
            FactorRow(
                key="weekly_trend",
                label="周线趋势",
                value=TREND_LABELS.get(weekly, weekly),
                tone={"up": "positive", "down": "negative"}.get(weekly, "neutral"),
            )
        )

    conflict = metadata.get("multi_period_conflict")
    if conflict is not None:
        conflicted = _truthy(conflict)
        rows.append(
            FactorRow(
                key="multi_period_conflict",
                label="多周期共振",
                value="存在冲突（大小周期方向不一致）" if conflicted else "共振（方向一致）",
                tone="warning" if conflicted else "positive",
            )
        )

    score = signal.get("score")
    if isinstance(score, (int, float)):
        rows.append(
            FactorRow(
                key="score",
                label="综合评分",
                value=f"{int(score)}/100",
                tone="positive" if float(score) >= 60 else ("warning" if float(score) >= 45 else "negative"),
            )
        )

    confidence = _finite(signal.get("confidence"))
    if confidence is not None:
        rows.append(
            FactorRow(
                key="confidence",
                label="置信度",
                value=f"{confidence:.2f}",
                tone="positive" if confidence >= 0.7 else "neutral",
            )
        )

    box_high = _finite(metadata.get("box_high"))
    if box_high is not None:
        rows.append(
            FactorRow(key="box_high", label="箱体上沿", value=format_price(box_high), tone="neutral")
        )

    risk_reasons = evidence.get("risk_reasons") or []
    if isinstance(risk_reasons, (list, tuple)) and risk_reasons:
        rows.append(
            FactorRow(
                key="risk_reasons",
                label="风控依据",
                value="；".join(str(item) for item in list(risk_reasons)[:3] if item),
                tone="warning",
            )
        )

    scenario_reasons = evidence.get("reasons") or []
    if isinstance(scenario_reasons, (list, tuple)) and scenario_reasons:
        rows.append(
            FactorRow(
                key="scenario_reasons",
                label="场景依据",
                value="；".join(str(item) for item in list(scenario_reasons)[:3] if item),
                tone="neutral",
            )
        )

    return rows


def _entry_zone(low: Optional[float], high: Optional[float]) -> str:
    if low is None and high is None:
        return "-"
    if low is not None and high is not None and low != high:
        return f"{format_price(low)}~{format_price(high)}"
    return format_price(high if high is not None else low)


def _discipline_line(
    entry_zone: str,
    stop_loss: Optional[float],
    target_price: Optional[float],
    position_ratio_label: str,
) -> str:
    bits = [f"仓位建议 {position_ratio_label}"]
    if entry_zone and entry_zone != "-":
        bits.append(f"参考价位 {entry_zone}")
    if stop_loss is not None:
        bits.append(f"止损 {format_price(stop_loss)}")
    if target_price is not None:
        bits.append(f"目标 {format_price(target_price)}")
    return " ｜ ".join(bits)


def _distance_pct(current: Optional[float], reference: Optional[float]) -> Optional[float]:
    if current is None or reference is None or reference == 0:
        return None
    return (current - reference) / reference * 100.0


def build_overview(
    *,
    symbol: str,
    name: str = "",
    signal: Optional[Dict[str, Any]] = None,
    events: Optional[Sequence[str]] = None,
    quote: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> StockOverview:
    """把信号 + 行情 + 事件组装成一屏详情（纯函数，不触网不读库）。"""

    moment = now or datetime.now()
    normalized_symbol = str(symbol or "").strip().upper()

    overview = StockOverview(
        symbol=normalized_symbol,
        name=str(name or "").strip(),
        as_of=moment.strftime("%Y-%m-%d"),
        generated_at=moment.isoformat(timespec="seconds"),
        events=[str(item) for item in (events or []) if str(item or "").strip()],
    )

    has_signal = isinstance(signal, dict) and bool(signal)
    if has_signal:
        record = signal or {}
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        quote_advice = metadata.get("position_advice")
        position_advice = quote_advice if isinstance(quote_advice, dict) else {}

        action = str(record.get("action") or "watch").strip().lower()
        scenario = str(record.get("scenario") or metadata.get("scenario") or "unknown").strip()
        reason = str(record.get("reason") or position_advice.get("reason") or "").strip()

        overview.action = action
        overview.action_label = ACTION_LABELS.get(action, action)
        overview.scenario = scenario
        overview.scenario_label = SCENARIO_LABELS.get(scenario, scenario or "趋势不明")
        overview.reason = reason
        overview.is_urgent = _truthy(metadata.get("is_urgent")) or action in {"sell", "clear"}
        overview.score = int(record["score"]) if isinstance(record.get("score"), (int, float)) else None
        overview.confidence = _finite(record.get("confidence"))

        overview.entry_low = _finite(record.get("entry_low"))
        overview.entry_high = _finite(record.get("entry_high"))
        overview.stop_loss = _finite(record.get("stop_loss"))
        overview.target_price = _finite(record.get("target_price"))
        overview.position_ratio = _finite(position_advice.get("ratio"))
        overview.position_reason = str(position_advice.get("reason") or "").strip()
        overview.watch_conditions = str(record.get("watch_conditions") or "").strip()
        overview.risk_summary = str(record.get("risk_summary") or "").strip()
        overview.signal_id = int(record["id"]) if isinstance(record.get("id"), (int, float)) else None
        overview.signal_created_at = (
            str(record.get("created_at")) if record.get("created_at") is not None else None
        )
        if not overview.name:
            overview.name = str(record.get("stock_name") or "").strip()

        overview.conclusion = f"{overview.action_label}（{overview.scenario_label}）"
        if reason:
            overview.conclusion = f"{overview.conclusion}：{reason}"
    else:
        overview.conclusion = "暂无决策信号"
        overview.degraded.append("暂无该标的的决策信号：请先运行每日信号（P1 推送）或等待下一次分析落库")

    overview.entry_zone = _entry_zone(overview.entry_low, overview.entry_high)
    overview.position_ratio_label = format_ratio(overview.position_ratio)
    overview.discipline = _discipline_line(
        overview.entry_zone,
        overview.stop_loss,
        overview.target_price,
        overview.position_ratio_label,
    )
    overview.factors = build_factors(signal)

    if isinstance(quote, dict) and quote:
        current = _finite(quote.get("current_price"))
        overview.quote = {
            "current_price": current,
            "change_percent": _finite(quote.get("change_percent")),
            "update_time": quote.get("update_time"),
        }
        if current is not None:
            overview.distance_to_stop_pct = _distance_pct(current, overview.stop_loss)
            overview.distance_to_target_pct = _distance_pct(current, overview.target_price)
    else:
        overview.degraded.append("行情暂不可用：现价与距止损/目标距离已隐藏")

    if not overview.events:
        overview.events = []

    return overview


# --------------------------------------------------------------------------- #
# 编排服务（懒加载依赖，全部失败隔离）
# --------------------------------------------------------------------------- #
class StockOverviewService:
    """个股详情聚合编排。依赖均可注入，便于离线测试。"""

    def __init__(
        self,
        *,
        signal_service: Any = None,
        event_source: Any = None,
        stock_service: Any = None,
        event_collector: Optional[Callable[[str], List[str]]] = None,
        quote_loader: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
    ):
        self._signal_service = signal_service
        self._event_source = event_source
        self._stock_service = stock_service
        self._event_collector = event_collector
        self._quote_loader = quote_loader

    # ---------------- 依赖 ---------------- #
    @property
    def signal_service(self):
        if self._signal_service is None:
            from src.services.decision_signal_service import DecisionSignalService

            self._signal_service = DecisionSignalService()
        return self._signal_service

    @property
    def event_source(self):
        if self._event_source is None:
            from src.services.event_source import EventSource

            self._event_source = EventSource()
        return self._event_source

    @property
    def stock_service(self):
        if self._stock_service is None:
            from src.services.stock_service import StockService

            self._stock_service = StockService()
        return self._stock_service

    # ---------------- 各数据块 ---------------- #
    def _load_signal(self, symbol: str, degraded: List[str]) -> Optional[Dict[str, Any]]:
        try:
            payload = self.signal_service.get_latest_active(stock_code=symbol, limit=1)
            items = payload.get("items") if isinstance(payload, dict) else None
            if items:
                record = items[0]
                return record if isinstance(record, dict) else None
        except Exception as exc:  # pragma: no cover - 依赖外部服务
            logger.warning("读取最新信号失败 %s: %s", symbol, exc)
            degraded.append("读取决策信号失败，已按“暂无信号”展示")
        return None

    def _load_events(self, symbol: str, degraded: List[str]) -> List[str]:
        if self._event_collector is not None:
            try:
                return list(self._event_collector(symbol) or [])
            except Exception as exc:  # pragma: no cover
                logger.warning("事件源失败 %s: %s", symbol, exc)
                degraded.append("事件源不可用，已省略事件板块")
                return []
        try:
            return list(self.event_source.collect([symbol]) or [])
        except Exception as exc:  # pragma: no cover - 依赖外部服务
            logger.warning("事件源失败 %s: %s", symbol, exc)
            degraded.append("事件源不可用，已省略事件板块")
            return []

    def _load_quote(self, symbol: str, degraded: List[str]) -> Optional[Dict[str, Any]]:
        if self._quote_loader is not None:
            try:
                quote = self._quote_loader(symbol)
                return quote if isinstance(quote, dict) else None
            except Exception as exc:  # pragma: no cover
                logger.warning("行情失败 %s: %s", symbol, exc)
                degraded.append("行情获取失败，现价与距离已隐藏")
                return None
        try:
            quote = self.stock_service.get_realtime_quote(symbol)
            return quote if isinstance(quote, dict) else None
        except Exception as exc:  # pragma: no cover - 依赖外部数据源
            logger.warning("行情失败 %s: %s", symbol, exc)
            degraded.append("行情获取失败，现价与距离已隐藏")
            return None

    # ---------------- 公共入口 ---------------- #
    def load(self, symbol: str, *, with_quote: bool = True) -> Dict[str, Any]:
        """聚合个股详情；任何子块失败都降级而非抛错（NFR-8）。"""

        normalized = str(symbol or "").strip().upper()
        degraded: List[str] = []
        signal = self._load_signal(normalized, degraded)
        events = self._load_events(normalized, degraded)
        quote = self._load_quote(normalized, degraded) if with_quote else None

        overview = build_overview(
            symbol=normalized,
            signal=signal,
            events=events,
            quote=quote,
        )
        # 合并子块降级说明（build_overview 自身也会写入降级项）
        for reason in degraded:
            if reason not in overview.degraded:
                overview.degraded.append(reason)
        return overview.to_dict()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover - CLI
    import argparse

    parser = argparse.ArgumentParser(description="个股详情聚合（H5 主入口）")
    parser.add_argument("symbol", help="股票代码，如 601919")
    parser.add_argument("--no-quote", action="store_true", help="跳过实时行情（离线）")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args(argv)

    service = StockOverviewService()
    payload = service.load(args.symbol, with_quote=not args.no_quote)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"{payload['symbol']} {payload['name']}".strip())
    print(payload["conclusion"])
    print(payload["discipline"])
    for row in payload["factors"]:
        print(f"  · {row['label']}: {row['value']}")
    print("— 事件与风险 —")
    for event in payload["events"] or ["（今日无重大事件提醒）"]:
        print(f"  · {event}")
    for note in payload["degraded"]:
        print(f"  ! {note}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(_main())
