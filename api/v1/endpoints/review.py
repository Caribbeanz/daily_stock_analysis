# -*- coding: utf-8 -*-
"""高抛低吸助手 · P3 周度复盘 API（FR-11）

- ``GET  /api/v1/review/weekly``      生成 / 读取本周复盘（FR-11.1~11.4，含 markdown）
- ``GET  /api/v1/review/execution``   读取某周的执行回填（FR-11.2）
- ``POST /api/v1/review/execution``   写入 / 覆盖执行回填（已执行 / 忽略 / 稍后）

说明：
- 复盘是**只读聚合**，不改动任何业务数据；
- 执行回填属数据管理（「是否按建议执行」人工登记），不涉及持仓/权重/止损，故不经
  ``action_service`` 写通道（决议 D3 边界）；回填内容仅供 FR-11.2 统计展示。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from src.repositories.execution_feedback_repo import EXECUTION_STATUSES
from src.services.weekly_review_service import (
    WeeklyReviewService,
    week_key_of,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class WeeklySignalItem(BaseModel):
    signal_id: Optional[int] = None
    symbol: str = ""
    name: str = ""
    action: str = ""
    action_label: str = ""
    scenario: str = ""
    created_at: str = ""
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None
    outcome: str = ""
    direction_correct: Optional[bool] = None
    return_pct: Optional[float] = None
    eval_status: str = ""


class WeeklyPositionItem(BaseModel):
    symbol: str = ""
    name: str = ""
    quantity: float = 0.0
    avg_cost: float = 0.0
    last_price: Optional[float] = None
    week_open_price: Optional[float] = None
    week_return_pct: Optional[float] = None
    week_pnl_amount: Optional[float] = None
    total_return_pct: Optional[float] = None
    execution_status: str = ""
    execution_label: str = ""
    note: Optional[str] = None


class WeeklyMarketItem(BaseModel):
    kind: str = "index"
    name: str = ""
    week_return_pct: Optional[float] = None
    value: Optional[float] = None
    note: str = ""


class WeeklyFocusItem(BaseModel):
    symbol: str = ""
    name: str = ""
    action_label: str = ""
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    entry_zone: str = ""
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None
    note: str = ""


class WeeklyReviewResponse(BaseModel):
    week_key: str = ""
    week_start: str = ""
    week_end: str = ""
    generated_at: str = ""
    headline: str = ""
    signal_summary: Dict[str, Any] = Field(default_factory=dict)
    signals: List[WeeklySignalItem] = Field(default_factory=list)
    positions: List[WeeklyPositionItem] = Field(default_factory=list)
    position_week_pnl: Optional[float] = None
    execution_counts: Dict[str, int] = Field(default_factory=dict)
    market: List[WeeklyMarketItem] = Field(default_factory=list)
    next_week: List[WeeklyFocusItem] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    detail_url: str = ""
    markdown: str = ""


class ExecutionItem(BaseModel):
    id: int = 0
    symbol: str = ""
    week_key: str = ""
    signal_id: Optional[int] = None
    execution_status: str = ""
    note: Optional[str] = None
    source: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ExecutionListResponse(BaseModel):
    week_key: str = ""
    items: List[ExecutionItem] = Field(default_factory=list)
    total: int = 0


class ExecutionUpsertRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    symbol: str = Field(..., min_length=1, max_length=16)
    execution_status: str = Field(..., description="executed | ignored | later")
    week_key: Optional[str] = Field(default=None, description="所属周（周一日期）；缺省为本周")
    signal_id: Optional[int] = Field(default=None, ge=1)
    note: Optional[str] = Field(default=None, max_length=1000)
    source: str = Field(default="web", max_length=16)


class ExecutionUpsertResponse(BaseModel):
    item: ExecutionItem


def _service() -> WeeklyReviewService:
    return WeeklyReviewService()


def _feedback_repo():
    from src.repositories.execution_feedback_repo import ExecutionFeedbackRepository

    return ExecutionFeedbackRepository()


async def _run(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


# --------------------------------------------------------------------------- #
# Endpoints（静态路径先于动态路径）
# --------------------------------------------------------------------------- #
@router.get("/weekly", response_model=WeeklyReviewResponse, summary="周度复盘（FR-11）")
async def weekly_review(
    refresh_outcomes: bool = Query(default=True, description="是否刷新信号结果（较慢，默认开启）"),
    limit_next_week: int = Query(default=20, ge=0, le=100, description="下周关注标的数量上限"),
    symbols: Optional[List[str]] = Query(default=None, description="下周关注标的（缺省为自选股）"),
) -> WeeklyReviewResponse:
    service = WeeklyReviewService(refresh_outcomes=refresh_outcomes, max_next_week=limit_next_week)
    try:
        report = await _run(service.build, symbols=symbols)
    except Exception as exc:
        logger.error("周度复盘生成失败: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": "周度复盘暂不可用"},
        )
    payload = report.to_dict()
    payload["markdown"] = report.to_markdown()
    return WeeklyReviewResponse(**payload)


@router.get("/execution", response_model=ExecutionListResponse, summary="读取执行回填（FR-11.2）")
async def execution_list(
    week_key: Optional[str] = Query(default=None, description="所属周（周一日期）；缺省为本周"),
) -> ExecutionListResponse:
    resolved = str(week_key or week_key_of()).strip()
    try:
        items = await _run(_feedback_repo().list_by_week, resolved)
    except Exception as exc:
        logger.error("读取执行回填失败: %s", exc)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": "读取执行回填暂不可用"})
    return ExecutionListResponse(week_key=resolved, items=[ExecutionItem(**i) for i in items], total=len(items))


@router.post("/execution", response_model=ExecutionUpsertResponse, summary="写入执行回填（FR-11.2）")
async def execution_upsert(request: ExecutionUpsertRequest) -> ExecutionUpsertResponse:
    if request.execution_status not in EXECUTION_STATUSES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_execution_status",
                "message": f"execution_status 须为 {sorted(EXECUTION_STATUSES)}",
            },
        )
    resolved_week = str(request.week_key or week_key_of()).strip()
    try:
        result = await _run(
            _feedback_repo().upsert,
            request.symbol,
            resolved_week,
            request.execution_status,
            signal_id=request.signal_id,
            note=request.note,
            source=request.source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_argument", "message": str(exc)})
    except Exception as exc:
        logger.error("写入执行回填失败: %s", exc)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": "写入执行回填暂不可用"})
    return ExecutionUpsertResponse(item=ExecutionItem(**result))
