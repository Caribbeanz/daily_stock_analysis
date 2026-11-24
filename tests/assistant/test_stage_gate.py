# -*- coding: utf-8 -*-
"""阶段门 S5.5 测试（需求 §5.14.5 八条改写规则 + 只收紧不放宽）。"""

from __future__ import annotations

import pytest

from src.assistant.domain import CandidateAdvice
from src.assistant.engine.indicators import Box
from src.assistant.engine.stage_gate import GateContext, apply_stage_gate, to_storage_advice
from src.assistant.enums import Action, CandidateReason, RiskLevel, RuleId, Stage, StageConfidence


def _box(valid: bool = True) -> Box:
    return Box(high=110.0, low=100.0, width=0.10, up_touches=3, down_touches=3, lookback=60,
               width_limit=0.12, valid=valid)


def ctx(stage: Stage, action: Action, **kwargs) -> GateContext:
    reason = kwargs.pop("reason", CandidateReason.SCORE_TABLE)
    return GateContext(
        stage=stage,
        confidence=kwargs.pop("confidence", StageConfidence.HIGH),
        candidate=CandidateAdvice(action=action, score=70.0, reason=reason),
        is_holding=kwargs.pop("is_holding", False),
        box=kwargs.pop("box", _box()),
        close=kwargs.pop("close", 100.0),
        **kwargs,
    )


# ---- 规则 ① S3 禁止买入类 --------------------------------------------------
def test_s3_buy_rewritten_to_wait_buy() -> None:
    res = apply_stage_gate(ctx(Stage.S3, Action.ADD))
    assert res.action is Action.WAIT_BUY
    assert res.hit is not None and res.hit.rule_id is RuleId.S3_NO_BUY
    assert res.hit.candidate is Action.ADD


def test_s3_buy_with_warning_and_holding_becomes_trim() -> None:
    res = apply_stage_gate(ctx(Stage.S3, Action.ADD, is_holding=True, risk_level=RiskLevel.WARNING))
    assert res.action is Action.TRIM
    assert res.hit.rule_id is RuleId.S3_NO_BUY


def test_s3_sell_allowed_untouched() -> None:
    res = apply_stage_gate(ctx(Stage.S3, Action.TRIM, is_holding=True))
    assert res.action is Action.TRIM
    assert res.hit is None


# ---- 规则 ② / ⑤ S4 试探仓与右侧确认 --------------------------------------
def test_s4_buy_without_probe_trigger_wait_for_confirm() -> None:
    res = apply_stage_gate(ctx(Stage.S4, Action.BUY))
    assert res.action is Action.WAIT_BUY
    assert res.hit.rule_id is RuleId.S4_WAIT_CONFIRM


def test_s4_buy_with_probe_trigger_becomes_probe() -> None:
    res = apply_stage_gate(ctx(Stage.S4, Action.BUY, probe_enabled=True, probe_trigger_satisfied=True))
    assert res.action is Action.PROBE
    assert res.hit.rule_id is RuleId.S4_PROBE


def test_s4_probe_disabled_falls_back_to_wait() -> None:
    """严格档关闭试探仓后，即使触发满足也只能等右侧确认。"""
    res = apply_stage_gate(ctx(Stage.S4, Action.BUY, probe_enabled=False, probe_trigger_satisfied=True))
    assert res.action is Action.WAIT_BUY


def test_s4_confirmed_add_rewritten_to_buy() -> None:
    """右侧确认后允许建仓，但不得写「加仓/摊薄」，统一为确认后建仓。"""
    res = apply_stage_gate(ctx(Stage.S4, Action.ADD, right_side_confirmed=True))
    assert res.action is Action.BUY
    assert res.hit.rule_id is RuleId.S4_NO_SCALE_IN


def test_s4_confirmed_buy_untouched() -> None:
    res = apply_stage_gate(ctx(Stage.S4, Action.BUY, right_side_confirmed=True))
    assert res.action is Action.BUY
    assert res.hit is None


# ---- 规则 ③ S2 不追突破 ---------------------------------------------------
def test_s2_breakout_add_rewritten() -> None:
    res = apply_stage_gate(ctx(Stage.S2, Action.ADD, reason=CandidateReason.BREAKOUT_ADD))
    assert res.action is Action.WAIT_BUY
    assert res.hit.rule_id is RuleId.S2_NO_BREAKOUT


def test_s2_bottom_buy_allowed() -> None:
    res = apply_stage_gate(ctx(Stage.S2, Action.ADD, reason=CandidateReason.BOX_BOTTOM_BUY, close=100.2))
    assert res.action is Action.ADD
    assert res.hit is None


