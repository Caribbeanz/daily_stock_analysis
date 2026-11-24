# -*- coding: utf-8 -*-
"""条件计划测试（需求 §5.11：同向过滤、只给价格触发、股数、硬上限、分支上限）。"""

from __future__ import annotations

from datetime import date

import pytest

from src.assistant.domain import KeyLevel
from src.assistant.engine.indicators import Box
from src.assistant.engine.plan import PlanContext, build_plan
from src.assistant.enums import Action, RiskLevel, Stage, TriggerType
from tests.assistant.helpers import make_portfolio, make_view, std_params, trending

DATA_DATE = date(2026, 11, 24)


def _box(low: float = 100.0, high: float = 108.0, valid: bool = True) -> Box:
    return Box(high=high, low=low, width=(high - low) / low, up_touches=3, down_touches=3,
               lookback=60, width_limit=0.12, valid=valid)


def _levels(low: float, high: float, ma: float, invalid: float) -> list[KeyLevel]:
    return [
        KeyLevel(price=low, kind="support", source="箱体下沿"),
        KeyLevel(price=ma, kind="support" if ma < high else "resistance", source="MA20 折算"),
        KeyLevel(price=high, kind="resistance", source="箱体上沿"),
        KeyLevel(price=invalid, kind="invalid", source="箱体下沿-容差"),
    ]


def _plan(stage: Stage, advice: Action, *, closes=None, holding: int = 0, box: Box | None = None,
          params=None, risk: RiskLevel = RiskLevel.INFO, cash: float = 400_000.0):
    view = make_view(closes or trending(150, 0.005), holding=holding)
    return build_plan(
        PlanContext(
            view=view,
            portfolio=make_portfolio(cash=cash),
            advice=advice,
            stage=stage,
            params=params or std_params(),
            key_levels=_levels(100.0, 108.0, 104.0, 99.0),
            box=box if box is not None else _box(),
            risk_level=risk,
            ma=104.0,
            data_date=DATA_DATE,
        )
    )


# ---- 同向过滤 --------------------------------------------------------------
def test_s3_sell_advice_has_no_buy_branches() -> None:
    """下跌阶段只减不加：分支动作必须与 advice 同向（需求 §5.11.2）。"""
    result = _plan(Stage.S3, Action.TRIM, closes=trending(150, -0.005), holding=1000)
    assert result.branches
    assert all(b.action in (Action.TRIM, Action.CLEAR, Action.NO_OP) for b in result.branches)
    assert any(b.action is Action.TRIM for b in result.branches)


def test_hold_advice_only_informational() -> None:
    result = _plan(Stage.S2, Action.HOLD, holding=1000)
    actions = {b.action for b in result.branches}
    assert actions <= {Action.NO_OP}, "持有/观望只出不操作分支"


def test_unheld_buy_advice_writes_buy_not_add() -> None:
    """未持仓写「买入」，不得出现「加仓」措辞（需求 §5.4 存储层分档）。"""
    result = _plan(Stage.S1, Action.WAIT_BUY, holding=0)
    assert result.branches
    assert any(b.action is Action.BUY for b in result.branches)
    assert all(b.action is not Action.ADD for b in result.branches)


# ---- 触发条件只给价格 ------------------------------------------------------
def test_triggers_are_price_only() -> None:
    result = _plan(Stage.S1, Action.WAIT_BUY)
    for branch in result.branches:
        assert branch.trigger_type in (TriggerType.PRICE_GTE, TriggerType.PRICE_LTE, TriggerType.PCT_GTE, TriggerType.PCT_LTE)
        assert branch.trigger_type.writable
        assert branch.trigger_price and branch.trigger_price > 0


def test_technical_basis_separated_from_trigger() -> None:
    """技术依据放 technical_basis，触发条件本身保持干净可直接照抄。"""
    result = _plan(Stage.S2, Action.HOLD, holding=1000)
    for branch in result.branches:
        assert "触发价来源" in branch.technical_basis
        assert "MA" not in str(branch.trigger_type.value)


