# -*- coding: utf-8 -*-
"""飞书入站四项能力（需求 §3.6）：白名单、二次确认、生效时点、只读查询、笔记。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from src.assistant.data.storage import AssistantStore
from src.assistant.inbound import AssistantInbound, InboundSettings
from tests.assistant.helpers import make_bars, trending

DAY = date(2026, 11, 26)
OWNER = "ou_owner"
STRANGER = "ou_stranger"


def _inbound(pipeline_ready: bool = True, **kwargs) -> tuple[AssistantInbound, AssistantStore]:
    store = AssistantStore(":memory:")
    store.create_all()
    if pipeline_ready:
        store.save_bars("600000", make_bars(trending(160)))
        store.append_watchlist("600000", "add", effective_date=date(2026, 5, 1))
        from src.assistant.pipeline import AssistantPipeline, PipelineSettings

        pipeline = AssistantPipeline(
            store=store,
            settings=PipelineSettings(total_capital=100_000.0),
            clock=lambda: datetime(2026, 11, 26, 18, 0),
        )
        pipeline.ensure_snapshot(DAY)
    settings = InboundSettings(
        allowed_user_ids=frozenset({OWNER}), confirm_ttl_seconds=kwargs.pop("ttl", 180)
    )
    inbound = AssistantInbound(
        store=store,
        settings=settings,
        clock=kwargs.pop("clock", lambda: datetime(2026, 11, 26, 18, 30)),
        **kwargs,
    )
    return inbound, store


class TestWhitelist:
    def test_stranger_cannot_write(self) -> None:
        inbound, store = _inbound()

        reply = inbound.request_watch(STRANGER, "add", "601919")

        assert "未授权" in reply
        assert "601919" not in store.effective_watchlist()
        assert store.list_inbound_log() == []

    def test_stranger_note_not_stored(self) -> None:
        inbound, store = _inbound()

        reply = inbound.add_note(STRANGER, "今天加了点仓")

        assert "未授权" in reply
        assert store.list_notes() == []  # 连内容都不落库

    def test_stranger_confirm_noop(self) -> None:
        inbound, store = _inbound()

        assert "未授权" in inbound.confirm(STRANGER)
        assert "601919" not in store.effective_watchlist()


class TestWatchConfirm:
    def test_request_only_echoes(self) -> None:
        inbound, store = _inbound()

        reply = inbound.request_watch(OWNER, "add", "601919")

        assert "确认" in reply
        assert "601919" not in store.effective_watchlist()  # 未确认不写入
        assert store.list_inbound_log() == []

    def test_confirm_writes_with_effective_date(self) -> None:
        # 当日已有快照 → 生效日顺延到下一交易日
        inbound, store = _inbound()

        inbound.request_watch(OWNER, "add", "601919")
        reply = inbound.confirm(OWNER)

        assert "601919" in store.effective_watchlist()
        assert "2026-11-27" in reply
        log = store.list_inbound_log(1)[0]
        assert log["confirmed"] is True
        assert log["code"] == "601919"
        assert log["effective_date"] == "2026-11-27"
        assert "601919" in log["after_state"]

    def test_confirm_without_snapshot_takes_today(self) -> None:
        inbound, store = _inbound(pipeline_ready=False)

        inbound.request_watch(OWNER, "add", "601919")
        reply = inbound.confirm(OWNER)

        assert "2026-11-26" in reply
        assert store.list_inbound_log(1)[0]["effective_date"] == "2026-11-26"

    def test_confirm_expires(self) -> None:
        now = [datetime(2026, 11, 26, 18, 30)]
        inbound, store = _inbound(clock=lambda: now[0])

        inbound.request_watch(OWNER, "add", "601919")
        now[0] += timedelta(seconds=181)
        reply = inbound.confirm(OWNER)

        assert "超时" in reply
        assert "601919" not in store.effective_watchlist()

    def test_confirm_without_pending(self) -> None:
        inbound, _ = _inbound()

        assert "没有待确认" in inbound.confirm(OWNER)

    def test_duplicate_add_is_rejected_before_confirm(self) -> None:
        inbound, store = _inbound()

        reply = inbound.request_watch(OWNER, "add", "600000")  # 已在关注列表

        assert "已在关注列表" in reply
        assert inbound.confirm(OWNER) == "没有待确认的操作。"

    def test_remove_absent_is_rejected(self) -> None:
        inbound, _ = _inbound()

        assert "不在关注列表" in inbound.request_watch(OWNER, "remove", "601919")

    def test_remove_confirm_keeps_holdings(self) -> None:
        inbound, store = _inbound()

        inbound.request_watch(OWNER, "remove", "600000")
        reply = inbound.confirm(OWNER)

        assert "600000" not in store.effective_watchlist()
        assert "持仓" in reply  # 明确提示不影响已有持仓记录

    def test_bad_code_rejected(self) -> None:
        inbound, _ = _inbound()

        assert "不是 6 位" in inbound.request_watch(OWNER, "add", "60000")
        assert "不是 6 位" in inbound.request_watch(OWNER, "add", "sh600000")

    def test_cancel_logs_unconfirmed(self) -> None:
        inbound, store = _inbound()

        inbound.request_watch(OWNER, "add", "601919")
        reply = inbound.cancel(OWNER)

        assert "已取消" in reply
        assert store.list_inbound_log(1)[0]["confirmed"] is False
        assert "601919" not in store.effective_watchlist()


class TestNotes:
    def test_note_uses_snapshot_date(self) -> None:
        inbound, store = _inbound()

        reply = inbound.add_note(OWNER, "缩量回踩，等 20.5 附近", code="600000")

        assert "2026-11-26" in reply
        row = store.list_notes()[0]
        assert row["trade_date"] == "2026-11-26"
        assert row["code"] == "600000"
        assert row["source"] == "feishu"

    def test_empty_note_rejected(self) -> None:
        inbound, store = _inbound()

        assert "内容为空" in inbound.add_note(OWNER, "   ")
        assert store.list_notes() == []

    def test_notes_listing(self) -> None:
        inbound, _ = _inbound()
        inbound.add_note(OWNER, "组合层面降一点仓")
        inbound.add_note(OWNER, "600000 等回踩", code="600000")

        reply = inbound.list_notes()

        assert "2 条笔记" in reply
        assert "组合" in reply


class TestBriefAndReview:
    def test_brief_without_snapshot(self) -> None:
        inbound, _ = _inbound(pipeline_ready=False)

        assert "还没有生成过快照" in inbound.brief()

    def test_brief_lists_conclusions(self) -> None:
        inbound, _ = _inbound()

        reply = inbound.brief()

        assert "数据截止 2026-11-26" in reply
        assert "600000" in reply
        assert "开盘后不更新" in reply

    def test_brief_single_stock_hides_position_in_group(self) -> None:
        inbound, _ = _inbound()

        group_reply = inbound.brief("600000", include_position=False)
        private_reply = inbound.brief("600000", include_position=True)

        assert "群聊不回显" in group_reply
        assert "群聊不回显" not in private_reply

    def test_brief_unknown_code_is_honest(self) -> None:
        inbound, _ = _inbound()

        assert "不在当日快照里" in inbound.brief("601919")

    def test_review_summarises_snapshot_and_notes(self) -> None:
        inbound, _ = _inbound()
        inbound.add_note(OWNER, "按计划减了一点")

        reply = inbound.review()

        assert "复盘" in reply
        assert "条件计划" in reply
        assert "按计划减了一点" in reply
        assert "不构成新建议" in reply

    def test_review_missing_day(self) -> None:
        inbound, _ = _inbound()

        assert "没有快照" in inbound.review(date(2026, 11, 25))


@pytest.mark.parametrize(
    ("raw", "ttl"),
    [("", 180), ("60", 60), ("5", 30), ("99999", 900), ("abc", 180)],
)
def test_settings_ttl_clamped(raw, ttl) -> None:
    settings = InboundSettings.from_env(
        {"ASSISTANT_INBOUND_ALLOWED_USERS": "ou_a, ou_b;ou_c", "ASSISTANT_INBOUND_CONFIRM_TTL": raw}
    )

    assert settings.confirm_ttl_seconds == ttl
    assert settings.allowed_user_ids == frozenset({"ou_a", "ou_b", "ou_c"})
    assert settings.is_allowed("ou_b") is True
    assert settings.is_allowed("ou_x") is False
