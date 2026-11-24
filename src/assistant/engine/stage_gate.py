# -*- coding: utf-8 -*-
"""阶段门 S5.5（方案 A，需求 §5.14.5 / 技术方案 §4.4）。

位置：**决策表（守卫 S5）之后、仓位约束（S6）之前**。

工作方式：决策表先正常产出候选 ``candidate_advice``，阶段门只做「事后收紧」——
校验候选是否落在该阶段允许集合内，冲突则改写为集合内动作并留痕 ``stage_gate_hit``。

三条不可违反：
1. **只收紧不放宽**：绝不把候选改成更激进的动作（含「观望 → 买入」这类方向反转）。
2. **只改动作方向**：不改风险等级、不改股数、不改仓位上限。
3. **不改评分**：评分逻辑不感知阶段（DP-6）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from src.assistant.domain import CandidateAdvice, StageGateHit
from src.assistant.engine.indicators import Box
from src.assistant.enums import (
    ADVICE_VALUES,
    BUY_SIDE,
    STAGE_ALLOWED,
    Action,
    CandidateReason,
    RiskLevel,
    RuleId,
    Stage,
    StageConfidence,
)


#: 买入类动作的激进程度，用于「不放宽」断言。
#: ``ADD``（加仓）与 ``BUY``（建仓）同级：需求 §5.14.5 要求右侧确认后把「加仓」
#: 统一改写为「确认后建仓」，这是措辞归一，不是放宽风险。
_BUY_AGGRESSIVENESS: Mapping[Action, int] = {
    Action.WATCH: 0,
    Action.WAIT_BUY: 1,
    Action.PROBE: 2,
    Action.BUY: 3,
    Action.ADD: 3,
}


@dataclass(frozen=True)
class GateContext:
    """阶段门判定所需的全部输入（一次性传齐，避免门内再去取数）。"""

    stage: Stage
    confidence: StageConfidence
    candidate: CandidateAdvice
    is_holding: bool
    risk_level: RiskLevel = RiskLevel.INFO
    probe_enabled: bool = True
    probe_trigger_satisfied: bool = False
    right_side_confirmed: bool = False
    box: Box | None = None
    close: float = 0.0
    price_buffer: float = 0.005

    @property
    def near_box_bottom(self) -> bool:
        """价格是否在箱体下沿附近（S2 允许「回下沿再买」判据）。"""
        if self.box is None or not self.box.valid:
            return False
        return self.close <= self.box.low * (1 + max(self.price_buffer * 4, 0.02))

    @property
    def near_box_top(self) -> bool:
        if self.box is None or not self.box.valid:
            return False
        return self.close >= self.box.high * (1 - max(self.price_buffer * 4, 0.02))


@dataclass(frozen=True)
class GateResult:
    """阶段门输出：最终动作 + 可选取代。"""

    action: Action
    hit: StageGateHit | None = None

    @property
    def rewritten(self) -> bool:
        return self.hit is not None and self.hit.rule_id is not RuleId.NONE


def apply_stage_gate(ctx: GateContext) -> GateResult:
    """执行阶段门校验与改写。"""
    candidate = ctx.candidate.action

    # S0 已在守卫 S2 拦截；此处只做防御性兜底，不产生策略
    if ctx.stage is Stage.S0:
        return GateResult(action=Action.UNAVAILABLE)

    final, rule, reason = _rewrite(ctx, candidate)
    final = _ensure_allowed(ctx, final, candidate)

    if final is candidate:
        return GateResult(action=final)

    hit = StageGateHit(
        stage=ctx.stage,
        candidate=candidate,
        final=final,
        rule_id=rule if rule is not RuleId.NONE else _fallback_rule(ctx.stage),
        reason=reason,
    )
    _assert_not_relaxed(candidate, final)
    return GateResult(action=final, hit=hit)


# ---------------------------------------------------------------------------
# 改写规则（顺序即优先级，与需求 §5.14.5 表格一致）
# ---------------------------------------------------------------------------
def _rewrite(ctx: GateContext, candidate: Action) -> tuple[Action, RuleId, str]:
    # 规则 ⑦：非 S4 阶段出现「试探建仓」→ 直接剔除
    if candidate is Action.PROBE and ctx.stage is not Stage.S4:
        return (
            _neutral(ctx),
            RuleId.PROBE_NOT_ALLOWED,
            "阶段门：试探仓仅限 S4 低位筑底，已剔除该动作",
        )

    # 规则 ①：S3 下跌阶段禁止任何买入类动作
    if ctx.stage is Stage.S3 and candidate in BUY_SIDE:
        if ctx.is_holding and ctx.risk_level.rank >= RiskLevel.WARNING.rank:
            return (
                Action.TRIM,
                RuleId.S3_NO_BUY,
                "阶段门：下跌阶段禁止买入类动作，已持仓且风险达警示级 → 改减仓",
            )
        return (
            Action.WAIT_BUY if not ctx.is_holding else Action.WATCH,
            RuleId.S3_NO_BUY,
            "阶段门：下跌阶段禁止买入类动作",
        )

    # 规则 ④：S5 无结构阶段只允许持有/观望与风险驱动减仓
    if ctx.stage is Stage.S5:
        box_trade = candidate in (Action.BUY, Action.ADD, Action.PROBE) or (
            candidate is Action.TRIM and ctx.candidate.reason is CandidateReason.BOX_TOP_TRIM
        )
        if box_trade or candidate is Action.WAIT_BUY:
            return (
                _neutral(ctx),
                RuleId.S5_NO_STRUCTURE,
                "阶段门：结构未成形，不新开仓、不做箱体交易",
            )

    # 规则 ② / ⑤：S4 筑底阶段，右侧未确认前不给买入类；满足触发条件才给试探仓
    if ctx.stage is Stage.S4 and candidate in (Action.BUY, Action.ADD):
        if ctx.right_side_confirmed:
            if candidate is Action.ADD:
                return (
                    Action.BUY,
                    RuleId.S4_NO_SCALE_IN,
                    "阶段门：右侧已确认，可建仓但不得写「加仓/摊薄」，统一为确认后建仓",
                )
            return (candidate, RuleId.NONE, "")
        if ctx.probe_enabled and ctx.probe_trigger_satisfied:
            return (
                Action.PROBE,
                RuleId.S4_PROBE,
                "阶段门：右侧未确认但满足试探仓全部触发条件 → 改试探建仓（限额一次）",
            )
        return (
            Action.WAIT_BUY,
            RuleId.S4_NO_SCALE_IN if candidate is Action.ADD else RuleId.S4_WAIT_CONFIRM,
            "阶段门：待右侧确认（站上 MA20 + 放量 + 突破箱体上沿）",
        )

    # 规则 ③：S2 震荡阶段不追突破加仓
    if ctx.stage is Stage.S2 and candidate is Action.ADD and ctx.candidate.reason is CandidateReason.BREAKOUT_ADD:
        return (
            Action.HOLD if ctx.is_holding else Action.WAIT_BUY,
            RuleId.S2_NO_BREAKOUT,
            "阶段门：震荡阶段不追突破，回下沿再买",
        )

    # 规则 ⑧：S1 上升阶段禁止「箱体上沿逆势高抛」（趋势票不做震荡思路）
    if (
        ctx.stage is Stage.S1
        and candidate is Action.TRIM
        and ctx.candidate.reason is CandidateReason.BOX_TOP_TRIM
    ):
        return (
            Action.HOLD,
            RuleId.S1_NO_COUNTERTREND_TRIM,
            "阶段门：上升趋势禁止用箱体上沿逆势高抛（移动止盈不受此限）",
        )

    return (candidate, RuleId.NONE, "")


def _neutral(ctx: GateContext) -> Action:
    """中性动作：未持仓观望，已持仓持有。"""
    return Action.HOLD if ctx.is_holding else Action.WATCH


def _ensure_allowed(ctx: GateContext, action: Action, original: Action) -> Action:
    """安全网：最终动作必须落在阶段允许集合内（需求 §5.14.3 枚举式硬约束）。"""
    allowed = STAGE_ALLOWED.get(ctx.stage, frozenset())
    if action in allowed:
        return action
    # 先退回中性动作；仍不允许时取集合内最接近的保守项
    neutral = _neutral(ctx)
    if neutral in allowed:
        return neutral
    for fallback in (Action.WATCH, Action.HOLD, Action.UNAVAILABLE):
        if fallback in allowed or fallback is Action.UNAVAILABLE:
            return fallback
    return Action.UNAVAILABLE


def _fallback_rule(stage: Stage) -> RuleId:
    return {
        Stage.S1: RuleId.S1_NO_COUNTERTREND_TRIM,
        Stage.S2: RuleId.S2_NO_BREAKOUT,
        Stage.S3: RuleId.S3_NO_BUY,
        Stage.S4: RuleId.S4_WAIT_CONFIRM,
        Stage.S5: RuleId.S5_NO_STRUCTURE,
    }.get(stage, RuleId.NONE)


def _assert_not_relaxed(candidate: Action, final: Action) -> None:
    """防放宽断言：买入类动作只能变弱，不能变强。"""
    if candidate in _BUY_AGGRESSIVENESS and final in _BUY_AGGRESSIVENESS:
        if _BUY_AGGRESSIVENESS[final] > _BUY_AGGRESSIVENESS[candidate]:
            raise AssertionError(f"阶段门违反「只收紧不放宽」：{candidate.value} → {final.value}")


def to_storage_advice(action: Action, is_holding: bool) -> Action | None:
    """把执行层动作映射回 ``advice`` 存储层 7 档（需求 §5.4）。

    - 持有中：BUY/PROBE 归入「加仓」（存储层无独立「买入」档）
    - 未持仓：ADD/BUY 归入「等待买点」（未持仓不下「加仓」）
    """
    if action is Action.UNAVAILABLE:
        return None
    if action in ADVICE_VALUES:
        if action is Action.ADD and not is_holding:
            return Action.WAIT_BUY
        return action
    if action in (Action.BUY, Action.PROBE):
        return Action.ADD if is_holding else Action.WAIT_BUY
    if action is Action.NO_OP:
        return Action.HOLD if is_holding else Action.WATCH
    return Action.WATCH if not is_holding else Action.HOLD
