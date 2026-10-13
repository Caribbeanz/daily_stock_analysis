# -*- coding: utf-8 -*-
"""高抛低吸助手 · 自选股服务（P0）

职责：
1. 把过去仅存在于 ``config.stock_list`` 的自选股，落库到 ``watchlist_items``
   （**单向同步**：配置 → 表；表中手工增删不受影响）；
2. 提供标的类型（个股 / ETF / LOF）推断与首批标的 seed；
3. 对外提供"给分析用"的启用列表。

需求依据：FR-1（自选股管理与持仓）、FR-19（场内基金支持）。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from src.config import get_config
from src.repositories.watchlist_repo import WatchlistRepository

logger = logging.getLogger(__name__)

__all__ = [
    "WatchlistService",
    "infer_asset_type",
    "DEFAULT_WATCHLIST",
    "ETF_CODE_PREFIXES",
]

# 场内基金常见代码前缀（沪 5x / 深 15x、16x）
ETF_CODE_PREFIXES = (
    "15", "16", "50", "51", "52", "53", "56", "58",
)

# 首批自选股（需求方确认，P0 验收样本）
DEFAULT_WATCHLIST: List[Dict[str, Optional[str]]] = [
    {"symbol": "601169", "name": "北京银行", "asset_type": "stock", "track_index": None},
    {"symbol": "601818", "name": "光大银行", "asset_type": "stock", "track_index": None},
    {"symbol": "601919", "name": "中远海控", "asset_type": "stock", "track_index": None},
    {"symbol": "000725", "name": "京东方A", "asset_type": "stock", "track_index": None},
    {"symbol": "513100", "name": "纳指ETF(国泰)", "asset_type": "etf", "track_index": "纳斯达克100"},
    {"symbol": "513180", "name": "恒生科技ETF(华夏)", "asset_type": "etf", "track_index": "恒生科技"},
    {"symbol": "513530", "name": "港股通红利ETF(华泰柏瑞)", "asset_type": "etf", "track_index": "中证港股通高股息"},
    {"symbol": "520660", "name": "港股通央企红利ETF(南方)", "asset_type": "etf", "track_index": "中证国新港股通央企红利"},
]


def infer_asset_type(symbol: str) -> str:
    """按代码前缀推断标的类型（启发式，人工可覆盖）。"""
    code = str(symbol or "").strip().upper()
    digits = code.split(".")[0]
    if digits.startswith("16"):
        return "lof"
    if digits.startswith(ETF_CODE_PREFIXES):
        return "etf"
    return "stock"


class WatchlistService:
    """自选股业务层。"""

    def __init__(self, repo: Optional[WatchlistRepository] = None):
        self.repo = repo or WatchlistRepository()

    # ---------------- 初始化 ---------------- #
    def ensure_ready(self, *, seed_defaults: bool = True) -> Dict[str, int]:
        """确保自选股表可用：先 seed 首批标的（可选），再从配置同步。"""
        seeded = self.seed_defaults() if seed_defaults else 0
        synced = self.sync_from_config()
        return {"seeded": seeded, "synced": synced, "total": self.repo.count()}

    def seed_defaults(self, items: Optional[List[Dict[str, Optional[str]]]] = None) -> int:
        """写入首批 8 只标的（幂等）。返回实际新增条数。"""
        rows = items if items is not None else DEFAULT_WATCHLIST
        created = 0
        for index, item in enumerate(rows):
            symbol = str(item.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            existed = self.repo.get(symbol)
            self.repo.upsert(
                symbol,
                name=item.get("name"),
                asset_type=str(item.get("asset_type") or infer_asset_type(symbol)),
                market=str(item.get("market") or "cn"),
                track_index=item.get("track_index"),
                enabled=True,
                sort_order=index * 10,
            )
            if existed is None:
                created += 1
        return created

    def sync_from_config(self) -> int:
        """把 ``config.stock_list`` 中尚未入库的代码补进表（单向同步）。"""
        try:
            stock_list = list(get_config().stock_list or [])
        except Exception as exc:  # pragma: no cover - 配置异常不应阻断主流程
            logger.warning("读取 config.stock_list 失败：%s", exc)
            return 0

        created = 0
        for raw in stock_list:
            symbol = str(raw or "").strip().upper()
            if not symbol:
                continue
            if self.repo.get(symbol) is not None:
                continue
            self.repo.upsert(symbol, asset_type=infer_asset_type(symbol), enabled=True)
            created += 1
        return created

    # ---------------- 查询 ---------------- #
    def list_watchlist(self, *, enabled_only: bool = True) -> List[Dict]:
        return self.repo.list_dicts(enabled_only=enabled_only)

    def add(
        self,
        symbol: str,
        *,
        name: Optional[str] = None,
        asset_type: Optional[str] = None,
        track_index: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Dict:
        row = self.repo.upsert(
            symbol,
            name=name,
            asset_type=asset_type or infer_asset_type(symbol),
            track_index=track_index,
            note=note,
            enabled=True,
        )
        return self.repo.to_dict(row)

    def disable(self, symbol: str) -> int:
        return self.repo.set_enabled(symbol, False)

    def enable(self, symbol: str) -> int:
        return self.repo.set_enabled(symbol, True)

    def remove(self, symbol: str) -> int:
        return self.repo.remove(symbol)
