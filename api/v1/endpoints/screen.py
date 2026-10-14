# -*- coding: utf-8 -*-
"""高抛低吸助手 · P4 大屏 API（FR-14.1）

- ``GET /api/v1/screen/snapshot``  一屏快照（指数 / 自选股卡片 / 持仓汇总 / 事件 / 复盘摘要）

说明：

1. **只读**：不改动任何业务数据，不引入写通道（FR-14.1.1 不做盘中监控，仅收盘后展示）；
2. **免登录常驻**：该路径已在 ``api/middlewares/auth.py`` 的 ``EXEMPT_PATHS`` 中放行，
   以满足 FR-14.1.4「浏览器全屏、免登录常驻」（NFR-12 内网 / 免登录）；
   快照**不含任何写能力与密钥**，仅展示本人自选股 / 持仓的行情与结论；
3. **数据截止可见**：响应显式返回 ``market_date``（最新交易日）与 ``quote_as_of``（行情时间），
   由前端显著标注（FR-14.1.3）；
4. **四端同源（NFR-9）**：卡片结论复用 H5 同一 :class:`StockOverviewService`，不另立口径。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class ScreenIndexItem(BaseModel):
    code: str = ""
    name: str = ""
    value: Optional[float] = None
    change_pct: Optional[float] = None


class ScreenCardItem(BaseModel):
    symbol: str = ""
    name: str = ""
    price: Optional[float] = None
    change_pct: Optional[float] = None
    quote_time: Optional[str] = None
    action: str = "watch"
    action_label: str = "观望"
    scenario_label: str = "趋势不明"
    conclusion: str = ""
    is_urgent: bool = False
    score: Optional[int] = None
    entry_zone: str = "-"
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None
    position_ratio_label: str = "-"
    distance_to_stop_pct: Optional[float] = None
    distance_to_target_pct: Optional[float] = None
    signal_created_at: Optional[str] = None
    has_position: bool = False
    quantity: float = 0.0
    avg_cost: Optional[float] = None
    pnl_pct: Optional[float] = None
    position_value: Optional[float] = None
    degraded: List[str] = Field(default_factory=list)


class ScreenPortfolioBlock(BaseModel):
    position_count: int = 0
    priced_count: int = 0
    total_cost: Optional[float] = None
    market_value: Optional[float] = None
    pnl_amount: Optional[float] = None
    pnl_pct: Optional[float] = None
    winners: int = 0
    losers: int = 0
    complete: bool = True


class ScreenSnapshotResponse(BaseModel):
    generated_at: str = ""
    market_date: str = ""
    quote_as_of: str = ""
    refresh_seconds: int = 300
    indices: List[ScreenIndexItem] = Field(default_factory=list)
    cards: List[ScreenCardItem] = Field(default_factory=list)
    portfolio: ScreenPortfolioBlock = Field(default_factory=ScreenPortfolioBlock)
    events: List[str] = Field(default_factory=list)
    weekly: Dict[str, Any] = Field(default_factory=dict)
    action_counts: Dict[str, int] = Field(default_factory=dict)
    urgent_count: int = 0
    degraded: List[str] = Field(default_factory=list)
    disclaimer: str = ""


# --------------------------------------------------------------------------- #
# 依赖
# --------------------------------------------------------------------------- #
def _service(*, force_refresh: bool = False):
    from src.config import get_config
    from src.services.screen_service import ScreenService

    refresh_seconds = 300
    cache_seconds = 15
    try:
        config = get_config()
        refresh_seconds = int(getattr(config, "hslb_screen_refresh_seconds", 300) or 300)
        cache_seconds = int(getattr(config, "hslb_screen_cache_seconds", 15) or 0)
    except Exception as exc:  # pragma: no cover - 配置不可用不阻断大屏
        logger.warning("读取大屏配置失败，使用默认值：%s", exc)

    service = ScreenService(
        refresh_seconds=refresh_seconds,
        cache_ttl_seconds=0 if force_refresh else cache_seconds,
    )
    return service


async def _run(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.get(
    "/snapshot",
    response_model=ScreenSnapshotResponse,
    summary="大屏一屏快照（FR-14.1）",
)
async def screen_snapshot(
    symbols: Optional[List[str]] = Query(default=None, description="标的；缺省为自选股 + 持仓"),
    with_quote: bool = Query(default=True, description="是否取实时行情（离线排查可关）"),
    with_weekly: bool = Query(default=True, description="是否附本周复盘摘要"),
    limit_events: int = Query(default=6, ge=0, le=20, description="事件提醒条数上限"),
    refresh: bool = Query(default=False, description="忽略快照缓存，强制重新聚合"),
) -> ScreenSnapshotResponse:
    service = _service(force_refresh=refresh)
    try:
        payload = await _run(
            service.build,
            symbols,
            with_quote=with_quote,
            with_weekly=with_weekly,
            limit_events=limit_events,
        )
    except Exception as exc:
        logger.error("大屏快照生成失败: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": "大屏快照暂不可用"},
        )
    return ScreenSnapshotResponse(**payload)
