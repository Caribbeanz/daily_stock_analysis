# -*- coding: utf-8 -*-
"""高抛低吸助手 · 规则体检回测（P1 上线前质量门 · FR-12）

目的：在开启每日推送（P1）之前，用**近 3 年历史数据**验证 P0 的建仓规则与
风控（止损/分批止盈）是否真的有效，避免"规则没效就上线"。

方法（walk-forward，无未来函数）：
1. 逐交易日 t：**只用截至 t 的数据**计算指标 → 场景引擎 → 风控引擎 → 得到建仓信号；
2. 若当日给出建仓信号，则自 t+1 起**模拟持仓**：
   - 盘中触及**止损位** → 止损离场（按止损价）；
   - 盘中触及**第一目标价** → 止盈离场（按目标价）；
   - 否则持有到 t+horizon 后按收盘价离场；
3. 统计：胜率、盈亏比、最大回撤、样本数，并与**同期买入持有**对照。

质量门阈值（可在 ``config/rules/backtest_rules.yaml`` 调整，缺省见 ``DEFAULT_GATE``）：
- 胜率 ≥ 50%｜盈亏比 ≥ 1.5｜最大回撤 ≤ 25%｜样本数 ≥ 30

**注意**：本回测只评估**建仓侧 + 出场风控**规则（`has_position=False` 视角），
不评估持仓中的加仓/减仓时序；历史表现不代表未来收益。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from src.core.indicators import (
    IndicatorSnapshot,
    InsufficientDataError,
    build_indicator_snapshot,
    resample_weekly,
)
from src.core.risk_engine import RiskDecision, RiskEngine
from src.core.rule_config import ScenarioRuleSet, load_scenario_rules, load_risk_rules
from src.core.rule_config import RiskRuleSet
from src.services.scenario_engine import ScenarioDecision, ScenarioEngine

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_GATE",
    "DEFAULT_FETCH_DAYS",
    "load_gate_thresholds",
    "Trade",
    "SymbolBacktest",
    "BacktestReport",
    "RuleBacktester",
]

DEFAULT_GATE: Dict[str, float] = {
    # 期望收益制口径（适配"截断亏损、让利润奔跑"的止损型趋势策略）：
    # 期望收益 > 0 + 盈亏比 ≥ 1.5 + 最大回撤 ≤ 25% + 样本 ≥ 30；
    # 胜率对这类策略天然偏低，故默认仅作**参考项**（min_win_rate > 0 时才强制）。
    "min_expectation": 0.0,
    "min_profit_factor": 1.5,
    "max_drawdown": 0.25,
    "min_samples": 30,
    "min_win_rate": 0.0,     # 0 = 不强制（参考项）；设为 0.5 即可恢复"胜率≥50%"硬门槛
}

DEFAULT_HORIZON_DAYS = 15
DEFAULT_WARMUP = 80
# 免费数据源（腾讯源）单标的最多返回约 800 根 K 线（≈3.2 年），
# 因此取数跨度取 600 天（≈793 根 ≈ 3 年），与需求"近 3 年"一致。
DEFAULT_FETCH_DAYS = 600
_GATE_CONFIG_PATH = "config/rules/backtest_rules.yaml"


def load_gate_thresholds(path: Optional[str] = None) -> Dict[str, float]:
    """从 ``config/rules/backtest_rules.yaml`` 读取质量门阈值；缺失则回退默认（NFR-6）。"""
    from pathlib import Path

    file = Path(path or _GATE_CONFIG_PATH)
    if not file.exists():
        return dict(DEFAULT_GATE)
    try:
        import yaml

        payload = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        gate = payload.get("gate") or {}
        merged = dict(DEFAULT_GATE)
        for key in DEFAULT_GATE:
            value = gate.get(key)
            if isinstance(value, (int, float)):
                merged[key] = float(value)
        return merged
    except Exception as exc:  # pragma: no cover - 配置异常不应阻断
        logger.warning("读取回测质量门配置失败，回退默认：%s", exc)
        return dict(DEFAULT_GATE)


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Trade:
    symbol: str
    entry_index: int
    entry_date: str
    entry_price: float
    stop_loss: Optional[float]
    target_price: Optional[float]
    exit_index: int = 0
    exit_date: str = ""
    exit_price: float = 0.0
    exit_reason: str = "horizon"      # stop | target | horizon
    hold_days: int = 0
    return_pct: float = 0.0           # 百分比，例如 5.2 表示 +5.2%

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "entry_date": self.entry_date,
            "entry_price": round(self.entry_price, 4),
            "stop_loss": None if self.stop_loss is None else round(self.stop_loss, 4),
            "target_price": None if self.target_price is None else round(self.target_price, 4),
            "exit_date": self.exit_date,
            "exit_price": round(self.exit_price, 4),
            "exit_reason": self.exit_reason,
            "hold_days": self.hold_days,
            "return_pct": round(self.return_pct, 3),
        }


@dataclass
class SymbolBacktest:
    symbol: str = ""
    trades: List[Trade] = field(default_factory=list)
    samples: int = 0
    wins: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    avg_return: float = 0.0
    max_drawdown: float = 0.0
    benchmark_return: float = 0.0
    bars: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "samples": self.samples,
            "wins": self.wins,
            "win_rate": round(self.win_rate, 4),
            "avg_win": round(self.avg_win, 3),
            "avg_loss": round(self.avg_loss, 3),
            "profit_factor": round(self.profit_factor, 3),
            "avg_return": round(self.avg_return, 3),
            "max_drawdown": round(self.max_drawdown, 4),
            "benchmark_return": round(self.benchmark_return, 3),
            "bars": self.bars,
            "trades": [t.to_dict() for t in self.trades],
        }


@dataclass
class BacktestReport:
    symbols: List[SymbolBacktest] = field(default_factory=list)
    overall: Optional[SymbolBacktest] = None
    gate: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_GATE))
    passed: bool = False
    violated: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "violated": list(self.violated),
            "gate": dict(self.gate),
            "overall": self.overall.to_dict() if self.overall else None,
            "symbols": [
                {k: v for k, v in s.to_dict().items() if k != "trades"} for s in self.symbols
            ],
            "notes": list(self.notes),
        }

    def to_markdown(self) -> str:
        lines = ["【P1 上线前 · 规则体检报告】", ""]
        verdict = "✅ 通过，可开启每日推送" if self.passed else "❌ 未通过，需先调参"
        lines.append(f"结论：{verdict}")
        if self.violated:
            lines.append("未达标项：" + "；".join(self.violated))
        lines.append("")
        lines.append("| 标的 | 样本 | 胜率 | 盈亏比 | 平均收益% | 最大回撤 | 同期买入持有% |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        rows = self.symbols + ([self.overall] if self.overall else [])
        for stat in rows:
            if stat is None:
                continue
            label = stat.symbol or "合计"
            lines.append(
                f"| {label} | {stat.samples} | {stat.win_rate * 100:.1f}% | "
                f"{stat.profit_factor:.2f} | {stat.avg_return:.2f} | "
                f"{stat.max_drawdown * 100:.1f}% | {stat.benchmark_return:.2f} |"
            )
        lines.append("")
        gate = self.gate
        parts = [
            f"期望收益>{float(gate.get('min_expectation', 0.0) or 0.0):.2f}%",
            f"盈亏比≥{gate['min_profit_factor']}",
            f"回撤≤{gate['max_drawdown']:.0%}",
            f"样本≥{gate['min_samples']:.0f}",
        ]
        if float(gate.get("min_win_rate", 0.0) or 0.0) > 0:
            parts.append(f"胜率≥{float(gate['min_win_rate']):.0%}")
        threshold_line = "阈值：" + "｜".join(parts)
        if float(gate.get("min_win_rate", 0.0) or 0.0) <= 0:
            threshold_line += "（胜率为参考项，不作拦截）"
        lines.append(threshold_line)
        for note in self.notes:
            lines.append(f"· {note}")
        lines.append("")
        lines.append("（历史表现不代表未来收益，仅供规则体检参考。）")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 回测器
# --------------------------------------------------------------------------- #
class RuleBacktester:
    """P0 建仓规则 + 风控出场的 walk-forward 回测器。"""

    def __init__(
        self,
        *,
        scenario_engine: Optional[ScenarioEngine] = None,
        risk_engine: Optional[RiskEngine] = None,
        risk_rules: Optional[RiskRuleSet] = None,
        scenario_rules: Optional[ScenarioRuleSet] = None,
        horizon_days: int = DEFAULT_HORIZON_DAYS,
        warmup: int = DEFAULT_WARMUP,
    ):
        self.scenario_engine = scenario_engine or ScenarioEngine(scenario_rules or load_scenario_rules())
        self.risk_engine = risk_engine or RiskEngine(risk_rules or load_risk_rules())
        self.horizon_days = int(horizon_days)
        self.warmup = int(warmup)

    # ---------------- 单日信号 ---------------- #
    def _signal_at(self, window: Any) -> Optional[Dict[str, Any]]:
        """只基于 ``window``（截至当日）生成信号；无建仓信号返回 None。"""
        try:
            snapshot: IndicatorSnapshot = build_indicator_snapshot(window)
        except InsufficientDataError:
            return None

        weekly_snapshot: Optional[IndicatorSnapshot] = None
        try:
            weekly = resample_weekly(window)
            if weekly is not None and not weekly.empty:
                weekly_snapshot = build_indicator_snapshot(weekly, min_bars=15)
        except Exception:
            weekly_snapshot = None

        box_high = self._box_high(window)
        scenario: ScenarioDecision = self.scenario_engine.evaluate(
            snapshot, weekly=weekly_snapshot, has_position=False, box_high=box_high
        )
        if scenario.action != "buy" or not math.isfinite(snapshot.close):
            return None

        if scenario.sub_action == "dip_buy":
            risk: RiskDecision = self.risk_engine.evaluate_dip_buy(snapshot)
        else:
            risk = self.risk_engine.evaluate_build(snapshot)

        target: Optional[float] = None
        for tier in risk.take_profit_plan:
            if tier.get("kind") == "target":
                price = _finite(tier.get("price"))
                if price is not None:
                    target = price
                    break

        return {
            "entry_price": float(snapshot.close),
            "stop_loss": _finite(risk.stop_loss),
            "target_price": target,
            "scenario": scenario.scenario,
            "sub_action": scenario.sub_action,
        }

    @staticmethod
    def _box_high(window: Any) -> Optional[float]:
        lookback = 20
        try:
            if len(window) <= lookback + 1:
                return None
            value = float(window["high"].iloc[-(lookback + 1):-1].max())
            return value if math.isfinite(value) else None
        except Exception:
            return None

    # ---------------- 单标的 ---------------- #
    def backtest_symbol(self, symbol: str, df: Any) -> SymbolBacktest:
        stat = SymbolBacktest(symbol=symbol, bars=0 if df is None else int(len(df)))
        if df is None or len(df) <= self.warmup + 2:
            return stat

        trades: List[Trade] = []
        total = len(df)
        index = self.warmup
        next_allowed = self.warmup

        while index < total - 1:
            if index < next_allowed:
                index += 1
                continue
            window = df.iloc[: index + 1]
            signal = self._signal_at(window)
            if signal is None:
                index += 1
                continue

            trade = self._simulate_trade(symbol, df, index, signal)
            trades.append(trade)
            next_allowed = trade.exit_index + 1
            index = trade.exit_index + 1

        stat.trades = trades
        _aggregate(stat, trades)
        stat.benchmark_return = _benchmark_return(df, self.warmup)
        return stat

    def _simulate_trade(self, symbol: str, df: Any, entry_index: int, signal: Dict[str, Any]) -> Trade:
        entry_price = float(signal["entry_price"])
        stop = signal.get("stop_loss")
        target = signal.get("target_price")
        dates = _dates_of(df)

        exit_index = min(entry_index + self.horizon_days, len(df) - 1)
        exit_price = float(df["close"].iloc[exit_index])
        exit_reason = "horizon"

        for offset in range(entry_index + 1, exit_index + 1):
            low = _finite(df["low"].iloc[offset])
            high = _finite(df["high"].iloc[offset])
            if stop is not None and low is not None and low <= stop:
                exit_index, exit_price, exit_reason = offset, float(stop), "stop"
                break
            if target is not None and high is not None and high >= target:
                exit_index, exit_price, exit_reason = offset, float(target), "target"
                break

        ret_pct = (exit_price - entry_price) / entry_price * 100.0 if entry_price else 0.0
        return Trade(
            symbol=symbol,
            entry_index=entry_index,
            entry_date=dates[entry_index],
            entry_price=entry_price,
            stop_loss=stop,
            target_price=target,
            exit_index=exit_index,
            exit_date=dates[exit_index],
            exit_price=exit_price,
            exit_reason=exit_reason,
            hold_days=exit_index - entry_index,
            return_pct=ret_pct,
        )

    # ---------------- 批量 ---------------- #
    def run(
        self,
        symbols: Sequence[str],
        *,
        fetch: Optional[Callable[[str, int], Any]] = None,
        fetch_days: int = DEFAULT_FETCH_DAYS,
        gate: Optional[Dict[str, float]] = None,
    ) -> BacktestReport:
        fetch = fetch or self._default_fetch
        report = BacktestReport(gate={**load_gate_thresholds(), **(gate or {})})

        all_trades: List[Trade] = []
        for symbol in symbols:
            try:
                df = fetch(str(symbol), fetch_days)
            except Exception as exc:
                logger.warning("回测取数失败 %s：%s", symbol, exc)
                report.notes.append(f"{symbol} 取数失败，已跳过（{exc}）")
                report.symbols.append(SymbolBacktest(symbol=str(symbol)))
                continue
            if df is None or len(df) == 0:
                report.notes.append(f"{symbol} 无历史数据，已跳过")
                report.symbols.append(SymbolBacktest(symbol=str(symbol)))
                continue
            stat = self.backtest_symbol(str(symbol), df)
            report.symbols.append(stat)
            all_trades.extend(stat.trades)

        all_trades.sort(key=lambda t: (t.symbol, t.entry_index))
        overall = SymbolBacktest(symbol="")
        overall.trades = all_trades
        _aggregate(overall, all_trades)
        overall.benchmark_return = _average([s.benchmark_return for s in report.symbols])
        report.overall = overall

        _apply_gate(report)
        report.notes.append(
            "方法：逐日 walk-forward（仅用当日及之前数据）；一次只持一仓；"
            "止损/目标优先于到期离场。"
        )
        return report

    @staticmethod
    def _default_fetch(symbol: str, days: int):  # pragma: no cover - 依赖网络
        from src.services.alert_indicators import normalize_ohlcv
        from src.services.p0_signal_builder import P0SignalBuilder

        raw = P0SignalBuilder._fetch_daily(symbol, days)  # noqa: SLF001 - 复用同一取数口径
        if raw is None:
            return None
        return normalize_ohlcv(raw, required_columns=("open", "high", "low", "close", "volume"))


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
def _dates_of(df: Any) -> List[str]:
    try:
        if "date" in df.columns:
            raw = list(df["date"])
        else:
            raw = list(df.index)
    except Exception:
        raw = list(range(len(df)))
    out = []
    for value in raw:
        text = str(value)
        if " " in text:
            text = text.split(" ")[0]
        out.append(text)
    if len(out) < len(df):
        out += [""] * (len(df) - len(out))
    return out


def _aggregate(stat: SymbolBacktest, trades: List[Trade]) -> None:
    stat.samples = len(trades)
    if not trades:
        return
    returns = [t.return_pct for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    stat.wins = len(wins)
    stat.win_rate = len(wins) / len(trades)
    stat.avg_win = sum(wins) / len(wins) if wins else 0.0
    stat.avg_loss = sum(losses) / len(losses) if losses else 0.0
    stat.avg_return = sum(returns) / len(returns)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    if gross_loss > 0:
        stat.profit_factor = gross_win / gross_loss
    else:
        stat.profit_factor = float("inf") if gross_win > 0 else 0.0

    equity = 1.0
    peak = 1.0
    mdd = 0.0
    for trade in sorted(trades, key=lambda t: (t.entry_index, t.symbol)):
        equity *= 1.0 + trade.return_pct / 100.0
        peak = max(peak, equity)
        mdd = max(mdd, (peak - equity) / peak if peak > 0 else 0.0)
    stat.max_drawdown = mdd


def _benchmark_return(df: Any, warmup: int) -> float:
    try:
        start = _finite(df["close"].iloc[min(warmup, len(df) - 1)])
        end = _finite(df["close"].iloc[-1])
        if start and end:
            return (end - start) / start * 100.0
    except Exception:
        pass
    return 0.0


def _average(values: Sequence[float]) -> float:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else 0.0


def _apply_gate(report: BacktestReport) -> None:
    stat = report.overall
    gate = report.gate
    if stat is None:
        report.passed = False
        report.violated.append("无样本")
        return
    violated: List[str] = []
    if stat.samples < gate["min_samples"]:
        violated.append(f"样本数 {stat.samples} < {gate['min_samples']:.0f}")
    # 期望收益（单笔平均收益）> 门槛：止损型趋势策略的核心指标
    min_expectation = float(gate.get("min_expectation", 0.0) or 0.0)
    if stat.avg_return <= min_expectation:
        violated.append(f"期望收益 {stat.avg_return:.2f}% ≤ {min_expectation:.2f}%")
    if stat.profit_factor < gate["min_profit_factor"]:
        pf = "∞" if math.isinf(stat.profit_factor) else f"{stat.profit_factor:.2f}"
        violated.append(f"盈亏比 {pf} < {gate['min_profit_factor']}")
    if stat.max_drawdown > gate["max_drawdown"]:
        violated.append(f"最大回撤 {stat.max_drawdown:.1%} > {gate['max_drawdown']:.0%}")
    # 胜率：默认仅作参考（min_win_rate <= 0 时不拦截）
    min_win_rate = float(gate.get("min_win_rate", 0.0) or 0.0)
    if min_win_rate > 0 and stat.win_rate < min_win_rate:
        violated.append(f"胜率 {stat.win_rate:.1%} < {min_win_rate:.0%}")
    report.violated = violated
    report.passed = not violated


def _main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover
    import argparse
    import json

    parser = argparse.ArgumentParser(description="P1 上线前 · 规则体检回测（walk-forward）")
    parser.add_argument("--symbols", nargs="*", default=["601919", "601288"])
    parser.add_argument("--days", type=int, default=DEFAULT_FETCH_DAYS, help="取数交易日跨度（默认 600 ≈ 近 3 年）")
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON_DAYS, help="最长持有交易日")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    backtester = RuleBacktester(horizon_days=args.horizon)
    report = backtester.run(args.symbols, fetch_days=args.days)

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.to_markdown())
    return 0 if report.passed else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
