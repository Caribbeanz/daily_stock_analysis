# -*- coding: utf-8 -*-
"""P2.1 对话·读 API 契约单测。

只校验路由注册与端点行为，不启动 HTTP 服务。

> 依赖说明：`api.v1.endpoints.chat` 仅依赖轻量服务层；`fastapi` 缺失时整体跳过。
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi")

try:  # 仓库既有约定：轻量环境下用占位模块隔离 litellm 依赖
    import litellm  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - 依赖轻量测试环境
    sys.modules["litellm"] = MagicMock()

chat_endpoint = pytest.importorskip(
    "api.v1.endpoints.chat",
    reason="需要完整依赖链（requirements.txt）才能导入端点模块",
)

from src.services.chat_read_service import ChatReadService  # noqa: E402


# --------------------------------------------------------------------------- #
# 路由注册
# --------------------------------------------------------------------------- #
def _paths() -> set:
    return {getattr(route, "path", "") for route in chat_endpoint.router.routes}


def test_routes_registered():
    paths = _paths()
    assert "/ask" in paths
    assert "/glossary" in paths
    assert "/feishu/events" in paths


def test_ask_route_is_post():
    routes = [r for r in chat_endpoint.router.routes if getattr(r, "path", "") == "/ask"]
    assert len(routes) == 1
    assert "POST" in routes[0].methods


# --------------------------------------------------------------------------- #
# /ask
# --------------------------------------------------------------------------- #
def test_ask_returns_glossary_answer_locally():
    request = chat_endpoint.ChatAskRequest(message="KDJ 是什么")
    response = _run(chat_endpoint.chat_ask(request))
    assert response.intent == "glossary"
    assert "超买超卖" in response.answer
    assert response.risk_note


def test_ask_marks_write_intent_as_pending():
    request = chat_endpoint.ChatAskRequest(message="添加 600519 到自选股")
    response = _run(chat_endpoint.chat_ask(request))
    assert response.intent == "write_intent"
    assert "P2.2" in response.answer


def test_ask_uses_injected_answerer(monkeypatch):
    def _factory():
        return ChatReadService(answerer=lambda m, i, s: f"回答：{m}")

    monkeypatch.setattr(chat_endpoint, "ChatReadService", _factory)
    request = chat_endpoint.ChatAskRequest(message="601919 现在什么情况", symbol="601919")
    response = _run(chat_endpoint.chat_ask(request))
    assert response.intent == "qa"
    assert response.answer.startswith("回答：")
    assert "601919" in response.symbols


# --------------------------------------------------------------------------- #
# /glossary
# --------------------------------------------------------------------------- #
def test_glossary_lists_all_terms():
    response = _run(chat_endpoint.chat_glossary(q=None, limit=50))
    assert len(response.items) == 21


def test_glossary_lookup_by_alias():
    response = _run(chat_endpoint.chat_glossary(q="BOLL", limit=10))
    assert len(response.items) == 1
    assert response.items[0].term.startswith("布林带")


# --------------------------------------------------------------------------- #
# 辅助：同步执行 async 端点
# --------------------------------------------------------------------------- #
def _run(coro):
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # 已在事件循环中（不应发生）：新建循环执行
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()
