# -*- coding: utf-8 -*-
"""P2.1 飞书入站（只读）单测：纯解析 + 注入发送器，不触网。"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from src.services.chat_read_service import ChatReadService
from src.services.feishu_inbound import (
    FeishuInboundService,
    extract_text,
    parse_event,
)

OWNER = "ou_e949ea38b809a3fc890576ad2dcc4f68"


def _event(text: str, *, open_id: str = OWNER, chat_type: str = "p2p") -> Dict[str, Any]:
    return {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": open_id}},
            "message": {
                "chat_id": "oc_test",
                "chat_type": chat_type,
                "message_id": "om_1",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }


def _service(sent: List[str], *, owner: str = OWNER) -> FeishuInboundService:
    def answerer(message: str, intent, session_id: str) -> str:
        return f"回答：{message}"

    return FeishuInboundService(
        chat_service=ChatReadService(answerer=answerer),
        sender=lambda text: (sent.append(text), True)[1],
        owner_open_id=owner,
    )


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def test_extract_text_from_json_string():
    assert extract_text('{"text":"你好"}') == "你好"


def test_extract_text_from_raw_string():
    assert extract_text("纯文本") == "纯文本"


def test_parse_event_fields():
    parsed = parse_event(_event("KDJ 是什么"))
    assert parsed.event_type == "im.message.receive_v1"
    assert parsed.open_id == OWNER
    assert parsed.chat_type == "p2p"
    assert parsed.text == "KDJ 是什么"


# --------------------------------------------------------------------------- #
# URL 校验
# --------------------------------------------------------------------------- #
def test_url_verification_echoes_challenge():
    service = _service([])
    assert service.handle_event({"type": "url_verification", "challenge": "abc"}) == {"challenge": "abc"}


# --------------------------------------------------------------------------- #
# 只读对话 + 回帖
# --------------------------------------------------------------------------- #
def test_owner_read_question_replies():
    sent: List[str] = []
    service = _service(sent)
    result = service.handle_event(_event("KDJ 是什么"))
    assert result["code"] == 0
    assert result["owner"] is True
    assert result["sent"] is True
    assert sent and "超买超卖" in sent[0]


def test_non_message_event_is_ignored():
    sent: List[str] = []
    service = _service(sent)
    result = service.handle_event({"header": {"event_type": "im.chat.updated_v1"}, "event": {}})
    assert result["msg"] == "ignored"
    assert not sent


# --------------------------------------------------------------------------- #
# 权限（决议 D4）
# --------------------------------------------------------------------------- #
def test_non_owner_write_is_denied():
    sent: List[str] = []
    service = _service(sent, owner=OWNER)
    result = service.handle_event(_event("添加 600519 到自选股", open_id="ou_other", chat_type="group"))
    assert result["owner"] is False
    assert sent and "无权限" in sent[0]


def test_non_owner_read_is_allowed():
    sent: List[str] = []
    service = _service(sent)
    result = service.handle_event(_event("KDJ 是什么", open_id="ou_other", chat_type="group"))
    assert result["owner"] is False
    assert sent and "超买超卖" in sent[0]


def test_write_intent_still_pending_for_owner():
    sent: List[str] = []
    service = _service(sent)
    service.handle_event(_event("添加 600519 到自选股"))
    assert sent and "P2.2" in sent[0]


# --------------------------------------------------------------------------- #
# 发送失败容错
# --------------------------------------------------------------------------- #
def test_sender_failure_is_tolerated():
    def boom(text: str) -> bool:
        raise RuntimeError("network down")

    service = FeishuInboundService(
        chat_service=ChatReadService(answerer=lambda m, i, s: "ok"),
        sender=boom,
        owner_open_id=OWNER,
    )
    result = service.handle_event(_event("KDJ 是什么"))
    assert result["code"] == 0
    assert result["sent"] is False
