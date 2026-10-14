# -*- coding: utf-8 -*-
"""高抛低吸助手 · 写操作 API（P2.2 / FR-14.2.4、FR-14.3.3）

三段式写通道（**全部写操作二次确认**，决议 D2 / D3）：

- ``POST /api/v1/actions/preview``  解析一句话写指令 → 返回"将要做什么"清单 + 确认令牌
- ``POST /api/v1/actions/execute``  凭令牌确认执行 → 逐条回执
- ``POST /api/v1/actions/cancel``   放弃待确认操作

所有写操作最终都落 ``ActionService`` 单一写点；本层只做解析与令牌编排。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from src.services.chat_write_service import ChatWriteService
from src.services.confirmation_store import ConfirmationStore

logger = logging.getLogger(__name__)

router = APIRouter()

# 进程级确认令牌表：preview 与 execute 是两次独立请求，需共享同一存储
_STORE = ConfirmationStore()


def _service() -> ChatWriteService:
    return ChatWriteService(store=_STORE)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class ActionPreviewRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    message: str = Field(..., min_length=1, max_length=2000, description="写指令原话（支持一句话多指令）")
    session_id: Optional[str] = Field(default=None, max_length=128)
    symbol: Optional[str] = Field(default=None, max_length=32, description="上下文标的")


class ActionPreviewResponse(BaseModel):
    status: str
    token: str = ""
    message: str = ""
    summary: str = ""
    intent: str = ""
    expires_at: Optional[float] = None
    items: List[Dict[str, Any]] = Field(default_factory=list)


class ActionExecuteRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=64, description="预览返回的确认令牌")
    session_id: Optional[str] = Field(default=None, max_length=128)


class ActionExecuteResponse(BaseModel):
    status: str
    ok: bool = False
    receipt: str = ""
    items: List[Dict[str, Any]] = Field(default_factory=list)


class ActionCancelRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=64)


class ActionCancelResponse(BaseModel):
    status: str
    message: str = ""


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.post("/preview", response_model=ActionPreviewResponse, summary="写操作预览（生成确认令牌）")
async def actions_preview(request: ActionPreviewRequest) -> ActionPreviewResponse:
    service = _service()
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: service.preview_message(
                request.message,
                symbol=request.symbol,
                session_id=request.session_id or "",
                source="h5",
            ),
        )
    except Exception as exc:
        logger.error("写操作预览失败: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": "写操作预览暂不可用"},
        )
    return ActionPreviewResponse(**result)


@router.post("/execute", response_model=ActionExecuteResponse, summary="写操作执行（二次确认后）")
async def actions_execute(request: ActionExecuteRequest) -> ActionExecuteResponse:
    service = _service()
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: service.confirm(token=request.token, actor="h5", source="h5"),
        )
    except Exception as exc:
        logger.error("写操作执行失败: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": "写操作执行暂不可用"},
        )
    return ActionExecuteResponse(**result)


@router.post("/cancel", response_model=ActionCancelResponse, summary="放弃待确认操作")
async def actions_cancel(request: ActionCancelRequest) -> ActionCancelResponse:
    service = _service()
    result = service.cancel(token=request.token)
    return ActionCancelResponse(**result)
