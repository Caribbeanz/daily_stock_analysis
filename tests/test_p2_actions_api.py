# -*- coding: utf-8 -*-
"""P2.2 写操作 API 契约单测（路由注册 + 端点行为）。

只校验路由与端点逻辑，不启动 HTTP 服务；`fastapi` / 完整依赖链缺失时整体跳过。
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi")

try:  # 仓库既有约定：轻量环境下用占位模块隔离 litellm 依赖
    import litellm  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.modules["litellm"] = MagicMock()

actions_endpoint = pytest.importorskip(
    "api.v1.endpoints.actions",
    reason="需要完整依赖链（requirements.txt）才能导入端点模块",
)

from src.services.action_service import ActionService  # noqa: E402
from src.services.confirmation_store import ConfirmationStore  # noqa: E402


# --------------------------------------------------------------------------- #
# 假依赖（避免触库）
# --------------------------------------------------------------------------- #
class FakeWatchlist:
    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    def list_watchlist(self, *, enabled_only: bool = True) -> List[Dict[str, Any]]:
        return list(self.rows)

    def add(self, symbol: str, **_: Any) -> Dict[str, Any]:
        self.rows.append({"symbol": symbol.upper()})
        return {"symbol": symbol}

    def remove(self, symbol: str) -> int:
        return 0


@pytest.fixture()
def patched(monkeypatch):
    """把 API 层的服务替换为离线假实现，并重置共享令牌表。"""
    from src.services.chat_write_service import ChatWriteService

    watchlist = FakeWatchlist()

    def _fake_service():
        return ChatWriteService(
            action_service=ActionService(watchlist_service=watchlist),
            store=actions_endpoint._STORE,
        )

    monkeypatch.setattr(actions_endpoint, "_service", _fake_service)
    actions_endpoint._STORE.clear()
    return watchlist


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# 路由注册
# --------------------------------------------------------------------------- #
def test_routes_registered():
    paths = {getattr(route, "path", "") for route in actions_endpoint.router.routes}
    assert {"/preview", "/execute", "/cancel"} <= paths


# --------------------------------------------------------------------------- #
# 端点行为
# --------------------------------------------------------------------------- #
def test_preview_then_execute_roundtrip(patched):
    preview = _run(
        actions_endpoint.actions_preview(
            actions_endpoint.ActionPreviewRequest(message="添加 600519 到自选股")
        )
    )
    assert preview.status == "pending_confirmation"
    assert preview.token

    executed = _run(
        actions_endpoint.actions_execute(
            actions_endpoint.ActionExecuteRequest(token=preview.token)
        )
    )
    assert executed.status == "executed" and executed.ok is True
    assert "✅" in executed.receipt
    assert any(row["symbol"] == "600519" for row in patched.rows)


def test_preview_rejects_out_of_scope(patched):
    preview = _run(
        actions_endpoint.actions_preview(
            actions_endpoint.ActionPreviewRequest(message="帮我下单买入 600519")
        )
    )
    assert preview.status == "rejected"
    assert preview.token == ""


def test_execute_with_unknown_token_is_nothing_pending(patched):
    executed = _run(
        actions_endpoint.actions_execute(
            actions_endpoint.ActionExecuteRequest(token="not-a-real-token")
        )
    )
    assert executed.status == "nothing_pending"
    assert executed.ok is False


def test_cancel_endpoint(patched):
    preview = _run(
        actions_endpoint.actions_preview(
            actions_endpoint.ActionPreviewRequest(message="添加 600519 到自选股")
        )
    )
    cancelled = _run(
        actions_endpoint.actions_cancel(
            actions_endpoint.ActionCancelRequest(token=preview.token)
        )
    )
    assert cancelled.status == "cancelled"
    assert patched.rows == []


def test_preview_requires_message():
    with pytest.raises(Exception):
        actions_endpoint.ActionPreviewRequest(message="")
