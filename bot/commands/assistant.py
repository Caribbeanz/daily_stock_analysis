# -*- coding: utf-8 -*-
"""股票分析助手的飞书入站命令（需求 §3.6，薄壳层）。

业务规则全在 :mod:`src.assistant.inbound`，这里只做三件事：
1. 把 ``BotMessage`` 翻译成入站调用（带 ``chat_type`` 决定是否隐藏持仓字段）；
2. 把返回文本包成 ``BotResponse``；
3. 提供无斜杠文本钩子——「确认 / 取消 / 关注 600000」这类指令用户不会加 ``/``，
   而 dispatcher 只对带前缀的文本走命令分发，所以要在自然语言路由之前拦一道。

**只在 `ASSISTANT_ENABLED=true` 时注册**（`bot/dispatcher.get_dispatcher` 里判断），
默认不影响现有机器人行为。
"""

from __future__ import annotations

import logging

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse, ChatType

logger = logging.getLogger(__name__)

__all__ = [
    "AssistantBriefCommand",
    "AssistantNoteCommand",
    "AssistantReviewCommand",
    "AssistantUnwatchCommand",
    "AssistantWatchCommand",
    "register_assistant_inbound",
]


def _inbound():  # noqa: ANN202 - 延迟构造，避免 bot 启动时就拉起存储层
    """每次调用现取一个入站处理器（内部只读/写 SQLite，构造很轻）。"""
    from src.assistant.data.storage import AssistantStore, default_db_path
    from src.assistant.inbound import AssistantInbound, InboundSettings

    store = AssistantStore(default_db_path())
    store.create_all()
    return AssistantInbound(store=store, settings=InboundSettings.from_env())


def _is_private(message: BotMessage) -> bool:
    return message.chat_type == ChatType.PRIVATE


def _text(message: BotMessage, inbound, text: str) -> BotResponse:  # noqa: ANN001
    return BotResponse.text_response(text, at_user=not _is_private(message))


class AssistantWatchCommand(BotCommand):
    """加入关注（写配置，二次确认后生效）。"""

    @property
    def name(self) -> str:
        return "watch"

    @property
    def aliases(self) -> list[str]:
        return ["关注", "自选", "guanzhu"]

    @property
    def description(self) -> str:
        return "加入关注清单（需二次确认，下一次快照生效）"

    @property
    def usage(self) -> str:
        return "/关注 <6 位代码>"

    def validate_args(self, args: list[str]) -> str | None:
        if not args:
            return "请给出 6 位代码，例如 /关注 601919"
        return None

    def execute(self, message: BotMessage, args: list[str]) -> BotResponse:
        inbound = _inbound()
        code = args[0].strip()
        if len(args) > 1:
            # 「/关注 601919 中国远洋」允许带名称，但名称只回显、不入库（避免硬凑名称）
            logger.info("关注指令附带名称 %s，仅回显不入库", args[1:])
        reply = inbound.request_watch(message.user_id, "add", code, raw_text=message.content)
        return _text(message, inbound, reply)


class AssistantUnwatchCommand(BotCommand):
    """移除关注（同样二次确认）。"""

    @property
    def name(self) -> str:
        return "unwatch"

    @property
    def aliases(self) -> list[str]:
        return ["移除", "取消关注", "yichu"]

    @property
    def description(self) -> str:
        return "移除关注（需二次确认，不影响已有持仓记录）"

    @property
    def usage(self) -> str:
        return "/移除 <6 位代码>"

    def validate_args(self, args: list[str]) -> str | None:
        if not args:
            return "请给出 6 位代码，例如 /移除 601919"
        return None

    def execute(self, message: BotMessage, args: list[str]) -> BotResponse:
        inbound = _inbound()
        reply = inbound.request_watch(
            message.user_id, "remove", args[0].strip(), raw_text=message.content
        )
        return _text(message, inbound, reply)


