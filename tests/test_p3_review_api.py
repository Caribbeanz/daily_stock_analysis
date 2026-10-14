# -*- coding: utf-8 -*-
"""P3 复盘 API 契约单测（路由注册 + 端点行为）。

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

review_endpoint = pytest.importorskip(
    "api.v1.endpoints.review",
    reason="需要完整依赖链（requirements.txt）才能导入端点模块",
)

from fastapi import HTTPException  # noqa: E402

from src.repositories.execution_feedback_repo import ExecutionFeedbackRepository  # noqa: E402
from src.storage import DatabaseManager  # noqa: E402
from src.services.weekly_review_service import WeeklyReviewService  # noqa: E402


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{(tmp_path / 'review.db').as_posix()}")
    execution_repo = ExecutionFeedbackRepository(db)
    monkeypatch.setattr(review_endpoint, "_feedback_repo", lambda: execution_repo)
    yield execution_repo
    DatabaseManager.reset_instance()


def _run(coro):
    return asyncio.run(coro)


def test_routes_registered():
    paths = {getattr(route, "path", "") for route in review_endpoint.router.routes}
    assert {"/weekly", "/execution"} <= paths


def test_execution_upsert_then_list(repo):
    body = review_endpoint.ExecutionUpsertRequest(
        symbol="601919",
        execution_status="executed",
        week_key="2026-10-12",
        note="已减仓",
    )
    result = _run(review_endpoint.execution_upsert(body))
    assert result.item.symbol == "601919"
    assert result.item.execution_status == "executed"

    listed = _run(review_endpoint.execution_list(week_key="2026-10-12"))
    assert listed.total == 1
    assert listed.items[0].execution_status == "executed"


def test_execution_upsert_overwrites(repo):
    for status in ("executed", "ignored", "later"):
        body = review_endpoint.ExecutionUpsertRequest(
            symbol="601919", execution_status=status, week_key="2026-10-12"
        )
        _run(review_endpoint.execution_upsert(body))
    listed = _run(review_endpoint.execution_list(week_key="2026-10-12"))
    assert listed.total == 1
    assert listed.items[0].execution_status == "later"


def test_execution_upsert_rejects_invalid_status(repo):
    body = review_endpoint.ExecutionUpsertRequest(
        symbol="601919", execution_status="nope", week_key="2026-10-12"
    )
    with pytest.raises(HTTPException) as exc:
        _run(review_endpoint.execution_upsert(body))
    assert exc.value.status_code == 400


def test_execution_list_defaults_to_current_week(repo):
    listed = _run(review_endpoint.execution_list(week_key=None))
    assert listed.week_key
    assert listed.items == []


def test_weekly_endpoint_uses_service(repo, monkeypatch):
    """端点应调用 WeeklyReviewService.build 并返回 markdown。"""

    class FakeService:
        def __init__(self, **_kwargs):
            pass

        def build(self, *, symbols=None):
            from datetime import datetime

            return WeeklyReviewService(
                signal_service=_EmptySignals(),
                outcome_service=_EmptyOutcomes(),
                position_service=_EmptyPositions(),
                watchlist_service=_EmptyWatchlist(),
                feedback_repo=repo,
                fetch=lambda symbol, days: None,
                market_provider=lambda _w: [],
                hotspot_provider=lambda: [],
            ).build(now=datetime(2026, 10, 14, 10, 0))

    monkeypatch.setattr(review_endpoint, "WeeklyReviewService", FakeService)
    result = _run(
        review_endpoint.weekly_review(refresh_outcomes=False, limit_next_week=20, symbols=None)
    )
    assert result.week_key == "2026-10-12"
    assert "周度复盘" in result.markdown


# --------------------------------------------------------------------------- #
# 极简替身
# --------------------------------------------------------------------------- #
class _EmptySignals:
    def list_signals(self, **_kwargs):
        return {"items": []}

    def get_latest_active(self, *, stock_code, limit=1):
        return {"items": []}


class _EmptyOutcomes:
    def list_signal_outcomes(self, signal_id):
        return {"items": []}


class _EmptyPositions:
    def list_positions(self):
        return {}


class _EmptyWatchlist:
    def list_watchlist(self, *, enabled_only=True):
        return []
