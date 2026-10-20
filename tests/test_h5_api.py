# -*- coding: utf-8 -*-
"""移动 H5（M1）API 契约单测：路由注册、免登录白名单、返回结构。

依赖链缺失（如本机未安装 sqlalchemy / pandas）时整体跳过；CI 完整环境会执行。
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

try:  # 仓库既有约定：轻量环境下用占位模块隔离 litellm 依赖
    import litellm  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.modules["litellm"] = MagicMock()

h5_endpoint = pytest.importorskip(
    "api.v1.endpoints.h5",
    reason="需要完整依赖链（requirements.txt）才能导入端点模块",
)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class FakeService:
    def list_watchlist(self, *, force: bool = False):
        return {
            "as_of": "2026-10-20",
            "items": [
                {
                    "symbol": "601919",
                    "name": "中远海控",
                    "asset_type": "stock",
                    "price": 15.32,
                    "change_percent": 1.24,
                    "action": "reduce",
                    "action_label": "减仓",
                    "display_action": "减仓",
                    "action_tone": "down",
                    "has_signal": True,
                    "is_urgent": False,
                    "scenario_label": "上涨趋势",
                    "signal_created_at": "2026-10-20T20:00:12",
                }
            ],
            "count": 1,
            "degraded": [],
            "disclaimer": "免责声明",
        }

    def detail(self, symbol: str):
        return {
            "symbol": symbol,
            "name": "中远海控",
            "as_of": "2026-10-20",
            "generated_at": "2026-10-20T20:05:00",
            "action": "reduce",
            "action_label": "减仓",
            "display_action": "减仓",
            "action_tone": "down",
            "has_signal": True,
            "is_urgent": False,
            "scenario": "up",
            "scenario_label": "上涨趋势",
            "conclusion": "减仓（上涨趋势）：跌破 5 日线",
            "reason": "短期均线转弱",
            "score": 62,
            "confidence": 0.7,
            "quote": {
                "open": 15.0,
                "high": 15.6,
                "low": 14.9,
                "close": 15.32,
                "prev_close": 15.13,
                "change": 0.19,
                "change_percent": 1.26,
                "volume": 1000000,
                "update_time": "2026-10-20T15:00:00",
            },
            "entry_low": None,
            "entry_high": None,
            "entry_zone": "-",
            "stop_loss": 14.5,
            "target_price": 17.0,
            "position_ratio": 0.2,
            "position_ratio_label": "20%",
            "position_reason": "",
            "discipline": "仓位建议 20% ｜ 止损 14.50",
            "watch_conditions": "",
            "risk_summary": "跌破 MA5",
            "distance_to_stop_pct": 5.66,
            "distance_to_target_pct": -9.88,
            "market": {"indices": [], "as_of": ""},
            "analysis": [
                {
                    "key": "technical",
                    "label": "技术面",
                    "verdict": "上涨趋势｜日线向上",
                    "tone": "up",
                    "available": True,
                    "details": [{"label": "日线趋势", "value": "向上"}],
                },
                {
                    "key": "sector",
                    "label": "板块",
                    "verdict": "板块数据暂未接入",
                    "tone": "neutral",
                    "available": False,
                    "details": [],
                },
            ],
            "signal_id": 12,
            "signal_created_at": "2026-10-20T20:00:12",
            "degraded": [],
            "disclaimer": "免责声明",
        }


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(h5_endpoint, "_SERVICE", FakeService())
    app = FastAPI()
    app.include_router(h5_endpoint.router, prefix="/api/v1/h5")
    return TestClient(app)


def test_watchlist_contract(client):
    response = client.get("/api/v1/h5/watchlist")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["display_action"] == "减仓"
    assert payload["items"][0]["has_signal"] is True


def test_detail_contract(client):
    response = client.get("/api/v1/h5/stocks/601919")
    assert response.status_code == 200
    payload = response.json()
    assert payload["symbol"] == "601919"
    assert payload["quote"]["open"] == 15.0
    assert payload["quote"]["close"] == 15.32
    assert [section["key"] for section in payload["analysis"]] == ["technical", "sector"]
    assert payload["analysis"][1]["available"] is False


def test_detail_rejects_invalid_code(client):
    response = client.get("/api/v1/h5/stocks/not-a-code")
    assert response.status_code == 400


def test_h5_paths_are_public_readonly():
    auth_module = pytest.importorskip(
        "api.middlewares.auth",
        reason="需要完整依赖链（sqlalchemy / python-dotenv）才能导入鉴权中间件",
    )
    # 移动 H5 为内网免登录页：列表与带代码的详情都必须放行
    assert auth_module._path_exempt("/api/v1/h5/watchlist") is True
    assert auth_module._path_exempt("/api/v1/h5/stocks/601919") is True
