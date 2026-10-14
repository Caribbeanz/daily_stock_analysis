# -*- coding: utf-8 -*-
"""高抛低吸助手 · 结构化记忆管理 API（P2.3 / FR-15.5）

- ``GET    /api/v1/memory``              记忆列表（可按类型 / 会话筛选，含软删）
- ``GET    /api/v1/memory/stats``        记忆统计（按类型）
- ``GET    /api/v1/memory/{id}``         记忆详情（含回链来源消息，FR-15.4）
- ``DELETE /api/v1/memory/{id}``         删除记忆（默认软删，``?hard=true`` 物理删）
- ``POST   /api/v1/memory/{id}/restore`` 恢复软删记忆
- ``POST   /api/v1/memory/compress``     触发某会话的记忆压缩
- ``POST   /api/v1/memory/clear``        一键清空（**需 confirm=true** 强确认，决议 D5）

**只增不删**：压缩不删除原文（决议 D5）；删除 / 清空是用户显式的数据管理操作，
不属"持仓/权重/止损"写通道，故不经 ``action_service``（决议 D3 边界）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from src.services.chat_memory_service import MEMORY_TYPES, ChatMemoryService

logger = logging.getLogger(__name__)

router = APIRouter()


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class MemoryItem(BaseModel):
    id: int
    memory_type: str
    content: str
    source_message_ids: List[int] = Field(default_factory=list)
    session_id: Optional[str] = None
    status: str = "active"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class MemoryListResponse(BaseModel):
    items: List[MemoryItem] = Field(default_factory=list)
    total: int = 0
    limit: int = 100
    offset: int = 0


class MemorySource(BaseModel):
    id: int
    role: str = ""
    content: str = ""
    created_at: Optional[str] = None


class MemoryDetailResponse(MemoryItem):
    sources: List[MemorySource] = Field(default_factory=list)


class MemoryStatsResponse(BaseModel):
    active: int = 0
    deleted: int = 0
    total: int = 0
    by_type: Dict[str, int] = Field(default_factory=dict)


class MemoryDeleteResponse(BaseModel):
    deleted: bool = False
    mode: str = ""
    id: int = 0


class MemoryRestoreResponse(BaseModel):
    restored: bool = False
    id: int = 0


class MemoryClearRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    confirm: bool = Field(default=False, description="一键清空需显式 true（强确认）")
    session_id: Optional[str] = Field(default=None, max_length=128, description="仅清空指定会话；留空清空全部")
    hard: bool = Field(default=True, description="true=物理删除；false=全部软删")


class MemoryClearResponse(BaseModel):
    cleared: int = 0
    mode: str = ""
    session_id: Optional[str] = None


class MemoryCompressRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(..., min_length=1, max_length=128)
    force: bool = Field(default=False, description="忽略阈值 / 周节奏，立即压缩")


class MemoryCompressResponse(BaseModel):
    compressed: int = 0
    scanned: int = 0
    last_message_id: Optional[int] = None
    skipped: str = ""
    types: Dict[str, int] = Field(default_factory=dict)


def _service() -> ChatMemoryService:
    return ChatMemoryService()


async def _run(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


# --------------------------------------------------------------------------- #
# Endpoints（注意：静态路径需声明在 /{memory_id} 之前）
# --------------------------------------------------------------------------- #
@router.get("", response_model=MemoryListResponse, summary="记忆列表（FR-15.5）")
async def memory_list(
    memory_type: Optional[str] = Query(default=None, description="按类型筛选"),
    session_id: Optional[str] = Query(default=None, description="按会话筛选"),
    include_deleted: bool = Query(default=False, description="是否包含已软删记忆"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> MemoryListResponse:
    if memory_type and memory_type not in MEMORY_TYPES:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_memory_type", "message": f"memory_type 须为 {list(MEMORY_TYPES)}"},
        )
    service = _service()
    try:
        result = await _run(
            service.list_memories,
            memory_type=memory_type,
            session_id=session_id,
            include_deleted=include_deleted,
            limit=limit,
            offset=offset,
        )
    except Exception as exc:
        logger.error("记忆列表失败: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": "记忆服务暂不可用"},
        )
    return MemoryListResponse(**result)


@router.get("/stats", response_model=MemoryStatsResponse, summary="记忆统计")
async def memory_stats() -> MemoryStatsResponse:
    service = _service()
    try:
        result = await _run(service.stats)
    except Exception as exc:
        logger.error("记忆统计失败: %s", exc)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": "记忆服务暂不可用"})
    return MemoryStatsResponse(**result)


@router.post("/compress", response_model=MemoryCompressResponse, summary="触发记忆压缩")
async def memory_compress(request: MemoryCompressRequest) -> MemoryCompressResponse:
    service = _service()
    try:
        result = await _run(service.compress_session, request.session_id, force=request.force)
    except Exception as exc:
        logger.error("记忆压缩失败: %s", exc)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": "记忆压缩暂不可用"})
    payload = {k: v for k, v in result.items() if k in MemoryCompressResponse.model_fields}
    return MemoryCompressResponse(**payload)


@router.post("/clear", response_model=MemoryClearResponse, summary="一键清空记忆（强确认）")
async def memory_clear(request: MemoryClearRequest) -> MemoryClearResponse:
    if not request.confirm:
        raise HTTPException(
            status_code=400,
            detail={"error": "confirmation_required", "message": "一键清空属破坏性操作，需 confirm=true"},
        )
    service = _service()
    try:
        result = await _run(service.clear, session_id=request.session_id, hard=request.hard)
    except Exception as exc:
        logger.error("清空记忆失败: %s", exc)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": "清空记忆暂不可用"})
    return MemoryClearResponse(**result)


@router.get("/{memory_id}", response_model=MemoryDetailResponse, summary="记忆详情（含来源）")
async def memory_detail(memory_id: int) -> MemoryDetailResponse:
    service = _service()
    try:
        record = await _run(service.get_memory, memory_id)
    except Exception as exc:
        logger.error("读取记忆详情失败: %s", exc)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": "记忆服务暂不可用"})
    if record is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "记忆不存在"})
    return MemoryDetailResponse(**record)


@router.delete("/{memory_id}", response_model=MemoryDeleteResponse, summary="删除记忆（默认软删）")
async def memory_delete(
    memory_id: int,
    hard: bool = Query(default=False, description="true=物理删除；false=软删（保留可审计）"),
) -> MemoryDeleteResponse:
    service = _service()
    try:
        result = await _run(service.delete, memory_id, hard=hard)
    except Exception as exc:
        logger.error("删除记忆失败: %s", exc)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": "删除记忆暂不可用"})
    if not result.get("deleted"):
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "记忆不存在"})
    return MemoryDeleteResponse(**result)


@router.post("/{memory_id}/restore", response_model=MemoryRestoreResponse, summary="恢复软删记忆")
async def memory_restore(memory_id: int) -> MemoryRestoreResponse:
    service = _service()
    try:
        result = await _run(service.restore, memory_id)
    except Exception as exc:
        logger.error("恢复记忆失败: %s", exc)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": "恢复记忆暂不可用"})
    if not result.get("restored"):
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "记忆不存在"})
    return MemoryRestoreResponse(**result)