class AssistantNoteCommand(BotCommand):
    """记录操作思路与心得（原文照存，不进任何决策）。"""

    @property
    def name(self) -> str:
        return "note"

    @property
    def aliases(self) -> list[str]:
        return ["笔记", "记录", "note"]

    @property
    def description(self) -> str:
        return "记录当日思路/心得（不进评分、不改任何结论）"

    @property
    def usage(self) -> str:
        return "/笔记 [代码] <内容>（不带参数时回看最近几条）"

    def validate_args(self, args: list[str]) -> str | None:
        return None

    def execute(self, message: BotMessage, args: list[str]) -> BotResponse:
        inbound = _inbound()
        if not args:
            return _text(message, inbound, inbound.list_notes())
        code = None
        body = list(args)
        if body and body[0].isdigit() and len(body[0]) == 6:
            code = body.pop(0)
        return _text(message, inbound, inbound.add_note(message.user_id, " ".join(body), code=code))


class AssistantBriefCommand(BotCommand):
    """查询当日结论与条件计划（只读快照）。"""

    @property
    def name(self) -> str:
        return "brief"

    @property
    def aliases(self) -> list[str]:
        return ["今日", "结论", "brief"]

    @property
    def description(self) -> str:
        return "查当日结论与条件计划（只读，开盘后不更新）"

    @property
    def usage(self) -> str:
        return "/今日 [6 位代码]"

    def validate_args(self, args: list[str]) -> str | None:
        return None

    def execute(self, message: BotMessage, args: list[str]) -> BotResponse:
        inbound = _inbound()
        code = args[0].strip() if args else None
        return _text(
            message,
            inbound,
            inbound.brief(code, include_position=_is_private(message)),
        )


class AssistantReviewCommand(BotCommand):
    """盘后复盘（只读回看，不产生新建议）。"""

    @property
    def name(self) -> str:
        return "review"

    @property
    def aliases(self) -> list[str]:
        return ["复盘", "review"]

    @property
    def description(self) -> str:
        return "盘后复盘摘要（只读回看，不构成新建议）"

    @property
    def usage(self) -> str:
        return "/复盘"

    def validate_args(self, args: list[str]) -> str | None:
        return None

    def execute(self, message: BotMessage, args: list[str]) -> BotResponse:
        inbound = _inbound()
        return _text(message, inbound, inbound.review())


# ---------------------------------------------------------------------------
# 无斜杠文本钩子
# ---------------------------------------------------------------------------
def _match_confirm(message: BotMessage) -> BotResponse | None:
    text = (message.content or "").strip()
    if text not in {"确认", "确定", "是的", "confirm", "y", "yes"}:
        return None
    inbound = _inbound()
    return _text(message, inbound, inbound.confirm(message.user_id))


def _match_cancel(message: BotMessage) -> BotResponse | None:
    text = (message.content or "").strip()
    if text not in {"取消", "算了", "cancel", "n", "no"}:
        return None
    inbound = _inbound()
    return _text(message, inbound, inbound.cancel(message.user_id))


def _match_watch(message: BotMessage) -> BotResponse | None:
    """「关注 601919」/「移除 601919」这类不带斜杠的写法。"""
    parts = (message.content or "").strip().split()
    if len(parts) < 2 or len(parts[0]) > 4:
        return None
    verb, code = parts[0], parts[1]
    action = {"关注": "add", "加自选": "add", "移除": "remove", "取消关注": "remove"}.get(verb)
    if action is None:
        return None
    inbound = _inbound()
    reply = inbound.request_watch(message.user_id, action, code, raw_text=message.content)
    return _text(message, inbound, reply)


def register_assistant_inbound(dispatcher) -> None:  # noqa: ANN001
    """把四条命令与三个文本钩子挂到分发器上（无斜杠文本先于自然语言路由）。"""
    for command in (
        AssistantWatchCommand(),
        AssistantUnwatchCommand(),
        AssistantNoteCommand(),
        AssistantBriefCommand(),
        AssistantReviewCommand(),
    ):
        dispatcher.register(command)
    for matcher in (_match_confirm, _match_cancel, _match_watch):
        dispatcher.register_text_matcher(matcher)
    logger.info("股票分析助手入站命令已注册（关注/笔记/当日结论/复盘）")
