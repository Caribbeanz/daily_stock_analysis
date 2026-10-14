# -*- coding: utf-8 -*-
"""高抛低吸助手 · 写操作执行门面（P2.2 / FR-14.3.3、FR-14.2.6）

**单一写点（决议 D3）**：对话 / H5 / 飞书 的一切写操作都必须经过本模块，
不允许任何旁路写口。流程为四段式：

``解析 → 预览(plan) → 二次确认(由上层令牌把关) → 执行(execute) → 逐条回执``

白名单（决议 D2，**不外扩**）：

============  ==========================  ==============
op            动作                        复用服务
============  ==========================  ==============
watchlist_add 加入自选                    WatchlistService
watchlist_remove 移出自选                 WatchlistService
set_position  设置/覆盖持仓               PositionService
remove_position 清空持仓                  PositionService
set_stop_loss 设置单标的止损（覆盖）      RuleOverrideService
remove_stop_loss 移除止损覆盖             RuleOverrideService
set_weight    调四因子权重（FR-7）        RuleOverrideService
============  ==========================  ==============

**明确拒绝**：清空全部（``clear_all``）、仓位比例（``set_position_ratio``，
不在白名单）、未识别写指令、以及任何"直接下单"类请求（FR-14.3.9）。

设计约束：

- ``execute`` 必须显式 ``confirmed=True``（由上层令牌校验后传入），
  未确认一律拒绝——"无确认不落库"是硬约束（FR-14.2.6 / 决议 D2）；
- 逐条独立：一条失败不影响其余（复合指令逐条回执）；
- 审计：每次执行通过可注入 ``audit_sink`` 落审计（P2.4 接入持久化与查询）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from src.agent.intent.resolver import WriteOp
from src.core.rule_config import FACTOR_KEYS, FACTOR_LABELS

logger = logging.getLogger(__name__)

__all__ = [
    "ActionRejected",
    "ActionItem",
    "ActionPlan",
    "ActionService",
    "describe_ops",
]

AuditSink = Callable[[Dict[str, Any]], None]

_WHITELIST_HINT = "当前仅支持：增/删自选股、设置/清空持仓、设置止损、调四因子权重"

# 白名单内 op → 人话动作名
OP_LABELS: Dict[str, str] = {
    "watchlist_add": "加入自选股",
    "watchlist_remove": "移出自选股",
    "set_position": "设置持仓",
    "remove_position": "清空持仓",
    "set_stop_loss": "设置止损",
    "remove_stop_loss": "移除止损覆盖",
    "set_weight": "调整四因子权重",
}

# 明确拒绝（白名单外）→ 拒绝原因
REJECTED_OPS: Dict[str, str] = {
    "clear_all": "「清空全部」不在白名单内（风险过高，需手工处理）",
    "set_position_ratio": "「仓位比例」不在白名单内（配额由风控规则统一给定）",
    "unknown_write": "未能识别具体的写指令，请说明「要改什么、改成多少」",
    "place_order": "直接下单/委托交易被禁止，本助手只做分析与提醒（FR-14.3.9）",
}


class ActionRejected(Exception):
    """写操作被拒（白名单外 / 参数非法 / 未经确认）。"""


def _fmt_price(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{number:.2f}" if abs(number) >= 1 else f"{number:.3f}"


def _fmt_weights(weights: Dict[str, float]) -> str:
    return " / ".join(f"{FACTOR_LABELS.get(k, k)} {weights.get(k, 0) * 100:.0f}%" for k in FACTOR_KEYS)


@dataclass
class ActionItem:
    """单条写操作的预览 / 执行结果。"""

    op: str
    label: str = ""
    description: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    valid: bool = True
    error: str = ""
    before: Any = None
    after: Any = None
    status: str = ""  # 执行后：ok / rejected / failed
    receipt: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "op": self.op,
            "label": self.label,
            "description": self.description,
            "params": dict(self.params),
            "valid": self.valid,
            "error": self.error,
            "before": self.before,
            "after": self.after,
            "status": self.status,
            "receipt": self.receipt,
        }


@dataclass
class ActionPlan:
    """一条消息的全部写操作（逐条可预览 / 可执行）。"""

    items: List[ActionItem] = field(default_factory=list)
    token: str = ""
    requested: int = 0

    @property
    def valid(self) -> bool:
        return all(item.valid for item in self.items) and bool(self.items)

    @property
    def executable(self) -> List[ActionItem]:
        return [item for item in self.items if item.valid]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "valid": self.valid,
            "requested": self.requested,
            "token": self.token,
        }

    def summary(self) -> str:
        """确认卡用的"将要做什么"清单（逐条）。"""
        lines: List[str] = []
        for index, item in enumerate(self.items, 1):
            if item.valid:
                lines.append(f"{index}. {item.description}")
            else:
                lines.append(f"{index}. ⛔ {item.description or item.op}：{item.error}")
        return "\n".join(lines)

    def receipt(self) -> str:
        """执行回执（逐条"做了什么 + 当前状态"）。"""
        lines: List[str] = []
        for index, item in enumerate(self.items, 1):
            if item.status == "ok":
                lines.append(f"{index}. {item.receipt}")
            elif item.status == "rejected":
                lines.append(f"{index}. ⛔ {item.receipt}")
            else:
                lines.append(f"{index}. ⚠️ 执行失败：{item.receipt or item.error or '未知错误'}")
        return "\n".join(lines)


def describe_ops(ops: Sequence[WriteOp]) -> List[str]:
    """把写指令渲染成人话（供对话回执 / 确认卡，不区分是否可执行）。"""
    descriptions: List[str] = []
    for op in ops:
        params = op.params or {}
        if op.op == "watchlist_add":
            descriptions.append(f"把 {params.get('symbol', '?')} 加入自选股")
        elif op.op == "watchlist_remove":
            descriptions.append(f"把 {params.get('symbol', '?')} 移出自选股")
        elif op.op == "set_position":
            descriptions.append(
                f"把 {params.get('symbol', '?')} 持仓设为 成本 {_fmt_price(params.get('cost'))}"
                f" × {params.get('quantity', '?')} 股"
            )
        elif op.op == "remove_position":
            descriptions.append(f"清空 {params.get('symbol', '?')} 持仓")
        elif op.op == "set_stop_loss":
            descriptions.append(f"把 {params.get('symbol', '?')} 止损设为 {_fmt_price(params.get('value'))}")
        elif op.op == "set_weight":
            descriptions.append(
                f"把 {FACTOR_LABELS.get(str(params.get('factor')), params.get('factor', '?'))} 权重"
                f"调整为 {float(params.get('value') or 0) * 100:.0f}%"
            )
        else:
            descriptions.append(REJECTED_OPS.get(op.op, f"未识别指令：{op.op}"))
    return descriptions


class ActionService:
    """写操作单一入口：预览 → 确认 → 执行 → 回执（+ 审计）。"""

    def __init__(
        self,
        *,
        watchlist_service: Any = None,
        position_service: Any = None,
        rule_override_service: Any = None,
        audit_sink: Optional[AuditSink] = None,
    ) -> None:
        self._watchlist = watchlist_service
        self._position = position_service
        self._rule_override = rule_override_service
        self._audit_sink = audit_sink

    # ---------------- 依赖（惰性，便于离线单测注入假实现） ---------------- #
    @property
    def watchlist(self):
        if self._watchlist is None:
            from src.services.watchlist_service import WatchlistService

            self._watchlist = WatchlistService()
        return self._watchlist

    @property
    def position(self):
        if self._position is None:
            from src.services.position_service import PositionService

            self._position = PositionService()
        return self._position

    @property
    def rule_override(self):
        if self._rule_override is None:
            from src.services.rule_override_service import RuleOverrideService

            self._rule_override = RuleOverrideService()
        return self._rule_override

    # ---------------- 预览 ---------------- #
    def plan(self, ops: Sequence[WriteOp]) -> ActionPlan:
        """把写指令转成可确认的预览计划（**不落库**）。

        ``context`` 在整条消息内共享，使复合指令（如多条调权重）能**顺序叠加**，
        预览即等于最终结果，避免"确认卡显示的值与落库值不一致"。
        """
        plan = ActionPlan(requested=len(list(ops)))
        context: Dict[str, Any] = {}
        for op in ops:
            plan.items.append(self._plan_item(op, context))
        return plan

    def _plan_item(self, op: WriteOp, context: Dict[str, Any]) -> ActionItem:
        name = str(getattr(op, "op", "") or "")
        params = dict(getattr(op, "params", {}) or {})
        label = OP_LABELS.get(name, name or "未知指令")

        if name in REJECTED_OPS:
            return ActionItem(
                op=name,
                label=label,
                description=REJECTED_OPS[name],
                params=params,
                valid=False,
                error=REJECTED_OPS[name],
            )
        planner = getattr(self, f"_plan_{name}", None)
        if planner is None:
            error = f"未支持的写操作「{name or '空'}」；{_WHITELIST_HINT}"
            return ActionItem(op=name, label=label, description=error, params=params, valid=False, error=error)

        try:
            return planner(params, context)
        except Exception as exc:  # 依赖 / 参数异常统一转为"不可执行"，不抛给用户
            logger.warning("写操作预览失败（op=%s）：%s", name, exc)
            return ActionItem(
                op=name,
                label=label,
                description=f"{label}：{exc}",
                params=params,
                valid=False,
                error=str(exc),
            )

    # ---------------- 执行 ---------------- #
    def execute(
        self,
        ops: Sequence[WriteOp],
        *,
        actor: str = "",
        source: str = "",
        confirmed: bool = False,
    ) -> ActionPlan:
        """执行写操作（**必须 confirmed=True**，即已通过二次确认）。

        Returns:
            ActionPlan（``items[].status`` ∈ ok / rejected / failed，``receipt`` 为人话回执）。
        """
        if not confirmed:
            raise ActionRejected("写操作需二次确认后才可执行（FR-14.2.6 / 决议 D2）")

        plan = self.plan(ops)
        for item in plan.items:
            if not item.valid:
                item.status = "rejected"
                item.receipt = item.error or item.description
                self._audit(item, actor=actor, source=source, confirmed=True, status="rejected")
                continue
            try:
                receipt = self._apply(item)
                item.status = "ok"
                item.receipt = receipt
            except Exception as exc:  # 单条失败不影响其余（复合指令逐条回执）
                logger.warning("写操作执行失败（op=%s）：%s", item.op, exc)
                item.status = "failed"
                item.receipt = str(exc)
            self._audit(item, actor=actor, source=source, confirmed=True, status=item.status)
        return plan

    def execute_token_plan(self, plan: ActionPlan, *, actor: str = "", source: str = "") -> ActionPlan:
        """执行一个**已确认**的预览计划（保留原 before/after，避免二次读取漂移）。"""
        for item in plan.items:
            if not item.valid:
                item.status = "rejected"
                item.receipt = item.error or item.description
                self._audit(item, actor=actor, source=source, confirmed=True, status="rejected")
                continue
            try:
                item.receipt = self._apply(item)
                item.status = "ok"
            except Exception as exc:
                logger.warning("写操作执行失败（op=%s）：%s", item.op, exc)
                item.status = "failed"
                item.receipt = str(exc)
            self._audit(item, actor=actor, source=source, confirmed=True, status=item.status)
        return plan

    def _apply(self, item: ActionItem) -> str:
        applier = getattr(self, f"_apply_{item.op}", None)
        if applier is None:  # pragma: no cover - 与白名单保持一致，理论上不可达
            raise ActionRejected(f"未支持的写操作「{item.op}」")
        return applier(item)

    # ---------------- 审计 ---------------- #
    def _audit(
        self,
        item: ActionItem,
        *,
        actor: str,
        source: str,
        confirmed: bool,
        status: str,
    ) -> None:
        record = {
            "action": item.op,
            "target": item.params.get("symbol") or item.params.get("factor") or "",
            "before": item.before,
            "after": item.after,
            "actor": actor,
            "source": source,
            "confirmed": confirmed,
            "status": status,
            "description": item.description,
        }
        if self._audit_sink is not None:
            try:
                self._audit_sink(record)
            except Exception as exc:  # 审计失败不影响已完成的写操作
                logger.warning("审计写入失败：%s", exc)

    # ======================================================================= #
    # 白名单各动作：_plan_<op>（只读预览） + _apply_<op>（落库）
    # ======================================================================= #
    # ---- 自选股 ---- #
    def _plan_watchlist_add(self, params: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> ActionItem:
        symbol = str(params.get("symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("必须指定标的代码")
        existed = self._watchlist_has(symbol)
        return ActionItem(
            op="watchlist_add",
            label=OP_LABELS["watchlist_add"],
            description=f"把 {symbol} 加入自选股" + ("（已在自选中，将保持不变）" if existed else ""),
            params={"symbol": symbol},
            before={"in_watchlist": existed},
            after={"in_watchlist": True},
        )

    def _apply_watchlist_add(self, item: ActionItem) -> str:
        symbol = item.params["symbol"]
        self.watchlist.add(symbol)
        existed = bool((item.before or {}).get("in_watchlist"))
        state = "已在自选股中" if existed else "已加入自选股"
        return f"✅ {symbol} {state}"

    def _plan_watchlist_remove(self, params: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> ActionItem:
        symbol = str(params.get("symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("必须指定标的代码")
        existed = self._watchlist_has(symbol)
        return ActionItem(
            op="watchlist_remove",
            label=OP_LABELS["watchlist_remove"],
            description=f"把 {symbol} 移出自选股" + ("" if existed else "（当前不在自选中）"),
            params={"symbol": symbol},
            before={"in_watchlist": existed},
            after={"in_watchlist": False},
        )

    def _apply_watchlist_remove(self, item: ActionItem) -> str:
        symbol = item.params["symbol"]
        self.watchlist.remove(symbol)
        return f"✅ {symbol} 已移出自选股"

    def _watchlist_has(self, symbol: str) -> bool:
        rows = self.watchlist.list_watchlist()
        return any(str(row.get("symbol") or "").strip().upper() == symbol for row in rows)

    # ---- 持仓 ---- #
    def _plan_set_position(self, params: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> ActionItem:
        symbol = str(params.get("symbol") or "").strip().upper()
        cost = params.get("cost")
        quantity = params.get("quantity")
        if not symbol:
            raise ValueError("必须指定标的代码")
        if cost is None or quantity is None:
            raise ValueError("需要同时给出成本价与数量（例：成本 12.9 数量 200 股）")
        cost_value, qty_value = float(cost), float(quantity)
        if cost_value <= 0 or qty_value <= 0:
            raise ValueError("成本价与数量必须大于 0")
        before = self._position_snapshot(symbol)
        text = "变更" if before else "新增"
        return ActionItem(
            op="set_position",
            label=OP_LABELS["set_position"],
            description=(
                f"{text}持仓：{symbol} 成本 {_fmt_price(cost_value)} × {qty_value:g} 股"
                + (f"（原：成本 {_fmt_price((before or {}).get('avg_cost'))} × {(before or {}).get('quantity'):g} 股）" if before else "")
            ),
            params={"symbol": symbol, "cost": cost_value, "quantity": qty_value},
            before=before,
            after={"avg_cost": cost_value, "quantity": qty_value},
        )

    def _apply_set_position(self, item: ActionItem) -> str:
        symbol = item.params["symbol"]
        self.position.set_position(
            symbol,
            avg_cost=float(item.params["cost"]),
            quantity=float(item.params["quantity"]),
        )
        return (
            f"✅ {symbol} 持仓已设为 成本 {_fmt_price(item.params['cost'])}"
            f" × {item.params['quantity']:g} 股"
        )

    def _plan_remove_position(self, params: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> ActionItem:
        symbol = str(params.get("symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("必须指定标的代码")
        before = self._position_snapshot(symbol)
        if before is None:
            raise ValueError(f"{symbol} 当前无持仓，无需清空")
        return ActionItem(
            op="remove_position",
            label=OP_LABELS["remove_position"],
            description=f"清空 {symbol} 持仓（原：成本 {_fmt_price(before.get('avg_cost'))} × {before.get('quantity'):g} 股）",
            params={"symbol": symbol},
            before=before,
            after=None,
        )

    def _apply_remove_position(self, item: ActionItem) -> str:
        symbol = item.params["symbol"]
        result = self.position.remove_position(symbol)
        return f"✅ {symbol} 持仓已清空（移除 {result.get('cleared_trades', 0)} 条流水）"

    def _position_snapshot(self, symbol: str) -> Optional[Dict[str, Any]]:
        try:
            positions = self.position.list_positions()
        except Exception as exc:  # 读不到持仓时按"无持仓"处理（不阻断预览）
            logger.warning("读取持仓失败：%s", exc)
            return None
        view = positions.get(symbol) if isinstance(positions, dict) else None
        if view is None:
            return None
        quantity = float(getattr(view, "quantity", 0) or 0)
        if quantity <= 0:
            return None
        return {"avg_cost": float(getattr(view, "avg_cost", 0) or 0), "quantity": quantity}

    # ---- 止损 ---- #
    def _plan_set_stop_loss(self, params: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> ActionItem:
        symbol = str(params.get("symbol") or "").strip().upper()
        value = params.get("value")
        if not symbol:
            raise ValueError("必须指定标的代码")
        if value is None:
            raise ValueError("需要给出止损价（例：止损设为 15 元）")
        price = float(value)
        if price <= 0:
            raise ValueError("止损价必须大于 0")
        before = self.rule_override.get_stop_loss(symbol)
        return ActionItem(
            op="set_stop_loss",
            label=OP_LABELS["set_stop_loss"],
            description=(
                f"把 {symbol} 止损设为 {_fmt_price(price)}"
                + (f"（原覆盖 {_fmt_price(before)}）" if before is not None else "（原按技术位计算）")
            ),
            params={"symbol": symbol, "value": round(price, 4)},
            before=before,
            after=round(price, 4),
        )

    def _apply_set_stop_loss(self, item: ActionItem) -> str:
        result = self.rule_override.set_stop_loss(item.params["symbol"], item.params["value"])
        return f"✅ {item.params['symbol']} 止损已设为 {_fmt_price(result['after'])}"

    def _plan_remove_stop_loss(self, params: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> ActionItem:
        symbol = str(params.get("symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("必须指定标的代码")
        before = self.rule_override.get_stop_loss(symbol)
        if before is None:
            raise ValueError(f"{symbol} 当前未设置止损覆盖，无需移除")
        return ActionItem(
            op="remove_stop_loss",
            label=OP_LABELS["remove_stop_loss"],
            description=f"移除 {symbol} 的止损覆盖（{_fmt_price(before)}）→ 回落技术位计算",
            params={"symbol": symbol},
            before=before,
            after=None,
        )

    def _apply_remove_stop_loss(self, item: ActionItem) -> str:
        self.rule_override.remove_stop_loss(item.params["symbol"])
        return f"✅ {item.params['symbol']} 止损覆盖已移除，回落技术位计算"

    # ---- 四因子权重 ---- #
    def _plan_set_weight(self, params: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> ActionItem:
        factor = str(params.get("factor") or "").strip().lower()
        value = params.get("value")
        if not factor:
            raise ValueError("需要说明调整哪个维度（技术面/消息面/基本面/行业大盘）")
        if value is None:
            raise ValueError("需要给出目标权重（例：调到 60%）")

        working = None
        if context is not None and isinstance(context.get("weights"), dict):
            working = context["weights"]
        before = dict(working) if working else self.rule_override.get_factor_weights()
        after = self.rule_override.adjust_factor_weights({factor: value}, base=before)
        if context is not None:
            context["weights"] = after  # 顺序叠加，供后续权重指令继续调整

        return ActionItem(
            op="set_weight",
            label=OP_LABELS["set_weight"],
            description=(
                f"把 {FACTOR_LABELS.get(factor, factor)} 权重调为 {float(value) * 100:.0f}%"
                f"（调整后：{_fmt_weights(after)}）"
            ),
            params={"factor": factor, "value": float(value)},
            before=before,
            after=after,
        )

    def _apply_set_weight(self, item: ActionItem) -> str:
        # 落库完整的四维权重（预览已算好 after），避免落库口径与确认卡不一致
        after_weights = item.after if isinstance(item.after, dict) else None
        if not after_weights:
            raise ActionRejected("权重预览结果缺失，请重新预览")
        result = self.rule_override.set_factor_weights(after_weights)
        before, after = result["before"], result["after"]
        factor = item.params["factor"]
        return (
            f"✅ {FACTOR_LABELS.get(factor, factor)}权重 "
            f"{before.get(factor, 0) * 100:.0f}%→{after.get(factor, 0) * 100:.0f}%"
            f"（当前：{_fmt_weights(after)}）"
        )
