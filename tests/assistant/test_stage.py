# -*- coding: utf-8 -*-
"""阶段判定与防抖测试（需求 §5.14.2）。"""

from __future__ import annotations

from datetime import date

import pytest

from src.assistant.domain import DataQuality
from src.assistant.engine.stage import StageDebounceState, apply_debounce, detect_stage
from src.assistant.enums import Stage, StageConfidence
from tests.assistant.helpers import boxed, make_view, std_params, trending, wide_oscillation


def _quality(n: int, sufficient: bool = True, gap: int = 0) -> DataQuality:
    return DataQuality(sufficient=sufficient, bars_used=n, gap_days=gap)


def test_uptrend_is_s1() -> None:
    view = make_view(trending(n=150, drift=0.005))
    result = detect_stage(view, std_params(), _quality(150))
    assert result.stage is Stage.S1
    assert result.confidence is StageConfidence.HIGH
    assert "斜率" in result.rationale()


def test_downtrend_is_s3() -> None:
    view = make_view(trending(n=150, drift=-0.005))
    result = detect_stage(view, std_params(), _quality(150))
    assert result.stage is Stage.S3
    assert any("低于" in r for r in result.reasons)


def test_box_at_high_is_s2() -> None:
    view = make_view(boxed(n=150, last_at="high"))
    result = detect_stage(view, std_params(), _quality(150))
    assert result.stage is Stage.S2
    assert result.metrics["pos20"] >= 0.5
    assert result.metrics["box"]["valid"] is True


def test_box_at_low_is_s4() -> None:
    view = make_view(boxed(n=150, last_at="low"))
    result = detect_stage(view, std_params(), _quality(150))
    assert result.stage is Stage.S4
    assert result.metrics["pos20"] < 0.5


def test_wide_range_is_s5_and_confidence_capped() -> None:
    """箱体不成立且有足够数据 → S5（不是假震荡），置信度最高只能「中」。"""
    view = make_view(wide_oscillation())
    result = detect_stage(view, std_params(), _quality(len(view.bars)))
    assert result.stage is Stage.S5
    assert result.confidence in (StageConfidence.MEDIUM, StageConfidence.LOW)


def test_insufficient_bars_is_s0() -> None:
    view = make_view(trending(n=150))
    result = detect_stage(view, std_params(), _quality(150, sufficient=False, gap=5))
    assert result.stage is Stage.S0
    assert result.blocked is True


def test_s0_reason_not_silent() -> None:
    """S0 必须显式说明原因，禁止默认成 S2/S5（需求 §5.14.2）。"""
    view = make_view(trending(n=30))
    result = detect_stage(view, std_params(), DataQuality(sufficient=False, bars_used=30, gap_days=6, reason="数据缺口 6 日 > 3 日"))
    assert result.stage is Stage.S0
    assert "缺口" in result.reasons[0]


def test_debounce_requires_consecutive_days() -> None:
    """连续 2 日满足新阶段条件才切换（标准档）。"""
    raw_s3 = detect_stage(make_view(trending(n=150, drift=-0.005)), std_params(), _quality(150))
    state = StageDebounceState(stage=Stage.S1)

    stage1, state1 = apply_debounce(raw_s3, state, days=2, trade_date=date(2026, 11, 20))
    assert stage1.stage is Stage.S1, "第 1 日不得切换"
    assert state1.pending_stage is Stage.S3
    assert state1.pending_days == 1

    stage2, state2 = apply_debounce(raw_s3, state1, days=2, trade_date=date(2026, 11, 21))
    assert stage2.stage is Stage.S3, "第 2 日才切换"
    assert stage2.switched is True
    assert "阶段切换 S1→S3" in stage2.reasons[0]


def test_debounce_resets_when_condition_breaks() -> None:
    raw_s3 = detect_stage(make_view(trending(n=150, drift=-0.005)), std_params(), _quality(150))
    raw_s1 = detect_stage(make_view(trending(n=150, drift=0.005)), std_params(), _quality(150))
    state = StageDebounceState(stage=Stage.S1)
    _, s1 = apply_debounce(raw_s3, state, days=2, trade_date=date(2026, 11, 20))
    stage2, s2 = apply_debounce(raw_s1, s1, days=2, trade_date=date(2026, 11, 21))
    assert stage2.stage is Stage.S1
    assert s2.pending_days == 0


def test_s0_takes_effect_immediately() -> None:
    """数据不足立即降 S0（fail-safe），不能拿旧阶段顶替。"""
    raw_s0 = detect_stage(make_view(trending(n=30)), std_params(), _quality(30, sufficient=False, gap=8))
    stage, state = apply_debounce(raw_s0, StageDebounceState(stage=Stage.S1), days=2, trade_date=date(2026, 11, 20))
    assert stage.stage is Stage.S0
    assert state.stage is Stage.S0


def test_recovery_from_s0_immediate() -> None:
    raw_s1 = detect_stage(make_view(trending(n=150, drift=0.005)), std_params(), _quality(150))
    stage, _ = apply_debounce(raw_s1, StageDebounceState(stage=Stage.S0), days=3, trade_date=date(2026, 11, 20))
    assert stage.stage is Stage.S1


def test_confidence_downgraded_while_pending() -> None:
    """未确认的新阶段沿用旧阶段时，置信度不能保持上一档的高置信。"""
    raw_s3 = detect_stage(make_view(trending(n=150, drift=-0.005)), std_params(), _quality(150))
    stage, _ = apply_debounce(raw_s3, StageDebounceState(stage=Stage.S1), days=3, trade_date=date(2026, 11, 20))
    assert stage.stage is Stage.S1
    assert stage.confidence is not StageConfidence.HIGH


def test_invalid_debounce_days_rejected() -> None:
    raw = detect_stage(make_view(trending(n=150)), std_params(), _quality(150))
    with pytest.raises(ValueError):
        apply_debounce(raw, StageDebounceState(), days=0, trade_date=date(2026, 11, 20))
