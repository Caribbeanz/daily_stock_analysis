# -*- coding: utf-8 -*-
"""P0 风控引擎单元测试（纯函数，无 IO）。"""

from __future__ import annotations

import math

import pytest

from src.core.indicators import IndicatorSnapshot
from src.core.risk_engine import PositionView, RiskEngine
from src.core.rule_config import PositionRules, RiskRuleSet, StopLossRules


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


def pos(**kwargs) -> PositionView:
    base = dict(symbol="601919", avg_cost=10.0, quantity=100.0, peak_price=10.6)
    base.update(kwargs)
    return PositionView(**base)


# --------------------------------------------------------------------------- #
# 止损
# --------------------------------------------------------------------------- #
def test_stop_loss_uses_nearest_ma_below_price():
    engine = RiskEngine()
    s = snap(close=10.5, ma={5: 10.2, 10: 10.0, 20: 9.8, 60: 9.0}, low_20=9.5)
    stop = engine.compute_stop_loss(s)
    assert stop == pytest.approx(10.2)


def test_stop_loss_falls_back_to_structure_low():
    engine = RiskEngine()
    s = snap(close=9.0, ma={5: 9.4, 10: 9.6, 20: 9.8, 60: 10.0}, low_20=8.8)
    stop = engine.compute_stop_loss(s)
    assert stop == pytest.approx(8.8)


def test_stop_loss_percent_mode():
    rules = RiskRuleSet(stop_loss=StopLossRules(mode="percent", percent=0.08))
    engine = RiskEngine(rules)
    s = snap(close=10.5)  # 浮盈 5% < 保本阈值 10%，不触发上移
    stop = engine.compute_stop_loss(s, pos(avg_cost=10.0))
    assert stop == pytest.approx(9.2)


def test_stop_loss_moves_to_cost_when_target_gain_reached():
    engine = RiskEngine()
    s = snap(close=11.0, ma={5: 9.6, 10: 9.4, 20: 9.2, 60: 9.0}, low_20=9.0)
    stop = engine.compute_stop_loss(s, pos(avg_cost=10.0))
    assert stop == pytest.approx(10.0)  # 保本线


# ---------------- 止损放宽（buffer / ATR 自适应，2026-10-13 调参） ---------------- #
def test_stop_loss_buffer_widens_technical_level():
    rules = RiskRuleSet(stop_loss=StopLossRules(mode="technical", ma=(5, 10, 20), buffer_pct=0.02))
    engine = RiskEngine(rules)
    s = snap(close=10.5, ma={5: 10.2, 10: 10.0, 20: 9.8, 60: 9.0}, low_20=9.5)
    stop = engine.compute_stop_loss(s)
    # 技术位 10.2 再向下留 2% 缓冲
    assert stop == pytest.approx(10.2 * 0.98)
    assert stop < 10.2


def test_stop_loss_atr_mult_widens_when_more_volatile():
    rules = RiskRuleSet(stop_loss=StopLossRules(mode="technical", ma=(5, 10, 20), atr_mult=2.0))
    engine = RiskEngine(rules)
    s = snap(close=10.5, ma={5: 10.2, 10: 10.0, 20: 9.8, 60: 9.0}, low_20=9.5, atr=0.5)
    stop = engine.compute_stop_loss(s)
    # atr_stop = 10.5 - 2*0.5 = 9.5 < 技术位 10.2 → 取更宽者
    assert stop == pytest.approx(9.5)


def test_stop_loss_atr_disabled_by_default_keeps_legacy_behaviour():
    engine = RiskEngine()
    s = snap(close=10.5, ma={5: 10.2, 10: 10.0, 20: 9.8, 60: 9.0}, low_20=9.5, atr=0.5)
    assert engine.compute_stop_loss(s) == pytest.approx(10.2)


# --------------------------------------------------------------------------- #
# 分批止盈
# --------------------------------------------------------------------------- #
def test_take_profit_plan_marks_triggered_tier():
    engine = RiskEngine()
    s = snap(close=11.2, ma={5: 10.8, 10: 10.5, 20: 10.2, 60: 10.0}, low_20=10.0)
    plan = engine.compute_take_profit_plan(s, pos(avg_cost=10.0))
    targets = [t for t in plan if t["kind"] == "target"]
    assert targets, "应有目标价止盈档位"
    assert targets[0]["triggered"] is True   # +10% 已到
    assert targets[0]["price"] == pytest.approx(11.0)
    # 目标价四档合计 ≈ 2/3（剩余 1/3 交给破线/移动止盈）
    assert sum(t["ratio"] for t in targets) == pytest.approx(2.0 / 3.0, abs=0.01)


def test_take_profit_plan_empty_without_position():
    engine = RiskEngine()
    s = snap()
    assert engine.compute_take_profit_plan(s, PositionView(symbol="x")) == []


# ---------------- 破线档位措辞：区分盈亏（B2 修复回归） ---------------- #
def test_ma_break_reason_says_take_profit_when_in_profit():
    engine = RiskEngine()
    # 现价 11.2 高于成本 10.0（浮盈），但跌破 MA5(11.5) → 触发破线档且属"止盈"
    s = snap(close=11.2, ma={5: 11.5, 10: 11.8, 20: 12.0, 60: 10.0}, low_20=10.0)
    plan = engine.compute_take_profit_plan(s, pos(avg_cost=10.0))
    ma_break = [t for t in plan if t["kind"] == "ma_break" and t["triggered"]]
    assert ma_break, "应触发破线档位"
    assert "止盈" in ma_break[0]["reason"]
    assert "减仓" not in ma_break[0]["reason"]


