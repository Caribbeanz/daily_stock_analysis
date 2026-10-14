# -*- coding: utf-8 -*-
"""高抛低吸助手 · 写操作审计服务（P2.4 / NFR-11 / FR-14.0.5）

职责：
1. **留痕**：把 ``action_service`` 的审计事件落 ``action_audits``（可回放 before/after）；
2. **查询**：给 H5「操作记录」提供列表 / 详情 / 统计；
3. **撤销**：由 before/after **纯函数**反向构造 ``WriteOp``（撤销 = 反向操作，
   同样走 ``action_service`` 单一写点并留审计）；
4. **广播**：写操作成功后发布 ``invalidate`` 域事件 → 各端增量刷新（方案 §3.4）。

撤销的语义边界（诚实说明）：
- 撤销**只针对已成功**（``status=ok``）且**尚未被撤销**的记录；"撤销的撤销"不支持；
- 「调权重」的撤销＝把被调的那个维度**调回原值**（``adjust_factor_weights`` 会按比例
  重分配余量），即撤销后续可产生等价但非字节级一致的四维权重向量；
- 撤销**不能逆天改命**：若期间又被别处改动，撤销只是再执行一次反向写并留痕。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

from src.agent.intent.resolver import WriteOp
from src.repositories.action_audit_repo import (
    AUDIT_STATUS_OK,
    ActionAuditRepository,
)
from src.services.action_service import OP_LABELS

logger = logging.getLogger(__name__)

__all__ = [
    "ActionAuditService",
    "build_undo_ops",
    "can_undo",
    "scope_for_action",
    "publish_invalidate",
    "INVALIDATE_EVENT",
]

INVALIDATE_EVENT = "invalidate"

# op → 影响的域（前端据 scopes 决定刷新哪些视图）
_SCOPE_BY_OP: Dict[str, List[str]] = {
    "watchlist_add": ["watchlist"],
    "watchlist_remove": ["watchlist"],
    "set_position": ["portfolio"],
    "remove_position": ["portfolio"],
    "set_stop_loss": ["portfolio"],
    "remove_stop_loss": ["portfolio"],
    "set_weight": ["weights"],
}

# 任何写操作都会新增审计 / 记忆 → 这几类视图始终需要刷新
_BASE_SCOPES = ["audit"]


def scope_for_action(action: str) -> List[str]:
    """返回某写操作影响的视图域（含基础域）。"""
    scopes = list(_BASE_SCOPES)
    for scope in _SCOPE_BY_OP.get(str(action or ""), []):
        if scope not in scopes:
            scopes.append(scope)
    return scopes


# --------------------------------------------------------------------------- #
# 撤销：由审计记录反向构造 WriteOp（纯函数，可单测）
# --------------------------------------------------------------------------- #
def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_undo_ops(record: Mapping[str, Any]) -> List[WriteOp]:
    """由一条**已成功**的审计记录反向构造撤销写指令。

    Returns:
        ``WriteOp`` 列表；不可撤销时返回空列表。
    """
    action = str(record.get("action") or "")
    target = str(record.get("target") or "")
    before = record.get("before")

    if action in {"watchlist_add", "watchlist_remove"}:
        if not target:
            return []
        was_in = bool((before or {}).get("in_watchlist")) if isinstance(before, Mapping) else False
        if action == "watchlist_add":
            # 原本不在自选 → 撤销 = 移出；原本已在 → 该操作是空操作，无需撤销
            return [WriteOp("watchlist_remove", {"symbol": target})] if not was_in else []
        return [WriteOp("watchlist_add", {"symbol": target})]

    if action == "set_position":
        if not target:
            return []
        if isinstance(before, Mapping) and before:
            cost, qty = _num(before.get("avg_cost")), _num(before.get("quantity"))
            if cost is None or qty is None:
                return []
            return [WriteOp("set_position", {"symbol": target, "cost": cost, "quantity": qty})]
        # 原本无持仓 → 撤销 = 清空
        return [WriteOp("remove_position", {"symbol": target})]

    if action == "remove_position":
        if not target or not isinstance(before, Mapping) or not before:
            return []
        cost, qty = _num(before.get("avg_cost")), _num(before.get("quantity"))
        if cost is None or qty is None:
            return []
        return [WriteOp("set_position", {"symbol": target, "cost": cost, "quantity": qty})]

    if action == "set_stop_loss":
        if not target:
            return []
        price = _num(before)
        if price is None:
            return [WriteOp("remove_stop_loss", {"symbol": target})]
        return [WriteOp("set_stop_loss", {"symbol": target, "value": price})]

    if action == "remove_stop_loss":
        if not target:
            return []
        price = _num(before)
        if price is None:
            return []
        return [WriteOp("set_stop_loss", {"symbol": target, "value": price})]

    if action == "set_weight":
        factor = target
        if not factor or not isinstance(before, Mapping):
            return []
        value = _num(before.get(factor))
        if value is None:
            return []
        return [WriteOp("set_weight", {"factor": factor, "value": value})]

    return []


def can_undo(record: Mapping[str, Any]) -> Dict[str, Any]:
    """判断一条审计记录是否可撤销。

    Returns:
        ``{"undoable": bool, "reason": str, "ops": List[WriteOp]}``
    """
    if record is None:
        return {"undoable": False, "reason": "记录不存在", "ops": []}
    if str(record.get("status") or "") != AUDIT_STATUS_OK:
        return {"undoable": False, "reason": "只有成功执行的操作才可撤销", "ops": []}
    if record.get("undone_by") is not None:
        return {"undoable": False, "reason": "该操作已被撤销", "ops": []}
    if record.get("undo_of") is not None:
        return {"undoable": False, "reason": "撤销操作本身不可再撤销", "ops": []}
    ops = build_undo_ops(record)
    if not ops:
        return {"undoable": False, "reason": "该操作没有可回退的变更", "ops": []}
    return {"undoable": True, "reason": "", "ops": ops}


# --------------------------------------------------------------------------- #
# 广播：写操作成功 → invalidate（各端增量刷新）
# --------------------------------------------------------------------------- #
def publish_invalidate(record: Mapping[str, Any], *, audit_id: Optional[int] = None) -> None:
    """发布一条 ``invalidate`` 域事件（发布失败不影响写链路）。"""
    try:
        from src.services.domain_event_bus import get_domain_event_bus

        action = str(record.get("action") or "")
        op_label = OP_LABELS.get(action, action)
        data = {
            "scopes": scope_for_action(action),
            "action": action,
            "label": op_label,
            "target": str(record.get("target") or ""),
            "source": str(record.get("source") or ""),
            "audit_id": audit_id,
            "undo_of": record.get("undo_of"),
        }
        get_domain_event_bus().publish(INVALIDATE_EVENT, data)
    except Exception as exc:  # pragma: no cover - 广播失败绝不影响写操作
        logger.warning("发布 invalidate 事件失败：%s", exc)


class ActionAuditService:
    """写操作审计编排：留痕 / 查询 / 撤销记录回填。"""

    def __init__(self, *, repo: Optional[ActionAuditRepository] = None) -> None:
        self._repo = repo

    @property
    def repo(self) -> ActionAuditRepository:
        if self._repo is None:
            self._repo = ActionAuditRepository()
        return self._repo

    # ---------------- 留痕 ---------------- #
    def record(self, event: Mapping[str, Any]) -> Optional[int]:
        """把一条 ``action_service`` 审计事件落库，返回审计 id。

        若事件携带 ``undo_of``（本条是撤销操作），则同时回填原记录的 ``undone_by``。
        """
        try:
            undo_of = event.get("undo_of")
            audit_id = self.repo.add(
                action=str(event.get("action") or ""),
                source=str(event.get("source") or ""),
                actor=str(event.get("actor") or ""),
                target=str(event.get("target") or ""),
                before=event.get("before"),
                after=event.get("after"),
                confirmed=bool(event.get("confirmed")),
                status=str(event.get("status") or ""),
                description=str(event.get("description") or ""),
                session_id=event.get("session_id"),
                undo_of=int(undo_of) if undo_of is not None else None,
            )
            if undo_of is not None and str(event.get("status") or "") == AUDIT_STATUS_OK:
                try:
                    self.repo.mark_undone(int(undo_of), audit_id)
                except Exception as exc:  # 回填失败不影响撤销本身
                    logger.warning("回填撤销关系失败（undo_of=%s）：%s", undo_of, exc)
            return audit_id
        except Exception as exc:  # 审计失败不影响已完成的写操作
            logger.warning("写操作审计落库失败：%s", exc)
            return None

    # ---------------- 查询 ---------------- #
    def list_records(
        self,
        *,
        source: Optional[str] = None,
        action: Optional[str] = None,
        status: Optional[str] = None,
        actor: Optional[str] = None,
        undoing: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        items = self.repo.list_records(
            source=source,
            action=action,
            status=status,
            actor=actor,
            undoing=undoing,
            limit=limit,
            offset=offset,
        )
        for item in items:
            verdict = can_undo(item)
            item["undoable"] = verdict["undoable"]
            item["undo_reason"] = verdict["reason"]
        total = self.repo.count(
            source=source, action=action, status=status, actor=actor, undoing=undoing
        )
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def get(self, audit_id: int, *, with_undo: bool = True) -> Optional[Dict[str, Any]]:
        record = self.repo.get(audit_id)
        if record is None:
            return None
        verdict = can_undo(record)
        record["undoable"] = verdict["undoable"]
        record["undo_reason"] = verdict["reason"]
        if with_undo:
            record["undo_ops"] = [
                {"op": op.op, **op.params} for op in verdict["ops"]
            ]
        return record

    def stats(self) -> Dict[str, Any]:
        total = self.repo.count()
        ok = self.repo.count(status=AUDIT_STATUS_OK)
        rejected = self.repo.count(status="rejected")
        failed = self.repo.count(status="failed")
        undone = self.repo.count(undoing=True)
        return {
            "total": total,
            "ok": ok,
            "rejected": rejected,
            "failed": failed,
            "undo_records": undone,
            "by_action": self.repo.count_by_action(),
        }

    def latest_undo_of(self, audit_id: int) -> Optional[int]:
        return self.repo.latest_undo_of(audit_id)
