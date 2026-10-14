# -*- coding: utf-8 -*-
"""P2.3 记忆管理 API 契约单测（路由注册 + 端点行为）。

只校验路由与端点逻辑，不启动 HTTP 服务；`fastapi` / 完整依赖链缺失时整体跳过。
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi")

try:  # 仓库既有约定：轻量环境下用占位模块隔离 litellm 依赖
    import litellm  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.modules["litellm"] = MagicMock()

memory_endpoint = pytest.importorskip(
    "api.v1.endpoints.memory",
    reason="需要完整依赖链（requirements.txt）才能导入端点模块",
)

from fastapi import HTTPException  # noqa: E402

from src.repositories.chat_memory_repo import ChatMemoryRepository  # noqa: E402
from src.services.chat_memory_service import ChatMemoryService  # noqa: E402
from src.storage import DatabaseManager  # noqa: E402


class FakeConfig:
    hslb_memory_enabled = True
    hslb_memory_compression_tokens = 12000
    hslb_memory_weekly_seconds = 7 * 24 * 3600


@pytest.fixture()
def service(tmp_path, monkeypatch):
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{(tmp_path / 'api.db').as_posix()}")
    svc = ChatMemoryService(repo=ChatMemoryRepository(db), db=db, config=FakeConfig())
    monkeypatch.setattr(memory_endpoint, "_service", lambda: svc)
    yield svc
    DatabaseManager.reset_instance()


def _run(coro):
    return asyncio.run(coro)


def _list(**kw):
    """直调端点时需显式传入 Query 默认值（框架默认值仅在 HTTP 路径解析）。"""
    params = {"memory_type": None, "session_id": None, "include_deleted": False, "limit": 100, "offset": 0}
    params.update(kw)
    return _run(memory_endpoint.memory_list(**params))


def _detail(memory_id):
    return _run(memory_endpoint.memory_detail(memory_id))


def _delete(memory_id, *, hard=False):
    return _run(memory_endpoint.memory_delete(memory_id, hard=hard))


# --------------------------------------------------------------------------- #
# 路由注册
# --------------------------------------------------------------------------- #
def test_routes_registered():
    paths = {getattr(route, "path", "") for route in memory_endpoint.router.routes}
    assert {"", "/stats", "/compress", "/clear", "/{memory_id}", "/{memory_id}/restore"} <= paths


# --------------------------------------------------------------------------- #
# 端点行为
# --------------------------------------------------------------------------- #
def test_list_empty(service):
    result = _list()
    assert result.total == 0 and result.items == []


def test_list_returns_created(service):
    service.remember("preference", "偏好稳健")
    result = _list()
    assert result.total == 1
    assert result.items[0].memory_type == "preference"


def test_list_rejects_invalid_type(service):
    with pytest.raises(HTTPException) as exc:
        _list(memory_type="nope")
    assert exc.value.status_code == 400


def test_stats(service):
    service.remember("preference", "a")
    service.remember("analysis_conclusion", "b")
    result = _run(memory_endpoint.memory_stats())
    assert result.active == 2
    assert result.by_type["preference"] == 1


def test_detail_and_404(service):
    mid = service.remember("preference", "a")
    detail = _detail(mid)
    assert detail.id == mid
    with pytest.raises(HTTPException) as exc:
        _detail(999999)
    assert exc.value.status_code == 404


def test_delete_soft_then_restore(service):
    mid = service.remember("preference", "a")
    deleted = _delete(mid)
    assert deleted.deleted is True and deleted.mode == "soft"
    assert _list().total == 0
    assert _list(include_deleted=True).total == 1
    restored = _run(memory_endpoint.memory_restore(mid))
    assert restored.restored is True
    assert _list().total == 1


def test_delete_404(service):
    with pytest.raises(HTTPException) as exc:
        _delete(123456)
    assert exc.value.status_code == 404


def test_clear_requires_confirm(service):
    service.remember("preference", "a")
    with pytest.raises(HTTPException) as exc:
        _run(memory_endpoint.memory_clear(memory_endpoint.MemoryClearRequest(confirm=False)))
    assert exc.value.status_code == 400
    assert _list().total == 1


def test_clear_with_confirm(service):
    service.remember("preference", "a")
    service.remember("position_change", "b")
    result = _run(memory_endpoint.memory_clear(memory_endpoint.MemoryClearRequest(confirm=True)))
    assert result.cleared == 2
    assert _list().total == 0


def test_compress_endpoint(service):
    service.db.save_conversation_message("s1", "user", "记住我偏好低波动")
    result = _run(
        memory_endpoint.memory_compress(memory_endpoint.MemoryCompressRequest(session_id="s1", force=True))
    )
    assert result.compressed == 1
    assert _list().total == 1
