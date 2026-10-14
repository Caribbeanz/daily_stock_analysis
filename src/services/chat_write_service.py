# -*- coding: utf-8 -*-
"""高抛低吸助手 · 对话·写编排（P2.2 / FR-14.3.3、FR-14.3.7）

把「一句话写指令」编排成三段式：**解析 → 预览（待确认）→ 确认执行 → 逐条回执**。

- 解析：复用 ``src.agent.intent.resolver``（规则优先 + LLM 兜底），支持复合指令；
- 预览：交给 ``ActionService.plan`` 生成"将要做什么"清单，并写入确认令牌；
- 确认：凭令牌（或按 actor 取最近一条，供飞书"回复确认"）执行；
- 回执：逐条"做了什么 + 当前状态"。

**合规（FR-14.3.8 / FR-14.3.9）**：越界请求（下单等）明确拒绝并给替代路径；
所有涉及买卖的回执强制附风险提示。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Sequence

from src.agent.intent.resolver import Intent, IntentKind, WriteOp, resolve
from src.services.action_service import ActionPlan, ActionService
from src.services.chat_read_service import OUT_OF_SCOPE_NOTE, RISK_NOTE
from src.services.confirmation_store import ConfirmationStore, is_cancel_text, is_confirm_text

logger = logging.getLogger(__name__)

__all__ = ["ChatWriteService", "CONFIRM_HINT"]

CONFIRM_HINT = "回复「确认」执行，「取消」放弃"

_CONFIRM_TEMPLATE = (
    "📝 将要执行以下 {count} 项操作：\n{summary}\n\n"
    + CONFIRM_HINT
    + "（{ttl} 分钟内有效）。"
)

# 会改动持仓 → 回执附风险提示
_RISK_OPS = {"set_position", "remove_position", "set_stop_loss", "remove_stop_loss"}


class ChatWriteService:
    """写对话编排：解析 → 预览 → 确认 → 回执。"""

    def __init__(
        self,
        *,
        action_service: Optional[ActionService] = None,
        store: Optional[ConfirmationStore] = None,
        resolver: Callable[..., Intent] = resolve,
        llm: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
    ) -> None:
        self._action_service = action_service
        self._store = store
        self._resolver = resolver
        self._llm = llm

    # ---------------- 依赖 ---------------- #
    @property
    def action_service(self) -> ActionService:
        if self._action_service is None:
            self._action_service = ActionService()
        return self._action_service

    @property
    def store(self) -> ConfirmationStore:
        if self._store is None:
            self._store = ConfirmationStore()
        return self._store

    # ---------------- 预览 ---------------- #
    def preview_message(
        self,
        message: str,
        *,
        symbol: Optional[str] = None,
        actor: str = "",
        session_id: str = "",
        source: str = "h5",
    ) -> Dict[str, Any]:
        """从用户原话解析写指令并生成待确认计划。"""
        intent = self._resolver(message, symbol_hint=symbol, llm=self._llm)
        if intent.kind is IntentKind.OUT_OF_SCOPE:
            return {
                "status": "rejected",
                "intent": intent.kind.value,
                "message": OUT_OF_SCOPE_NOTE,
                "items": [],
            }
        if intent.kind is not IntentKind.WRITE_INTENT:
            return {
                "status": "not_write",
                "intent": intent.kind.value,
                "message": "这句话不包含需要执行的操作；如需分析或问答，可直接提问。",
                "items": [],
            }
        return self.preview(
            intent.write_ops, actor=actor, session_id=session_id, source=source
        )

    def preview(
        self,
        ops: Sequence[WriteOp],
        *,
        actor: str = "",
        session_id: str = "",
        source: str = "h5",
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """把写指令渲染为"待确认"计划（不落库）。

        Args:
            meta: 附加确认上下文（如撤销时 ``{"undo_of": <audit_id>}``），
                确认执行时会并入审计记录。
        """
        plan = self.action_service.plan(list(ops))
        if not plan.items:
            return {"status": "nothing_to_do", "message": "没有识别到可执行的写操作。", "items": []}

        if not plan.valid:
            # 全部不可执行（越界 / 白名单外 / 参数缺失）→ 直接给拒绝说明，不产生令牌
            return {
                "status": "rejected",
                "message": "以下操作未通过校验，请修正后重试：\n" + plan.summary(),
                "items": [item.to_dict() for item in plan.items],
            }

        pending = self.store.put(
            plan,
            actor=actor,
            session_id=session_id,
            source=source,
            summary=plan.summary(),
            meta=meta,
        )
        message = _CONFIRM_TEMPLATE.format(
            count=len(plan.items),
            summary=plan.summary(),
            ttl=int(self.store.ttl_seconds / 60) or 1,
        )
        return {
            "status": "pending_confirmation",
            "token": pending.token,
            "expires_at": pending.expires_at,
            "message": message,
            "summary": plan.summary(),
            "items": [item.to_dict() for item in plan.items],
        }

    # ---------------- 确认 / 取消 ---------------- #
    def confirm(
        self,
        *,
        token: str = "",
        actor: str = "",
        source: str = "",
    ) -> Dict[str, Any]:
        """凭令牌执行（token 为空时按 actor 取最近一条待确认）。"""
        pending = self.store.pop(token) if token else self.store.pop_latest_for_actor(actor)
        if pending is None:
            return {
                "status": "nothing_pending",
                "message": "没有待确认的操作（可能已执行、已取消或已超过 5 分钟有效期）。",
                "items": [],
            }

        plan: ActionPlan = pending.payload
        audit_context: Dict[str, Any] = dict(pending.meta or {})
        if pending.session_id:
            audit_context.setdefault("session_id", pending.session_id)
        plan = self.action_service.execute_token_plan(
            plan,
            actor=actor or pending.actor,
            source=source or pending.source,
            audit_context=audit_context or None,
        )
        items = [item.to_dict() for item in plan.items]
        ok = all(item.status == "ok" for item in plan.items)
        receipt = plan.receipt()
        if any(item.op in _RISK_OPS and item.status == "ok" for item in plan.items):
            receipt = f"{receipt}\n\n{RISK_NOTE}"
        return {
            "status": "executed" if ok else ("partial" if any(i["status"] == "ok" for i in items) else "failed"),
            "ok": ok,
            "receipt": receipt,
            "items": items,
        }

    def cancel(self, *, token: str = "", actor: str = "") -> Dict[str, Any]:
        if token:
            removed = self.store.cancel(token)
        else:
            removed = self.store.pop_latest_for_actor(actor) is not None
        return {
            "status": "cancelled" if removed else "nothing_pending",
            "message": "已取消，未做任何修改。" if removed else "没有待确认的操作。",
        }

    # ---------------- 供文本渠道（飞书）使用 ---------------- #
    def has_pending(self, actor: str) -> bool:
        """该 actor 是否有待确认操作（供飞书判断"确认/取消"是否成立）。"""
        return self.store.latest_for_actor(actor) is not None

    @staticmethod
    def handles_text(text: str) -> Optional[str]:
        """判断文本是否是确认 / 取消指令（供飞书入站分流）。"""
        if is_confirm_text(text):
            return "confirm"
        if is_cancel_text(text):
            return "cancel"
        return None
