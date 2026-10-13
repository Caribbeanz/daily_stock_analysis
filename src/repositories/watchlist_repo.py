# -*- coding: utf-8 -*-
"""高抛低吸助手 · 自选股数据访问层（P0）

封装 ``watchlist_items`` 表的数据库操作。表结构见 ``src/storage.py::WatchlistItem``。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select, func

from src.storage import DatabaseManager, WatchlistItem

logger = logging.getLogger(__name__)

__all__ = ["WatchlistRepository"]


class WatchlistRepository:
    """自选股的数据库访问层。"""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    # ---------------- 查询 ---------------- #
    def list_items(self, *, enabled_only: bool = False) -> List[WatchlistItem]:
        with self.db.get_session() as session:
            stmt = select(WatchlistItem)
            if enabled_only:
                stmt = stmt.where(WatchlistItem.enabled.is_(True))
            stmt = stmt.order_by(WatchlistItem.sort_order.asc(), WatchlistItem.id.asc())
            return list(session.execute(stmt).scalars().all())

    def list_dicts(self, *, enabled_only: bool = False) -> List[Dict[str, Any]]:
        return [self.to_dict(item) for item in self.list_items(enabled_only=enabled_only)]

    def get(self, symbol: str, *, asset_type: Optional[str] = None) -> Optional[WatchlistItem]:
        symbol = self._normalize_symbol(symbol)
        with self.db.get_session() as session:
            stmt = select(WatchlistItem).where(WatchlistItem.symbol == symbol)
            if asset_type:
                stmt = stmt.where(WatchlistItem.asset_type == asset_type)
            return session.execute(stmt).scalars().first()

    def count(self, *, enabled_only: bool = False) -> int:
        with self.db.get_session() as session:
            stmt = select(func.count(WatchlistItem.id))
            if enabled_only:
                stmt = stmt.where(WatchlistItem.enabled.is_(True))
            return int(session.execute(stmt).scalar() or 0)

    # ---------------- 写入 ---------------- #
    def upsert(
        self,
        symbol: str,
        *,
        name: Optional[str] = None,
        asset_type: str = "stock",
        market: str = "cn",
        track_index: Optional[str] = None,
        enabled: bool = True,
        note: Optional[str] = None,
        sort_order: Optional[int] = None,
    ) -> WatchlistItem:
        """按 (symbol, asset_type) 幂等写入；已存在则补齐非空字段。"""
        symbol = self._normalize_symbol(symbol)
        asset_type = self._normalize_asset_type(asset_type)

        with self.db.get_session() as session:
            stmt = (
                select(WatchlistItem)
                .where(WatchlistItem.symbol == symbol)
                .where(WatchlistItem.asset_type == asset_type)
            )
            row = session.execute(stmt).scalars().first()
            if row is None:
                row = WatchlistItem(
                    symbol=symbol,
                    name=name,
                    asset_type=asset_type,
                    market=market or "cn",
                    track_index=track_index,
                    enabled=bool(enabled),
                    sort_order=int(sort_order) if sort_order is not None else self._next_sort_order(session),
                    note=note,
                )
                session.add(row)
            else:
                if name:
                    row.name = name
                if track_index:
                    row.track_index = track_index
                if note:
                    row.note = note
                if sort_order is not None:
                    row.sort_order = int(sort_order)
                row.enabled = bool(enabled)
            session.commit()
            session.refresh(row)
            return row

    def set_enabled(self, symbol: str, enabled: bool, *, asset_type: Optional[str] = None) -> int:
        symbol = self._normalize_symbol(symbol)
        with self.db.get_session() as session:
            stmt = select(WatchlistItem).where(WatchlistItem.symbol == symbol)
            if asset_type:
                stmt = stmt.where(WatchlistItem.asset_type == asset_type)
            rows = list(session.execute(stmt).scalars().all())
            for row in rows:
                row.enabled = bool(enabled)
            session.commit()
            return len(rows)

    def remove(self, symbol: str, *, asset_type: Optional[str] = None) -> int:
        symbol = self._normalize_symbol(symbol)
        with self.db.get_session() as session:
            stmt = select(WatchlistItem).where(WatchlistItem.symbol == symbol)
            if asset_type:
                stmt = stmt.where(WatchlistItem.asset_type == asset_type)
            rows = list(session.execute(stmt).scalars().all())
            for row in rows:
                session.delete(row)
            session.commit()
            return len(rows)

    # ---------------- 工具 ---------------- #
    def _next_sort_order(self, session) -> int:
        current = session.execute(select(func.max(WatchlistItem.sort_order))).scalar()
        return int(current or 0) + 10

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        return str(symbol or "").strip().upper()

    @staticmethod
    def _normalize_asset_type(asset_type: str) -> str:
        value = str(asset_type or "stock").strip().lower()
        return value if value in {"stock", "etf", "lof"} else "stock"

    @staticmethod
    def to_dict(row: WatchlistItem) -> Dict[str, Any]:
        return {
            "symbol": row.symbol,
            "name": row.name,
            "asset_type": row.asset_type,
            "market": row.market,
            "track_index": row.track_index,
            "enabled": bool(row.enabled),
            "sort_order": row.sort_order,
            "note": row.note,
        }
