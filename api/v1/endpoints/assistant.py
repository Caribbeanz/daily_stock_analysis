# -*- coding: utf-8 -*-
"""助手只读快照 API + 参数配置 API（方案 §8.1 / §8.2）。

三条护栏：

1. **只读快照**：所有 GET 只从 L3 快照回读，不重算、不改写、不触发取数。
2. **三元组一致**：每个响应带 ``data_date / snapshot_id / calc_version``；
   不完整或请求的 snapshot_id 与库里不一致即返回 409「待同步」，不硬拼结论。
3. **鉴权**：由全局 auth 中间件统一处理（未启用鉴权时与现有 API 同策略），
   本模块不另开白名单。

配置写入走 :class:`AssistantConfigService`，append-only 留痕、下一次快照生效。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query

from api.deps import get_assistant_config_service, get_assistant_store
from api.v1.errors import api_error
from api.v1.schemas.assistant import (
    AssistantWriteResponse,
    FundsUpdateRequest,
    OverrideUpdateRequest,
    PresetUpdateRequest,
)
from src.assistant.config.service import ConfigValidationError
from src.assistant.stage_text import stage_detail

logger = logging.getLogger(__name__)

router = APIRouter()

TRIPLE_KEYS = ("data_date", "snapshot_id", "calc_version")


def _load(store: Any, snapshot_id: str) -> dict:
    content = store.snapshot_content(snapshot_id)
    if content is None:
        raise api_error(404, "snapshot_not_found", "快照不存在")
    missing = [key for key in TRIPLE_KEYS if not content.get(key)]
    if missing or content["snapshot_id"] != snapshot_id:
        raise api_error(409, "out_of_sync", "待同步：快照三元组不完整或不一致")
    return content


def _envelope(content: dict, key: str, value: Any) -> dict:
    return {
        **{name: content[name] for name in TRIPLE_KEYS},
        "bar_date": content.get("bar_date"),
        key: value,
    }


def _bad_config(exc: Exception) -> Any:
    return api_error(400, "invalid_config", str(exc))


# ---------------------------------------------------------------------------
# 只读快照（§8.1）
# ---------------------------------------------------------------------------
@router.get("/snapshot")
def get_snapshot(
    data_date: date | None = Query(None, description="快照日；留空取最新"),
    store: Any = Depends(get_assistant_store),
) -> dict:
    row = store.latest_snapshot(data_date)
    if row is None:
        raise api_error(404, "snapshot_not_found", "该交易日没有可用快照")
    return row


@router.get("/market")
def get_market(
    snapshot_id: str = Query(...),
    store: Any = Depends(get_assistant_store),
) -> dict:
    content = _load(store, snapshot_id)
    market = store.snapshot_market(snapshot_id)
    if market is None:
        # 管线目前只把大盘一句写进正文，未落 market_snapshot 表：如实标不可用
        return _envelope(content, "market_snapshot", {"available": False, "reason": "未写入快照"})
    return _envelope(content, "market_snapshot", {**market, "available": True})


@router.get("/sectors")
def get_sectors(
    snapshot_id: str = Query(...),
    store: Any = Depends(get_assistant_store),
) -> dict:
    content = _load(store, snapshot_id)
    return _envelope(content, "sector_snapshot", store.snapshot_sectors(snapshot_id))


@router.get("/holdings")
def get_holdings(
    snapshot_id: str = Query(...),
    store: Any = Depends(get_assistant_store),
) -> dict:
    content = _load(store, snapshot_id)
    return _envelope(content, "holding_conclusion", content.get("conclusions") or [])


@router.get("/holding/{code}")
def get_holding(
    code: str,
    snapshot_id: str = Query(...),
    store: Any = Depends(get_assistant_store),
) -> dict:
    content = _load(store, snapshot_id)
    rows = [row for row in (content.get("conclusions") or []) if row.get("code") == code]
    if not rows:
        raise api_error(404, "holding_not_found", "该快照中没有这只标的")
    row = rows[0]
    text = store.load_llm_text(snapshot_id, "stock", code)
    return _envelope(
        content,
        "holding_conclusion",
        {
            **row,
            # 阶段打法/允许/禁止/切换由后端给（与飞书同源），前端不拼文案
            "stage_detail": stage_detail(row.get("stage") or "", row.get("stage_confidence")),
            "conditional_plan": [item for item in (content.get("plans") or []) if item.get("code") == code],
            "risk_signals": [item for item in (content.get("risks") or []) if item.get("code") == code],
            # 详情页要的「个股人话结论」：只读缓存，不在 GET 里触发 LLM
            "llm_text": {
                "available": text is not None,
                "text": (text or {}).get("text", ""),
                "model": (text or {}).get("model", ""),
                "generated_at": (text or {}).get("generated_at", ""),
            },
        },
    )


@router.get("/plans/{code}")
def get_plans(
    code: str,
    snapshot_id: str = Query(...),
    store: Any = Depends(get_assistant_store),
) -> dict:
    content = _load(store, snapshot_id)
    return _envelope(
        content, "conditional_plan", [row for row in (content.get("plans") or []) if row.get("code") == code]
    )


@router.get("/risks")
def get_risks(
    snapshot_id: str = Query(...),
    store: Any = Depends(get_assistant_store),
) -> dict:
    content = _load(store, snapshot_id)
    return _envelope(content, "risk_signals", content.get("risks") or [])


@router.get("/llm_text")
def get_llm_text(
    snapshot_id: str = Query(...),
    position: str = Query(..., description="summary | stock | market"),
    code: str | None = Query(None, description="个股文案的代码；summary 留空"),
    store: Any = Depends(get_assistant_store),
) -> dict:
    content = _load(store, snapshot_id)
    cached = store.load_llm_text(snapshot_id, position, code)
    if cached is None:
        return _envelope(
            content, "llm_text", {"available": False, "text": "", "model": "", "channel": "", "generated_at": ""}
        )
    return _envelope(
        content,
        "llm_text",
        {
            "available": True,
            "text": cached["text"],
            "model": cached["model"],
            "channel": cached["channel"],
            "generated_at": cached["generated_at"],
        },
    )


# ---------------------------------------------------------------------------
# 参数配置（§8.2；资金落 L4 见 D-12）
# ---------------------------------------------------------------------------
@router.get("/params")
def get_params(service: Any = Depends(get_assistant_config_service)) -> dict:
    return service.current()


@router.put("/params/preset", response_model=AssistantWriteResponse)
def put_preset(
    request: PresetUpdateRequest,
    service: Any = Depends(get_assistant_config_service),
) -> dict:
    try:
        return service.set_preset(request.preset)
    except ConfigValidationError as exc:
        raise _bad_config(exc) from exc


@router.put("/params/override", response_model=AssistantWriteResponse)
def put_override(
    request: OverrideUpdateRequest,
    service: Any = Depends(get_assistant_config_service),
) -> dict:
    try:
        return service.set_override(request.key, request.value)
    except ConfigValidationError as exc:
        raise _bad_config(exc) from exc


@router.delete("/params/override", response_model=AssistantWriteResponse)
def delete_override(
    key: str = Query(..., description="要回滚的参数键"),
    service: Any = Depends(get_assistant_config_service),
) -> dict:
    try:
        return service.rollback_override(key)
    except ConfigValidationError as exc:
        raise _bad_config(exc) from exc


@router.put("/params/funds", response_model=AssistantWriteResponse)
def put_funds(
    request: FundsUpdateRequest,
    service: Any = Depends(get_assistant_config_service),
) -> dict:
    """保存总资金/可用现金到 L4（env 仅作初始值）。"""
    try:
        return service.set_funds(request.total_capital, request.cash_available)
    except ConfigValidationError as exc:
        raise _bad_config(exc) from exc
