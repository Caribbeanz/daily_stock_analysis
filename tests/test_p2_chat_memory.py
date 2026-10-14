# -*- coding: utf-8 -*-
"""P2.3 结构化记忆：仓储 / 抽取 / 压缩 / 管理单测（真实 SQLite 临时库）。

运行方式（规避 tests/conftest.py 的重型前端依赖）：
    PYTHONPATH=. pytest --noconftest tests/test_p2_chat_memory.py
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.repositories.chat_memory_repo import ChatMemoryRepository
from src.services.chat_memory_service import (
    MEMORY_TYPES,
    ChatMemoryService,
    classify_message,
    extract_candidates,
)
from src.storage import DatabaseManager


class FakeConfig:
    def __init__(self, *, enabled: bool = True, threshold: int = 12000, weekly: int = 7 * 24 * 3600):
        self.hslb_memory_enabled = enabled
        self.hslb_memory_compression_tokens = threshold
        self.hslb_memory_weekly_seconds = weekly


@pytest.fixture()
def env(tmp_path):
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{(tmp_path / 'memory.db').as_posix()}")
    repo = ChatMemoryRepository(db)
    yield db, repo
    DatabaseManager.reset_instance()


def _service(db, repo, **cfg):
    return ChatMemoryService(repo=repo, db=db, config=FakeConfig(**cfg))


# --------------------------------------------------------------------------- #
# 纯函数抽取
# --------------------------------------------------------------------------- #
def test_classify_preference_and_position():
    pref = classify_message("user", "我偏好稳健一些，以后不要再早上提醒我")
    types = {c.memory_type for c in pref}
    assert "preference" in types

    pos = classify_message("user", "我已经把 601919 持仓设为 成本 12.5 × 200 股")
    assert any(c.memory_type == "position_change" for c in pos)


def test_classify_assistant_conclusion_only_on_marker():
    assert classify_message("assistant", "结论：建议减仓 30%") != []
    assert classify_message("assistant", "今天天气不错") == []


def test_extract_candidates_dedup():
    messages = [
        {"role": "user", "content": "记住我偏好低波动"},
        {"role": "user", "content": "记住我偏好低波动"},
    ]
    result = extract_candidates(messages)
    assert len(result) == 1
    assert result[0].memory_type == "preference"


# --------------------------------------------------------------------------- #
# 仓储 CRUD
# --------------------------------------------------------------------------- #
def test_repo_add_list_count(env):
    _, repo = env
    mid = repo.add("preference", "偏好稳健", source_message_ids=[1, 2], session_id="s1")
    assert mid > 0
    items = repo.list_memories()
    assert len(items) == 1
    assert items[0]["source_message_ids"] == [1, 2]
    assert repo.count() == 1
    assert repo.count_by_type().get("preference") == 1


def test_repo_soft_and_hard_delete(env):
    _, repo = env
    mid = repo.add("preference", "偏好稳健")
    assert repo.soft_delete(mid) == 1
    assert repo.count(status="active") == 0
    assert repo.count(status="deleted") == 1
    assert repo.count(status=None) == 1
    assert repo.restore(mid) == 1
    assert repo.count(status="active") == 1
    assert repo.hard_delete(mid) == 1
    assert repo.count(status=None) == 0


def test_repo_clear_all(env):
    _, repo = env
    repo.add("preference", "a")
    repo.add("position_change", "b")
    assert repo.clear(hard=True) == 2
    assert repo.count(status=None) == 0


def test_repo_cursor_roundtrip(env):
    _, repo = env
    assert repo.get_cursor("s1") == 0
    repo.set_cursor("s1", 42)
    assert repo.get_cursor("s1") == 42
    state = repo.get_cursor_state("s1")
    assert state["last_message_id"] == 42
    assert state["updated_at"] is not None


# --------------------------------------------------------------------------- #
# 服务：显式写入 / 管理
# --------------------------------------------------------------------------- #
def test_service_remember_validates_type(env):
    db, repo = env
    svc = _service(db, repo)
    assert svc.remember("bogus_type", "x") is None
    assert svc.remember("preference", "   ") is None
    assert svc.remember("preference", "偏好稳健") is not None


def test_service_get_memory_with_sources(env):
    db, repo = env
    mid1 = db.save_conversation_message("s1", "user", "我偏好低波动")
    mid2 = db.save_conversation_message("s1", "assistant", "好的")
    svc = _service(db, repo)
    mem_id = svc.remember("preference", "偏好低波动", source_message_ids=[mid1, mid2], session_id="s1")
    record = svc.get_memory(mem_id)
    assert record is not None
    assert {s["id"] for s in record["sources"]} == {mid1, mid2}


def test_service_delete_restore_clear_stats(env):
    db, repo = env
    svc = _service(db, repo)
    a = svc.remember("preference", "a")
    svc.remember("analysis_conclusion", "b")
    assert svc.stats()["active"] == 2
    assert svc.delete(a)["mode"] == "soft"
    assert svc.stats()["active"] == 1 and svc.stats()["deleted"] == 1
    assert svc.restore(a)["restored"] is True
    cleared = svc.clear(hard=True)
    assert cleared["cleared"] == 2
    assert svc.stats()["total"] == 0


def test_service_record_action_only_ok(env):
    db, repo = env
    svc = _service(db, repo)
    assert svc.record_action({"action": "set_position", "status": "failed", "description": "设置持仓"}) is None
    mid = svc.record_action(
        {"action": "set_position", "status": "ok", "target": "601919", "description": "把 601919 持仓设为 成本 12.5 × 200 股"}
    )
    assert mid is not None
    record = svc.get_memory(mid)
    assert record["memory_type"] == "confirmed_action"
    assert "601919" in record["content"]


# --------------------------------------------------------------------------- #
# 服务：压缩
# --------------------------------------------------------------------------- #
def test_compress_force_creates_memories_and_advances_cursor(env):
    db, repo = env
    db.save_conversation_message("s1", "user", "记住我偏好低波动")
    db.save_conversation_message("s1", "assistant", "结论：建议观望")
    svc = _service(db, repo)
    result = svc.compress_session("s1", force=True)
    assert result["compressed"] == 2
    assert result["last_message_id"] > 0
    assert repo.get_cursor("s1") == result["last_message_id"]

    # 再次压缩：无新消息
    again = svc.compress_session("s1", force=True)
    assert again["compressed"] == 0
    assert again["skipped"] == "no_new_messages"


def test_compress_below_threshold_skips(env):
    db, repo = env
    db.save_conversation_message("s1", "user", "记住我偏好低波动")
    svc = _service(db, repo, threshold=12000)
    result = svc.compress_session("s1")
    assert result["compressed"] == 0
    assert result["skipped"] == "below_threshold"


def test_should_compress_token_threshold(env):
    db, repo = env
    messages = [{"role": "user", "content": "记住我偏好低波动" * 50}]
    svc = _service(db, repo, threshold=10)
    assert svc.should_compress("s1", messages) is True


def test_should_compress_disabled(env):
    db, repo = env
    svc = _service(db, repo, enabled=False)
    assert svc.should_compress("s1", [{"role": "user", "content": "x" * 10}]) is False


def test_should_compress_weekly_after_cursor_age(env):
    db, repo = env
    repo.set_cursor("s1", 5)
    svc = _service(db, repo, threshold=10_000_000, weekly=7 * 24 * 3600)
    # 游标刚建立 → 未到一周，不触发
    assert svc.should_compress("s1", [{"role": "user", "content": "hi"}]) is False

    # 模拟游标已过期（注入时钟）
    past = datetime.now() - timedelta(days=8)
    svc._clock = lambda: past + timedelta(days=8)
    repo.get_cursor_state = lambda sid: {"last_message_id": 5, "updated_at": past}  # type: ignore[assignment]
    assert svc.should_compress("s1", [{"role": "user", "content": "hi"}]) is True


def test_all_memory_types_declared():
    assert set(MEMORY_TYPES) == {"position_change", "preference", "confirmed_action", "analysis_conclusion"}