# ---- 股数与硬上限 ----------------------------------------------------------
def test_shares_multiple_of_lot() -> None:
    result = _plan(Stage.S1, Action.WAIT_BUY)
    for branch in result.branches:
        if branch.shares:
            assert branch.shares % 100 == 0


def test_sell_shares_not_exceed_position() -> None:
    result = _plan(Stage.S3, Action.TRIM, closes=trending(150, -0.005), holding=300)
    for branch in result.branches:
        if branch.action in (Action.TRIM, Action.CLEAR):
            assert branch.shares <= 300


def test_shares_respect_single_cap() -> None:
    result = _plan(Stage.S1, Action.WAIT_BUY)
    for branch in result.branches:
        if branch.action in (Action.BUY, Action.ADD):
            assert branch.pct_of_total <= 0.20 + 1e-6


def test_insufficient_cash_no_branch() -> None:
    """现金不足 1 手 → 不出该分支（不得输出越界股数）。"""
    result = _plan(Stage.S1, Action.WAIT_BUY, cash=1_000.0)
    buy_branches = [b for b in result.branches if b.action in (Action.BUY, Action.ADD)]
    assert buy_branches == []


# ---- 风险与阶段约束 --------------------------------------------------------
def test_warning_risk_blocks_buy_branches() -> None:
    result = _plan(Stage.S1, Action.WAIT_BUY, risk=RiskLevel.WARNING)
    assert all(b.action not in (Action.BUY, Action.ADD, Action.PROBE) for b in result.branches)


def test_s5_never_generates_buy_branch() -> None:
    result = _plan(Stage.S5, Action.WATCH)
    assert all(b.action not in (Action.BUY, Action.ADD, Action.PROBE) for b in result.branches)


def test_s4_watch_allows_probe_branch_with_label() -> None:
    """S4 观望时允许保留试探建仓分支，但须标「需自行确认」。"""
    result = _plan(Stage.S4, Action.WATCH)
    probes = [b for b in result.branches if b.action is Action.PROBE]
    assert probes, "S4 观望应保留试探建仓分支"
    assert "试探仓" in probes[0].notes


def test_probe_branch_absent_when_disabled() -> None:
    params = std_params().with_override("probe_enabled", False)
    result = _plan(Stage.S4, Action.WATCH, params=params)
    assert all(b.action is not Action.PROBE for b in result.branches)


# ---- 分支上限与 S0 ---------------------------------------------------------
def test_branch_cap_enforced() -> None:
    params = std_params().with_override("plan_max_branches", 2)
    result = _plan(Stage.S1, Action.WAIT_BUY, params=params)
    actionable = [b for b in result.branches if b.action is not Action.NO_OP]
    assert len(actionable) <= 2


def test_s0_generates_nothing() -> None:
    result = _plan(Stage.S0, Action.WATCH)
    assert result.branches == ()
    assert "S0" in result.notes[0]


def test_t_plus_one_no_mixed_sides() -> None:
    """A 股 T+1：同一标的买入分支与卖出分支不得同时出现（需求 §5.11.4）。"""
    result = _plan(Stage.S2, Action.HOLD, holding=1000)
    has_buy = any(b.action in (Action.BUY, Action.ADD) for b in result.branches)
    has_sell = any(b.action in (Action.TRIM, Action.CLEAR) for b in result.branches)
    assert not (has_buy and has_sell)


def test_aggregate_warns_when_over_cap() -> None:
    """全买入分支合计越界时必须给提示，且不删改分支股数（需求 §5.11.5）。"""
    result = _plan(Stage.S1, Action.WAIT_BUY)
    if result.aggregate is None:
        pytest.skip("该场景无买入分支")
    assert result.aggregate.total_amount > 0
    if result.aggregate.over_single_cap or result.aggregate.over_total_cap:
        assert "请自行取舍" in result.aggregate.message
        assert "不代为决定" in result.aggregate.message
