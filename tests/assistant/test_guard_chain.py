# -*- coding: utf-8 -*-
"""守卫链端到端测试（需求 §5.4 顺序、§5.14.5 阶段门、§5.10 风险守卫）。"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from src.assistant.config import default_param_set
from src.assistant.domain import FundamentalView, MarketView
from src.assistant.engine.guard_chain import GuardChainInput, run_guard_chain
from src.assistant.enums import Action, AdviceState, BUY_SIDE, GuardStep, RiskLevel, STAGE_ALLOWED, Stage
from tests.assistant.helpers import make_portfolio, make_view, now, trending


def _run(view, *, params=None, portfolio=None, **kwargs):
    return run_guard_chain(
        GuardChainInput(
            view=view,
            portfolio=portfolio or make_portfolio(),
            params=params or default_param_set(),
            calc_version="c1-20261124",
            generated_at=now(),
            **kwargs,
        )
    ).result


def test_trace_covers_all_guard_steps() -> None:
    """守卫链顺序 S0→S6 八步全部留痕（可审计）。"""
    result = _run(make_view(trending(150, 0.005), holding=1000))
    steps = [t.step for t in result.trace]
    assert steps == [
        GuardStep.S0,
        GuardStep.S1,
        GuardStep.S2,
        GuardStep.S3,
        GuardStep.S4,
        GuardStep.S5,
        GuardStep.S5_5,
        GuardStep.S6,
    ]


def test_uptrend_holding_gets_valid_advice_and_plans() -> None:
    result = _run(make_view(trending(150, 0.005), holding=1000))
    assert result.stage_result.stage is Stage.S1
    assert result.conclusion.advice_state is AdviceState.VALID
    assert result.conclusion.advice in STAGE_ALLOWED[Stage.S1]
    assert result.plans, "S1 加仓方向应有条件计划"
    assert result.param_hash and len(result.param_hash) == 64
    assert result.calc_version == "c1-20261124"


def test_advice_always_in_stage_allowed_set() -> None:
    """枚举式硬约束：advice 必须落在阶段允许集合内（需求 §5.14.3）。"""
    cases = [
        (trending(150, 0.005), 0),
        (trending(150, 0.005), 1000),
        (trending(150, -0.005), 0),
        (trending(150, -0.005), 1000),
    ]
    for closes, holding in cases:
        result = _run(make_view(closes, holding=holding))
        if result.conclusion.advice is None:
            continue
        assert result.conclusion.advice in STAGE_ALLOWED[result.stage_result.stage], (
            f"{result.stage_result.stage} 下出现集合外动作 {result.conclusion.advice}"
        )


def test_downtrend_unheld_never_gets_buy_advice() -> None:
    result = _run(make_view(trending(150, -0.005), holding=0))
    assert result.stage_result.stage is Stage.S3
    assert result.conclusion.advice not in BUY_SIDE
    assert all(b.action not in BUY_SIDE for b in result.plans)


def test_severe_fundamental_blocks_buy() -> None:
    """严重级风险（FUN-1）→ 不出现任何买入类意见（需求 §5.10.3 硬约束 1）。"""
    view = make_view(trending(150, 0.005), holding=0)
    view = replace(view, fundamental=FundamentalView(available=True, audit_opinion_abnormal=True, report_period="2026Q3"))
    result = _run(view)
    assert result.risk_level is RiskLevel.SEVERE
    assert result.conclusion.advice not in BUY_SIDE
    assert all(b.action not in BUY_SIDE and b.action is not Action.PROBE for b in result.plans)


def test_l2_triggers_clear() -> None:
    """L1 后继续下跌触发 L2 → 清仓（需求 §5.5）。"""
    view = make_view(trending(150, -0.005), holding=1000)
    close = view.last_close
    result = _run(view, l1_triggered=True, l1_reference_price=close * 1.25)
    assert result.risk_level is RiskLevel.SEVERE
    assert result.conclusion.advice is Action.CLEAR


def test_data_gap_gt_3_early_exit_to_s0() -> None:
    """缺口 >3 日 → 阶段 S0、不出计划、卡片标「数据不足」（需求 §4.6.3）。"""
    view = make_view(trending(150, 0.005), holding=1000)
    expected = view.bars[-1].trade_date + timedelta(days=6)
    result = _run(view, expected_data_date=expected)
    assert result.stage_result.stage is Stage.S0
    assert result.conclusion.advice_state is AdviceState.NO_DATA
    assert result.conclusion.advice is None
    assert result.plans == ()
    assert result.data_quality.gap_days == 6


def test_suspended_is_unavailable() -> None:
    view = make_view(trending(150, 0.005), holding=1000)
    view = replace(view, suspended=True)
    result = _run(view)
    assert result.conclusion.advice_state is AdviceState.UNAVAILABLE
    assert result.conclusion.advice_display == "不可操作"


def test_deterministic_same_input_same_output() -> None:
    """同参数 + 同数据 + 同规则版本 → 同结论（DP-5）。"""
    view = make_view(trending(150, 0.005), holding=1000)
    a = _run(view)
    b = _run(view)
    assert a.conclusion.advice is b.conclusion.advice
    assert a.conclusion.score == b.conclusion.score
    assert a.param_hash == b.param_hash
    assert [x.as_dict() for x in a.plans] == [x.as_dict() for x in b.plans]


def test_key_levels_all_have_source() -> None:
    """关键价位必须带来源说明，否则卡片不完整（需求 §5.3）。"""
    result = _run(make_view(trending(150, 0.005), holding=1000))
    assert result.key_levels
    for level in result.key_levels:
        assert level.source
        assert level.price > 0
    kinds = {lv.kind for lv in result.key_levels}
    assert "invalid" in kinds


def test_market_weak_does_not_force_liquidation() -> None:
    """大盘偏弱 + 个股正常 → 不得输出清仓（需求 §5.10.3 B8）。"""
    market = MarketView(regime="偏弱", history_regime=("偏弱", "偏弱", "偏弱"), advance_ratio=0.55)
    result = _run(make_view(trending(150, 0.005), holding=1000), market=market)
    assert result.conclusion.advice is not Action.CLEAR
    assert result.risk_level.rank <= RiskLevel.WARNING.rank


def test_probe_payload_exposes_stop_and_cap() -> None:
    """试探仓三要素：上限 / 破下沿清仓 / 不是底仓（需求 §5.14.3.1）。"""
    view = make_view([100.0] * 60 + [100.0] * 140, holding=0)
    result = _run(view)
    payload = result.probe
    assert payload["enabled"] is True
    assert payload["box_low"] is not None
    assert payload["stop_price"] < payload["box_low"]
    assert payload["max_pct"] > 0


def test_bars_used_and_gap_recorded() -> None:
    view = make_view(trending(150, 0.005))
    result = _run(view)
    assert result.data_quality.bars_used == 150
    assert result.data_quality.gap_days == 0
