# -*- coding: utf-8 -*-
"""高抛低吸助手 · 统一风控引擎（P0 核心新建件）

定位（与既有能力的分工）：
- 既有 ``src/services/portfolio_risk_service.py`` 负责**组合级**风险（集中度/回撤/止损接近度）；
- 本引擎负责**单标的级**风险：止损位、分批止盈计划、仓位建议；
- 两者**并行使用**，不重复造。

设计原则：
1. **纯函数 / 无 IO**：不碰数据库、不联网，输入输出皆为 dataclass，便于单测与回测复用；
2. **规则可配**：所有阈值来自 ``config/rules/risk_rules.yaml``（经 ``rule_config`` 加载）；
3. **只给建议、不下单**（NG6）。

需求依据：FR-9（止损/分批止盈/仓位）、FR-9.5、FR-18。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.core.indicators import IndicatorSnapshot
from src.core.rule_config import RiskRuleSet

__all__ = [
    "PositionView",
    "RiskDecision",
    "RiskEngine",
]


def _finite(*values: Optional[float]) -> List[float]:
    return [float(v) for v in values if v is not None and math.isfinite(float(v))]


def _round(value: float, digits: int = 4) -> float:
    return round(float(value), digits) if math.isfinite(float(value)) else float("nan")


# --------------------------------------------------------------------------- #
# 输入 / 输出
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PositionView:
    """单标的持仓视图（由 ``portfolio_*`` 组装，或由配置给出）。"""

    symbol: str
    avg_cost: float = 0.0
    quantity: float = 0.0
    position_ratio: float = 0.0        # 该标的当前占总仓比例
    peak_price: float = 0.0            # 持有期最高价（移动止盈用）
    market: str = "cn"

    def has_position(self) -> bool:
        return self.quantity > 0 and self.avg_cost > 0


@dataclass
class RiskDecision:
    """风控结论。字段可直接映射到 ``decision_signals``。"""

    stop_loss: float = float("nan")
    take_profit_plan: List[Dict[str, Any]] = field(default_factory=list)
    position_advice: Dict[str, Any] = field(default_factory=dict)
    is_urgent: bool = False
    reasons: List[str] = field(default_factory=list)

    def action(self) -> str:
        return str(self.position_advice.get("action") or "hold")

    def action_ratio(self) -> float:
        try:
            return float(self.position_advice.get("ratio") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def to_metadata(self) -> Dict[str, Any]:
        """供写入 ``decision_signals.metadata_json`` 的紧凑结构。"""
        return {
            "position_advice": self.position_advice,
            "take_profit_plan": self.take_profit_plan,
            "risk_reasons": self.reasons,
        }


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #
class RiskEngine:
    """单标的级风控：止损 / 分批止盈 / 仓位。"""

    def __init__(self, rules: Optional[RiskRuleSet] = None):
        self.rules = rules or RiskRuleSet()

    # ---------------- 止损 ---------------- #
    def compute_stop_loss(
        self,
        snapshot: IndicatorSnapshot,
        position: Optional[PositionView] = None,
    ) -> float:
        """技术位止损为主，可叠加固定百分比；支持"缓冲 + ATR 自适应"放宽。

        口径（mode=technical 时）：
        1. 取现价**下方最近的均线**为技术位；若无，退化为结构前低 ``low_20``；
        2. 技术位再向下留 ``buffer_pct`` 缓冲（避免下影线扫损）；
        3. 若 ``atr_mult > 0``：与 ``close − atr_mult × ATR`` 取**更宽（更低）**者，
           使高波动标的的止损随波动率自适应，避免被正常波动震出。
        """
        rules = self.rules.stop_loss
        close = snapshot.close
        technical: Optional[float] = None
        percent: Optional[float] = None

        ma_below = [snapshot.ma.get(p) for p in rules.ma]
        ma_below = _finite(*ma_below)
        ma_below = [v for v in ma_below if v < close]
        if ma_below:
            technical = max(ma_below)  # 距价格最近（最紧）的技术位
        elif math.isfinite(snapshot.low_20):
            technical = float(snapshot.low_20)  # 退化为结构前低

        # 技术位下方留缓冲
        if technical is not None and math.isfinite(technical) and rules.buffer_pct > 0:
            technical = technical * (1.0 - rules.buffer_pct)

        # ATR 自适应参考止损（仅在 atr_mult > 0 时启用）
        atr_stop: Optional[float] = None
        if rules.atr_mult > 0:
            atr_value = float(getattr(snapshot, "atr", float("nan")))
            if math.isfinite(atr_value) and atr_value > 0 and math.isfinite(close):
                atr_stop = close - float(rules.atr_mult) * atr_value

        base_for_percent = (
            position.avg_cost if position is not None and position.has_position() else close
        )
        if math.isfinite(base_for_percent) and base_for_percent > 0:
            percent = base_for_percent * (1.0 - rules.percent)

        mode = (rules.mode or "technical").lower()
        if mode == "percent":
            stop = percent
        elif mode == "atr":
            stop = atr_stop if atr_stop is not None else technical
            if stop is None:
                stop = percent
        elif mode == "either":
            candidates = _finite(technical, percent)
            stop = max(candidates) if candidates else float("nan")
        else:  # technical
            stop = technical if technical is not None else percent

        # 自适应放宽：technical / atr 模式下滑动至更宽的一侧（不贴脸）
        if atr_stop is not None and mode in {"technical", "atr"} and stop is not None and math.isfinite(stop):
            stop = min(stop, atr_stop)

        if stop is None or not math.isfinite(stop):
            return float("nan")

        # 保本：浮盈达标后止损上移至成本（FR-9.5）
        if position is not None and position.has_position():
            gain = (close - position.avg_cost) / position.avg_cost
            if gain >= self.rules.take_profit.move_stop_to_cost_at:
                stop = max(stop, position.avg_cost)
        return _round(min(stop, close))

    # ---------------- 分批止盈 ---------------- #
    def compute_take_profit_plan(
        self,
        snapshot: IndicatorSnapshot,
        position: PositionView,
    ) -> List[Dict[str, Any]]:
        """三层分批止盈：目标价 / 破线 / 移动止盈（FR-9.5）。"""
        rules = self.rules.take_profit
        if not position.has_position():
            return []

        close = snapshot.close
        cost = position.avg_cost
        # 破线档位用于"保护浮盈"，但对**亏损持仓**其语义是减仓止损，不能叫"止盈"（FR-18 人话结论）。
        in_profit = bool(math.isfinite(close) and cost > 0 and close >= cost)
        break_verb = "止盈" if in_profit else "减仓"
        plan: List[Dict[str, Any]] = []

        for target, ratio in zip(rules.targets, _cycle_pad(rules.target_ratios, len(rules.targets))):
            price = _round(cost * (1.0 + target))
            plan.append(
                {
                    "kind": "target",
                    "trigger": f"+{target * 100:.0f}%",
                    "price": price,
                    "ratio": _round(ratio),
                    "reason": f"浮盈达到 {target * 100:.0f}%，止盈 {ratio * 100:.0f}%",
                    "triggered": bool(math.isfinite(close) and close >= price),
                }
            )

        for period, ratio in zip(rules.ma_break, _cycle_pad(rules.ma_break_ratios, len(rules.ma_break))):
            ma_value = snapshot.ma.get(period)
            if ma_value is None or not math.isfinite(ma_value):
                continue
            plan.append(
                {
                    "kind": "ma_break",
                    "trigger": f"破 MA{period}",
                    "price": _round(ma_value),
                    "ratio": _round(ratio),
                    "reason": f"跌破 MA{period}，{break_verb} {ratio * 100:.0f}%",
                    "triggered": bool(math.isfinite(close) and close < ma_value),
                }
            )

        trail = rules.trail
        if math.isfinite(close) and cost > 0:
            gain = (close - cost) / cost
            peak = position.peak_price if position.peak_price > 0 else close
            drawdown = (peak - close) / peak if peak > 0 else 0.0
            if gain >= trail.activate:
                plan.append(
                    {
                        "kind": "trail",
                        "trigger": f"回撤 {trail.drawdown * 100:.0f}%",
                        "price": _round(peak * (1.0 - trail.drawdown)),
                        "ratio": 1.0,
                        "reason": "浮盈超过阈值后启动移动止盈",
                        "triggered": bool(drawdown >= trail.drawdown),
                    }
                )
        return plan

    # ---------------- 仓位 ---------------- #
    def position_sizing(
        self,
        kind: str,
        held_ratio: float = 0.0,
    ) -> Dict[str, Any]:
        """给出建议仓位比例。kind ∈ {build, add, dip_buy}。"""
        rules = self.rules.position
        held = max(0.0, float(held_ratio or 0.0))
        room = max(0.0, rules.cap - held)

        if kind == "build":
            ratio = min(rules.build_max, room)
            batches = max(1, int(rules.build_batches))
            return {
                "action": "buy",
                "ratio": _round(ratio),
                "per_batch": _round(ratio / batches),
                "batches": batches,
                "reason": f"首次建仓 ≤ {rules.build_max * 100:.0f}%，分 {batches} 批",
            }
        if kind == "add":
            rung = self._next_ladder_rung(held)
            ratio = min(rung, room)
            return {
                "action": "add",
                "ratio": _round(ratio),
                "reason": f"金字塔加仓一档 {rung * 100:.0f}%（越涨加得越少）",
            }
        if kind == "dip_buy":
            ratio = min(rules.dip_buy_max, room)
            batches = max(1, int(rules.dip_buy_batches))
            return {
                "action": "buy",
                "ratio": _round(ratio),
                "per_batch": _round(ratio / batches),
                "batches": batches,
                "reason": f"抄底单笔 ≤ {rules.dip_buy_max * 100:.0f}%，止跌确认后分批",
            }
        return {"action": "hold", "ratio": 0.0, "reason": f"未知建仓类型：{kind}"}

    def _next_ladder_rung(self, held_ratio: float) -> float:
        ladder = self.rules.position.add_ladder or (0.30,)
        cumulative = 0.0
        for rung in ladder:
            cumulative += float(rung)
            if held_ratio < cumulative:
                return float(rung)
        return float(ladder[-1])

    def total_position_room(self, held_ratio: float = 0.0) -> float:
        return max(0.0, self.rules.position.cap - max(0.0, float(held_ratio or 0.0)))

    # ---------------- 组合入口 ---------------- #
    def evaluate_hold(
        self,
        snapshot: IndicatorSnapshot,
        position: PositionView,
    ) -> RiskDecision:
        """持仓风控：止损优先 → 分批止盈 → 破线减仓 → 防守。"""
        decision = RiskDecision()
        decision.stop_loss = self.compute_stop_loss(snapshot, position)
        decision.take_profit_plan = self.compute_take_profit_plan(snapshot, position)

        close = snapshot.close
        ma20 = snapshot.ma.get(20)

        # 1) 硬止损（最高优先级，FR-9 / FR-18）
        if math.isfinite(decision.stop_loss) and math.isfinite(close) and close <= decision.stop_loss:
            decision.is_urgent = self.rules.urgency.stop_loss_breach
            decision.position_advice = {
                "action": "sell",
                "ratio": 1.0,
                "reason": f"现价 {close:.2f} 已跌破止损位 {decision.stop_loss:.2f}，无条件离场",
            }
            decision.reasons.append("trigger:stop_loss")
            return decision

        # 2) 分批止盈（有触发的档位）
        hit_tiers = [tier for tier in decision.take_profit_plan if tier.get("triggered")]
        if hit_tiers:
            tier = hit_tiers[0]
            decision.position_advice = {
                "action": "reduce",
                "ratio": float(tier.get("ratio") or 0.0),
                "reason": str(tier.get("reason")),
            }
            decision.reasons.append(f"trigger:take_profit:{tier.get('kind')}")
            return decision

        # 3) 有效跌破 MA20 → 趋势转弱
        if (
            math.isfinite(close)
            and ma20 is not None
            and math.isfinite(ma20)
            and close < ma20
        ):
            # 注意：破线止盈档位（take_profit.ma_break）若含 20，会在此前的分支 2 提前返回；
            # 本分支是为「配置未覆盖 MA20」时兜底，复用按破位均线分级的减仓建议（FR-4.2）。
            decision.position_advice = self._ma_break_advice(snapshot)
            decision.is_urgent = self.rules.urgency.ma20_breach and close < ma20
            decision.reasons.append("trigger:ma20_break")
            return decision

        # 4) 过热 / 死叉 → 轻度防守
        if math.isfinite(snapshot.bias.get(12, float("nan"))) and snapshot.bias.get(12, 0.0) >= 10.0:
            decision.position_advice = {
                "action": "reduce",
                "ratio": 0.20,
                "reason": "BIAS 过热，建议轻度减仓回避回调",
            }
            decision.reasons.append("trigger:bias_overheat")
            return decision
        if snapshot.macd_dead_cross():
            decision.position_advice = {
                "action": "reduce",
                "ratio": 0.20,
                "reason": "MACD 死叉，建议轻度减仓",
            }
            decision.reasons.append("trigger:macd_dead_cross")
            return decision

        decision.position_advice = {"action": "hold", "ratio": 0.0, "reason": "无风控触发，继续持有"}
        return decision

    def _ma_break_advice(self, snapshot: IndicatorSnapshot) -> Dict[str, Any]:
        """按破位均线给分级减仓建议（FR-4.2）。"""
        close = snapshot.close
        for period, ratio in ((5, 0.20), (10, 0.30), (20, 0.50)):
            ma_value = snapshot.ma.get(period)
            if ma_value is not None and math.isfinite(ma_value) and close < ma_value:
                return {
                    "action": "reduce",
                    "ratio": ratio,
                    "reason": f"跌破 MA{period}，建议减仓 {ratio * 100:.0f}%",
                }
        return {"action": "hold", "ratio": 0.0, "reason": "均线未破位"}

    def evaluate_entry(
        self,
        snapshot: IndicatorSnapshot,
        *,
        kind: str,
        held_ratio: float = 0.0,
        position: Optional[PositionView] = None,
    ) -> RiskDecision:
        """买入类建议：**强制附带止损位**（FR-9）。"""
        decision = RiskDecision()
        decision.stop_loss = self.compute_stop_loss(snapshot, position)
        decision.position_advice = self.position_sizing(kind, held_ratio=held_ratio)
        decision.reasons.append(f"entry:{kind}")
        if not math.isfinite(decision.stop_loss):
            decision.reasons.append("warn:no_stop_loss_reference")
        return decision

    # 语义化别名（与 P0 详设文档接口一致）
    def evaluate_build(self, snapshot: IndicatorSnapshot, *, held_ratio: float = 0.0) -> RiskDecision:
        return self.evaluate_entry(snapshot, kind="build", held_ratio=held_ratio)

    def evaluate_add(self, snapshot: IndicatorSnapshot, *, held_ratio: float = 0.0) -> RiskDecision:
        return self.evaluate_entry(snapshot, kind="add", held_ratio=held_ratio)

    def evaluate_dip_buy(self, snapshot: IndicatorSnapshot, *, held_ratio: float = 0.0) -> RiskDecision:
        return self.evaluate_entry(snapshot, kind="dip_buy", held_ratio=held_ratio)


def _cycle_pad(values: tuple, length: int) -> List[float]:
    """把比例列表补齐/截断到与目标数一致（配置容错）。"""
    items = [float(v) for v in values if isinstance(v, (int, float))]
    if not items:
        items = [1.0 / max(1, length)]
    if len(items) >= length:
        return items[:length]
    padded = list(items)
    while len(padded) < length:
        padded.append(items[-1])
    return padded
