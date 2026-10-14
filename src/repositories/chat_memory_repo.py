# -*- coding: utf-8 -*-
"""高抛低吸助手 · 结构化对话记忆数据访问层（P2.3 / FR-15.5）

封装 ``chat_memories`` 与 ``chat_memory_cursors`` 两张表的数据库操作。
表结构见 ``src/storage.py::ChatMemory`` / ``src/storage.py::ChatMemoryCursor``。

设计约束（决议 D5）：
- **压缩只增不删**：写入记忆不删除原文；删除仅由用户显式触发；
- **软删保留审计**：默认软删（``status='deleted'``），硬删为破坏性操作；
- **回链可追溯**：每条记忆持 ``source_message_ids``（FR-15.4）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, func, select

from src.storage import ChatMemory, ChatMemoryCursor, DatabaseManager

logger = logging.getLogger(__name__)

__all__ = ["ChatMemoryRepository", "MEMORY_STATUS_ACTIVE", "MEMORY_STATUS_DELETED"]

MEMORY_STATUS_ACTIVE = "active"
MEMORY_STATUS_DELETED = "deleted"


class ChatMemoryRepository:
    """``chat_memories`` 的数据库访问层。"""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    # ---------------- 写入 ---------------- #
    def add(
        self,
        memory_type: str,
        content: str,
        *,
        source_message_ids: Optional[List[int]] = None,
        session_id: Optional[str] = None,
        status: str = MEMORY_STATUS_ACTIVE,
    ) -> int:
        """新增一条结构化记忆，返回自增 id。"""
        ids = [int(i) for i in (source_message_ids or []) if str(i).strip()]
        with self.db.get_session() as session:
            row = ChatMemory(
                memory_type=str(memory_type or "").strip(),
                content=str(content or "").strip(),
                source_message_ids_json=json.dumps(ids),
                session_id=(str(session_id).strip() or None) if session_id else None,
                status=str(status or MEMORY_STATUS_ACTIVE).strip(),
            )
            session.add(row)
            session.flush()
            new_id = int(row.id)
            session.commit()
            return new_id

    def soft_delete(self, memory_id: int) -> int:
        """把记忆标记为 ``deleted``（保留可审计）。返回受影响行数。"""
        with self.db.get_session() as session:
            row = session.get(ChatMemory, int(memory_id))
            if row is None:
                return 0
            row.status = MEMORY_STATUS_DELETED
            session.commit()
            return 1

    def restore(self, memory_id: int) -> int:
        """把软删的记忆恢复为 ``active``。返回受影响行数。"""
        with self.db.get_session() as session:
            row = session.get(ChatMemory, int(memory_id))
            if row is None:
                return 0
            row.status = MEMORY_STATUS_ACTIVE
            session.commit()
            return 1

    def hard_delete(self, memory_id: int) -> int:
        """物理删除一条记忆。返回受影响行数。"""
        with self.db.get_session() as session:
            row = session.get(ChatMemory, int(memory_id))
            if row is None:
                return 0
            session.delete(row)
            session.commit()
            return 1

    def clear(self, *, session_id: Optional[str] = None, hard: bool = True) -> int:
        """清空记忆（全部或指定会话）。

        ``hard=True`` 物理删除；``hard=False`` 全部置为软删。
        Returns:
            受影响行数。
        """
        with self.db.get_session() as session:
            if hard:
                stmt = delete(ChatMemory)
                if session_id:
                    stmt = stmt.where(ChatMemory.session_id == session_id)
                result = session.execute(stmt)
                session.commit()
                return int(result.rowcount or 0)
            stmt = select(ChatMemory)
            if session_id:
                stmt = stmt.where(ChatMemory.session_id == session_id)
            rows = list(session.execute(stmt).scalars().all())
            for row in rows:
                row.status = MEMORY_STATUS_DELETED
            session.commit()
            return len(rows)

    # ---------------- 查询 ---------------- #
    def get(self, memory_id: int) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.get(ChatMemory, int(memory_id))
            return self.to_dict(row) if row is not None else None

    def list_memories(
        self,
        *,
        memory_type: Optional[str] = None,
        session_id: Optional[str] = None,
        status: Optional[str] = MEMORY_STATUS_ACTIVE,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            stmt = select(ChatMemory)
            if memory_type:
                stmt = stmt.where(ChatMemory.memory_type == memory_type)
            if session_id:
                stmt = stmt.where(ChatMemory.session_id == session_id)
            if status is not None:
                stmt = stmt.where(ChatMemory.status == status)
            stmt = stmt.order_by(ChatMemory.created_at.desc(), ChatMemory.id.desc())
            stmt = stmt.offset(max(0, int(offset))).limit(max(1, int(limit)))
            rows = list(session.execute(stmt).scalars().all())
            return [self.to_dict(row) for row in rows]

    def count(
        self,
        *,
        memory_type: Optional[str] = None,
        session_id: Optional[str] = None,
        status: Optional[str] = MEMORY_STATUS_ACTIVE,
    ) -> int:
        with self.db.get_session() as session:
            stmt = select(func.count(ChatMemory.id))
            if memory_type:
                stmt = stmt.where(ChatMemory.memory_type == memory_type)
            if session_id:
                stmt = stmt.where(ChatMemory.session_id == session_id)
            if status is not None:
                stmt = stmt.where(ChatMemory.status == status)
            return int(session.execute(stmt).scalar() or 0)

    def count_by_type(self, *, status: str = MEMORY_STATUS_ACTIVE) -> Dict[str, int]:
        with self.db.get_session() as session:
            stmt = (
                select(ChatMemory.memory_type, func.count(ChatMemory.id))
                .where(ChatMemory.status == status)
                .group_by(ChatMemory.memory_type)
            )
            return {str(t): int(c) for t, c in session.execute(stmt).all()}

    # ---------------- 压缩游标 ---------------- #
    def get_cursor(self, session_id: str) -> int:
        with self.db.get_session() as session:
            row = session.get(ChatMemoryCursor, str(session_id))
            return int(row.last_message_id) if row is not None else 0

    def get_cursor_state(self, session_id: str) -> Dict[str, Any]:
        """返回 ``{last_message_id, updated_at}``；无游标时 ``updated_at=None``。"""
        with self.db.get_session() as session:
            row = session.get(ChatMemoryCursor, str(session_id))
            if row is None:
                return {"last_message_id": 0, "updated_at": None}
            return {
                "last_message_id": int(row.last_message_id or 0),
                "updated_at": row.updated_at,
            }

    def set_cursor(self, session_id: str, last_message_id: int) -> None:
        with self.db.get_session() as session:
            row = session.get(ChatMemoryCursor, str(session_id))
            if row is None:
                row = ChatMemoryCursor(session_id=str(session_id), last_message_id=int(last_message_id or 0))
                session.add(row)
            else:
                row.last_message_id = int(last_message_id or 0)
            session.commit()

    # ---------------- 工具 ---------------- #
    @staticmethod
    def to_dict(row: ChatMemory) -> Dict[str, Any]:
        try:
            source_ids = json.loads(row.source_message_ids_json or "[]")
        except (TypeError, ValueError):
            source_ids = []
        if not isinstance(source_ids, list):
            source_ids = []
        return {
            "id": int(row.id),
            "memory_type": row.memory_type,
            "content": row.content,
            "source_message_ids": [int(i) for i in source_ids],
            "session_id": row.session_id,
            "status": row.status,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
