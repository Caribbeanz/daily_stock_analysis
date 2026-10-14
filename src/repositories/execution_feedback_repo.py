# -*- coding: utf-8 -*-
"""高抛低吸助手 · 「是否按建议执行」回填数据访问层（P3 / FR-11.2）

封装 ``execution_feedback`` 表的数据库操作。表结构见
``src/storage.py::ExecutionFeedback``。

设计约束：
- ``symbol`` + ``week_key`` 唯一 → 同一标的同一周只保留**一条**回填（后写覆盖）；
- ``execution_status`` ∈ {executed, ignored, later}，对应 FR-14.3.5 的对话回执口径；
- 与 ``decision_signal_feedback``（信号是否有用）**语义不同，切勿混用**。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select

from src.storage import DatabaseManager, ExecutionFeedback

logger = logging.getLogger(__name__)

__all__ = ["ExecutionFeedbackRepository", "EXECUTION_STATUSES"]

# FR-14.3.5：已执行 / 忽略 / 稍后
EXECUTION_STATUSES = frozenset({"executed", "ignored", "later"})


class ExecutionFeedbackRepository:
    """``execution_feedback`` 的数据库访问层。"""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    def upsert(
        self,
        symbol: str,
        week_key: str,
        execution_status: str,
        *,
        signal_id: Optional[int] = None,
        note: Optional[str] = None,
        source: str = "web",
    ) -> Dict[str, Any]:
        """写入 / 覆盖某标的某周的执行回填，返回记录字典。"""
        normalized_symbol = str(symbol or "").strip().upper()
        normalized_week = str(week_key or "").strip()
        status = str(execution_status or "").strip()
        if not normalized_symbol:
            raise ValueError("symbol is required")
        if not normalized_week:
            raise ValueError("week_key is required")
        if status not in EXECUTION_STATUSES:
            raise ValueError(
                f"execution_status 非法：{status!r}（应为 {'/'.join(sorted(EXECUTION_STATUSES))}）"
            )
        with self.db.get_session() as session:
            stmt = select(ExecutionFeedback).where(
                ExecutionFeedback.symbol == normalized_symbol,
                ExecutionFeedback.week_key == normalized_week,
            )
            row = session.execute(stmt).scalars().first()
            payload_note = (str(note).strip() or None) if note is not None else None
            if row is None:
                row = ExecutionFeedback(
                    symbol=normalized_symbol,
                    week_key=normalized_week,
                    signal_id=int(signal_id) if signal_id else None,
                    execution_status=status,
                    note=payload_note,
                    source=str(source or "web").strip() or "web",
                )
                session.add(row)
            else:
                row.execution_status = status
                if signal_id is not None:
                    row.signal_id = int(signal_id)
                row.note = payload_note
                row.source = str(source or "web").strip() or "web"
            session.flush()
            result = self.to_dict(row)
            session.commit()
            return result

    def get(self, symbol: str, week_key: str) -> Optional[Dict[str, Any]]:
        normalized_symbol = str(symbol or "").strip().upper()
        normalized_week = str(week_key or "").strip()
        if not normalized_symbol or not normalized_week:
            return None
        with self.db.get_session() as session:
            stmt = select(ExecutionFeedback).where(
                ExecutionFeedback.symbol == normalized_symbol,
                ExecutionFeedback.week_key == normalized_week,
            )
            row = session.execute(stmt).scalars().first()
            return self.to_dict(row) if row is not None else None

    def list_by_week(self, week_key: str) -> List[Dict[str, Any]]:
        normalized_week = str(week_key or "").strip()
        if not normalized_week:
            return []
        with self.db.get_session() as session:
            stmt = (
                select(ExecutionFeedback)
                .where(ExecutionFeedback.week_key == normalized_week)
                .order_by(ExecutionFeedback.symbol.asc())
            )
            rows = list(session.execute(stmt).scalars().all())
            return [self.to_dict(row) for row in rows]

    def delete(self, symbol: str, week_key: str) -> int:
        normalized_symbol = str(symbol or "").strip().upper()
        normalized_week = str(week_key or "").strip()
        if not normalized_symbol or not normalized_week:
            return 0
        with self.db.get_session() as session:
            result = session.execute(
                delete(ExecutionFeedback).where(
                    ExecutionFeedback.symbol == normalized_symbol,
                    ExecutionFeedback.week_key == normalized_week,
                )
            )
            session.commit()
            return int(result.rowcount or 0)

    @staticmethod
    def to_dict(row: ExecutionFeedback) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "symbol": row.symbol,
            "week_key": row.week_key,
            "signal_id": int(row.signal_id) if row.signal_id else None,
            "execution_status": row.execution_status,
            "note": row.note,
            "source": row.source,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
