# -*- coding: utf-8 -*-
"""高抛低吸助手 · 对话·读 API（P2.1 / FR-14.3）

统一读对话入口。**不含任何写操作**——写指令一律返回"P2.2 开放"的回执，
真正的执行落在 P2.2 的 ``action_service``（决议 D3）。

- ``POST /api/v1/chat/ask``      读对话（意图分流 + 风险提示）
- ``GET  /api/v1/chat/glossary`` 名词表（FR-14.3.2）
- ``POST /api/v1/chat/feishu/events`` 飞书机器人事件回调（只读，见 feishu_inbound）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from src.services.chat_read_service import ChatReadService

logger = logging.getLogger(__name__)

router = APIRouter()


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class ChatAskRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    message: str = Field(..., min_length=1, max_length=2000)
    session_id: Optional[str] = Field(default=None, max_length=128)
    symbol: Optional[str] = Field(default=None, max_length=32, description="上下文标的（详情页「就地问」传入）")


class ChatAskResponse(BaseModel):
    intent: str
    answer: str
    risk_note: str = ""
    symbols: List[str] = Field(default_factory=list)
    terms: List[str] = Field(default_factory=list)
    degraded: bool = False
    source: str = ""
    reason: str = ""
    session_id: str = ""


class GlossaryItem(BaseModel):
    term: str
    explanation: str
    aliases: List[str] = Field(default_factory=list)


class GlossaryResponse(BaseModel):
    items: List[GlossaryItem]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.post("/ask", response_model=ChatAskResponse, summary="对话·读（意图分流 + 风险提示）")
async def chat_ask(request: ChatAskRequest) -> ChatAskResponse:
    """读对话统一入口：问答 / 名词解释 / 个股深度分析 / 持仓体检。

    写操作（增删自选 / 改持仓 / 调权重 / 设止损）只识别、不执行，
    返回"P2.2 开放"回执；越界请求（下单）明确拒绝（FR-14.3.9）。
    """
    service = ChatReadService()
    session_id = request.session_id or ""

    def _run():
        return service.ask(request.message, symbol_hint=request.symbol, session_id=session_id)

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _run)
    except Exception as exc:
        logger.error("对话·读失败: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": "对话服务暂不可用"},
        )

    payload: Dict[str, Any] = result.to_dict()
    payload["session_id"] = session_id
    return ChatAskResponse(**payload)


@router.get("/glossary", response_model=GlossaryResponse, summary="名词表（FR-14.3.2）")
async def chat_glossary(
    q: Optional[str] = Query(default=None, description="按名词/别名查询；留空返回全部"),
    limit: int = Query(default=50, ge=1, le=200),
) -> GlossaryResponse:
    from src.services import glossary as glossary_service

    if q and q.strip():
        entry = glossary_service.lookup(q)
        if entry is not None:
            items = [entry]
        else:
            items = glossary_service.search(q, limit=limit)
    else:
        items = glossary_service.list_entries()[:limit]

    return GlossaryResponse(
        items=[
            GlossaryItem(term=e.term, explanation=e.explanation, aliases=list(e.aliases))
            for e in items
        ]
    )


@router.post("/feishu/events", summary="飞书机器人事件回调（只读，P2.1）")
async def chat_feishu_events(payload: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    """飞书事件订阅回调：URL 校验 + 消息事件 → 只读对话 → 文本回复。

    - 本人 `open_id`（白名单）：可读；写操作回执"P2.2 开放"；
    - 群内其他人：只读，写请求回复无权限（决议 D4）。
    """
    from src.services.feishu_inbound import FeishuInboundService

    service = FeishuInboundService()
    try:
        return service.handle_event(payload)
    except Exception as exc:
        logger.error("飞书事件处理失败: %s", exc)
        return {"code": 0, "msg": "internal error handled"}
