# -*- coding: utf-8 -*-
"""高抛低吸助手 · 规则配置加载器（P0）

把 ``config/rules/*.yaml`` 读成**强类型 dataclass**，供场景引擎与风控引擎消费。
设计要点：
1. 所有阈值**来自配置**，代码不写死（NFR-6）；
2. 文件缺失或字段缺失时**回退到内置默认值**，保证系统始终可运行；
3. 只读、无副作用，便于单测。

需求依据：FR-9 风控规则、FR-18 提醒—技术支撑映射、需求附录 · 默认参数表。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import yaml

__all__ = [
    "RULES_DIR",
    "StopLossRules",
    "TrailRules",
    "TakeProfitRules",
    "PositionRules",
    "UrgencyRules",
    "RiskRuleSet",
    "TrendRules",
    "ThresholdRules",
    "TradeCostRules",
    "BuildRules",
    "UpMarketRules",
    "DownMarketRules",
    "SidewaysMarketRules",
    "ScenarioRuleSet",
    "load_risk_rules",
    "load_scenario_rules",
]

REPO_ROOT = Path(__file__).resolve().parents[2]


def _rules_dir() -> Path:
    override = os.getenv("HSLB_RULES_DIR")
    if override:
        return Path(override).expanduser()
    return REPO_ROOT / "config" / "rules"


RULES_DIR = _rules_dir()


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _section(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    return value if isinstance(value, Mapping) else {}


def _tuple_int(value: Any, default: Tuple[int, ...]) -> Tuple[int, ...]:
    if isinstance(value, (list, tuple)):
        try:
            return tuple(int(item) for item in value)
        except (TypeError, ValueError):
            return default
    return default


def _tuple_float(value: Any, default: Tuple[float, ...]) -> Tuple[float, ...]:
    if isinstance(value, (list, tuple)):
        try:
            return tuple(float(item) for item in value)
        except (TypeError, ValueError):
            return default
    return default


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if value is None:
        return default
    return bool(value)


def _str_tuple(value: Any, default: Tuple[str, ...]) -> Tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return default


# --------------------------------------------------------------------------- #
# 风控规则
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StopLossRules:
    mode: str = "technical"
    ma: Tuple[int, ...] = (5, 10, 20)
    prev_low_lookback: int = 20
    percent: float = 0.08
    # 止损放宽（应对高波动标的被"下影线扫损"）：
    buffer_pct: float = 0.0        # 在选定技术位下方再留的缓冲比例（0.02 = 2%）
    atr_period: int = 14           # ATR 周期
    atr_mult: float = 0.0          # > 0 时启用 ATR 自适应：止损放宽到 min(技术位, close − atr_mult×ATR)


@dataclass(frozen=True)
class TrailRules:
    activate: float = 0.10
    drawdown: float = 0.08


@dataclass(frozen=True)
class TakeProfitRules:
    targets: Tuple[float, ...] = (0.10, 0.15, 0.20, 0.25)
    target_ratios: Tuple[float, ...] = (0.1667, 0.1667, 0.1667, 0.1666)
    ma_break: Tuple[int, ...] = (5, 10, 20)
    ma_break_ratios: Tuple[float, ...] = (0.333, 0.333, 0.334)
    trail: TrailRules = field(default_factory=TrailRules)
    move_stop_to_cost_at: float = 0.10


@dataclass(frozen=True)
class PositionRules:
    build_max: float = 0.30
    build_batches: int = 2
    add_ladder: Tuple[float, ...] = (0.30, 0.20, 0.10)
    cap: float = 0.65
    dip_buy_max: float = 0.25
    dip_buy_batches: int = 2


@dataclass(frozen=True)
class UrgencyRules:
    stop_loss_breach: bool = True
    ma20_breach: bool = True


@dataclass(frozen=True)
class RiskRuleSet:
    stop_loss: StopLossRules = field(default_factory=StopLossRules)
    take_profit: TakeProfitRules = field(default_factory=TakeProfitRules)
    position: PositionRules = field(default_factory=PositionRules)
    urgency: UrgencyRules = field(default_factory=UrgencyRules)
    version: int = 1


# --------------------------------------------------------------------------- #
# 场景规则
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TrendRules:
    ma_periods: Tuple[int, ...] = (5, 10, 20, 60)
    sideways_ma_spread_pct: float = 0.02
    slope_lookback: int = 1


@dataclass(frozen=True)
class ThresholdRules:
    bias_overheat: float = 10.0
    bias_oversold: float = -10.0
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    rsi_dip_buy: float = 20.0
    kdj_j_overbought: float = 100.0
    kdj_j_oversold: float = 0.0
    volume_surge_ratio: float = 1.5
    boll_dip_tolerance: float = 0.01


@dataclass(frozen=True)
class TradeCostRules:
    round_trip_pct: float = 0.001
    min_t_range_pct: float = 0.015


@dataclass(frozen=True)
class BuildRules:
    require_weekly_uptrend: bool = True
    weekly_ma_fast: int = 5
    weekly_ma_slow: int = 10
    daily_buy_points: Tuple[str, ...] = ("pullback_after_breakout", "bottom_volume_reversal")
    pullback_ma: Tuple[int, ...] = (20, 60)
    breakout_lookback: int = 20
    breakout_buffer_pct: float = 0.005
    exclude_events: Tuple[str, ...] = ("limit_up", "limit_down", "gap")
    # 建仓过滤门槛（用于滤除弱趋势 / 动能不足的低质量买点，降低被震出概率）
    min_trend_strength: str = "weak"      # weak | medium | strong：日线趋势强度下限
    require_macd_positive: bool = False   # True 时要求 MACD 柱 > 0（动能向上）


@dataclass(frozen=True)
class UpMarketRules:
    reduce_on_ma5: float = 0.20
    reduce_on_ma10: float = 0.30
    reduce_on_ma20: float = 0.50


@dataclass(frozen=True)
class DownMarketRules:
    stop_loss_lookback_low: int = 20
    dip_buy_enabled: bool = True


@dataclass(frozen=True)
class SidewaysMarketRules:
    box_lookback: int = 20


@dataclass(frozen=True)
class ScenarioRuleSet:
    trend: TrendRules = field(default_factory=TrendRules)
    thresholds: ThresholdRules = field(default_factory=ThresholdRules)
    trade_cost: TradeCostRules = field(default_factory=TradeCostRules)
    build: BuildRules = field(default_factory=BuildRules)
    up_market: UpMarketRules = field(default_factory=UpMarketRules)
    down_market: DownMarketRules = field(default_factory=DownMarketRules)
    sideways_market: SidewaysMarketRules = field(default_factory=SidewaysMarketRules)
    version: int = 1


# --------------------------------------------------------------------------- #
# 加载入口
# --------------------------------------------------------------------------- #
def load_risk_rules(path: Optional[Path] = None) -> RiskRuleSet:
    """读取风控规则；文件缺失或字段缺失时回退默认值。"""
    data = _load_yaml(path or (RULES_DIR / "risk_rules.yaml"))

    stop_loss = _section(data, "stop_loss")
    take_profit = _section(data, "take_profit")
    trail = _section(take_profit, "trail")
    position = _section(data, "position")
    urgency = _section(data, "urgency")

    defaults = RiskRuleSet()
    return RiskRuleSet(
        stop_loss=StopLossRules(
            mode=str(stop_loss.get("mode", defaults.stop_loss.mode)).strip() or "technical",
            ma=_tuple_int(stop_loss.get("ma"), defaults.stop_loss.ma),
            prev_low_lookback=_int(stop_loss.get("prev_low_lookback"), defaults.stop_loss.prev_low_lookback),
            percent=_float(stop_loss.get("percent"), defaults.stop_loss.percent),
            buffer_pct=_float(stop_loss.get("buffer_pct"), defaults.stop_loss.buffer_pct),
            atr_period=_int(stop_loss.get("atr_period"), defaults.stop_loss.atr_period),
            atr_mult=_float(stop_loss.get("atr_mult"), defaults.stop_loss.atr_mult),
        ),
        take_profit=TakeProfitRules(
            targets=_tuple_float(take_profit.get("targets"), defaults.take_profit.targets),
            target_ratios=_tuple_float(take_profit.get("target_ratios"), defaults.take_profit.target_ratios),
            ma_break=_tuple_int(take_profit.get("ma_break"), defaults.take_profit.ma_break),
            ma_break_ratios=_tuple_float(take_profit.get("ma_break_ratios"), defaults.take_profit.ma_break_ratios),
            trail=TrailRules(
                activate=_float(trail.get("activate"), defaults.take_profit.trail.activate),
                drawdown=_float(trail.get("drawdown"), defaults.take_profit.trail.drawdown),
            ),
            move_stop_to_cost_at=_float(
                take_profit.get("move_stop_to_cost_at"), defaults.take_profit.move_stop_to_cost_at
            ),
        ),
        position=PositionRules(
            build_max=_float(position.get("build_max"), defaults.position.build_max),
            build_batches=_int(position.get("build_batches"), defaults.position.build_batches),
            add_ladder=_tuple_float(position.get("add_ladder"), defaults.position.add_ladder),
            cap=_float(position.get("cap"), defaults.position.cap),
            dip_buy_max=_float(position.get("dip_buy_max"), defaults.position.dip_buy_max),
            dip_buy_batches=_int(position.get("dip_buy_batches"), defaults.position.dip_buy_batches),
        ),
        urgency=UrgencyRules(
            stop_loss_breach=_bool(urgency.get("stop_loss_breach"), defaults.urgency.stop_loss_breach),
            ma20_breach=_bool(urgency.get("ma20_breach"), defaults.urgency.ma20_breach),
        ),
        version=_int(data.get("version"), defaults.version),
    )


def load_scenario_rules(path: Optional[Path] = None) -> ScenarioRuleSet:
    """读取场景规则；文件缺失或字段缺失时回退默认值。"""
    data = _load_yaml(path or (RULES_DIR / "scenario_rules.yaml"))

    trend = _section(data, "trend")
    thresholds = _section(data, "thresholds")
    trade_cost = _section(data, "trade_cost")
    build = _section(data, "build")
    up_market = _section(data, "up_market")
    down_market = _section(data, "down_market")
    sideways = _section(data, "sideways_market")

    defaults = ScenarioRuleSet()
    return ScenarioRuleSet(
        trend=TrendRules(
            ma_periods=_tuple_int(trend.get("ma_periods"), defaults.trend.ma_periods),
            sideways_ma_spread_pct=_float(trend.get("sideways_ma_spread_pct"), defaults.trend.sideways_ma_spread_pct),
            slope_lookback=_int(trend.get("slope_lookback"), defaults.trend.slope_lookback),
        ),
        thresholds=ThresholdRules(
            bias_overheat=_float(thresholds.get("bias_overheat"), defaults.thresholds.bias_overheat),
            bias_oversold=_float(thresholds.get("bias_oversold"), defaults.thresholds.bias_oversold),
            rsi_overbought=_float(thresholds.get("rsi_overbought"), defaults.thresholds.rsi_overbought),
            rsi_oversold=_float(thresholds.get("rsi_oversold"), defaults.thresholds.rsi_oversold),
            rsi_dip_buy=_float(thresholds.get("rsi_dip_buy"), defaults.thresholds.rsi_dip_buy),
            kdj_j_overbought=_float(thresholds.get("kdj_j_overbought"), defaults.thresholds.kdj_j_overbought),
            kdj_j_oversold=_float(thresholds.get("kdj_j_oversold"), defaults.thresholds.kdj_j_oversold),
            volume_surge_ratio=_float(thresholds.get("volume_surge_ratio"), defaults.thresholds.volume_surge_ratio),
            boll_dip_tolerance=_float(thresholds.get("boll_dip_tolerance"), defaults.thresholds.boll_dip_tolerance),
        ),
        trade_cost=TradeCostRules(
            round_trip_pct=_float(trade_cost.get("round_trip_pct"), defaults.trade_cost.round_trip_pct),
            min_t_range_pct=_float(trade_cost.get("min_t_range_pct"), defaults.trade_cost.min_t_range_pct),
        ),
        build=BuildRules(
            require_weekly_uptrend=_bool(build.get("require_weekly_uptrend"), defaults.build.require_weekly_uptrend),
            weekly_ma_fast=_int(build.get("weekly_ma_fast"), defaults.build.weekly_ma_fast),
            weekly_ma_slow=_int(build.get("weekly_ma_slow"), defaults.build.weekly_ma_slow),
            daily_buy_points=_str_tuple(build.get("daily_buy_points"), defaults.build.daily_buy_points),
            pullback_ma=_tuple_int(build.get("pullback_ma"), defaults.build.pullback_ma),
            breakout_lookback=_int(build.get("breakout_lookback"), defaults.build.breakout_lookback),
            breakout_buffer_pct=_float(build.get("breakout_buffer_pct"), defaults.build.breakout_buffer_pct),
            exclude_events=_str_tuple(build.get("exclude_events"), defaults.build.exclude_events),
            min_trend_strength=str(build.get("min_trend_strength", defaults.build.min_trend_strength)).strip().lower()
            or defaults.build.min_trend_strength,
            require_macd_positive=_bool(build.get("require_macd_positive"), defaults.build.require_macd_positive),
        ),
        up_market=UpMarketRules(
            reduce_on_ma5=_float(up_market.get("reduce_on_ma5"), defaults.up_market.reduce_on_ma5),
            reduce_on_ma10=_float(up_market.get("reduce_on_ma10"), defaults.up_market.reduce_on_ma10),
            reduce_on_ma20=_float(up_market.get("reduce_on_ma20"), defaults.up_market.reduce_on_ma20),
        ),
        down_market=DownMarketRules(
            stop_loss_lookback_low=_int(down_market.get("stop_loss_lookback_low"), defaults.down_market.stop_loss_lookback_low),
            dip_buy_enabled=_bool(down_market.get("dip_buy_enabled"), defaults.down_market.dip_buy_enabled),
        ),
        sideways_market=SidewaysMarketRules(
            box_lookback=_int(sideways.get("box_lookback"), defaults.sideways_market.box_lookback),
        ),
        version=_int(data.get("version"), defaults.version),
    )
