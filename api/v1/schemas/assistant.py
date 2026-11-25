# -*- coding: utf-8 -*-
"""助手配置 API 的请求/响应模型（只读快照端点直接返回快照子行）。"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class PresetUpdateRequest(BaseModel):
    preset: str = Field(..., description="loose | standard | strict")


class OverrideUpdateRequest(BaseModel):
    key: str = Field(..., description="参数键名（须为可覆盖的数值/布尔参数）")
    value: Any = Field(..., description="新值；越界或非数值键会被拒绝")


class FundsUpdateRequest(BaseModel):
    total_capital: float = Field(..., gt=0, description="总资金（用户填写，无默认值）")
    cash_available: Optional[float] = Field(None, ge=0, description="可用现金，留空则按总资金减持仓市值推算")


class AssistantWriteResponse(BaseModel):
    ok: bool
    effective_date: Optional[str] = Field(None, description="最早生效快照日（下一次快照生成时生效）")
    effective_snapshot_date: Optional[str] = Field(None, description="切档/资金变更的生效日")
    param_hash: Optional[str] = None
    key: Optional[str] = None
    value: Optional[Any] = None
    requires_confirm: bool = False
