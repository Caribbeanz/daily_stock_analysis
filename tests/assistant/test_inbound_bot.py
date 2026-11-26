# -*- coding: utf-8 -*-
"""入站命令与 dispatcher 钩子（薄壳层）：确认语句不带斜杠也要能被接住。"""

from __future__ import annotations

import pytest

from bot.commands.assistant import register_assistant_inbound
from bot.dispatcher import CommandDispatcher
from bot.models import BotMessage, BotResponse, ChatType, Platform
from src.assistant.data.storage import AssistantStore
from src.assistant.inbound import AssistantInbound, InboundSettings


def _message(text: str, *, user_id: str = "ou_owner", private: bool = True) -> BotMessage:
    return BotMessage(
        platform=Platform.FEISHU.value,
        message_id="m1",
        user_id=user_id,
        user_name="tester",
        chat_id="c1",
        chat_type=ChatType.PRIVATE if private else ChatType.GROUP,
        content=text,
    )


@pytest.fixture()
def inbound(monkeypatch):
    """把命令层的 `_inbound()` 换成离线存储器，避免动真实库。"""
    store = AssistantStore(":memory:")
    store.create_all()
    store.append_watchlist("600000", "add")
    service = AssistantInbound(
        store=store,
        settings=InboundSettings(allowed_user_ids=frozenset({"ou_owner"})),
    )
    import bot.commands.assistant as module

    monkeypatch.setattr(module, "_inbound", lambda: service)
    return service, store


class TestDispatcherHook:
    def test_text_matcher_runs_before_nl_routing(self) -> None:
        dispatcher = CommandDispatcher()
        calls: list[str] = []

        def matcher(message: BotMessage) -> BotResponse | None:
            calls.append(message.content)
            if message.content == "确认":
                return BotResponse.text_response("接住了")
            return None

        dispatcher.register_text_matcher(matcher)

        response = dispatcher.dispatch(_message("确认"))

        assert response.text == "接住了"
        assert calls == ["确认"]

    def test_matcher_none_falls_through(self) -> None:
        dispatcher = CommandDispatcher()
        dispatcher.register_text_matcher(lambda message: None)

        response = dispatcher.dispatch(_message("随便一句话"))

        assert response.text == ""  # 非命令、非 @，按原逻辑不处理

    def test_matcher_exception_does_not_break_dispatch(self) -> None:
        dispatcher = CommandDispatcher()

        def boom(message: BotMessage):  # noqa: ANN001, ANN202
            raise RuntimeError("hook failed")

        dispatcher.register_text_matcher(boom)

        response = dispatcher.dispatch(_message("随便一句话"))

        assert response.text == ""


class TestCommands:
    def test_registers_four_capabilities(self, inbound) -> None:
        dispatcher = CommandDispatcher()
        register_assistant_inbound(dispatcher)

        names = set(dispatcher._commands)
        assert {"watch", "unwatch", "note", "brief", "review"} <= names
        assert dispatcher.get_command("关注") is not None  # 中文别名

    def test_confirm_without_slash(self, inbound) -> None:
        service, store = inbound
        dispatcher = CommandDispatcher()
        register_assistant_inbound(dispatcher)

        first = dispatcher.dispatch(_message("/关注 601919"))
        assert "确认" in first.text

        second = dispatcher.dispatch(_message("确认"))

        assert "601919" in store.effective_watchlist()
        assert "留痕号" in second.text

    def test_watch_verb_without_slash(self, inbound) -> None:
        service, store = inbound
        dispatcher = CommandDispatcher()
        register_assistant_inbound(dispatcher)

        response = dispatcher.dispatch(_message("关注 601919"))

        assert "确认" in response.text
        assert "601919" not in store.effective_watchlist()

    def test_cancel_without_slash(self, inbound) -> None:
        service, store = inbound
        dispatcher = CommandDispatcher()
        register_assistant_inbound(dispatcher)

        dispatcher.dispatch(_message("/关注 601919"))
        response = dispatcher.dispatch(_message("取消"))

        assert "已取消" in response.text
        assert "601919" not in store.effective_watchlist()

    def test_note_command_splits_code(self, inbound) -> None:
        service, store = inbound
        dispatcher = CommandDispatcher()
        register_assistant_inbound(dispatcher)

        response = dispatcher.dispatch(_message("/笔记 600000 缩量回踩再买"))

        assert "已记录" in response.text
        note = store.list_notes()[0]
        assert note["code"] == "600000"
        assert note["content"] == "缩量回踩再买"

    def test_note_without_args_lists(self, inbound) -> None:
        service, store = inbound
        dispatcher = CommandDispatcher()
        register_assistant_inbound(dispatcher)

        assert "还没有笔记" in dispatcher.dispatch(_message("/笔记")).text

    def test_brief_is_read_only(self, inbound) -> None:
        service, store = inbound
        dispatcher = CommandDispatcher()
        register_assistant_inbound(dispatcher)

        response = dispatcher.dispatch(_message("/今日"))

        assert "还没有生成过快照" in response.text

    def test_group_reply_mentions_user(self, inbound) -> None:
        service, store = inbound
        dispatcher = CommandDispatcher()
        register_assistant_inbound(dispatcher)

        response = dispatcher.dispatch(_message("/今日", private=False))

        assert response.at_user is True

    def test_unauthorized_user_blocked(self, inbound) -> None:
        service, store = inbound
        dispatcher = CommandDispatcher()
        register_assistant_inbound(dispatcher)

        response = dispatcher.dispatch(_message("/关注 601919", user_id="ou_stranger"))

        assert "未授权" in response.text
        assert "601919" not in store.effective_watchlist()
