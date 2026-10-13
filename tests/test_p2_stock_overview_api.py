# -*- coding: utf-8 -*-
"""P2.0 个股详情聚合 · API 契约单测。

只校验路由注册与端点行为（含参数校验、错误包装），不启动 HTTP 服务。

> 依赖说明：`api.v1.endpoints.stocks` 会连带导入分析器依赖链
> （litellm / json_repair / newspaper 等）。**在仅装了轻量依赖的环境里本文件整体跳过**；
> 装齐 `requirements.txt` 的环境会正常执行。
"""

from __future__ import annotations

import sys
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi")

try:  # 仓库既有约定：轻量环境下用占位模块隔离 litellm 依赖
    import litellm  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - 依赖轻量测试环境
    sys.modules["litellm"] = MagicMock()

stocks_endpoint = pytest.importorskip(
    "api.v1.endpoints.stocks",
    reason="需要完整依赖链（requirements.txt）才能导入端点模块",
)

from fastapi import HTTPException  # noqa: E402

from api.v1.schemas.stocks import StockOverviewResponse  # noqa: E402
from src.services.stock_overview_service import build_overview  # noqa: E402


def _signal() -> Dict[str, Any]:
    return {
        "id": 42,
        "stock_code": "601919",
        "stock_name": "中远海控",
        "action": "reduce",
        "score": 62,
        "confidence": 0.8,
        "entry_low": 12.1,
        "entry_high": 12.8,
        "stop_loss": 11.5,
        "target_price": 14.2,
        "reason": "[reduce] 接近箱体上沿，建议减仓",
        "metadata": {"scenario": "sideways", "position_advice": {"ratio": 0.3}},
    }


def test_route_registered_with_response_model():
    matches = [
        route
        for route in stocks_endpoint.router.routes
        if getattr(route, "path", "") == "/{stock_code}/overview"
    ]
    assert len(matches) == 1
    assert matches[0].response_model is StockOverviewResponse
    assert "GET" in matches[0].methods


def test_endpoint_passes_validated_code_and_returns_schema(monkeypatch):
    captured: Dict[str, Any] = {}

    class _Service:
        def load(self, symbol: str, *, with_quote: bool = True) -> Dict[str, Any]:
            captured["symbol"] = symbol
            captured["with_quote"] = with_quote
            return build_overview(symbol=symbol, signal=_signal()).to_dict()

    monkeypatch.setattr(stocks_endpoint, "StockOverviewService", lambda: _Service())
    response = stocks_endpoint.get_stock_overview("601919", with_quote=False)

    assert isinstance(response, StockOverviewResponse)
    assert response.action_label == "减仓"
    assert captured["symbol"] == "601919"
    assert captured["with_quote"] is False


def test_endpoint_rejects_invalid_code():
    with pytest.raises(HTTPException) as excinfo:
        stocks_endpoint.get_stock_overview("", with_quote=False)
    assert excinfo.value.status_code == 400


def test_endpoint_wraps_internal_error(monkeypatch):
    class _Service:
        def load(self, symbol: str, *, with_quote: bool = True) -> Dict[str, Any]:
            raise RuntimeError("explode")

    monkeypatch.setattr(stocks_endpoint, "StockOverviewService", lambda: _Service())
    with pytest.raises(HTTPException) as excinfo:
        stocks_endpoint.get_stock_overview("601919", with_quote=False)
    assert excinfo.value.status_code == 500
    assert excinfo.value.detail["error"] == "internal_error"
