# -*- coding: utf-8 -*-
"""高抛低吸助手 · 飞书机器人入站（P2.1 只读 / 决议 D4）

职责：把飞书事件订阅回调（``im.message.receive_v1``）转成一次**只读对话**，
并把回答回帖到飞书。

权限（P2 决议 D4）：
- **本人 open_id（白名单）**：可读；
- **群内其他人**：只读 —— 写请求一律回复"无权限"；
- 写操作在 P2.1 本身也尚未开放（P2.2 才接 ``action_service``），
  因此本模块对**任何人**的写请求都只回执"P2.2 开放"，不会执行。

本模块**不触网、不读库**，发送通过可注入的 ``sender``（默认取项目既有
``FeishuSender``），便于离线单测。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.agent.intent.resolver import IntentKind
from src.services.chat_read_service import ChatReadService

logger = logging.getLogger(__name__)

__all__ = ["FeishuInboundService", "ParsedMessage", "parse_event", "extract_text"]

Sender = Callable[[str], bool]


@dataclass
class ParsedMessage:
    """从事件回调里提取的关键字段。"""

    event_type: str = ""
    open_id: str = ""
    chat_id: str = ""
    chat_type: str = ""
    text: str = ""
    message_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "open_id": self.open_id,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "text": self.text,
            "message_id": self.message_id,
        }


def extract_text(content: Any) -> str:
    """解析飞书消息 content（可能是 JSON 字符串）。"""
    raw = content
    if isinstance(content, str):
        try:
            raw = json.loads(content)
        except (ValueError, TypeError):
            return content.strip()
    if isinstance(raw, dict):
        text = raw.get("text") or raw.get("content") or ""
        return str(text).strip()
    return str(content or "").strip()


def parse_event(payload: Dict[str, Any]) -> ParsedMessage:
    """把飞书事件回调载荷解析为 :class:`ParsedMessage`（纯函数）。"""
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}

    return ParsedMessage(
        event_type=str(header.get("event_type") or payload.get("type") or ""),
        open_id=str(sender_id.get("open_id") or ""),
        chat_id=str(message.get("chat_id") or ""),
        chat_type=str(message.get("chat_type") or ""),
        text=extract_text(message.get("content")),
        message_id=str(message.get("message_id") or ""),
    )


class FeishuInboundService:
    """飞书入站 → 只读对话 → 回帖。"""

    def __init__(
        self,
        *,
        chat_service: Optional[ChatReadService] = None,
        sender: Optional[Sender] = None,
        config: Any = None,
        owner_open_id: Optional[str] = None,
    ) -> None:
        self._chat_service = chat_service
        self._sender = sender
        self._config = config
        self._owner_open_id = owner_open_id

    # ---------------- 依赖 ---------------- #
    @property
    def chat_service(self) -> ChatReadService:
        if self._chat_service is None:
            self._chat_service = ChatReadService()
        return self._chat_service

    @property
    def config(self) -> Any:
        if self._config is None:
            from src.config import get_config

            self._config = get_config()
        return self._config

    @property
    def owner_open_id(self) -> str:
        if self._owner_open_id is not None:
            return str(self._owner_open_id).strip()
        return str(getattr(self.config, "feishu_owner_open_id", "") or "").strip()

    @property
    def sender(self) -> Optional[Sender]:
        if self._sender is not None:
            return self._sender
        try:
            from src.notification_sender.feishu_sender import FeishuSender

            sender = FeishuSender(self.config)
            return lambda text: bool(sender.send_to_feishu(text))
        except Exception as exc:  # 依赖缺失 → 不发送（仍返回处理结果）
            logger.warning("飞书发送器初始化失败：%s", exc)
            return None

    # ---------------- 权限 ---------------- #
    def is_owner(self, open_id: str) -> bool:
        owner = self.owner_open_id
        return bool(owner) and str(open_id or "").strip() == owner

    # ---------------- 主流程 ---------------- #
    def handle_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # 1) URL 校验（飞书开放平台回调验证）
        challenge = payload.get("challenge")
        if challenge is not None and str(payload.get("type") or "") == "url_verification":
            return {"challenge": challenge}

        parsed = parse_event(payload)
        if parsed.event_type != "im.message.receive_v1" or not parsed.text:
            return {"code": 0, "msg": "ignored"}

        owner = self.is_owner(parsed.open_id)
        reply = self._build_reply(parsed, owner)

        sent = False
        sender = self.sender
        if sender is not None:
            try:
                sent = bool(sender(reply))
            except Exception as exc:  # 发送失败不影响回调返回
                logger.warning("飞书回帖失败：%s", exc)

        return {
            "code": 0,
            "msg": "ok",
            "owner": owner,
            "sent": sent,
            "intent": self._last_intent,
        }

    # ---------------- 内部 ---------------- #
    _last_intent: str = ""

    def _build_reply(self, parsed: ParsedMessage, owner: bool) -> str:
        answer = self.chat_service.ask(parsed.text)
        self._last_intent = answer.intent

        # 群内非本人 + 写意图 → 明确"无权限"（D4）
        if answer.intent == IntentKind.WRITE_INTENT.value and not owner:
            return "无权限：本群仅可**查询**；写操作（增删自选 / 改持仓 / 调权重 / 止损）仅限本人私聊，且需二次确认。"

        return answer.full_text
