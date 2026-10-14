# -*- coding: utf-8 -*-
"""高抛低吸助手 · 写操作审计数据访问层（P2.4 / NFR-11）

封装 ``action_audits`` 表的数据库操作。表结构见 ``src/storage.py::ActionAudit``。

设计约束：
- 审计**只增不改**（除撤销关系回填 ``undone_by`` 外不修改历史记录）；
- ``before`` / ``after`` 以 JSON 文本存储（可回放"改前 / 改后"）；
- 撤销关系双向回填：撤销记录 ``undo_of`` → 原记录，原记录 ``undone_by`` → 撤销记录。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select

from src.storage import ActionAudit, DatabaseManager

logger = logging.getLogger(__name__)

__all__ = ["ActionAuditRepository"]

# 审计状态
AUDIT_STATUS_OK = "ok"
AUDIT_STATUS_REJECTED = "rejected"
AUDIT_STATUS_FAILED = "failed"


def _dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value), ensure_ascii=False)


def _load(text: Any) -> Any:
    if text in (None, ""):
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


class ActionAuditRepository:
    """``action_audits`` 的数据库访问层。"""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    # ---------------- 写入 ---------------- #
    def add(
        self,
        *,
        action: str,
        source: str = "",
        actor: str = "",
        target: str = "",
        before: Any = None,
        after: Any = None,
        confirmed: bool = False,
        status: str = "",
        description: str = "",
        session_id: Optional[str] = None,
        undo_of: Optional[int] = None,
    ) -> int:
        """新增一条审计记录，返回自增 id。"""
        with self.db.get_session() as session:
            row = ActionAudit(
                source=str(source or "")[:16],
                actor=str(actor or "")[:64],
                action=str(action or "")[:32],
                target=str(target or "")[:32],
                before_json=_dump(before),
                after_json=_dump(after),
                confirmed=bool(confirmed),
                status=str(status or "")[:16],
                description=str(description or ""),
                session_id=(str(session_id).strip() or None) if session_id else None,
                undo_of=int(undo_of) if undo_of is not None else None,
            )
            session.add(row)
            session.flush()
            new_id = int(row.id)
            session.commit()
            return new_id

    def mark_undone(self, audit_id: int, undo_id: int) -> int:
        """把原记录标记为"已被撤销"（``undone_by = undo_id``）。返回受影响行数。"""
        with self.db.get_session() as session:
            row = session.get(ActionAudit, int(audit_id))
            if row is None:
                return 0
            row.undone_by = int(undo_id)
            session.commit()
            return 1

    # ---------------- 查询 ---------------- #
    def get(self, audit_id: int) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.get(ActionAudit, int(audit_id))
            return self.to_dict(row) if row is not None else None

    def latest_undo_of(self, audit_id: int) -> Optional[int]:
        """返回针对 ``audit_id`` 的最新撤销记录 id（无则 None）。"""
        with self.db.get_session() as session:
            stmt = (
                select(ActionAudit.id)
                .where(ActionAudit.undo_of == int(audit_id))
                .order_by(ActionAudit.id.desc())
                .limit(1)
            )
            value = session.execute(stmt).scalar()
            return int(value) if value is not None else None

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
    ) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            stmt = select(ActionAudit)
            if source:
                stmt = stmt.where(ActionAudit.source == source)
            if action:
                stmt = stmt.where(ActionAudit.action == action)
            if status:
                stmt = stmt.where(ActionAudit.status == status)
            if actor:
                stmt = stmt.where(ActionAudit.actor == actor)
            if undoing is True:
                stmt = stmt.where(ActionAudit.undo_of.isnot(None))
            elif undoing is False:
                stmt = stmt.where(ActionAudit.undo_of.is_(None))
            stmt = stmt.order_by(ActionAudit.created_at.desc(), ActionAudit.id.desc())
            stmt = stmt.offset(max(0, int(offset))).limit(max(1, int(limit)))
            rows = list(session.execute(stmt).scalars().all())
            return [self.to_dict(row) for row in rows]

    def count(
        self,
        *,
        source: Optional[str] = None,
        action: Optional[str] = None,
        status: Optional[str] = None,
        actor: Optional[str] = None,
        undoing: Optional[bool] = None,
    ) -> int:
        with self.db.get_session() as session:
            stmt = select(func.count(ActionAudit.id))
            if source:
                stmt = stmt.where(ActionAudit.source == source)
            if action:
                stmt = stmt.where(ActionAudit.action == action)
            if status:
                stmt = stmt.where(ActionAudit.status == status)
            if actor:
                stmt = stmt.where(ActionAudit.actor == actor)
            if undoing is True:
                stmt = stmt.where(ActionAudit.undo_of.isnot(None))
            elif undoing is False:
                stmt = stmt.where(ActionAudit.undo_of.is_(None))
            return int(session.execute(stmt).scalar() or 0)

    def count_by_action(self, *, status: Optional[str] = None) -> Dict[str, int]:
        with self.db.get_session() as session:
            stmt = select(ActionAudit.action, func.count(ActionAudit.id))
            if status:
                stmt = stmt.where(ActionAudit.status == status)
            stmt = stmt.group_by(ActionAudit.action)
            return {str(a): int(c) for a, c in session.execute(stmt).all()}

    # ---------------- 工具 ---------------- #
    @staticmethod
    def to_dict(row: ActionAudit) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "source": row.source or "",
            "actor": row.actor or "",
            "action": row.action or "",
            "target": row.target or "",
            "before": _load(row.before_json),
            "after": _load(row.after_json),
            "confirmed": bool(row.confirmed),
            "status": row.status or "",
            "description": row.description or "",
            "session_id": row.session_id,
            "undo_of": int(row.undo_of) if row.undo_of is not None else None,
            "undone_by": int(row.undone_by) if row.undone_by is not None else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
