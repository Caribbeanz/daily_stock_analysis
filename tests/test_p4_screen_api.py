# -*- coding: utf-8 -*-
"""P4 大屏 API 契约单测（路由注册 + 免登录白名单 + 端点行为）。

用最小 FastAPI 应用挂载 /api/v1/screen 做真实 HTTP 调用（httpx 可用）；
另校验真实应用确实在 /api/v1 下挂载了该路由。依赖链缺失时整体跳过。
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

screen_endpoint = pytest.importorskip(
    "api.v1.endpoints.screen",
    reason="需要完整依赖链（requirements.txt）才能导入端点模块",
)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


SAMPLE_PAYLOAD = {
    "generated_at": "2026-10-14 15:30:00",
    "market_date": "2026-10-14",
    "quote_as_of": "2026-10-14 15:00:00",
    "refresh_seconds": 300,
    "indices": [
        {"code": "sh000001", "name": "上证指数", "value": 3842.0, "change_pct": 1.105}
    ],
    "cards": [
        {
            "symbol": "601169",
            "name": "北京银行",
            "price": 6.42,
            "change_pct": 0.63,
            "action": "reduce",
            "action_label": "减仓",
            "conclusion": "减仓（上涨趋势）：触及止盈位",
            "has_position": True,
            "quantity": 1900.0,
            "avg_cost": 6.418,
            "pnl_pct": 0.031,
        }
    ],
    "portfolio": {
        "position_count": 1,
        "priced_count": 1,
        "total_cost": 12194.2,
        "market_value": 12198.0,
        "pnl_amount": 3.8,
        "pnl_pct": 0.031,
        "winners": 1,
        "losers": 0,
        "complete": True,
    },
    "events": ["601169：发布三季报"],
    "weekly": {"week_key": "2026-10-12", "headline": "本周胜率 60%（6/10）", "win_rate": 60.0},
    "action_counts": {"reduce": 1},
    "urgent_count": 0,
    "degraded": [],
    "disclaimer": "仅供学习参考",
}


class FakeService:
    def __init__(self, payload=None, error=None):
        self.payload = SAMPLE_PAYLOAD if payload is None else payload
        self.error = error
        self.calls = []

    def build(self, symbols=None, *, with_quote=True, with_weekly=True, limit_events=6):
        self.calls.append(
            {
                "symbols": symbols,
                "with_quote": with_quote,
                "with_weekly": with_weekly,
                "limit_events": limit_events,
            }
        )
        if self.error is not None:
            raise self.error
        return dict(self.payload)


@pytest.fixture()
def client(monkeypatch):
    created = {}

    def factory(*, force_refresh=False):
        service = FakeService()
        created["service"] = service
        created["force_refresh"] = force_refresh
        return service

    monkeypatch.setattr(screen_endpoint, "_service", factory)

    app = FastAPI()
    app.include_router(screen_endpoint.router, prefix="/api/v1/screen")
    with TestClient(app) as test_client:
        yield test_client, created


# --------------------------------------------------------------------------- #
# 路由与白名单
# --------------------------------------------------------------------------- #
def test_router_registers_snapshot():
    paths = {getattr(route, "path", "") for route in screen_endpoint.router.routes}
    assert "/snapshot" in paths


def test_real_app_mounts_screen_route():
    try:
        from api.app import create_app
    except Exception as exc:  # pragma: no cover - 依赖链不完整
        pytest.skip(f"应用无法构建：{exc}")

    paths = create_app().openapi()["paths"]
    assert "/api/v1/screen/snapshot" in paths
    assert "get" in paths["/api/v1/screen/snapshot"]


def test_auth_exempt_paths_include_screen_snapshot():
    """FR-14.1.4 免登录常驻：鉴权开启时大屏取数仍须放行。"""
    from api.middlewares.auth import EXEMPT_PATHS, _path_exempt

    assert "/api/v1/screen/snapshot" in EXEMPT_PATHS
    assert _path_exempt("/api/v1/screen/snapshot") is True
    assert _path_exempt("/api/v1/screen/snapshot/") is True
    # 其余 screen 路径（若未来新增写口）不自动放行
    assert _path_exempt("/api/v1/screen/other") is False


# --------------------------------------------------------------------------- #
# 端点行为
# --------------------------------------------------------------------------- #
def test_snapshot_returns_full_payload(client):
    http, created = client
    response = http.get("/api/v1/screen/snapshot")

    assert response.status_code == 200
    body = response.json()
    assert body["market_date"] == "2026-10-14"
    assert body["quote_as_of"] == "2026-10-14 15:00:00"
    assert body["refresh_seconds"] == 300
    assert body["indices"][0]["name"] == "上证指数"
    assert body["cards"][0]["symbol"] == "601169"
    assert body["cards"][0]["action_label"] == "减仓"
    assert body["portfolio"]["complete"] is True
    assert body["weekly"]["win_rate"] == pytest.approx(60.0)
    assert body["action_counts"] == {"reduce": 1}
    assert created["force_refresh"] is False
    # 默认参数一路透传到服务层
    assert created["service"].calls == [
        {"symbols": None, "with_quote": True, "with_weekly": True, "limit_events": 6}
    ]


def test_query_params_are_forwarded(client):
    http, created = client
    response = http.get(
        "/api/v1/screen/snapshot",
        params=[
            ("symbols", "601169"),
            ("symbols", "513100"),
            ("with_quote", "false"),
            ("with_weekly", "false"),
            ("limit_events", "3"),
        ],
    )

    assert response.status_code == 200
    call = created["service"].calls[0]
    assert call["symbols"] == ["601169", "513100"]
    assert call["with_quote"] is False
    assert call["with_weekly"] is False
    assert call["limit_events"] == 3


def test_limit_events_out_of_range_is_422(client):
    http, _ = client
    assert http.get("/api/v1/screen/snapshot", params={"limit_events": 99}).status_code == 422
    assert http.get("/api/v1/screen/snapshot", params={"limit_events": -1}).status_code == 422


def test_refresh_true_forces_force_refresh(client):
    http, created = client
    response = http.get("/api/v1/screen/snapshot", params={"refresh": "true"})

    assert response.status_code == 200
    assert created["force_refresh"] is True


def test_snapshot_maps_service_failure_to_500(monkeypatch):
    def factory(*, force_refresh=False):
        return FakeService(error=RuntimeError("data source down"))

    monkeypatch.setattr(screen_endpoint, "_service", factory)
    app = FastAPI()
    app.include_router(screen_endpoint.router, prefix="/api/v1/screen")

    with TestClient(app) as http:
        response = http.get("/api/v1/screen/snapshot")

    assert response.status_code == 500
    assert response.json()["detail"]["error"] == "internal_error"


def test_default_service_reads_config(monkeypatch):
    """真实 ``_service`` 工厂应从配置读取刷新节奏与缓存 TTL。"""
    from src.config import get_config

    config = get_config()
    monkeypatch.setattr(config, "hslb_screen_refresh_seconds", 600, raising=False)
    monkeypatch.setattr(config, "hslb_screen_cache_seconds", 42, raising=False)

    service = screen_endpoint._service()
    assert service.refresh_seconds == 600
    assert service.cache_ttl_seconds == 42

    forced = screen_endpoint._service(force_refresh=True)
    assert forced.cache_ttl_seconds == 0
