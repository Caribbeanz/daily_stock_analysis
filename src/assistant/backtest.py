# -*- coding: utf-8 -*-
"""条件计划回合模拟（需求 §9.4.2 的「回合」口径）。

需求 §9.4.4 点名的关键缺口是：现有 `src/core/backtest_engine.py` 是 **Long-only 后验
评估**（给定建议看未来 N 日是否命中），**不模拟「条件单被触发 → 按股数成交 → 按出场规则
平仓」的完整回合**，因此它的 `win_rate_pct` **不能**当本方案的胜率用。

本模块只补这段模拟，做成**纯函数**：输入是「某日某个条件计划分支」与「该标的的日线」，
输出是回合与统计。计划从哪来（历史快照 / 现场回放）由调用方决定，模块本身不碰网络、
不碰数据库，便于逐条单测。

## 口径（都对着需求原文）

- **回合**＝触发成交 → 按出场规则平仓；**未闭合单列、不计入分母**（§9.4.2）。
- **保守假设**（§9.4.3）：同一天既触及买入又触及止损时，**先按止损算**；
  跳空越过触发价时按**开盘价**成交但不追单（报价已过就按开盘价吃，不假设更优价格）。
- **T+1**：买入当日不可卖，最早次日出场（§9.4.3 交易约束）。
- **成本**：买卖各收 `cost_bps`（默认 3bp 单边）；分红不计入（本模块不看分红数据，
  宁可少算收益也不虚构分红）。
- **样本充足度**：已闭合回合 < `min_closed_rounds`（默认 30，与仓库 Outcome 门槛同量级）时
  **只报计数、不报胜率**（§9.4.3）。
- **分阶段/分标的**必须给（§9.4.2 硬要求）：整体胜率不得单独作为通过依据。

## 本模块不做的

做 T 分支、分级止损的多档减仓、部分成交、停牌与涨跌停无法成交的判定、分红再投、
组合层仓位联动——这些需要更细的成交模型，属后续扩展；**缺的项会在 `notes` 里写明**，
不假装做过。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from src.assistant.domain import Bar

__all__ = [
    "PlanRound",
    "RoundConfig",
    "pending_plan_rounds",
    "simulate_rounds",
    "summarize_rounds",
]

logger = logging.getLogger(__name__)

#: 买入方向的动作（其余按卖出处理）；与 enums.Action 的字面量一致
_BUY_ACTIONS = frozenset({"买入", "加仓", "试探建仓", "T仓买入"})
_SELL_ACTIONS = frozenset({"减仓", "清仓", "T仓卖出"})


@dataclass(frozen=True)
class RoundConfig:
    """回合模拟参数。"""

    #: 单边成本（基点）；买卖各收一次
    cost_bps: float = 3.0
    #: 触发后最长持有天数（未触及出场规则时按最后收盘强平，标 ``expired``）
    max_hold_days: int = 60
    #: 已闭合回合数下限；不足只报计数不报胜率
    min_closed_rounds: int = 30
    #: 同一分支的有效天数缺省值（分支自带 ``valid_days`` 时以分支为准）
    default_valid_days: int = 1

    def __post_init__(self) -> None:
        if self.cost_bps < 0:
            raise ValueError("cost_bps 不能为负")
        if self.max_hold_days < 1:
            raise ValueError("max_hold_days 至少为 1")


@dataclass
class PlanRound:
    """一个回合（可能未闭合）。"""

    code: str
    branch_id: str
    scenario: str = ""
    stage: str = ""
    action: str = ""
    shares: int = 0
    entry_date: date | None = None
    entry_price: float | None = None
    exit_date: date | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    days_held: int = 0
    gross_pnl: float = 0.0
    cost: float = 0.0
    net_pnl: float = 0.0
    closed: bool = False
    note: str = ""

    @property
    def win(self) -> bool:
        return self.closed and self.net_pnl > 0


def _trigger_hit(branch: Mapping[str, Any], bar: Bar) -> float | None:
    """判断当日是否触发；返回成交价，未触发返回 ``None``。

    跳空处理：``price_lte`` 下若开盘已低于触发价，按开盘价成交（不假设能拿到更好的价，
    也不追单）；``price_gte`` 对称。
    """
    kind = str(branch.get("trigger_type") or "")
    price = branch.get("trigger_price")
    pct = branch.get("pct_change")
    if kind == "price_lte" and isinstance(price, (int, float)) and price > 0:
        if bar.low <= price:
            return min(price, bar.open) if bar.open > 0 else price
        return None
    if kind == "price_gte" and isinstance(price, (int, float)) and price > 0:
        if bar.high >= price:
            return max(price, bar.open) if bar.open > 0 else price
        return None
    if kind in ("pct_lte", "pct_gte") and isinstance(pct, (int, float)) and pct:
        return None  # 涨跌幅触发需要前收盘，由调用方折算成价格后再传 price_*
    return None


def _exit_on(bar: Bar, *, stop_loss: float | None, take_profit: float | None) -> tuple[float, str] | None:
    """当日出场判定；同日同时触及止损与止盈时**先按止损**（保守，§9.4.3）。"""
    if stop_loss is not None and bar.low <= stop_loss:
        return stop_loss, "stop_loss"
    if take_profit is not None and bar.high >= take_profit:
        return take_profit, "take_profit"
    return None


def simulate_rounds(
    plans: Iterable[Mapping[str, Any]],
    bars_by_code: Mapping[str, Sequence[Bar]],
    *,
    config: RoundConfig | None = None,
) -> list[PlanRound]:
    """把「日级条件计划分支」模拟成回合。

    ``plans`` 每项至少要有：``code`` / ``branch_id`` / ``date``（该计划适用的交易日）/
    ``trigger_type`` / ``action``；可选 ``trigger_price`` / ``shares`` / ``valid_days`` /
    ``stop_loss`` / ``take_profit`` / ``scenario`` / ``stage``。

    **只买不卖**：买入类分支生成回合，卖出类分支不建模（它不是回合的起点），
    在笔记里标注跳过——宁可少算，也不要造一个口径不明的「卖出回合」。
    """
    cfg = config or RoundConfig()
    rounds: list[PlanRound] = []
    for plan in plans:
        code = str(plan.get("code") or "")
        bars = list(bars_by_code.get(code) or ())
        if not code or not bars:
            continue
        action = str(plan.get("action") or "")
        if action not in _BUY_ACTIONS:
            rounds.append(
                PlanRound(
                    code=code,
                    branch_id=str(plan.get("branch_id") or ""),
                    scenario=str(plan.get("scenario") or ""),
                    stage=str(plan.get("stage") or ""),
                    action=action,
                    closed=False,
                    note=f"卖出类分支不生成回合（action={action}）",
                )
            )
            continue

        plan_date = plan.get("date")
        if not isinstance(plan_date, date):
            continue
        valid_days = int(plan.get("valid_days") or cfg.default_valid_days)
        window = [bar for bar in bars if bar.trade_date > plan_date][: max(1, valid_days)]
        trigger_price = None
        entry_bar = None
        for bar in window:
            filled = _trigger_hit(plan, bar)
            if filled is not None:
                trigger_price, entry_bar = filled, bar
                break
        shares = int(plan.get("shares") or 0)
        if entry_bar is None:
            rounds.append(
                PlanRound(
                    code=code,
                    branch_id=str(plan.get("branch_id") or ""),
                    scenario=str(plan.get("scenario") or ""),
                    stage=str(plan.get("stage") or ""),
                    action=action,
                    shares=shares,
                    closed=False,
                    note="有效期内未触发",
                )
            )
            continue

        stop_loss = plan.get("stop_loss")
        take_profit = plan.get("take_profit")
        stop_loss = float(stop_loss) if isinstance(stop_loss, (int, float)) else None
        take_profit = float(take_profit) if isinstance(take_profit, (int, float)) else None

        after_entry = [bar for bar in bars if bar.trade_date > entry_bar.trade_date]
        exit_price: float | None = None
        exit_date: date | None = None
        exit_reason = ""
        for bar in after_entry[: cfg.max_hold_days]:  # T+1：买入当日不可卖
            hit = _exit_on(bar, stop_loss=stop_loss, take_profit=take_profit)
            if hit is not None:
                exit_price, exit_reason = hit
                exit_date = bar.trade_date
                break
        if exit_price is None and after_entry:
            last = after_entry[min(len(after_entry), cfg.max_hold_days) - 1]
            exit_price, exit_date, exit_reason = last.close, last.trade_date, "max_hold"

        gross = 0.0
        cost = 0.0
        if shares and entry_bar.close > 0:
            entry_value = trigger_price * shares
            gross = (exit_price - trigger_price) * shares if exit_price is not None else 0.0
            cost = entry_value * cfg.cost_bps / 10000.0
            if exit_price is not None:
                cost += exit_price * shares * cfg.cost_bps / 10000.0
        rounds.append(
            PlanRound(
                code=code,
                branch_id=str(plan.get("branch_id") or ""),
                scenario=str(plan.get("scenario") or ""),
                stage=str(plan.get("stage") or ""),
                action=action,
                shares=shares,
                entry_date=entry_bar.trade_date,
                entry_price=trigger_price,
                exit_date=exit_date,
                exit_price=exit_price,
                exit_reason=exit_reason,
                days_held=(exit_date - entry_bar.trade_date).days if exit_date else 0,
                gross_pnl=gross,
                cost=cost,
                net_pnl=gross - cost,
                closed=exit_price is not None,
                note="" if shares else "分支股数为 0，仅记录触发",
            )
        )
    return rounds


def _stats(rounds: Sequence[PlanRound]) -> dict[str, Any]:
    closed = [row for row in rounds if row.closed and row.shares]
    wins = [row for row in closed if row.net_pnl > 0]
    losses = [row for row in closed if row.net_pnl <= 0]
    avg_win = sum(row.net_pnl for row in wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(row.net_pnl for row in losses) / len(losses)) if losses else 0.0
    return {
        "rounds": len(rounds),
        "closed": len(closed),
        "open": len(rounds) - len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "payoff": round(avg_win / avg_loss, 2) if avg_loss else None,
        "avg_days": round(sum(row.days_held for row in closed) / len(closed), 1) if closed else None,
        "net_pnl": round(sum(row.net_pnl for row in closed), 2),
    }


def summarize_rounds(
    rounds: Sequence[PlanRound],
    *,
    config: RoundConfig | None = None,
) -> dict[str, Any]:
    """出统计：胜率只在样本够时给，且必须同时给分阶段/分标的（§9.4.2）。"""
    cfg = config or RoundConfig()
    overall = _stats(rounds)
    enough = overall["closed"] >= cfg.min_closed_rounds
    by_stage: dict[str, dict[str, Any]] = {}
    by_code: dict[str, dict[str, Any]] = {}
    for field_name, bucket in (("stage", by_stage), ("code", by_code)):
        keys = sorted({getattr(row, field_name) or "—" for row in rounds})
        for key in keys:
            subset = [row for row in rounds if (getattr(row, field_name) or "—") == key]
            stats = _stats(subset)
            # 单桶样本不足只报计数，不报胜率（§9.4.2/§9.4.3 同口径）
            if stats["closed"] >= cfg.min_closed_rounds:
                stats["win_rate"] = round(stats["wins"] / stats["closed"], 4)
            else:
                stats["win_rate"] = None
            bucket[key] = stats
    if enough:
        overall["win_rate"] = round(overall["wins"] / overall["closed"], 4)
    else:
        overall["win_rate"] = None
        overall["note"] = (
            f"已闭合回合 {overall['closed']} < {cfg.min_closed_rounds}，"
            "样本不足，本次只报计数不报胜率（需求 §9.4.3）"
        )
    overall["sample_sufficient"] = enough
    overall["by_stage"] = by_stage
    overall["by_code"] = by_code
    overall["not_modelled"] = [
        "做 T 分支与分级止损多档减仓",
        "部分成交与停牌/涨跌停无法成交",
        "分红再投与组合层仓位联动",
    ]
    return overall


def pending_plan_rounds(rounds: Sequence[PlanRound]) -> list[PlanRound]:
    """未闭合回合单列（§9.4.2：不计入胜率分母）。"""
    return [row for row in rounds if not row.closed]
