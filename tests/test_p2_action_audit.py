# -*- coding: utf-8 -*-
"""P2.4 写操作审计 + 域事件总线单测（真实 SQLite 临时库）。

运行方式（规避 tests/conftest.py 的重型前端依赖）：
    PYTHONPATH=. pytest --noconftest tests/test_p2_action_audit.py
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from src.agent.intent.resolver import WriteOp
from src.repositories.action_audit_repo import ActionAuditRepository
from src.services.action_audit_service import (
    ActionAuditService,
    build_undo_ops,
    can_undo,
    publish_invalidate,
    scope_for_action,
)
from src.services.action_service import ActionService
from src.services.domain_event_bus import DomainEventBus, get_domain_event_bus
from src.storage import DatabaseManager


@pytest.fixture()
def repo(tmp_path):
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{(tmp_path / 'audit.db').as_posix()}")
    yield ActionAuditRepository(db)
    DatabaseManager.reset_instance()


# --------------------------------------------------------------------------- #
# 仓储
# --------------------------------------------------------------------------- #
def test_repo_add_and_get(repo):
    audit_id = repo.add(
        action="set_position",
        source="h5",
        actor="h5",
        target="601919",
        before=None,
        after={"avg_cost": 12.9, "quantity": 200},
        confirmed=True,
        status="ok",
        description="新增持仓：601919",
    )
    assert audit_id > 0
    record = repo.get(audit_id)
    assert record["action"] == "set_position"
    assert record["target"] == "601919"
    assert record["before"] is None
    assert record["after"] == {"avg_cost": 12.9, "quantity": 200}
    assert record["confirmed"] is True
    assert record["undo_of"] is None and record["undone_by"] is None


def test_repo_list_and_count_filters(repo):
    repo.add(action="watchlist_add", target="600519", status="ok", source="h5")
    repo.add(action="watchlist_remove", target="600519", status="ok", source="feishu")
    repo.add(action="clear_all", target="", status="rejected", source="h5")

    assert repo.count() == 3
    assert repo.count(status="ok") == 2
    assert repo.count(source="feishu") == 1
    assert repo.count(action="watchlist_add") == 1
    items = repo.list_records(status="ok")
    assert {i["action"] for i in items} == {"watchlist_add", "watchlist_remove"}
    assert repo.count_by_action()["watchlist_add"] == 1


def test_repo_mark_undone_and_latest(repo):
    original = repo.add(action="watchlist_add", target="600519", status="ok")
    undo = repo.add(action="watchlist_remove", target="600519", status="ok", undo_of=original)
    assert repo.latest_undo_of(original) == undo
    assert repo.mark_undone(original, undo) == 1
    assert repo.get(original)["undone_by"] == undo


# --------------------------------------------------------------------------- #
# 撤销指令构造（纯函数）
# --------------------------------------------------------------------------- #
def test_build_undo_ops_watchlist_add_new():
    ops = build_undo_ops({"action": "watchlist_add", "target": "600519", "before": {"in_watchlist": False}})
    assert len(ops) == 1 and ops[0].op == "watchlist_remove" and ops[0].params["symbol"] == "600519"


def test_build_undo_ops_watchlist_add_noop():
    # 原本已在自选 → 该 add 是空操作 → 无可撤销
    assert build_undo_ops({"action": "watchlist_add", "target": "600519", "before": {"in_watchlist": True}}) == []


def test_build_undo_ops_watchlist_remove():
    ops = build_undo_ops({"action": "watchlist_remove", "target": "600519", "before": {"in_watchlist": True}})
    assert ops[0].op == "watchlist_add"


def test_build_undo_ops_set_position_restores_previous():
    ops = build_undo_ops(
        {
            "action": "set_position",
            "target": "601919",
            "before": {"avg_cost": 12.5, "quantity": 100},
        }
    )
    assert ops[0].op == "set_position"
    assert ops[0].params == {"symbol": "601919", "cost": 12.5, "quantity": 100.0}


def test_build_undo_ops_set_position_new_becomes_remove():
    ops = build_undo_ops({"action": "set_position", "target": "601919", "before": None})
    assert ops[0].op == "remove_position"


def test_build_undo_ops_remove_position_restores():
    ops = build_undo_ops(
        {"action": "remove_position", "target": "601919", "before": {"avg_cost": 12.9, "quantity": 200}}
    )
    assert ops[0].op == "set_position"


def test_build_undo_ops_stop_loss_both_directions():
    removed = build_undo_ops({"action": "set_stop_loss", "target": "600519", "before": None})
    assert removed[0].op == "remove_stop_loss"
    restored = build_undo_ops({"action": "remove_stop_loss", "target": "600519", "before": 15.0})
    assert restored[0].op == "set_stop_loss" and restored[0].params["value"] == 15.0


def test_build_undo_ops_set_weight_restores_factor():
    ops = build_undo_ops(
        {
            "action": "set_weight",
            "target": "technical",
            "before": {"technical": 0.45, "market": 0.25, "news": 0.15, "fundamental": 0.15},
        }
    )
    assert ops[0].op == "set_weight"
    assert ops[0].params == {"factor": "technical", "value": 0.45}


def test_build_undo_ops_unknown_action_empty():
    assert build_undo_ops({"action": "clear_all", "target": ""}) == []


# --------------------------------------------------------------------------- #
# can_undo 规则
# --------------------------------------------------------------------------- #
def _ok_record(**overrides: Any) -> Dict[str, Any]:
    base = {
        "action": "watchlist_add",
        "target": "600519",
        "before": {"in_watchlist": False},
        "after": {"in_watchlist": True},
        "status": "ok",
        "undone_by": None,
        "undo_of": None,
    }
    base.update(overrides)
    return base


def test_can_undo_ok_record():
    assert can_undo(_ok_record())["undoable"] is True


def test_can_undo_rejects_failed_record():
    assert can_undo(_ok_record(status="failed"))["undoable"] is False


def test_can_undo_rejects_already_undone():
    assert can_undo(_ok_record(undone_by=9))["undoable"] is False


def test_can_undo_rejects_undo_record():
    assert can_undo(_ok_record(undo_of=7))["undoable"] is False


def test_can_undo_rejects_noop():
    assert can_undo(_ok_record(before={"in_watchlist": True}))["undoable"] is False


# --------------------------------------------------------------------------- #
# 审计服务
# --------------------------------------------------------------------------- #
def test_service_record_persists(repo):
    service = ActionAuditService(repo=repo)
    audit_id = service.record(
        {
            "action": "set_weight",
            "target": "technical",
            "before": {"technical": 0.45},
            "after": {"technical": 0.6},
            "actor": "h5",
            "source": "h5",
            "confirmed": True,
            "status": "ok",
            "description": "把技术面权重调为 60%",
        }
    )
    assert audit_id is not None
    record = service.get(audit_id)
    assert record["undoable"] is True
    assert record["undo_ops"][0]["op"] == "set_weight"


def test_service_record_marks_undone_when_undo_of(repo):
    service = ActionAuditService(repo=repo)
    original = service.record(
        {"action": "watchlist_add", "target": "600519", "before": {"in_watchlist": False},
         "after": {"in_watchlist": True}, "status": "ok", "confirmed": True}
    )
    undo = service.record(
        {"action": "watchlist_remove", "target": "600519", "before": {"in_watchlist": True},
         "after": {"in_watchlist": False}, "status": "ok", "confirmed": True, "undo_of": original}
    )
    assert service.get(original)["undone_by"] == undo
    assert service.get(undo)["undoable"] is False  # 撤销记录不可再撤销


def test_service_stats(repo):
    service = ActionAuditService(repo=repo)
    service.record({"action": "watchlist_add", "target": "600519", "status": "ok"})
    service.record({"action": "clear_all", "target": "", "status": "rejected"})
    stats = service.stats()
    assert stats["total"] == 2
    assert stats["ok"] == 1 and stats["rejected"] == 1
    assert stats["by_action"]["watchlist_add"] == 1


def test_service_list_annotates_undoable(repo):
    service = ActionAuditService(repo=repo)
    service.record({"action": "watchlist_add", "target": "600519",
                    "before": {"in_watchlist": False}, "after": {"in_watchlist": True}, "status": "ok"})
    result = service.list_records()
    assert result["items"][0]["undoable"] is True


# --------------------------------------------------------------------------- #
# 域事件总线
# --------------------------------------------------------------------------- #
def test_scope_for_action():
    assert "watchlist" in scope_for_action("watchlist_add")
    assert "portfolio" in scope_for_action("set_position")
    assert "weights" in scope_for_action("set_weight")
    assert "audit" in scope_for_action("watchlist_add")


def test_event_bus_recent_without_loop():
    bus = DomainEventBus()
    bus.publish("invalidate", {"scopes": ["portfolio"]})
    recent = bus.recent(1)
    assert recent[0]["type"] == "invalidate"
    assert recent[0]["data"]["scopes"] == ["portfolio"]


def test_event_bus_delivers_within_loop():
    bus = DomainEventBus()

    async def main():
        queue: asyncio.Queue = asyncio.Queue()
        bus.subscribe(queue)
        bus.publish("invalidate", {"scopes": ["weights"]})
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        return event

    event = asyncio.run(main())
    assert event.type == "invalidate"
    assert event.data["scopes"] == ["weights"]


def test_publish_invalidate_records_recent():
    bus = get_domain_event_bus()
    bus.clear()
    publish_invalidate({"action": "set_position", "target": "601919", "source": "h5"}, audit_id=7)
    recent = bus.recent(1)
    assert recent[0]["type"] == "invalidate"
    assert "portfolio" in recent[0]["data"]["scopes"]
    assert recent[0]["data"]["audit_id"] == 7
    bus.clear()


# --------------------------------------------------------------------------- #
# action_service 的 audit_context 透传
# --------------------------------------------------------------------------- #
class _FakeWatchlist:
    def __init__(self) -> None:
        self.rows: List[str] = []

    def list_watchlist(self, **_: Any) -> List[Dict[str, Any]]:
        return [{"symbol": s} for s in self.rows]

    def add(self, symbol: str, **_: Any) -> Dict[str, Any]:
        self.rows.append(symbol.upper())
        return {"symbol": symbol}

    def remove(self, symbol: str) -> int:
        self.rows = [s for s in self.rows if s != symbol.upper()]
        return 1


def test_execute_merges_audit_context():
    records: List[Dict[str, Any]] = []
    service = ActionService(watchlist_service=_FakeWatchlist(), audit_sink=records.append)
    service.execute(
        [WriteOp("watchlist_add", {"symbol": "600519"})],
        confirmed=True,
        actor="h5",
        source="h5",
        audit_context={"undo_of": 5, "session_id": "s1"},
    )
    assert records[0]["undo_of"] == 5
    assert records[0]["session_id"] == "s1"
    # 核心字段不被覆盖
    assert records[0]["action"] == "watchlist_add"
    assert records[0]["status"] == "ok"
