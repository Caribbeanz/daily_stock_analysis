# -*- coding: utf-8 -*-
"""高抛低吸助手 · P0 信号编排（端到端最小闭环）

职责（对应 P0 详设 §5 时序）：
  读自选股 → 拉数据 → 指标 → 场景 → 风控 → 组装 → **落库 ``decision_signals``**

关键约束：
1. **四端同源**：唯一写入点即 ``DecisionSignalService``（NFR-9）；
2. **失败隔离**：单只标的失败只记录、不中断整批（NFR-8 / FR-2.3）；
3. **可回溯**：每个信号的 ``evidence`` 能还原"依据了哪些指标/阈值"（FR-18）；
4. P0 **只到落库为止**，不做推送（P1）。

用法::

    from src.services.p0_signal_builder import P0SignalBuilder

    builder = P0SignalBuilder()
    summary = builder.run()                 # 跑全部启用的自选股
    summary = builder.run(symbols=["601919"])
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.core.indicators import (
    InsufficientDataError,
    IndicatorSnapshot,
    build_indicator_snapshot,
    resample_weekly,
)
from src.core.risk_engine import PositionView, RiskDecision, RiskEngine
from src.core.rule_config import ScenarioRuleSet, load_scenario_rules, load_risk_rules
from src.core.rule_config import RiskRuleSet
from src.services.scenario_engine import ScenarioDecision, ScenarioEngine
from src.services.watchlist_service import WatchlistService

logger = logging.getLogger(__name__)

__all__ = ["P0SignalBuilder"]

_SOURCE_TYPE = "analysis"
_TRIGGER_SOURCE = "p0_engine"
_MARKET_PHASE = "postmarket"
_DEFAULT_HISTORY_DAYS = 240

_STRENGTH_CONFIDENCE = {"strong": 0.85, "medium": 0.7, "weak": 0.55}


@dataclass
class SymbolResult:
    symbol: str
    name: Optional[str] = None
    ok: bool = False
    action: Optional[str] = None
    scenario: Optional[str] = None
    sub_action: Optional[str] = None
    signal_id: Optional[int] = None
    created: Optional[bool] = None
    duplicate: Optional[bool] = None
    error: Optional[str] = None
    stop_loss: Optional[float] = None
    is_urgent: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "ok": self.ok,
            "action": self.action,
            "scenario": self.scenario,
            "sub_action": self.sub_action,
            "signal_id": self.signal_id,
            "created": self.created,
            "duplicate": self.duplicate,
            "error": self.error,
            "stop_loss": self.stop_loss,
            "is_urgent": self.is_urgent,
        }


@dataclass
class BuildSummary:
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    results: List[SymbolResult] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "results": [r.to_dict() for r in self.results],
        }


class P0SignalBuilder:
    """P0 端到端信号编排器。"""

    def __init__(
        self,
        *,
        watchlist_service: Optional[WatchlistService] = None,
        risk_engine: Optional[RiskEngine] = None,
        scenario_engine: Optional[ScenarioEngine] = None,
        risk_rules: Optional[RiskRuleSet] = None,
        scenario_rules: Optional[ScenarioRuleSet] = None,
        signal_service: Optional[Any] = None,
        history_days: int = _DEFAULT_HISTORY_DAYS,
    ):
        self.watchlist = watchlist_service or WatchlistService()
        self.risk_engine = risk_engine or RiskEngine(risk_rules or load_risk_rules())
        self.scenario_engine = scenario_engine or ScenarioEngine(scenario_rules or load_scenario_rules())
        self._signal_service = signal_service
        self.history_days = history_days

    # ---------------- 依赖 ---------------- #
    @property
    def signal_service(self):
        if self._signal_service is None:
            from src.services.decision_signal_service import DecisionSignalService

            self._signal_service = DecisionSignalService()
        return self._signal_service

    @staticmethod
    def _fetch_daily(symbol: str, days: int):
        from data_provider import DataFetcherManager

        result = DataFetcherManager().get_daily_data(symbol, days=days)
        # 统一入口返回 ``(DataFrame, source_name)``；此处只取 DataFrame。
        # 兼容部分实现直接返回 DataFrame 的情况。
        if isinstance(result, tuple):
            return result[0] if result else None
        return result

    # ---------------- 持仓 ---------------- #
    @staticmethod
    def load_positions() -> Dict[str, PositionView]:
        """从 ``portfolio_positions`` 读取持仓（表为空则返回 {}）。只读、容错。"""
        try:
            from sqlalchemy import select

            from src.storage import DatabaseManager, PortfolioPosition

            with DatabaseManager.get_instance().get_session() as session:
                rows = list(session.execute(select(PortfolioPosition)).scalars().all())
        except Exception as exc:  # pragma: no cover - 持仓子系统不可用不应阻断
            logger.warning("读取持仓失败，按无持仓处理：%s", exc)
            return {}

        positions: Dict[str, PositionView] = {}
        for row in rows:
            symbol = str(getattr(row, "symbol", "") or "").strip().upper()
            if not symbol:
                continue
            positions[symbol] = PositionView(
                symbol=symbol,
                avg_cost=float(getattr(row, "avg_cost", 0.0) or 0.0),
                quantity=float(getattr(row, "quantity", 0.0) or 0.0),
                peak_price=float(getattr(row, "last_price", 0.0) or 0.0),
                market=str(getattr(row, "market", "cn") or "cn"),
            )
        return positions

    # ---------------- 单标的 ---------------- #
    def build_for_symbol(
        self,
        item: Dict[str, Any],
        *,
        position: Optional[PositionView] = None,
    ) -> SymbolResult:
        symbol = str(item.get("symbol") or "").strip().upper()
        result = SymbolResult(symbol=symbol, name=item.get("name"))
        if not symbol:
            result.error = "empty symbol"
            return result

        try:
            daily = self._prepare_daily(symbol)
            daily_snapshot = build_indicator_snapshot(daily)

            weekly = resample_weekly(daily)
            weekly_snapshot: Optional[IndicatorSnapshot] = None
            if weekly is not None and not weekly.empty:
                try:
                    weekly_snapshot = build_indicator_snapshot(weekly, min_bars=15)
                except InsufficientDataError:
                    weekly_snapshot = None

            box_high = self._box_high(daily)
            has_position = position is not None and position.has_position()

            scenario = self.scenario_engine.evaluate(
                daily_snapshot,
                weekly=weekly_snapshot,
                has_position=has_position,
                box_high=box_high,
            )
            risk = self._evaluate_risk(scenario, daily_snapshot, position)

            action = self._resolve_action(scenario, risk, has_position)
            payload = self._build_payload(
                item=item,
                scenario=scenario,
                risk=risk,
                action=action,
                snapshot=daily_snapshot,
                box_high=box_high,
            )
            outcome = self.signal_service.create_signal_with_outcome(payload)

            result.ok = True
            result.action = action
            result.scenario = scenario.scenario
            result.sub_action = scenario.sub_action
            result.stop_loss = None if not math.isfinite(risk.stop_loss) else risk.stop_loss
            result.is_urgent = bool(risk.is_urgent)
            result.signal_id = outcome.item.get("id")
            result.created = outcome.created
            result.duplicate = outcome.duplicate
            return result
        except Exception as exc:  # 失败隔离
            logger.warning("P0 signal build failed for %s: %s", symbol, exc)
            result.error = f"{type(exc).__name__}: {exc}"
            return result

    # ---------------- 批量 ---------------- #
    def run(
        self,
        *,
        symbols: Optional[List[str]] = None,
        ensure_ready: bool = True,
        limit: Optional[int] = None,
    ) -> BuildSummary:
        if ensure_ready:
            try:
                self.watchlist.ensure_ready()
            except Exception as exc:  # pragma: no cover
                logger.warning("watchlist ensure_ready failed: %s", exc)

        items = self.watchlist.list_watchlist(enabled_only=True)
        if symbols:
            wanted = {str(s).strip().upper() for s in symbols}
            items = [i for i in items if str(i.get("symbol", "")).upper() in wanted]
        if limit is not None:
            items = items[: max(0, int(limit))]

        positions = self.load_positions()
        summary = BuildSummary(total=len(items))
        for item in items:
            symbol = str(item.get("symbol") or "").upper()
            result = self.build_for_symbol(item, position=positions.get(symbol))
            summary.results.append(result)
            if result.ok:
                summary.succeeded += 1
            else:
                summary.failed += 1
        return summary

    # ---------------- 内部 ---------------- #
    def _prepare_daily(self, symbol: str):
        from src.services.alert_indicators import normalize_ohlcv

        raw = self._fetch_daily(symbol, self.history_days)
        if raw is None:
            raise InsufficientDataError(f"no data for {symbol}")
        frame = normalize_ohlcv(raw, required_columns=("open", "high", "low", "close", "volume"))
        if frame is None or frame.empty:
            raise InsufficientDataError(f"empty normalized data for {symbol}")
        return frame

    @staticmethod
    def _box_high(daily) -> Optional[float]:
        lookback = 20
        try:
            if len(daily) <= lookback + 1:
                return None
            window = daily["high"].iloc[-(lookback + 1):-1]
            value = float(window.max())
            return value if math.isfinite(value) else None
        except Exception:
            return None

    def _evaluate_risk(
        self,
        scenario: ScenarioDecision,
        snapshot: IndicatorSnapshot,
        position: Optional[PositionView],
    ) -> RiskDecision:
        if position is not None and position.has_position():
            return self.risk_engine.evaluate_hold(snapshot, position)
        if scenario.action == "buy" and scenario.sub_action == "build":
            return self.risk_engine.evaluate_build(snapshot)
        if scenario.action == "buy" and scenario.sub_action == "dip_buy":
            return self.risk_engine.evaluate_dip_buy(snapshot)
        decision = RiskDecision()
        decision.stop_loss = self.risk_engine.compute_stop_loss(snapshot, position)
        decision.position_advice = {"action": "watch", "ratio": 0.0, "reason": "无明确买点，观望"}
        return decision

    @staticmethod
    def _resolve_action(scenario: ScenarioDecision, risk: RiskDecision, has_position: bool) -> str:
        if has_position:
            return risk.action()  # 持仓时风控优先（止损/止盈/减仓）
        if risk.action() in {"buy", "add"} and scenario.action == "buy":
            return scenario.action
        if scenario.action == "buy":
            return "buy"
        return "watch"

    def _build_payload(
        self,
        *,
        item: Dict[str, Any],
        scenario: ScenarioDecision,
        risk: RiskDecision,
        action: str,
        snapshot: IndicatorSnapshot,
        box_high: Optional[float],
    ) -> Dict[str, Any]:
        close = snapshot.close
        confidence = _STRENGTH_CONFIDENCE.get(scenario.strength, 0.6)
        score = self._score(scenario, risk, action)

        entry_low = entry_high = None
        if action in {"buy", "add"} and math.isfinite(close):
            pullback_ref = snapshot.ma.get(20)
            entry_low = round(min(close, pullback_ref) if pullback_ref and math.isfinite(pullback_ref) else close * 0.985, 3)
            entry_high = round(close, 3)
            if entry_low > entry_high:
                entry_low, entry_high = entry_high, entry_low

        target_price = self._first_target(risk)

        metadata: Dict[str, Any] = {
            "asset_type": item.get("asset_type") or "stock",
            "scenario": scenario.scenario,
            "trend": scenario.trend,
            "weekly_trend": scenario.weekly_trend,
            "multi_period_conflict": scenario.multi_period_conflict,
            "sub_action": scenario.sub_action,
            "is_urgent": bool(risk.is_urgent),
            "position_advice": risk.position_advice,
            "take_profit_plan": risk.take_profit_plan,
            "box_high": box_high,
            "as_of": snapshot.as_of,
            "engine": _TRIGGER_SOURCE,
        }
        if item.get("track_index"):
            metadata["track_index"] = item.get("track_index")

        payload: Dict[str, Any] = {
            "stock_code": item.get("symbol"),
            "stock_name": item.get("name"),
            "market": item.get("market") or "cn",
            "source_type": _SOURCE_TYPE,
            "trigger_source": _TRIGGER_SOURCE,
            "decision_profile": "balanced",
            "market_phase": _MARKET_PHASE,
            "action": action,
            "confidence": confidence,
            "score": score,
            "horizon": "swing" if action in {"buy", "add"} else None,
            "entry_low": entry_low,
            "entry_high": entry_high,
            "stop_loss": None if not math.isfinite(risk.stop_loss) else risk.stop_loss,
            "target_price": target_price,
            "reason": self._one_line_reason(scenario, risk, action),
            "risk_summary": self._risk_summary(risk),
            "watch_conditions": self._watch_conditions(action),
            "evidence": {"scenario": scenario.evidence, "reasons": scenario.reasons, "risk_reasons": risk.reasons},
            "metadata": metadata,
            "report_language": "zh",
        }
        return {k: v for k, v in payload.items() if v is not None}

    @staticmethod
    def _first_target(risk: RiskDecision) -> Optional[float]:
        for tier in risk.take_profit_plan:
            if tier.get("kind") == "target" and not tier.get("triggered"):
                price = tier.get("price")
                if isinstance(price, (int, float)) and math.isfinite(float(price)):
                    return float(price)
        return None

    @staticmethod
    def _score(scenario: ScenarioDecision, risk: RiskDecision, action: str) -> int:
        base = 50
        if scenario.strength == "strong":
            base += 12
        elif scenario.strength == "weak":
            base -= 6
        if scenario.trend == "up":
            base += 10
        elif scenario.trend == "down":
            base -= 10
        if scenario.multi_period_conflict:
            base -= 8
        if action in {"buy", "add"}:
            base += 6
        elif action in {"reduce", "sell"}:
            base -= 6
        return max(0, min(100, int(base)))

    @staticmethod
    def _one_line_reason(scenario: ScenarioDecision, risk: RiskDecision, action: str) -> str:
        adv = risk.position_advice or {}
        reason = str(adv.get("reason") or "")
        scenario_reason = scenario.reasons[0] if scenario.reasons else ""
        parts = [p for p in (scenario_reason, reason) if p]
        text = "；".join(parts) or "按当前指标给出中性建议"
        return f"[{action}] {text}"

    @staticmethod
    def _risk_summary(risk: RiskDecision) -> Optional[str]:
        if not risk.reasons:
            return None
        return "；".join(risk.reasons)

    @staticmethod
    def _watch_conditions(action: str) -> Optional[str]:
        mapping = {
            "buy": "跌破止损位则放弃建仓；放量滞涨则暂缓",
            "add": "回踩不破可加，破止损位停止加仓",
            "hold": "跌破止损位离场；触及止盈档位分批减",
            "reduce": "若重新站稳关键均线可减轻仓位",
            "sell": "已触发风控，无条件离场；重新站上 MA20 再评估",
            "watch": "等待周线确认向上或出现明确买点",
            "avoid": "趋势未明，暂不参与",
        }
        return mapping.get(action)


def _main(argv: Optional[List[str]] = None) -> int:
    """命令行入口：生成 P0 信号并落库。

    示例：
        python -m src.services.p0_signal_builder                 # 全部启用自选股
        python -m src.services.p0_signal_builder --symbols 601919 513100
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="高抛低吸助手 · P0 信号生成（指标→场景→风控→落库 decision_signals）"
    )
    parser.add_argument("--symbols", nargs="*", default=None, help="指定标的；缺省为全部启用的自选股")
    parser.add_argument("--limit", type=int, default=None, help="最多处理前 N 只")
    parser.add_argument("--no-seed", action="store_true", help="不写入首批默认标的")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    builder = P0SignalBuilder()
    summary = builder.run(
        symbols=args.symbols,
        ensure_ready=not args.no_seed,
        limit=args.limit,
    )
    print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
