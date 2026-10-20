# -*- coding: utf-8 -*-
"""高抛低吸助手 · 移动 H5 只读接口（M1）

- ``GET /api/v1/h5/watchlist``      自选股列表（名称 / 代码 / 当日价 / 涨跌幅 / 建议）
- ``GET /api/v1/h5/stocks/{code}``  个股详情（四价 + 建议总结 + 五维分析）

约束：

1. **只读**：不提供任何写能力，也不返回持仓明细；
2. **免登录**：两条路径已加入鉴权白名单（内网形态，与大屏一致）；
3. **四端同源**：结论与四要素来自已落库 ``decision_signals``，本接口只做聚合；
4. **失败隔离 + 诚实降级**：单项失败写入 ``degraded``，不伪装正常。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from api.v1.endpoints.stocks import _validate_and_normalize_stock_code
from api.v1.schemas.h5 import H5DetailResponse, H5WatchlistResponse
from src.services.h5_service import H5Service

logger = logging.getLogger(__name__)

router = APIRouter()

_SERVICE: Optional[H5Service] = None
_SERVICE_LOCK = threading.Lock()


def _service() -> H5Service:
    """进程级单例：短缓存需要跨请求复用，否则每次请求都会重新取数。"""

    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            cache_seconds = 15
            market_cache_seconds = 900
            sector_cache_seconds = 900
            industry_cache_seconds = 604800
            try:
                from src.config import get_config

                config = get_config()
                cache_seconds = int(getattr(config, "hslb_h5_cache_seconds", 15) or 0)
                market_cache_seconds = int(getattr(config, "hslb_h5_market_cache_seconds", 900) or 0)
                sector_cache_seconds = int(getattr(config, "hslb_h5_sector_cache_seconds", 900) or 0)
                industry_cache_seconds = int(
                    getattr(config, "hslb_h5_industry_cache_seconds", 604800) or 0
                )
            except Exception as exc:  # pragma: no cover - 配置不可用不阻断页面
                logger.warning("读取 H5 缓存配置失败，使用默认值：%s", exc)
            _SERVICE = H5Service(
                cache_ttl_seconds=cache_seconds,
                market_cache_ttl_seconds=market_cache_seconds,
                sector_cache_ttl_seconds=sector_cache_seconds,
                industry_cache_ttl_seconds=industry_cache_seconds,
            )
        return _SERVICE


async def _run(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


@router.get(
    "/watchlist",
    response_model=H5WatchlistResponse,
    summary="移动 H5 自选股列表",
)
async def h5_watchlist(
    refresh: bool = Query(False, description="忽略短缓存，强制重新聚合"),
    with_market: bool = Query(True, description="是否附带首页大盘块（轻量轮询可关）"),
) -> H5WatchlistResponse:
    try:
        payload = await _run(_service().list_watchlist, force=refresh, with_market=with_market)
    except Exception as exc:
        logger.error("H5 列表聚合失败: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": "自选股列表暂不可用"},
        )
    return H5WatchlistResponse(**payload)


@router.get(
    "/stocks/{stock_code}",
    response_model=H5DetailResponse,
    summary="移动 H5 个股详情",
)
async def h5_stock_detail(stock_code: str) -> H5DetailResponse:
    normalized = _validate_and_normalize_stock_code(stock_code)
    try:
        payload = await _run(_service().detail, normalized)
    except Exception as exc:
        logger.error("H5 详情聚合失败 %s: %s", normalized, exc)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": "个股详情暂不可用"},
        )
    return H5DetailResponse(**payload)
