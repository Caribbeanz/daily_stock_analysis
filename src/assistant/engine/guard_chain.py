# -*- coding: utf-8 -*-
"""守卫链 S0→S6 主入口（需求 §5.4 / 技术方案 §4.1）。

顺序固定、每步产出可审计中间结果（:class:`GuardTrace`）：

```
S0 数据校验 → S1 基本面 → S2 技术面（含阶段判定）→ S3 估值
→ S4 风险等级守卫 → S5 决策表 → S5.5 阶段门 → S6 仓位约束 + 条件计划
```

四层收紧（S4 风险 → S5 决策表 → S5.5 阶段门 → S6 仓位）**全部单向收紧**；
评分与决策表完全不感知阶段，阶段只由 S5.5 事后裁剪动作集合（DP-6 / R18-Q1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Sequence

from src.assistant.config.overrides import ParamSet
from src.assistant.domain import (
    Bar,
    CandidateAdvice,
    DataQuality,
    GuardChainResult,
    GuardTrace,
    HoldingConclusion,
    MarketView,
    PortfolioView,
    PositionAdvice,
    SectorView,
    StageResult,
    StockView,
)
from src.assistant.engine import indicators
from src.assistant.engine.plan import PlanContext, build_plan
from src.assistant.engine.risk import RiskContext, RiskSummary, assess_risks
from src.assistant.engine.scoring import ScoreBreakdown, band_of, decide, score_stock
from src.assistant.engine.stage import StageDebounceState, apply_debounce, detect_stage
from src.assistant.engine.stage_gate import GateContext, apply_stage_gate, to_storage_advice
from src.assistant.enums import (
    BUY_SIDE,
    Action,
    AdviceState,
    CandidateReason,
    GuardStep,
    RiskCode,
    RiskLevel,
    Stage,
)


@dataclass(frozen=True)
class GuardChainInput:
    """一次守卫链运行的完整输入。"""

    view: StockView
    portfolio: PortfolioView
    params: ParamSet
    calc_version: str
    generated_at: datetime
    market: MarketView | None = None
    sector: SectorView | None = None
    expected_data_date: date | None = None
    trading_days: Sequence[date] | None = None
    stage_state: StageDebounceState = field(default_factory=StageDebounceState)
    previous_trend: str | None = None
    previous_advice: Action | None = None
    l1_triggered: bool = False
    l1_reference_price: float | None = None
    sector_mapped: bool = True


@dataclass(frozen=True)
class GuardChainOutcome:
    """守卫链结果 + 更新后的跨日状态（供调度层落盘/回读）。"""

    result: GuardChainResult
    stage_state: StageDebounceState


def run_guard_chain(inp: GuardChainInput) -> GuardChainOutcome:
    """执行完整守卫链，返回快照级结果。"""
    params = inp.params
    view = inp.view
    traces: list[GuardTrace] = []

    # ---- S0 数据校验 ----------------------------------------------------
    quality = _check_data(inp, params)
    traces.append(
        GuardTrace(
            step=GuardStep.S0,
            summary=f"数据{'充足' if quality.sufficient else '不足'}：bars={quality.bars_used} gap={quality.gap_days}",
            payload={"flags": quality.flags, "reason": quality.reason},
        )
    )

    # ---- S1 基本面（事实采集，不改档位）--------------------------------
    fund = view.fundamental
    traces.append(
        GuardTrace(
            step=GuardStep.S1,
            summary="财报事实已采集" if fund.available else "财报缺失，标注缺失并降置信度（不改档位）",
            payload={"available": fund.available, "period": fund.report_period},
        )
    )

    # ---- S2 技术面（含阶段判定）----------------------------------------
    tech = _tech_snapshot(view, params, quality)
    raw_stage = detect_stage(view, params, quality)
    stage_result, new_state = apply_debounce(
        raw_stage,
        inp.stage_state,
        days=int(params.get("stage_debounce_days")),
        trade_date=view.bars[-1].trade_date if view.bars else date.today(),
    )
    traces.append(
        GuardTrace(
            step=GuardStep.S2,
            summary=f"阶段 {stage_result.stage.value}（{stage_result.stage.label}），置信度 {stage_result.confidence.value}",
            payload={
                "metrics": dict(stage_result.metrics),
                "reasons": list(stage_result.reasons),
                "switched": stage_result.switched,
            },
        )
    )

    # 停牌 / 数据不足：不出阶段结论以外的任何东西
    if inp.view.suspended:
        return _early_exit(inp, quality, stage_result, new_state, traces, AdviceState.UNAVAILABLE, "停牌：不可操作")
    if stage_result.stage is Stage.S0:
        return _early_exit(inp, quality, stage_result, new_state, traces, AdviceState.NO_DATA, quality.reason or "数据不足")

    # ---- S3 估值（辅助事实）--------------------------------------------
    traces.append(
        GuardTrace(
            step=GuardStep.S3,
            summary="估值分位缺失" if view.valuation.is_missing else "估值分位已采集（与价格位置分离）",
            payload={
                "pe_pctile": view.valuation.pe_percentile,
                "pb_pctile": view.valuation.pb_percentile,
            },
        )
    )

    # ---- S4 风险等级守卫 ------------------------------------------------
    risk_ctx = RiskContext(
        view=view,
        data_date=view.bars[-1].trade_date,
        params=params,
        data_quality=quality,
        stage=stage_result.stage,
        trend=tech["trend"],
        position_tag=tech["position_tag"],
        market=inp.market,
        sector=inp.sector,
        portfolio=inp.portfolio,
        key_levels=tech["levels"],
        previous_trend=inp.previous_trend,
        l1_triggered=inp.l1_triggered,
        l1_reference_price=inp.l1_reference_price,
        sector_mapped=inp.sector_mapped,
    )
    risk_summary = assess_risks(risk_ctx)
    traces.append(
        GuardTrace(
            step=GuardStep.S4,
            summary=f"最高风险等级 {risk_summary.highest.value}（{risk_summary.total} 条信号）",
            payload={"counts": dict(risk_summary.counts)},
        )
    )

    # ---- S5 决策表（评分 → 候选 advice）--------------------------------
    breakdown = score_stock(
        view,
        params,
        ma=tech["ma"],
        slope=tech["slope"],
        pos20=tech["pos20"],
        alignment=tech["alignment"],
        box=tech["box"],
        market=inp.market,
        sector=inp.sector,
    )
    candidate = decide(
        breakdown.total,
        is_holding=view.position.is_holding,
        params=params,
        previous_advice=inp.previous_advice,
    )
    candidate_after_risk = _apply_risk_guard(candidate, risk_summary)
    traces.append(
        GuardTrace(
            step=GuardStep.S5,
            summary=candidate.summary,
            payload={
                "score": breakdown.as_dict(),
                "candidate": candidate.action.value,
                "after_risk": candidate_after_risk.action.value,
            },
        )
    )

    # ---- S5.5 阶段门 ----------------------------------------------------
    gate = apply_stage_gate(
        GateContext(
            stage=stage_result.stage,
            confidence=stage_result.confidence,
            candidate=candidate_after_risk,
            is_holding=view.position.is_holding,
            risk_level=risk_summary.highest,
            probe_enabled=bool(params.get("probe_enabled")),
            probe_trigger_satisfied=_probe_trigger_satisfied(view, tech, params, risk_summary),
            right_side_confirmed=_right_side_confirmed(view, tech, params),
            box=tech["box"],
            close=view.last_close,
            price_buffer=float(params.get("plan_buffer_pct")),
        )
    )
    traces.append(
        GuardTrace(
            step=GuardStep.S5_5,
            summary=(
                f"改写 {gate.hit.candidate.value}→{gate.action.value}（{gate.hit.rule_id.value}）"
                if gate.rewritten
                else f"候选 {gate.action.value} 在 {stage_result.stage.value} 允许集合内，未改写"
            ),
            payload=gate.hit.as_dict() if gate.hit else {},
        )
    )

    # ---- S6 仓位约束 + 条件计划 ----------------------------------------
    final_action = gate.action
    position_advice = _position_advice(inp, breakdown, final_action, risk_summary, stage_result.stage)
    plan = build_plan(
        PlanContext(
            view=view,
            portfolio=inp.portfolio,
            advice=final_action,
            stage=stage_result.stage,
            params=params,
            key_levels=tech["levels"],
            box=tech["box"],
            risk_level=risk_summary.highest,
            right_side_confirmed=_right_side_confirmed(view, tech, params),
            probe_trigger_satisfied=_probe_trigger_satisfied(view, tech, params, risk_summary),
            ma=tech["ma"],
            data_date=view.bars[-1].trade_date,
        )
    )
    traces.append(
        GuardTrace(
            step=GuardStep.S6,
            summary=f"仓位 {position_advice.tier}档 目标 {position_advice.target_pct:.1%}；条件计划 {len(plan.branches)} 条分支",
            payload={"cap_reasons": list(position_advice.cap_reasons), "plan_notes": list(plan.notes)},
        )
    )

    advice = to_storage_advice(final_action, view.position.is_holding)
    conclusion = HoldingConclusion(
        code=view.code,
        name=view.name,
        advice=advice,
        advice_state=AdviceState.VALID,
        stage=stage_result.stage,
        stage_confidence=stage_result.confidence,
        stage_gate_hit=gate.hit,
        trend=tech["trend"],
        position_tag=tech["position_tag"],
        score=breakdown.total,
        is_holding=view.position.is_holding,
        est_level=tech["position_tag"],
        div_yield=view.valuation.dividend_yield,
        pnl_pct=view.pnl_pct,
        last_close=view.last_close,
        pct_change=_pct_change(view),
        risk_level=risk_summary.highest,
        human_text=_human_text(stage_result, final_action, risk_summary),
        degraded=quality.degraded or view.source_tag.value == "D3",
    )

    result = GuardChainResult(
        code=view.code,
        name=view.name,
        data_date=view.bars[-1].trade_date,
        calc_version=inp.calc_version,
        param_hash=params.param_hash,
        generated_at=inp.generated_at,
        data_quality=quality,
        stage_result=stage_result,
        conclusion=conclusion,
        plans=plan.branches,
        plan_aggregate=plan.aggregate,
        risks=risk_summary.signals,
        position_advice=position_advice,
        key_levels=tech["levels"],
        trace=tuple(traces),
        probe=_probe_payload(view, tech, params, risk_summary),
    )
    return GuardChainOutcome(result=result, stage_state=new_state)


# ---------------------------------------------------------------------------
# S0 数据校验
# ---------------------------------------------------------------------------
def _check_data(inp: GuardChainInput, params: ParamSet) -> DataQuality:
    view = inp.view
    bars = list(view.bars)
    min_bars = max(int(params.get("min_valid_bars")), int(params.get("ma_window")) + int(params.get("slope_k")) + 1)
    flags: list[str] = []
    if not bars:
        return DataQuality(sufficient=False, bars_used=0, gap_days=0, flags=("no_bars",), reason="无 K 线数据")

    gap_days = _gap_days(bars, inp.expected_data_date, inp.trading_days)
    if gap_days > 3:
        flags.append("gap_gt_3")
    if len(bars) < min_bars:
        flags.append("insufficient_bars")
    if view.source_tag.value == "D3":
        flags.append("stale_cache")
    if view.suspended:
        flags.append("suspended")

    sufficient = "gap_gt_3" not in flags and "insufficient_bars" not in flags
    reason = ""
    if not sufficient:
        if "gap_gt_3" in flags:
            reason = f"数据缺口 {gap_days} 日 > 3 日，阶段降为 S0"
        elif "insufficient_bars" in flags:
            reason = f"有效 bar {len(bars)} < 最少 {min_bars} 条，无法判定"
    return DataQuality(
        sufficient=sufficient,
        bars_used=len(bars),
        gap_days=gap_days,
        flags=tuple(flags),
        reason=reason,
    )


def _gap_days(bars: Sequence[Bar], expected: date | None, trading_days: Sequence[date] | None) -> int:
    if expected is None:
        return 0
    last = bars[-1].trade_date
    if last >= expected:
        return 0
    if trading_days:
        missing = [d for d in trading_days if last < d <= expected]
        return len(missing)
    return (expected - last).days


# ---------------------------------------------------------------------------
# S2 技术面快照
# ---------------------------------------------------------------------------
def _tech_snapshot(view: StockView, params: ParamSet, quality: DataQuality) -> dict:
    ma_window = int(params.get("ma_window"))
    slope_k = int(params.get("slope_k"))
    theta = float(params.get("stage_theta"))
    eps = float(params.get("stage_epsilon"))
    closes = [bar.close for bar in view.bars]
    ma_series = [val for val in indicators.sma(closes, ma_window) if val is not None]
    ma = ma_series[-1] if ma_series else closes[-1]
    slope = indicators.relative_slope(ma_series, slope_k) if ma_series else 0.0
    pos20 = indicators.price_position(view.bars, 20 if len(view.bars) >= 20 else len(view.bars))
    alignment = indicators.ma_alignment_score(closes, ma_window)
    box = indicators.detect_box(
        view.bars,
        lookback=int(params.get("box_lookback")),
        width_max=float(params.get("box_width_max")),
        touch_min=int(params.get("box_touch_min")),
        touch_tol=float(params.get("box_touch_tol")),
    )
    levels = indicators.build_key_levels(
        view.bars, ma=ma, box=box, lookback=int(params.get("box_lookback")), touch_tol=float(params.get("box_touch_tol"))
    )
    close = closes[-1]
    trend = "偏强" if (close > ma * (1 + eps) and slope > theta) else ("偏弱" if (close < ma * (1 - eps) and slope < -theta) else "中性")
    position_tag = "高位" if pos20 >= 0.5 else "低位"
    return {
        "ma": ma,
        "slope": slope,
        "pos20": pos20,
        "alignment": alignment,
        "box": box,
        "levels": levels,
        "trend": trend,
        "position_tag": position_tag,
        "close": close,
    }


# ---------------------------------------------------------------------------
# S4 风险守卫对 advice 的作用（只收紧）
# ---------------------------------------------------------------------------
def _apply_risk_guard(candidate: CandidateAdvice, risks: RiskSummary) -> CandidateAdvice:
    if not risks.signals:
        return candidate

    codes = {sig.code for sig in risks.signals}
    buy_like = candidate.action in BUY_SIDE or candidate.action is Action.PROBE

    if risks.highest is RiskLevel.SEVERE:
        if RiskCode.TEC_3 in codes:
            return CandidateAdvice(Action.CLEAR, candidate.score, CandidateReason.RISK_DRIVEN, "严重级：L2 清仓")
        if RiskCode.TEC_2 in codes:
            return CandidateAdvice(Action.TRIM, candidate.score, CandidateReason.RISK_DRIVEN, "严重级：L1 先减后清")
        if buy_like:
            return CandidateAdvice(
                Action.TRIM if candidate.action is Action.CLEAR else Action.WATCH,
                candidate.score,
                CandidateReason.RISK_DRIVEN,
                "严重级风险：禁止买入类，建议降级（只降一次）",
            )
        return candidate

    if risks.highest is RiskLevel.WARNING and buy_like:
        return CandidateAdvice(
            Action.WATCH, candidate.score, CandidateReason.RISK_DRIVEN, "警示级风险：禁止加仓/建仓/买入类"
        )
    return candidate


# ---------------------------------------------------------------------------
# S6 仓位建议
# ---------------------------------------------------------------------------
def _position_advice(
    inp: GuardChainInput,
    breakdown: ScoreBreakdown,
    action: Action,
    risks: RiskSummary,
    stage: Stage,
) -> PositionAdvice:
    params = inp.params
    light = float(params.get("tier_light_max"))
    mid = float(params.get("tier_mid_max"))
    heavy = float(params.get("tier_heavy_max"))
    single_cap = float(params.get("single_cap"))
    cash_floor = float(params.get("cash_floor"))

    band = band_of(breakdown.total)
    if band == "strong":
        target, tier = heavy, "重"
    elif band == "mild":
        target, tier = mid, "中"
    else:
        target, tier = light, "轻"

    cap_reasons: list[str] = []
    if action in (Action.TRIM, Action.CLEAR) or stage is Stage.S5:
        target = 0.0
        tier = "轻"
        cap_reasons.append("防守阶段/减仓方向：不新增仓位")
    if risks.highest.rank >= RiskLevel.WARNING.rank:
        target = min(target, light)
        cap_reasons.append(f"风险等级 {risks.highest.value}：目标仓位压到轻档")
    if target > single_cap:
        target = single_cap
        cap_reasons.append(f"单只硬上限 {single_cap:.0%}")

    cash_remaining_pct = (inp.portfolio.cash_available / inp.portfolio.total_capital) if inp.portfolio.total_capital else 0.0
    if cash_remaining_pct < cash_floor + target:
        target = max(0.0, cash_remaining_pct - cash_floor)
        cap_reasons.append(f"现金底线 {cash_floor:.0%}：可用额度收紧")

    return PositionAdvice(
        tier=tier,
        target_pct=round(target, 4),
        target_amount=round(target * inp.portfolio.total_capital, 2),
        capped_pct=round(target, 4),
        cap_reasons=tuple(cap_reasons),
        t_cap_amount=round(float(params.get("t_cap")) * inp.portfolio.total_capital, 2),
    )


# ---------------------------------------------------------------------------
# 右侧确认 / 试探仓触发
# ---------------------------------------------------------------------------
def _right_side_confirmed(view: StockView, tech: dict, params: ParamSet) -> bool:
    """右侧确认：站上 MA20 + 放量 + 突破箱体上沿（需求 §5.14.3.1「转正」）。"""
    box = tech["box"]
    ma = tech["ma"]
    if box is None or not box.valid or not view.bars:
        return False
    close = view.bars[-1].close
    vol_ratio = _vol_ratio(view)
    return close > ma and close > box.high and vol_ratio >= float(params.get("volume_confirm_multiple"))


def _probe_trigger_satisfied(view: StockView, tech: dict, params: ParamSet, risks: RiskSummary) -> bool:
    """试探仓全部触发条件（需求 §5.14.3.1，须同时满足 ①–⑤）。"""
    box = tech["box"]
    if box is None or not box.valid or not view.bars:
        return False
    if view.position.is_holding:
        return False  # 「一次建仓」，已持仓不再给试探
    close = view.bars[-1].close
    tol = float(params.get("box_touch_tol"))
    near_bottom = box.low * (1 - tol) <= close <= box.low * (1 + tol * 2)
    if not near_bottom:
        return False
    vols = [bar.volume for bar in view.bars]
    if len(vols) < 20:
        return False
    avg3 = sum(vols[-3:]) / 3
    avg20 = sum(vols[-20:]) / 20
    if avg20 <= 0 or avg3 > avg20 * float(params.get("probe_shrink_ratio")):
        return False
    if risks.highest.rank >= RiskLevel.WARNING.rank:
        return False
    return True


def _vol_ratio(view: StockView) -> float:
    vols = [bar.volume for bar in view.bars]
    if not vols:
        return 0.0
    base = sum(vols[-20:]) / len(vols[-20:])
    return (vols[-1] / base) if base > 0 else 0.0


def _probe_payload(view: StockView, tech: dict, params: ParamSet, risks: RiskSummary) -> dict:
    enabled = bool(params.get("probe_enabled"))
    box = tech["box"]
    return {
        "enabled": enabled,
        "trigger_satisfied": _probe_trigger_satisfied(view, tech, params, risks) if enabled else False,
        "box_low": box.low if box else None,
        "stop_price": round(box.low * (1 - float(params.get("box_touch_tol"))), 2) if box else None,
        "max_pct": round(float(params.get("single_cap")) * float(params.get("probe_ratio")), 4),
    }


# ---------------------------------------------------------------------------
# 早退（S0 / 停牌）
# ---------------------------------------------------------------------------
def _early_exit(
    inp: GuardChainInput,
    quality: DataQuality,
    stage_result: StageResult,
    new_state: StageDebounceState,
    traces: list[GuardTrace],
    advice_state: AdviceState,
    reason: str,
) -> GuardChainOutcome:
    conclusion = HoldingConclusion(
        code=inp.view.code,
        name=inp.view.name,
        advice=None,
        advice_state=advice_state,
        stage=stage_result.stage,
        stage_confidence=stage_result.confidence,
        trend="不明",
        position_tag="不明",
        is_holding=inp.view.position.is_holding,
        risk_level=RiskLevel.INFO,
        human_text=reason,
        degraded=quality.degraded,
    )
    result = GuardChainResult(
        code=inp.view.code,
        name=inp.view.name,
        data_date=inp.view.bars[-1].trade_date if inp.view.bars else date.today(),
        calc_version=inp.calc_version,
        param_hash=inp.params.param_hash,
        generated_at=inp.generated_at,
        data_quality=quality,
        stage_result=stage_result,
        conclusion=conclusion,
        trace=tuple(traces),
    )
    return GuardChainOutcome(result=result, stage_state=new_state)


def _pct_change(view: StockView) -> float | None:
    """最近两根收盘算涨跌幅（%）；不足两根返回 None，不拿 0 冒充「平盘」。"""
    bars = view.bars
    if len(bars) < 2:
        return None
    prev = bars[-2].close
    if not prev:
        return None
    return (bars[-1].close / prev - 1) * 100


def _human_text(stage_result: StageResult, action: Action, risks: RiskSummary) -> str:
    """一句人话（详情页模板，需求 §5.14.7）。"""
    stage = stage_result.stage
    if stage is Stage.S5:
        base = "现在是无结构（看不清方向），所以今天先不动、等结构成形"
    elif stage is Stage.S1:
        base = "现在是上升趋势，所以回踩不追高、跌破 MA20 再改思路"
    elif stage is Stage.S2:
        base = "现在是高位震荡，所以只在箱体上下沿动手、箱体中部不乱动"
    elif stage is Stage.S3:
        base = "现在是下跌趋势，所以只做反弹减与破位清、不抄底"
    elif stage is Stage.S4:
        base = "现在是低位筑底，等右侧确认再正式建仓，确认前最多小额度试探"
    else:
        base = "今天数据不足，先不出结论"
    if risks.highest.rank >= RiskLevel.WARNING.rank:
        return f"{base}；今天有{risks.highest.value}级风险，买入类动作全部禁止。"
    return f"{base}；今天结论：{action.value}。"
