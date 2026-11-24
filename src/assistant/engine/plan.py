# -*- coding: utf-8 -*-
"""条件计划生成（需求 §5.11 / §5.14.6，技术方案 §4.6）。

四条硬约束：
1. **先读阶段定模板**，再做**同向过滤**：分支动作必须与当日 ``advice`` 同向，反向只出「不操作 + 原因」。
2. **只给价格/涨跌幅触发**：均线、指标、量能**不得**写进触发条件（券商条件单设不了），
   技术依据只放 ``technical_basis``（§6.6）。
3. **各分支独立**：每条分支单独通过全部硬上限；越界即不出该分支；不做优先级、不自动裁决，
   但必须给「全触发合计」越界提示。
4. **股数确定性**：``floor(可用金额 / 触发价 / 100) × 100``；卖出不超过可卖持仓。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from src.assistant.config.overrides import ParamSet
from src.assistant.domain import (
    KeyLevel,
    PlanAggregate,
    PlanBranch,
    PortfolioView,
    StockView,
)
from src.assistant.engine.indicators import Box
from src.assistant.enums import Action, BUY_SIDE, RiskLevel, Stage, TriggerType


@dataclass(frozen=True)
class PlanContext:
    """条件计划生成的完整输入。"""

    view: StockView
    portfolio: PortfolioView
    advice: Action
    stage: Stage
    params: ParamSet
    key_levels: Sequence[KeyLevel] = ()
    box: Box | None = None
    risk_level: RiskLevel = RiskLevel.INFO
    right_side_confirmed: bool = False
    probe_trigger_satisfied: bool = False
    ma: float | None = None
    data_date: object | None = None


@dataclass(frozen=True)
class PlanResult:
    branches: tuple[PlanBranch, ...] = ()
    aggregate: PlanAggregate | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


def build_plan(ctx: PlanContext) -> PlanResult:
    """按阶段模板生成条件计划。``stage=S0`` 与异常态不生成。"""
    if ctx.stage is Stage.S0:
        return PlanResult(notes=("阶段 S0（无法判定）：不生成条件计划",))
    if ctx.advice is Action.UNAVAILABLE:
        return PlanResult(notes=("不可操作（停牌/异常）：不生成条件计划",))

    specs = _template(ctx)
    if not specs:
        return PlanResult(notes=(f"阶段 {ctx.stage.value} + 意见 {ctx.advice.value}：无适用模板",))

    max_branches = int(ctx.params.get("plan_max_branches"))
    valid_days = int(ctx.params.get("plan_valid_days"))
    prev_close = _prev_close(ctx.view)

    built: list[PlanBranch] = []
    informational: list[PlanBranch] = []
    for idx, spec in enumerate(specs):
        branch = _build_branch(
            ctx,
            spec,
            index=idx + 1,
            valid_days=valid_days,
            prev_close=prev_close,
        )
        if branch is None:
            continue
        if branch.action is Action.NO_OP:
            informational.append(branch)
        else:
            built.append(branch)

    # 同向过滤（需求 §5.11.2）
    filtered: list[PlanBranch] = []
    for branch in built:
        if _same_direction(branch.action, ctx.advice):
            filtered.append(branch)
    dropped = len(built) - len(filtered)

    # 风险等级约束（需求 §5.10.3）：警示级禁买入类分支，严重级再禁新建 T 计划
    if ctx.risk_level.rank >= RiskLevel.WARNING.rank:
        filtered = [b for b in filtered if b.action not in BUY_SIDE and b.action is not Action.PROBE]

    # T+1：同一标的当日买入分支与卖出分支不得同时出现（需求 §5.11.4）
    if ctx.view.lot_rule.t_plus_one:
        has_buy = any(b.action in BUY_SIDE or b.action is Action.PROBE for b in filtered)
        has_sell = any(b.action in (Action.TRIM, Action.CLEAR) for b in filtered)
        if has_buy and has_sell:
            keep_sell = ctx.advice in (Action.TRIM, Action.CLEAR, Action.WAIT_SELL)
            filtered = [b for b in filtered if (b.action in (Action.TRIM, Action.CLEAR)) == keep_sell]
            dropped += 1

    kept = filtered[:max_branches]
    overflow = len(filtered) - len(kept)

    branches = kept + informational[:1]
    notes = []
    if dropped:
        notes.append(f"{dropped} 条分支与当日意见不同向或违反风险约束，已过滤")
    if overflow:
        notes.append(f"{overflow} 条分支超出上限 {max_branches}，按阶段模板优先级截断")
    aggregate = _aggregate(ctx, kept)
    if ctx.data_date is not None:
        notes.append(f"适用交易日：{ctx.data_date} 的下一交易日（仅当日有效）")
    return PlanResult(branches=tuple(branches), aggregate=aggregate, notes=tuple(notes))


# ---------------------------------------------------------------------------
# 阶段模板（需求 §5.14.6）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _BranchSpec:
    scenario: str
    trigger_type: TriggerType
    price_ref: str  # box_high / box_low / ma / recent_high / recent_low / invalid
    action: Action
    direction: str = "buy"  # buy | sell | none
    is_probe: bool = False
    note: str = ""
    buffer: float = 0.0


def _buy_action(ctx: PlanContext) -> Action:
    """买入类动作的措辞：已持仓写「加仓」，未持仓写「买入」（需求 §5.4 存储层分档）。"""
    return Action.ADD if ctx.view.position.is_holding else Action.BUY


def _template(ctx: PlanContext) -> list[_BranchSpec]:
    stage = ctx.stage
    advice = ctx.advice
    buffer = float(ctx.params.get("plan_buffer_pct"))
    probe_enabled = bool(ctx.params.get("probe_enabled"))
    out: list[_BranchSpec] = []

    buy_like = advice in BUY_SIDE
    sell_like = advice in (Action.TRIM, Action.CLEAR, Action.WAIT_SELL)

    if stage is Stage.S1:
        if buy_like:
            buy = _buy_action(ctx)
            out.append(_BranchSpec("回踩 MA20 买入", TriggerType.PRICE_LTE, "ma", buy, "buy",
                                   note="回踩位 = MA20±ε；跌破 MA20 不追"))
            out.append(_BranchSpec("突破前高买入", TriggerType.PRICE_GTE, "recent_high", buy, "buy",
                                   note="突破 = 前高 + 容差", buffer=buffer))
            out.append(_BranchSpec("区间内不动", TriggerType.PRICE_LTE, "ma", Action.NO_OP, "none"))
        elif advice is Action.HOLD:
            out.append(_BranchSpec("跌破 MA20 转震荡", TriggerType.PRICE_LTE, "ma", Action.NO_OP, "none",
                                   note="仅提示，不设单"))
        else:  # 减仓（移动止盈）
            out.append(_BranchSpec("回落到 MA20 止盈减仓", TriggerType.PRICE_LTE, "ma", Action.TRIM, "sell"))
            out.append(_BranchSpec("跌破失效位继续减", TriggerType.PRICE_LTE, "invalid", Action.TRIM, "sell"))

    elif stage is Stage.S2:
        if advice in (Action.HOLD, Action.TRIM):
            out.append(_BranchSpec("到箱体上沿减 T 仓", TriggerType.PRICE_GTE, "box_high", Action.TRIM, "sell",
                                   note="上沿 = 箱体高点 − 容差"))
            if advice is Action.HOLD:
                out.append(_BranchSpec("回到箱体下沿买 T 仓", TriggerType.PRICE_LTE, "box_low", _buy_action(ctx), "buy",
                                       note="下沿 = 箱体低点 + 容差"))
            out.append(_BranchSpec("区间内不动", TriggerType.PRICE_LTE, "box_low", Action.NO_OP, "none"))
        elif buy_like:
            buy = _buy_action(ctx)
            out.append(_BranchSpec("回到箱体下沿买", TriggerType.PRICE_LTE, "box_low", buy, "buy"))
            out.append(_BranchSpec("突破上沿确认买（仅一次）", TriggerType.PRICE_GTE, "box_high", buy, "buy",
                                   buffer=buffer))
            out.append(_BranchSpec("区间内不动", TriggerType.PRICE_LTE, "box_low", Action.NO_OP, "none"))
        else:
            out.append(_BranchSpec("跌破下沿转下跌", TriggerType.PRICE_LTE, "box_low", Action.NO_OP, "none"))

    elif stage is Stage.S3:
        # 下跌阶段主要卖点在反弹，不只挂破位
        out.append(_BranchSpec("反弹到 MA20 减仓", TriggerType.PRICE_GTE, "ma", Action.TRIM, "sell",
                               note="反弹位 = MA20（下方反弹压力）"))
        out.append(_BranchSpec("跌破失效位减 L1", TriggerType.PRICE_LTE, "invalid", Action.TRIM, "sell",
                               note="L1：先减一部分，保留底仓"))
        out.append(_BranchSpec("破位加速清 L2", TriggerType.PRICE_LTE, "invalid", Action.CLEAR, "sell",
                               note="L2：L1 后继续下跌才清"))
        out.append(_BranchSpec("反弹不过压力不动", TriggerType.PRICE_LTE, "ma", Action.NO_OP, "none"))

    elif stage is Stage.S4:
        if advice in (Action.WAIT_BUY, Action.WATCH):
            out.append(_BranchSpec("站上箱体上沿且放量 → 确认建仓", TriggerType.PRICE_GTE, "box_high", Action.BUY,
                                   "buy", buffer=buffer, note="右侧确认：站上 MA20 + 放量 + 突破上沿"))
            if probe_enabled:
                out.append(_BranchSpec("回到箱体下沿且缩量 → 试探建仓", TriggerType.PRICE_LTE, "box_low",
                                       Action.PROBE, "buy", is_probe=True,
                                       note="试探仓，需自行确认；限额一次，不补仓"))
            out.append(_BranchSpec("跌破箱体下沿 → 不操作", TriggerType.PRICE_LTE, "invalid", Action.NO_OP, "none"))
        elif advice is Action.PROBE:
            out.append(_BranchSpec("回到箱体下沿且缩量 → 试探建仓", TriggerType.PRICE_LTE, "box_low", Action.PROBE,
                                   "buy", is_probe=True, note="试探仓，需自行确认"))
            out.append(_BranchSpec("跌破箱体下沿 → 清试探仓", TriggerType.PRICE_LTE, "invalid", Action.CLEAR, "sell",
                                   note="试探仓止损 = 箱体下沿 − 容差"))
        elif advice is Action.BUY:
            out.append(_BranchSpec("确认后建仓", TriggerType.PRICE_GTE, "box_high", Action.BUY, "buy",
                                   buffer=buffer, note="确认后建仓，不叫加仓/摊薄"))
        else:  # HOLD / TRIM
            out.append(_BranchSpec("跌破箱体下沿 → 清试探仓 / L1 减", TriggerType.PRICE_LTE, "invalid",
                                   Action.TRIM, "sell"))
            out.append(_BranchSpec("全部不操作", TriggerType.PRICE_LTE, "box_low", Action.NO_OP, "none"))

    elif stage is Stage.S5:
        # 不生成买入分支；已持仓只给下行观察分支
        if ctx.view.position.is_holding:
            out.append(_BranchSpec("跌破失效位减 L1", TriggerType.PRICE_LTE, "invalid", Action.TRIM, "sell"))
            out.append(_BranchSpec("破位加速清 L2", TriggerType.PRICE_LTE, "invalid", Action.CLEAR, "sell"))
            out.append(_BranchSpec("结构未成形 → 不操作", TriggerType.PRICE_LTE, "invalid", Action.NO_OP, "none",
                                   note="站上/跌破近 20 日高/低点再看"))
        else:
            out.append(_BranchSpec("结构未成形，今日不设单", TriggerType.PRICE_LTE, "recent_low", Action.NO_OP, "none",
                                   note="近 20 日高/低点仅作观察位，不写进条件单"))

    if sell_like and not any(b.action in (Action.TRIM, Action.CLEAR) for b in out):
        out.append(_BranchSpec("跌破失效位减仓", TriggerType.PRICE_LTE, "invalid", Action.TRIM, "sell"))
    return out


# ---------------------------------------------------------------------------
# 分支构造（价位解析 + 股数 + 硬上限校验）
# ---------------------------------------------------------------------------
def _build_branch(
    ctx: PlanContext,
    spec: _BranchSpec,
    *,
    index: int,
    valid_days: int,
    prev_close: float | None,
) -> PlanBranch | None:
    price = _resolve_price(ctx, spec.price_ref, spec.buffer)
    if price is None or price <= 0:
        return None

    shares, check = _size_branch(ctx, spec, price)
    if spec.action is Action.NO_OP:
        shares = 0
    if spec.action not in (Action.NO_OP,) and shares <= 0:
        # 金额不足 1 手 / 越界：不出该分支（需求 §5.11.4）
        return None

    amount = shares * price
    pct = amount / ctx.portfolio.total_capital if ctx.portfolio.total_capital else 0.0
    notes = [spec.note] if spec.note else []
    if spec.action is Action.PROBE:
        notes.append("试探仓 · 破下沿清仓 · 不是底仓")
    if spec.action is Action.NO_OP:
        notes.append("不操作（仅提示）")

    trigger_type = spec.trigger_type
    return PlanBranch(
        branch_id=f"B{index}",
        scenario=spec.scenario,
        trigger_type=trigger_type,
        trigger_price=round(price, 2),
        action=spec.action,
        shares=shares,
        est_amount=round(amount, 2),
        pct_of_total=pct,
        valid_days=valid_days,
        # 「可设条件单」= 有价格可挂 **且** 有单可下：NO_OP/观望/持有只是提示，
        # 让用户去券商挂一个「什么都不做」的条件单没有意义（也不该出现在精简版里）
        settable=trigger_type.writable
        and spec.action not in (Action.NO_OP, Action.WATCH, Action.HOLD),
        notes="；".join(notes),
        position_check=check,
        technical_basis=f"触发价来源：{spec.price_ref}" + (f"（容差 {spec.buffer:.2%}）" if spec.buffer else ""),
        pct_change=(_pct_from_prev(price, prev_close) if prev_close else None),
        is_probe=spec.is_probe,
    )


def _resolve_price(ctx: PlanContext, ref: str, buffer: float) -> float | None:
    """把「价位引用」折算成**具体价格**（均线/箱体都必须落到价格，需求 §5.14.6 末条）。"""
    box = ctx.box
    levels = list(ctx.key_levels)
    if ref == "ma":
        return ctx.ma
    if ref == "box_high":
        return (box.high * (1 + buffer)) if box is not None and box.valid else None
    if ref == "box_low":
        # 下沿买点 = 箱体低点 + 容差（略高于下沿，避免永远等不到）
        return (box.low * (1 + buffer)) if box is not None and box.valid else None
    if ref == "invalid":
        lv = next((item for item in levels if item.kind == "invalid"), None)
        return lv.price if lv else None
    if ref in ("recent_high", "recent_low"):
        kind = "resistance" if ref == "recent_high" else "support"
        candidates = [item for item in levels if item.kind == kind]
        if not candidates:
            return None
        price = candidates[0].price
        return price * (1 + buffer) if kind == "resistance" else price * (1 + buffer)
    if ref == "prev_close":
        return _prev_close(ctx.view)
    return None


def _size_branch(ctx: PlanContext, spec: _BranchSpec, price: float) -> tuple[int, dict]:
    """股数与硬上限校验（需求 §5.11.4 股数换算）。"""
    pf = ctx.portfolio
    params = ctx.params
    single_cap = float(params.get("single_cap"))
    total_cap = float(params.get("total_cap"))
    cash_floor = float(params.get("cash_floor"))
    t_cap = float(params.get("t_cap"))
    lot = ctx.view.lot_rule.lot_size

    current_value = ctx.view.market_value or ctx.view.position.shares * (ctx.view.last_close if ctx.view.bars else 0.0)
    single_remaining = max(0.0, single_cap * pf.total_capital - current_value)
    total_remaining = max(0.0, total_cap * pf.total_capital - pf.holding_market_value)
    cash_remaining = max(0.0, pf.cash_available - cash_floor * pf.total_capital)
    t_remaining = max(0.0, t_cap * pf.total_capital - ctx.view.position.t_shares * price)

    check: dict[str, object] = {
        "single_cap": single_cap,
        "total_cap": total_cap,
        "cash_floor": cash_floor,
    }

    if spec.action in (Action.TRIM, Action.CLEAR):
        sellable = ctx.view.position.available_to_sell
        if spec.action is Action.CLEAR:
            shares = (sellable // lot) * lot
        else:
            ratio = float(params.get("stop_l1_ratio"))
            shares = int(min(math.floor(sellable * ratio / lot) * lot, sellable // lot * lot))
        check.update({"sellable": sellable, "shares": shares})
        check["ok"] = shares > 0
        return shares, check

    if spec.action is Action.NO_OP:
        check["ok"] = True
        return 0, check

    # 买入类：目标档位 → 可用金额取四项最小值
    tier_pct = _tier_pct(ctx)
    if spec.is_probe:
        probe_ratio = float(params.get("probe_ratio"))
        budget = min(
            tier_pct * pf.total_capital * probe_ratio,
            single_cap * pf.total_capital * probe_ratio,
            cash_remaining,
        )
    else:
        budget = min(tier_pct * pf.total_capital, single_remaining, total_remaining, cash_remaining)
        if spec.action is Action.ADD and ctx.stage is Stage.S2:
            budget = min(budget, t_remaining)  # 箱体低吸属 T 仓

    shares = int(math.floor(budget / price / lot) * lot)
    check.update(
        {
            "budget": round(budget, 2),
            "single_remaining": round(single_remaining, 2),
            "total_remaining": round(total_remaining, 2),
            "cash_remaining": round(cash_remaining, 2),
            "shares": shares,
            "ok": shares > 0,
        }
    )
    return shares, check


def _tier_pct(ctx: PlanContext) -> float:
    """动态基础建议仓位（需求 §5.6 档位：轻 <10% / 中 10–15% / 重 15–20%）。"""
    params = ctx.params
    light = float(params.get("tier_light_max"))
    mid = float(params.get("tier_mid_max"))
    if ctx.stage in (Stage.S1, Stage.S4):
        return mid if ctx.risk_level.rank < RiskLevel.WARNING.rank else light
    if ctx.stage is Stage.S2:
        return light
    if ctx.stage is Stage.S3:
        return 0.0
    return light


def _same_direction(action: Action, advice: Action) -> bool:
    """同向过滤（需求 §5.11.2 表）。

    例外（需求 §5.14.3.1）：``advice=观望`` 时**允许保留试探建仓分支**，
    但须标注「试探仓，需自行确认」——试探仓是「不完全踏空」的第三条路，不是买入结论。
    """
    if action is Action.NO_OP:
        return True
    if action is Action.PROBE and advice is Action.WATCH:
        return True
    buy_advice = advice in BUY_SIDE or advice is Action.PROBE or advice is Action.BUY
    sell_advice = advice in (Action.TRIM, Action.CLEAR, Action.WAIT_SELL)
    action_buy = action in BUY_SIDE or action is Action.PROBE or action is Action.BUY
    action_sell = action in (Action.TRIM, Action.CLEAR)
    if buy_advice:
        return action_buy
    if sell_advice:
        return action_sell
    return False  # 持有/观望：只留「不操作」


def _aggregate(ctx: PlanContext, branches: Sequence[PlanBranch]) -> PlanAggregate | None:
    buys = [b for b in branches if b.action in BUY_SIDE or b.action is Action.PROBE]
    if not buys:
        return None
    total = sum(b.est_amount for b in buys)
    pct = total / ctx.portfolio.total_capital if ctx.portfolio.total_capital else 0.0
    single = float(ctx.params.get("single_cap"))
    total_cap = float(ctx.params.get("total_cap"))
    current = ctx.view.market_value or 0.0
    over_single = (current + total) > single * ctx.portfolio.total_capital + 1e-6
    over_total = (ctx.portfolio.holding_market_value + total) > total_cap * ctx.portfolio.total_capital + 1e-6
    message = ""
    if over_single or over_total:
        message = (
            f"若以上买入分支全部触发，合计将达总资金 {pct:.1%}，"
            f"超出单只 {single:.0%} / 总仓 {total_cap:.0%} 上限，请自行取舍。"
            "系统不代为决定，也不删改任何分支股数。"
        )
    return PlanAggregate(
        total_amount=round(total, 2),
        total_pct=pct,
        over_single_cap=over_single,
        over_total_cap=over_total,
        message=message,
    )


def _prev_close(view: StockView) -> float | None:
    if len(view.bars) < 2:
        return None
    return view.bars[-2].close


def _pct_from_prev(price: float, prev_close: float | None) -> float | None:
    if not prev_close:
        return None
    return price / prev_close - 1
