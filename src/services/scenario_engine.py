# -*- coding: utf-8 -*-
"""高抛低吸助手 · 场景引擎（P0）

职责：把"指标快照"翻译成"场景 + 方向"，为后续风控与信号组装提供输入。

覆盖需求：
- FR-3 趋势研判（上涨 / 下跌 / 震荡 + 变盘 / 异动 / 背离；日线 + 周线）
- FR-17.1 建仓口径（周线趋势向上 + 日线买点，**明确不用单一金叉**）
- FR-17.4 抄底口径（止跌确认后才分批加）
- FR-18 提醒—技术支撑映射（输出 ``evidence`` 便于回溯）

本模块**纯函数、无 IO**，指标由 ``src.core.indicators`` 提供。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.core.indicators import IndicatorSnapshot
from src.core.rule_config import ScenarioRuleSet

__all__ = [
    "TrendResult",
    "ScenarioDecision",
    "ScenarioEngine",
]

TREND_UP = "up"
TREND_DOWN = "down"
TREND_SIDEWAYS = "sideways"

_SCENARIO_BREAKOUT = "breakout"
_SCENARIO_VOLATILE = "volatile"

# 趋势强度排序（建仓过滤门槛用）
_STRENGTH_RANK = {"weak": 0, "medium": 1, "strong": 2}


def _strength_rank(value: str) -> int:
    return _STRENGTH_RANK.get(str(value or "").strip().lower(), 0)


def _finite(value: Optional[float]) -> bool:
    return value is not None and math.isfinite(float(value))


@dataclass(frozen=True)
class TrendResult:
    trend: str
    strength: str          # strong | medium | weak
    reasons: List[str] = field(default_factory=list)


@dataclass
class ScenarioDecision:
    scenario: str                  # up | down | sideways | breakout | volatile
    trend: str
    weekly_trend: str
    strength: str
    action: str                    # buy | add | hold | reduce | sell | watch | avoid
    sub_action: Optional[str] = None   # build | dip_buy | take_profit | None
    multi_period_conflict: bool = False
    reasons: List[str] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)


class ScenarioEngine:
    """趋势与场景判定。"""

    def __init__(self, rules: Optional[ScenarioRuleSet] = None):
        self.rules = rules or ScenarioRuleSet()

    # ---------------- 趋势 ---------------- #
    def classify_trend(self, snapshot: IndicatorSnapshot) -> TrendResult:
        t = self.rules.trend
        periods = t.ma_periods
        fast = self._ma(snapshot, periods[0] if len(periods) > 0 else 5)
        mid = self._ma(snapshot, periods[1] if len(periods) > 1 else 10)
        slow = self._ma(snapshot, periods[2] if len(periods) > 2 else 20)

        if not all(_finite(v) for v in (fast, mid, slow)):
            return TrendResult(TREND_SIDEWAYS, "weak", ["均线数据不足，按震荡保守处理"])

        close = snapshot.close
        spread = (max(fast, mid, slow) - min(fast, mid, slow)) / max(abs(slow), 1e-9)
        slope_up = snapshot.ma_slope_up(periods[2] if len(periods) > 2 else 20)

        if fast >= mid >= slow and slope_up and close >= slow:
            return TrendResult(TREND_UP, "strong", ["MA5≥MA10≥MA20 且 MA20 向上"])
        if fast <= mid <= slow and not slope_up and close <= slow:
            return TrendResult(TREND_DOWN, "strong", ["MA5≤MA10≤MA20 且 MA20 向下"])
        if spread <= t.sideways_ma_spread_pct:
            return TrendResult(TREND_SIDEWAYS, "medium", [f"均线纠缠（间距 {spread * 100:.1f}%）"])
        if fast >= mid and close >= mid:
            return TrendResult(TREND_UP, "weak", ["短期均线偏多但未形成完全多头排列"])
        if fast <= mid and close <= mid:
            return TrendResult(TREND_DOWN, "weak", ["短期均线偏空但未形成完全空头排列"])
        return TrendResult(TREND_SIDEWAYS, "weak", ["多空交织，按震荡处理"])

    @staticmethod
    def _ma(snapshot: IndicatorSnapshot, period: int) -> Optional[float]:
        value = snapshot.ma.get(period)
        return float(value) if value is not None and math.isfinite(float(value)) else None

    # ---------------- 买点（FR-17.1） ---------------- #
    def detect_daily_buy_point(
        self,
        snapshot: IndicatorSnapshot,
        *,
        box_high: Optional[float] = None,
    ) -> Dict[str, Any]:
        build = self.rules.build
        thresholds = self.rules.thresholds
        ratio = snapshot.volume_ratio()
        surge = _finite(ratio) and ratio >= thresholds.volume_surge_ratio
        hits: List[str] = []
        reasons: List[str] = []
        close = snapshot.close

        # ① 放量突破平台后回踩不破
        if "pullback_after_breakout" in build.daily_buy_points:
            for period in build.pullback_ma:
                ma_value = self._ma(snapshot, period)
                if ma_value is None:
                    continue
                near = abs(close - ma_value) / ma_value <= 0.03
                holding = close >= ma_value * (1.0 - build.breakout_buffer_pct)
                broke = box_high is not None and box_high > 0 and close >= box_high
                if near and holding and (broke or surge):
                    hits.append("pullback_after_breakout")
                    reasons.append(f"回踩 MA{period} 不破且守住平台")
                    break

        # ② 底部放量 + MACD 金叉/底背离 + 重新站上 MA20
        if "bottom_volume_reversal" in build.daily_buy_points:
            ma20 = self._ma(snapshot, 20)
            above_ma20 = ma20 is not None and close >= ma20
            if above_ma20 and surge and (snapshot.macd_golden_cross() or snapshot.kdj_golden_cross()):
                hits.append("bottom_volume_reversal")
                reasons.append("站上 MA20 + 放量 + MACD/KDJ 金叉")

        return {
            "hit": bool(hits),
            "hits": hits,
            "reasons": reasons,
            "volume_ratio": ratio,
        }

    # ---------------- 止跌确认（FR-17.4） ---------------- #
    def detect_bottom_reversal(self, snapshot: IndicatorSnapshot) -> Dict[str, Any]:
        """超跌后的"止跌确认"信号（必须出现其一才允许抄底）。"""
        signals: List[str] = []
        if snapshot.macd_hist_shrinking():
            signals.append("macd_hist_shrinking")
        if snapshot.kdj_golden_cross():
            signals.append("kdj_golden_cross")
        if snapshot.volume_ratio() and _finite(snapshot.volume_ratio()) and snapshot.volume_ratio() < 0.8:
            signals.append("volume_shrink")
        return {"confirmed": bool(signals), "signals": signals}

    def is_oversold(self, snapshot: IndicatorSnapshot) -> Dict[str, Any]:
        th = self.rules.thresholds
        hits: List[str] = []
        bias12 = snapshot.bias.get(12)
        if _finite(snapshot.rsi) and snapshot.rsi <= th.rsi_dip_buy:
            hits.append(f"RSI={snapshot.rsi:.1f}≤{th.rsi_dip_buy:g}")
        if _finite(bias12) and bias12 is not None and bias12 <= th.bias_oversold:
            hits.append(f"BIAS12={bias12:.1f}%≤{th.bias_oversold:g}")
        if _finite(snapshot.j) and snapshot.j <= th.kdj_j_oversold:
            hits.append(f"KDJ_J={snapshot.j:.1f}≤{th.kdj_j_oversold:g}")
        if _finite(snapshot.boll_lower) and snapshot.close <= snapshot.boll_lower * (1 + th.boll_dip_tolerance):
            hits.append("触及布林下轨")
        return {"oversold": bool(hits), "hits": hits}

    # ---------------- 建仓过滤门槛（FR-17.1 强化） ---------------- #
    def _build_filter_notes(self, daily_trend: TrendResult, daily: IndicatorSnapshot) -> List[str]:
        """返回"不满足建仓门槛"的原因列表；为空即通过。

        目的：降低弱趋势（多空交织）与动能不足（MACD 柱 ≤ 0）的建仓频次，
        这类信号在实盘中极易被正常回踩震出（P1 体检根因）。
        """
        build = self.rules.build
        notes: List[str] = []

        if _strength_rank(daily_trend.strength) < _strength_rank(getattr(build, "min_trend_strength", "weak")):
            notes.append(
                f"日线趋势强度 {daily_trend.strength} 低于门槛 {build.min_trend_strength}，暂不出手"
            )

        if getattr(build, "require_macd_positive", False):
            hist = daily.macd_hist
            if not math.isfinite(float(hist)) or float(hist) <= 0:
                notes.append("MACD 柱未转正（动能不足），暂不出手")

        return notes

    # ---------------- 综合 ---------------- #
    def evaluate(
        self,
        daily: IndicatorSnapshot,
        *,
        weekly: Optional[IndicatorSnapshot] = None,
        has_position: bool = False,
        box_high: Optional[float] = None,
    ) -> ScenarioDecision:
        daily_trend = self.classify_trend(daily)
        weekly_trend = self.classify_trend(weekly) if weekly is not None else TrendResult(TREND_SIDEWAYS, "weak", [])

        decision = ScenarioDecision(
            scenario=daily_trend.trend,
            trend=daily_trend.trend,
            weekly_trend=weekly_trend.trend,
            strength=daily_trend.strength,
            action="hold" if has_position else "watch",
        )
        decision.reasons.extend(daily_trend.reasons)

        # 多周期冲突 → 降级为谨慎（FR-3.3）
        if weekly_trend.trend != TREND_SIDEWAYS and daily_trend.trend != weekly_trend.trend:
            decision.multi_period_conflict = True
            decision.reasons.append("日线与周线趋势冲突，降级为谨慎")

        # 日内异动（放量急涨急跌）→ 常规策略失效
        ratio = daily.volume_ratio()
        if _finite(ratio) and ratio >= 2.5:
            decision.scenario = _SCENARIO_VOLATILE
            decision.reasons.append(f"放量异动（量比 {ratio:.1f}），谨慎追涨杀跌")

        if has_position:
            decision.action = "hold"
            decision.evidence = self._evidence(daily, weekly, box_high)
            return decision

        # ---- 无持仓：判断是否可建仓 / 抄底 ----
        weekly_ok = (
            weekly_trend.trend == TREND_UP
            if self.rules.build.require_weekly_uptrend
            else True
        )
        buy_point = self.detect_daily_buy_point(daily, box_high=box_high)
        oversold = self.is_oversold(daily)
        reversal = self.detect_bottom_reversal(daily)

        # 建仓过滤门槛（FR-17.1 强化）：趋势强度 + MACD 动能
        build_filter_notes = self._build_filter_notes(daily_trend, daily)
        build_ok = not build_filter_notes

        if weekly_ok and buy_point["hit"] and not decision.multi_period_conflict and build_ok:
            decision.action = "buy"
            decision.sub_action = "build"
            decision.reasons.append("周线向上 + 日线买点共振，提示建仓")
        elif (
            daily_trend.trend == TREND_DOWN
            and oversold["oversold"]
            and reversal["confirmed"]
            and self.rules.down_market.dip_buy_enabled
        ):
            decision.action = "buy"
            decision.sub_action = "dip_buy"
            decision.reasons.append("下跌超跌且已出现止跌确认，可小仓分批抄底")
        else:
            decision.action = "watch"
            if not weekly_ok and buy_point["hit"]:
                decision.reasons.append("日线有买点但周线未确认向上，暂不出手")
            if weekly_ok and buy_point["hit"] and build_filter_notes:
                decision.reasons.extend(build_filter_notes)

        decision.evidence = self._evidence(daily, weekly, box_high)
        return decision

    @staticmethod
    def _evidence(
        daily: IndicatorSnapshot,
        weekly: Optional[IndicatorSnapshot],
        box_high: Optional[float],
    ) -> Dict[str, Any]:
        """可回溯依据（FR-18）。"""
        return {
            "as_of": daily.as_of,
            "close": daily.close,
            "ma": daily.ma,
            "macd_hist": daily.macd_hist,
            "kdj": {"k": daily.k, "d": daily.d, "j": daily.j},
            "rsi": daily.rsi,
            "bias": daily.bias,
            "boll": {"upper": daily.boll_upper, "mid": daily.boll_mid, "lower": daily.boll_lower},
            "volume_ratio": daily.volume_ratio(),
            "box_high": box_high,
            "weekly_ma": weekly.ma if weekly is not None else None,
            "rules_version": 1,
        }