# ---- 规则 ④ S5 无结构 ------------------------------------------------------
@pytest.mark.parametrize("action", [Action.BUY, Action.ADD, Action.PROBE, Action.WAIT_BUY])
def test_s5_buy_like_neutralized(action: Action) -> None:
    res = apply_stage_gate(ctx(Stage.S5, action))
    assert res.action is Action.WATCH
    assert res.hit is not None


def test_s5_holding_buy_becomes_hold() -> None:
    res = apply_stage_gate(ctx(Stage.S5, Action.ADD, is_holding=True))
    assert res.action is Action.HOLD


def test_s5_box_top_trim_rewritten_but_risk_trim_passes() -> None:
    box_trim = apply_stage_gate(ctx(Stage.S5, Action.TRIM, reason=CandidateReason.BOX_TOP_TRIM, is_holding=True))
    risk_trim = apply_stage_gate(ctx(Stage.S5, Action.TRIM, reason=CandidateReason.RISK_DRIVEN, is_holding=True))
    assert box_trim.action is Action.HOLD
    assert risk_trim.action is Action.TRIM, "风险驱动的减仓不受 S5 限制（安全优先）"


# ---- 规则 ⑥ 非 S4 出现试探建仓 --------------------------------------------
@pytest.mark.parametrize("stage", [Stage.S1, Stage.S2, Stage.S3, Stage.S5])
def test_probe_outside_s4_removed(stage: Stage) -> None:
    res = apply_stage_gate(ctx(stage, Action.PROBE))
    assert res.action is not Action.PROBE
    assert res.hit.rule_id is RuleId.PROBE_NOT_ALLOWED


# ---- 规则 ⑧ S1 禁止箱体上沿逆势高抛 ---------------------------------------
def test_s1_box_top_trim_rewritten() -> None:
    res = apply_stage_gate(ctx(Stage.S1, Action.TRIM, reason=CandidateReason.BOX_TOP_TRIM, is_holding=True))
    assert res.action is Action.HOLD
    assert res.hit.rule_id is RuleId.S1_NO_COUNTERTREND_TRIM


def test_s1_trailing_take_profit_trim_allowed() -> None:
    """S1 允许「移动止盈后的减仓」，只禁「箱体上沿逆势高抛」。"""
    res = apply_stage_gate(ctx(Stage.S1, Action.TRIM, reason=CandidateReason.SCORE_TABLE, is_holding=True))
    assert res.action is Action.TRIM
    assert res.hit is None


# ---- 通用契约 --------------------------------------------------------------
def test_no_relaxation_invariant() -> None:
    """任何改写都不得把买入类改得更激进（只收紧不放宽）。"""
    cases = [
        (Stage.S3, Action.WAIT_BUY),
        (Stage.S5, Action.WAIT_BUY),
        (Stage.S4, Action.BUY),
        (Stage.S2, Action.ADD),
    ]
    order = {Action.WATCH: 0, Action.WAIT_BUY: 1, Action.PROBE: 2, Action.ADD: 3, Action.BUY: 3}
    for stage, action in cases:
        res = apply_stage_gate(ctx(stage, action))
        if res.hit is None:
            continue
        candidate, final = res.hit.candidate, res.action
        if candidate in order and final in order:
            assert order[final] <= order[candidate], f"{stage} {candidate}→{final} 属放宽"


def test_s0_never_produces_strategy() -> None:
    res = apply_stage_gate(ctx(Stage.S0, Action.ADD))
    assert res.action is Action.UNAVAILABLE
    assert res.hit is None


def test_hit_serialization_fields() -> None:
    res = apply_stage_gate(ctx(Stage.S3, Action.ADD))
    payload = res.hit.as_dict()
    assert payload == {"stage": "S3", "candidate": "加仓", "final": "等待买点", "rule_id": "stage_s3_no_buy"}


def test_storage_advice_mapping() -> None:
    """执行层动作 → advice 存储层 7 档（未持仓不下「加仓」）。"""
    assert to_storage_advice(Action.ADD, is_holding=True) is Action.ADD
    assert to_storage_advice(Action.ADD, is_holding=False) is Action.WAIT_BUY
    assert to_storage_advice(Action.PROBE, is_holding=False) is Action.WAIT_BUY
    assert to_storage_advice(Action.PROBE, is_holding=True) is Action.ADD
    assert to_storage_advice(Action.UNAVAILABLE, is_holding=True) is None
