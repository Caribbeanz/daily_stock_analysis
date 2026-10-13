# -*- coding: utf-8 -*-
"""P0 场景引擎单元测试（纯函数，无 IO）。"""

from __future__ import annotations

import pytest

from src.core.indicators import IndicatorSnapshot
from src.core.rule_config import BuildRules, DownMarketRules, ScenarioRuleSet
from src.services.scenario_engine import ScenarioEngine


def snap(**kwargs) -> IndicatorSnapshot:
    base = dict(
        close=10.0,
        prev_close=10.0,
        ma={5: 10.0, 10: 10.0, 20: 10.0, 60: 10.0},
        prev_ma={20: 10.0},
        macd_hist=0.05,
        prev_macd_hist=0.03,
        k=60.0,
        d=55.0,
        j=70.0,
        prev_k=58.0,
        prev_d=56.0,
        rsi=60.0,
        prev_rsi=58.0,
        boll_mid=10.0,
        boll_upper=11.0,
        boll_lower=9.0,
        bias={6: 2.0, 12: 3.0, 24: 4.0},
        volume=1000.0,
        avg_volume_5=1000.0,
        high_20=10.5,
        low_20=9.5,
        as_of="2026-10-13",
    )
    base.update(kwargs)
    return IndicatorSnapshot(**base)


def test_classify_trend_up():
    engine = ScenarioEngine()
    s = snap(close=11.0, ma={5: 10.8, 10: 10.5, 20: 10.0, 60: 9.0}, prev_ma={20: 9.8})
    result = engine.classify_trend(s)
    assert result.trend == "up"
    assert result.strength == "strong"


def test_classify_trend_down():
    engine = ScenarioEngine()
    s = snap(close=9.0, ma={5: 9.2, 10: 9.5, 20: 10.0, 60: 10.5}, prev_ma={20: 10.2})
    result = engine.classify_trend(s)
    assert result.trend == "down"


def test_classify_trend_sideways_when_ma_tangled():
    engine = ScenarioEngine()
    s = snap(close=10.0, ma={5: 10.02, 10: 10.0, 20: 9.99, 60: 10.0}, prev_ma={20: 10.0})
    result = engine.classify_trend(s)
    assert result.trend == "sideways"


def test_build_signal_requires_weekly_uptrend():
    engine = ScenarioEngine()
    # 日线：多头排列 + 回踩 MA20 附近不破 + 突破箱体
    daily = snap(
        close=10.2,
        ma={5: 10.3, 10: 10.2, 20: 10.0, 60: 9.5},
        prev_ma={20: 9.9},
        volume=1800.0,
        avg_volume_5=1000.0,
    )
    weekly_up = snap(close=11.0, ma={5: 10.8, 10: 10.5, 20: 10.0, 60: 9.0}, prev_ma={20: 9.8})
    weekly_down = snap(close=9.0, ma={5: 9.2, 10: 9.5, 20: 10.0, 60: 10.5}, prev_ma={20: 10.2})

    ok = engine.evaluate(daily, weekly=weekly_up, has_position=False, box_high=10.1)
    assert ok.action == "buy"
    assert ok.sub_action == "build"

    blocked = engine.evaluate(daily, weekly=weekly_down, has_position=False, box_high=10.1)
    assert blocked.action != "buy"


def test_dip_buy_requires_bottom_reversal_confirmation():
    engine = ScenarioEngine()
    # 超跌（RSI 低）+ 止跌确认（MACD 柱缩短 + KDJ 金叉）
    daily = snap(
        close=8.0,
        ma={5: 8.2, 10: 8.6, 20: 9.0, 60: 9.5},
        prev_ma={20: 9.2},
        rsi=15.0,
        bias={6: -12.0, 12: -14.0, 24: -16.0},
        boll_lower=8.1,
        macd_hist=-0.02,
        prev_macd_hist=-0.05,
        k=30.0,
        d=35.0,
        prev_k=34.0,
        prev_d=36.0,
        volume=700.0,
        avg_volume_5=1000.0,
    )
    decision = engine.evaluate(daily, weekly=None, has_position=False)
    assert decision.trend == "down"
    assert decision.sub_action == "dip_buy"
    assert decision.action == "buy"


def test_hold_position_returns_hold_from_scenario():
    engine = ScenarioEngine()
    s = snap(close=11.0, ma={5: 10.8, 10: 10.5, 20: 10.0, 60: 9.0}, prev_ma={20: 9.8})
    decision = engine.evaluate(s, has_position=True)
    assert decision.action == "hold"


def test_oversold_detection_and_evidence_present():
    engine = ScenarioEngine()
    s = snap(close=8.0, rsi=15.0, bias={6: -12.0, 12: -14.0, 24: -16.0}, boll_lower=8.05)
    oversold = engine.is_oversold(s)
    assert oversold["oversold"] is True
    decision = engine.evaluate(s, has_position=True)
    assert decision.evidence.get("rsi") == pytest.approx(15.0)
    assert decision.evidence.get("rules_version") == 1


# ---------------- 建仓过滤门槛（2026-10-13 调参：滤除低质量买点） ---------------- #
def _buy_ready_daily(**kwargs):
    """日线买点成立（回踩 MA20 不破 + 突破箱体 + 放量）的基线快照。"""
    base = dict(
        close=10.2,
        ma={5: 10.3, 10: 10.2, 20: 10.0, 60: 9.5},
        prev_ma={20: 9.9},
        volume=1800.0,
        avg_volume_5=1000.0,
    )
    base.update(kwargs)
    return snap(**base)


def test_build_filter_blocks_when_macd_not_positive():
    rules = ScenarioRuleSet(build=BuildRules(require_macd_positive=True))
    engine = ScenarioEngine(rules)
    daily = _buy_ready_daily(macd_hist=-0.02, prev_macd_hist=-0.05)
    weekly_up = snap(close=11.0, ma={5: 10.8, 10: 10.5, 20: 10.0, 60: 9.0}, prev_ma={20: 9.8})
    decision = engine.evaluate(daily, weekly=weekly_up, has_position=False, box_high=10.1)
    assert decision.action != "buy"
    assert any("动能" in r for r in decision.reasons)


def test_build_filter_blocks_weak_trend_strength():
    rules = ScenarioRuleSet(build=BuildRules(min_trend_strength="strong"))
    engine = ScenarioEngine(rules)
    # 斜率不向上 → 日线趋势强度仅为 weak，低于 strong 门槛
    daily = _buy_ready_daily(prev_ma={20: 10.0})
    weekly_up = snap(close=11.0, ma={5: 10.8, 10: 10.5, 20: 10.0, 60: 9.0}, prev_ma={20: 9.8})
    decision = engine.evaluate(daily, weekly=weekly_up, has_position=False, box_high=10.1)
    assert decision.action != "buy"
    assert any("趋势强度" in r for r in decision.reasons)


def test_build_filter_passes_when_conditions_met():
    rules = ScenarioRuleSet(build=BuildRules(min_trend_strength="medium", require_macd_positive=True))
    engine = ScenarioEngine(rules)
    daily = _buy_ready_daily(macd_hist=0.05, prev_macd_hist=0.03)
    weekly_up = snap(close=11.0, ma={5: 10.8, 10: 10.5, 20: 10.0, 60: 9.0}, prev_ma={20: 9.8})
    decision = engine.evaluate(daily, weekly=weekly_up, has_position=False, box_high=10.1)
    assert decision.action == "buy"
    assert decision.sub_action == "build"
