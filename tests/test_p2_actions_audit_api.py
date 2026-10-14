# -*- coding: utf-8 -*-
"""P2.4 审计 / 撤销 / SSE API 契约单测（路由注册 + 端点行为）。

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

from fastapi import HTTPException  # noqa: E402

from src.repositories.action_audit_repo import ActionAuditRepository  # noqa: E402
from src.services.action_audit_service import ActionAuditService  # noqa: E402
from src.services.action_service import ActionService  # noqa: E402
from src.services.chat_write_service import ChatWriteService  # noqa: E402
from src.services.confirmation_store import ConfirmationStore  # noqa: E402
from src.storage import DatabaseManager  # noqa: E402


class FakeWatchlist:
    def __init__(self) -> None:
        self.rows: List[str] = []

    def list_watchlist(self, **_: Any) -> List[Dict[str, Any]]:
        return [{"symbol": s} for s in self.rows]

    def add(self, symbol: str, **_: Any) -> Dict[str, Any]:
        self.rows.append(symbol.upper())
        return {"symbol": symbol}

    def remove(self, symbol: str) -> int:
        before = len(self.rows)
        self.rows = [s for s in self.rows if s != symbol.upper()]
        return before - len(self.rows)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{(tmp_path / 'audit_api.db').as_posix()}")
    repo = ActionAuditRepository(db)
    service = ActionAuditService(repo=repo)
    store = ConfirmationStore()
    watchlist = FakeWatchlist()

    monkeypatch.setattr(actions_endpoint, "ActionAuditService", lambda: service)
    monkeypatch.setattr(actions_endpoint, "_record_memory", lambda record: None)

    def _fake_service() -> ChatWriteService:
        return ChatWriteService(
            action_service=ActionService(
                watchlist_service=watchlist,
                audit_sink=actions_endpoint._on_audit,
            ),
            store=store,
        )

    monkeypatch.setattr(actions_endpoint, "_service", _fake_service)
    yield service, repo, watchlist
    DatabaseManager.reset_instance()


def _run(coro):
    return asyncio.run(coro)


def _list(**kw):
    """直调端点时需显式传入 Query 默认值（框架默认值仅在 HTTP 路径解析）。"""
    params = {
        "source": None,
        "action": None,
        "status": None,
        "actor": None,
        "undoing": None,
        "limit": 100,
        "offset": 0,
    }
    params.update(kw)
    return _run(actions_endpoint.actions_audit_list(**params))


# --------------------------------------------------------------------------- #
# 路由注册
# --------------------------------------------------------------------------- #
def test_audit_routes_registered():
    paths = {getattr(route, "path", "") for route in actions_endpoint.router.routes}
    assert "/audit" in paths
    assert "/audit/stats" in paths
    assert "/audit/{audit_id}" in paths
    assert "/audit/{audit_id}/undo" in paths
    assert "/stream" in paths


# --------------------------------------------------------------------------- #
# 列表 / 详情 / 统计
# --------------------------------------------------------------------------- #
def test_audit_list_returns_records(env):
    service, repo, _ = env
    repo.add(action="watchlist_add", target="600519", status="ok", source="h5")
    repo.add(action="clear_all", target="", status="rejected", source="h5")
    result = _list()
    assert result.total == 2
    assert {item.action for item in result.items} == {"watchlist_add", "clear_all"}
    assert all(item.action_label for item in result.items)


def test_audit_list_filters_status(env):
    service, repo, _ = env
    repo.add(action="watchlist_add", target="600519", status="ok")
    repo.add(action="clear_all", target="", status="rejected")
    result = _list(status="ok")
    assert result.total == 1
    assert result.items[0].status == "ok"


def test_audit_detail_returns_undo_ops(env):
    service, repo, _ = env
    audit_id = repo.add(
        action="watchlist_add",
        target="600519",
        before={"in_watchlist": False},
        after={"in_watchlist": True},
        status="ok",
    )
    detail = _run(actions_endpoint.actions_audit_detail(audit_id))
    assert detail.undoable is True
    assert detail.undo_ops and detail.undo_ops[0]["op"] == "watchlist_remove"


def test_audit_detail_404(env):
    with pytest.raises(HTTPException) as exc:
        _run(actions_endpoint.actions_audit_detail(999999))
    assert exc.value.status_code == 404


def test_audit_stats(env):
    service, repo, _ = env
    repo.add(action="watchlist_add", target="600519", status="ok")
    repo.add(action="clear_all", target="", status="rejected")
    stats = _run(actions_endpoint.actions_audit_stats())
    assert stats.total == 2
    assert stats.ok == 1 and stats.rejected == 1


# --------------------------------------------------------------------------- #
# 撤销预览
# --------------------------------------------------------------------------- #
def test_undo_preview_returns_token(env):
    service, repo, _ = env
    audit_id = repo.add(
        action="watchlist_add",
        target="600519",
        before={"in_watchlist": False},
        after={"in_watchlist": True},
        status="ok",
    )
    result = _run(actions_endpoint.actions_audit_undo(audit_id, None))
    assert result.status == "pending_confirmation"
    assert result.token
    assert "移出自选股" in result.summary


def test_undo_preview_404(env):
    with pytest.raises(HTTPException) as exc:
        _run(actions_endpoint.actions_audit_undo(123456, None))
    assert exc.value.status_code == 404


def test_undo_preview_rejects_already_undone(env):
    service, repo, _ = env
    original = repo.add(action="watchlist_add", target="600519", status="ok", before={"in_watchlist": False})
    repo.mark_undone(original, 999)
    with pytest.raises(HTTPException) as exc:
        _run(actions_endpoint.actions_audit_undo(original, None))
    assert exc.value.status_code == 400
    assert "已被撤销" in exc.value.detail["message"]


def test_undo_round_trip_marks_undone(env):
    service, repo, watchlist = env
    watchlist.add("600519")  # 已加入自选 → 撤销会移出
    original = repo.add(
        action="watchlist_add",
        target="600519",
        before={"in_watchlist": False},
        after={"in_watchlist": True},
        status="ok",
        confirmed=True,
    )
    preview = _run(actions_endpoint.actions_audit_undo(original, None))
    assert preview.token

    executed = _run(actions_endpoint.actions_execute(actions_endpoint.ActionExecuteRequest(token=preview.token)))
    assert executed.status == "executed"
    assert "600519" not in [s.upper() for s in watchlist.rows]

    # 审计回填：原记录 undone_by 指向撤销记录
    refreshed = service.get(original)
    assert refreshed["undone_by"] is not None
    assert refreshed["undoable"] is False


# --------------------------------------------------------------------------- #
# SSE
# --------------------------------------------------------------------------- #
def test_format_sse_event():
    text = actions_endpoint._format_sse_event("invalidate", {"scopes": ["portfolio"]})
    assert text.startswith("event: invalidate\ndata: ")
    assert text.endswith("\n\n")
    assert "portfolio" in text


def test_stream_endpoint_connected_event():
    async def consume():
        response = await actions_endpoint.actions_stream()
        assert response.media_type == "text/event-stream"
        agen = response.body_iterator
        first = await agen.__anext__()
        await agen.aclose()
        return first

    first = asyncio.run(consume())
    assert "event: connected" in first
