# -*- coding: utf-8 -*-
"""高抛低吸助手 · 写操作 API（P2.2 + P2.4 / FR-14.2.4、FR-14.3.3、NFR-11）

写通道（**全部写操作二次确认**，决议 D2 / D3）：

- ``POST /api/v1/actions/preview``        解析一句话写指令 → "将要做什么"清单 + 确认令牌
- ``POST /api/v1/actions/execute``        凭令牌确认执行 → 逐条回执
- ``POST /api/v1/actions/cancel``         放弃待确认操作

审计与实时（P2.4 / NFR-11 / FR-14.0.5）：

- ``GET  /api/v1/actions/audit``          操作记录列表（可按来源 / 动作 / 状态筛选）
- ``GET  /api/v1/actions/audit/{id}``     操作记录详情（含是否可撤销 + 撤销指令预览）
- ``GET  /api/v1/actions/audit/stats``    操作记录统计
- ``POST /api/v1/actions/audit/{id}/undo`` 撤销预览（**同样二次确认**，不直接落库）
- ``GET  /api/v1/actions/stream``         SSE：写操作成功 → ``invalidate`` 端间刷新

所有写操作最终都落 ``ActionService`` 单一写点；本层只做解析、令牌编排、审计查询与广播。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from src.services.action_audit_service import (
    ActionAuditService,
    can_undo,
    publish_invalidate,
)
from src.services.action_service import OP_LABELS, ActionService
from src.services.chat_write_service import ChatWriteService
from src.services.confirmation_store import ConfirmationStore
from src.services.domain_event_bus import get_domain_event_bus

logger = logging.getLogger(__name__)

router = APIRouter()

# 进程级确认令牌表：preview 与 execute 是两次独立请求，需共享同一存储
_STORE = ConfirmationStore()


def _record_memory(record: Dict[str, Any]) -> None:
    """把已确认执行的写操作记入结构化记忆（P2.3 / FR-15.3「用户确认执行的操作」）。

    记忆写入属**数据管理**、非组合写通道，故不经 ``action_service``（决议 D3 边界）。
    """
    try:
        from src.services.chat_memory_service import ChatMemoryService

        ChatMemoryService().record_action(record)
    except Exception as exc:  # 记忆写入失败不影响已完成的写操作
        logger.warning("写操作记忆记录失败: %s", exc)


def _on_audit(record: Dict[str, Any]) -> None:
    """写操作审计钩子（P2.4）：落库 → 记忆 → 端间广播。

    三件事彼此隔离：任一失败都不影响已完成的写操作（``ActionService`` 已兜底异常）。
    """
    audit_id: Optional[int] = None
    try:
        audit_id = ActionAuditService().record(record)
    except Exception as exc:  # pragma: no cover - record 内部已兜底
        logger.warning("写操作审计落库失败: %s", exc)

    if str(record.get("status") or "") != "ok":
        return

    _record_memory(record)
    publish_invalidate(record, audit_id=audit_id)


def _service() -> ChatWriteService:
    return ChatWriteService(
        action_service=ActionService(audit_sink=_on_audit),
        store=_STORE,
    )


def _audit_service() -> ActionAuditService:
    return ActionAuditService()


async def _run(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


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


class ActionUndoRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    actor: str = Field(default="h5", max_length=64)
    session_id: Optional[str] = Field(default=None, max_length=128)


class ActionAuditItem(BaseModel):
    id: int
    source: str = ""
    actor: str = ""
    action: str = ""
    action_label: str = ""
    target: str = ""
    before: Any = None
    after: Any = None
    confirmed: bool = False
    status: str = ""
    description: str = ""
    session_id: Optional[str] = None
    undo_of: Optional[int] = None
    undone_by: Optional[int] = None
    created_at: Optional[str] = None
    undoable: bool = False
    undo_reason: str = ""


class ActionAuditListResponse(BaseModel):
    items: List[ActionAuditItem] = Field(default_factory=list)
    total: int = 0
    limit: int = 100
    offset: int = 0


class ActionAuditDetailResponse(ActionAuditItem):
    undo_ops: List[Dict[str, Any]] = Field(default_factory=list)


class ActionAuditStatsResponse(BaseModel):
    total: int = 0
    ok: int = 0
    rejected: int = 0
    failed: int = 0
    undo_records: int = 0
    by_action: Dict[str, int] = Field(default_factory=dict)


def _with_label(record: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(record)
    payload["action_label"] = OP_LABELS.get(str(record.get("action") or ""), str(record.get("action") or ""))
    return payload


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.post("/preview", response_model=ActionPreviewResponse, summary="写操作预览（生成确认令牌）")
async def actions_preview(request: ActionPreviewRequest) -> ActionPreviewResponse:
    service = _service()
    try:
        result = await _run(
            service.preview_message,
            request.message,
            symbol=request.symbol,
            session_id=request.session_id or "",
            source="h5",
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
        result = await _run(
            service.confirm,
            token=request.token,
            actor="h5",
            source="h5",
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


# ------------------------------ 审计（NFR-11） ------------------------------ #
@router.get("/audit", response_model=ActionAuditListResponse, summary="操作记录列表")
async def actions_audit_list(
    source: Optional[str] = Query(default=None, description="按来源筛选：h5 / feishu"),
    action: Optional[str] = Query(default=None, description="按动作筛选（op）"),
    status: Optional[str] = Query(default=None, description="按状态筛选：ok / rejected / failed"),
    actor: Optional[str] = Query(default=None, description="按操作者筛选"),
    undoing: Optional[bool] = Query(default=None, description="true=仅看撤销记录；false=排除撤销记录"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> ActionAuditListResponse:
    service = _audit_service()
    try:
        result = await _run(
            service.list_records,
            source=source,
            action=action,
            status=status,
            actor=actor,
            undoing=undoing,
            limit=limit,
            offset=offset,
        )
    except Exception as exc:
        logger.error("操作记录查询失败: %s", exc)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": "审计服务暂不可用"})
    items = [_with_label(item) for item in result.get("items", [])]
    return ActionAuditListResponse(
        items=[_audit_item(item) for item in items],
        total=result.get("total", 0),
        limit=result.get("limit", limit),
        offset=result.get("offset", offset),
    )


@router.get("/audit/stats", response_model=ActionAuditStatsResponse, summary="操作记录统计")
async def actions_audit_stats() -> ActionAuditStatsResponse:
    service = _audit_service()
    try:
        result = await _run(service.stats)
    except Exception as exc:
        logger.error("操作记录统计失败: %s", exc)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": "审计服务暂不可用"})
    return ActionAuditStatsResponse(**result)


@router.get("/audit/{audit_id}", response_model=ActionAuditDetailResponse, summary="操作记录详情")
async def actions_audit_detail(audit_id: int) -> ActionAuditDetailResponse:
    service = _audit_service()
    try:
        record = await _run(service.get, audit_id)
    except Exception as exc:
        logger.error("操作记录详情失败: %s", exc)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": "审计服务暂不可用"})
    if record is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "操作记录不存在"})
    return ActionAuditDetailResponse(**_with_label(record))


@router.post(
    "/audit/{audit_id}/undo",
    response_model=ActionPreviewResponse,
    summary="撤销预览（同样二次确认，不直接落库）",
)
async def actions_audit_undo(
    audit_id: int,
    request: Optional[ActionUndoRequest] = None,
) -> ActionPreviewResponse:
    request = request or ActionUndoRequest()
    audit_service = _audit_service()
    try:
        record = await _run(audit_service.get, audit_id, with_undo=False)
    except Exception as exc:
        logger.error("撤销预览读取记录失败: %s", exc)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": "审计服务暂不可用"})
    if record is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "操作记录不存在"})

    verdict = can_undo(record)
    if not verdict["undoable"]:
        raise HTTPException(
            status_code=400,
            detail={"error": "not_undoable", "message": verdict["reason"]},
        )

    service = _service()
    try:
        result = await _run(
            service.preview,
            verdict["ops"],
            actor=request.actor or "h5",
            session_id=request.session_id or "",
            source="h5",
            meta={"undo_of": int(audit_id)},
        )
    except Exception as exc:
        logger.error("撤销预览失败: %s", exc)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": "撤销预览暂不可用"})
    result.setdefault("intent", "undo")
    return ActionPreviewResponse(**result)


def _audit_item(item: Dict[str, Any]) -> ActionAuditItem:
    allowed = set(ActionAuditItem.model_fields)
    return ActionAuditItem(**{k: v for k, v in item.items() if k in allowed})


# ------------------------------ 实时（FR-14.0.5） ------------------------------ #
def _format_sse_event(event_type: str, data: Dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get(
    "/stream",
    responses={200: {"description": "SSE 事件流", "content": {"text/event-stream": {}}}},
    summary="写操作 / 端间刷新 SSE 流",
    description="写操作成功后推送 invalidate 事件，各端据 scopes 增量刷新（无需重开页面）",
)
async def actions_stream() -> StreamingResponse:
    async def event_generator():
        bus = get_domain_event_bus()
        queue: asyncio.Queue = asyncio.Queue()
        yield _format_sse_event("connected", {"message": "Connected to action stream"})
        bus.subscribe(queue)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    yield _format_sse_event(event.type, event.data)
                except asyncio.TimeoutError:
                    yield _format_sse_event("heartbeat", {"timestamp": datetime.now().isoformat()})
        except asyncio.CancelledError:
            logger.debug("SSE action stream disconnected")
            raise
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