def test_ma_break_reason_says_reduce_when_at_loss():
    engine = RiskEngine()
    # 成本 12.0、现价 10.5 → 亏损，破 MA5 应表述为"减仓"而非"止盈"
    s = snap(close=10.5, ma={5: 11.0, 10: 11.2, 20: 11.5, 60: 12.0}, low_20=10.0)
    plan = engine.compute_take_profit_plan(s, pos(avg_cost=12.0, peak_price=12.0))
    ma_break = [t for t in plan if t["kind"] == "ma_break" and t["triggered"]]
    assert ma_break, "应触发破线档位"
    assert "减仓" in ma_break[0]["reason"]
    assert "止盈" not in ma_break[0]["reason"]


# ---------------- 破 MA20 兜底分支（B1 修复回归：此前引用未定义变量 advice） ---------------- #
def test_evaluate_hold_ma20_break_fallback_without_ma20_tier():
    from src.core.rule_config import TakeProfitRules

    # 破线档位仅含 MA5/MA10 → 跌破 MA20 时不会在止盈分支提前返回，落到兜底分支
    rules = RiskRuleSet(
        take_profit=TakeProfitRules(ma_break=(5, 10), ma_break_ratios=(0.5, 0.5)),
    )
    engine = RiskEngine(rules)
    # close=10.0：>ma5(9.8)、>ma10(9.9)，故未触发破线止盈档；<ma20(10.5) 触发兜底
    s = snap(close=10.0, ma={5: 9.8, 10: 9.9, 20: 10.5, 60: 9.0}, low_20=9.5)
    decision = engine.evaluate_hold(s, pos(avg_cost=10.0))
    advice = decision.position_advice
    assert advice["action"] == "reduce"
    assert advice["ratio"] == pytest.approx(0.50)  # 破 MA20 → 减 50%
    assert "MA20" in advice["reason"]


# --------------------------------------------------------------------------- #
# 仓位
# --------------------------------------------------------------------------- #
def test_position_sizing_build():
    engine = RiskEngine()
    advice = engine.position_sizing("build")
    assert advice["action"] == "buy"
    assert advice["ratio"] == pytest.approx(0.30)
    assert advice["batches"] == 2
    assert advice["per_batch"] == pytest.approx(0.15)


def test_position_sizing_respects_cap():
    engine = RiskEngine()
    advice = engine.position_sizing("build", held_ratio=0.60)
    assert advice["ratio"] == pytest.approx(0.05)  # cap 0.65 - 0.60


def test_position_sizing_add_ladder_first_rung():
    engine = RiskEngine()
    assert engine.position_sizing("add", held_ratio=0.0)["ratio"] == pytest.approx(0.30)
    assert engine.position_sizing("add", held_ratio=0.45)["ratio"] == pytest.approx(0.20)


def test_position_sizing_dip_buy():
    engine = RiskEngine()
    advice = engine.position_sizing("dip_buy")
    assert advice["ratio"] == pytest.approx(0.25)
    assert advice["action"] == "buy"


# --------------------------------------------------------------------------- #
# 综合
# --------------------------------------------------------------------------- #
def test_evaluate_hold_triggers_hard_stop_loss():
    engine = RiskEngine()
    s = snap(close=9.0, ma={5: 9.4, 10: 9.6, 20: 9.8, 60: 10.0}, low_20=9.6)
    decision = engine.evaluate_hold(s, pos(avg_cost=10.0))
    assert decision.action() == "sell"
    assert decision.is_urgent is True
    assert "trigger:stop_loss" in decision.reasons


def test_evaluate_hold_reduces_on_take_profit_tier():
    engine = RiskEngine()
    s = snap(close=11.2, ma={5: 10.8, 10: 10.5, 20: 10.2, 60: 10.0}, low_20=10.0)
    decision = engine.evaluate_hold(s, pos(avg_cost=10.0))
    assert decision.action() == "reduce"
    assert decision.action_ratio() == pytest.approx(0.1667, abs=0.01)


def test_evaluate_hold_plain_hold():
    engine = RiskEngine()
    s = snap(
        close=10.4,
        ma={5: 10.2, 10: 10.1, 20: 10.0, 60: 9.8},
        low_20=9.9,
        bias={6: 2.0, 12: 3.0, 24: 4.0},
        macd_hist=0.08,
        prev_macd_hist=0.05,
    )
    decision = engine.evaluate_hold(s, pos(avg_cost=10.0))
    assert decision.action() == "hold"
    assert decision.is_urgent is False
    assert math.isfinite(decision.stop_loss)


def test_evaluate_build_attaches_stop_loss():
    engine = RiskEngine()
    s = snap(close=10.2, ma={5: 9.9, 10: 9.8, 20: 9.6, 60: 9.0}, low_20=9.4)
    decision = engine.evaluate_build(s)
    assert decision.action() == "buy"
    assert decision.action_ratio() <= 0.30 + 1e-9
    assert math.isfinite(decision.stop_loss)
