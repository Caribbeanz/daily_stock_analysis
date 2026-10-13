# -*- coding: utf-8 -*-
"""P2.1 对话·读编排单测（注入回答器，离线）。"""

from __future__ import annotations

from typing import Dict

import pytest

from src.services.chat_read_service import (
    RISK_NOTE,
    WRITE_PENDING_NOTE,
    ChatReadService,
)


def _service(record=None):
    calls = record if record is not None else []

    def answerer(message: str, intent, session_id: str) -> str:
        calls.append({"message": message, "intent": intent.kind.value, "session_id": session_id})
        return f"回答：{message}"

    return ChatReadService(answerer=answerer), calls


# --------------------------------------------------------------------------- #
# 名词解释：本地作答，不走 Agent
# --------------------------------------------------------------------------- #
def test_glossary_answered_locally():
    service, calls = _service()
    result = service.ask("KDJ 是什么")
    assert result.intent == "glossary"
    assert "超买超卖" in result.answer
    assert not calls, "名词解释不应调用 Agent"
    assert result.risk_note == RISK_NOTE  # 涉及买卖判断的口径 → 附风险提示


# --------------------------------------------------------------------------- #
# 写意图：只回执、不执行（决议 D3）
# --------------------------------------------------------------------------- #
def test_write_intent_is_not_executed():
    service, calls = _service()
    result = service.ask("添加 600519 到自选股")
    assert result.intent == "write_intent"
    assert "P2.2" in result.answer
    assert WRITE_PENDING_NOTE[:10] in result.answer
    assert not calls, "写意图不得触发任何执行"
    assert result.risk_note == ""


# --------------------------------------------------------------------------- #
# 越界：明确拒绝（FR-14.3.9）
# --------------------------------------------------------------------------- #
def test_out_of_scope_is_refused():
    service, calls = _service()
    result = service.ask("帮我买入 601919")
    assert result.intent == "out_of_scope"
    assert "做不到" in result.answer
    assert "券商" in result.answer
    assert not calls
    assert result.risk_note == ""


# --------------------------------------------------------------------------- #
# 读意图：走注入的回答器
# --------------------------------------------------------------------------- #
def test_qa_goes_through_answerer():
    service, calls = _service()
    result = service.ask("601919 现在什么情况", session_id="s1")
    assert result.intent == "qa"
    assert result.answer.startswith("回答：")
    assert result.risk_note == RISK_NOTE
    assert calls and calls[0]["session_id"] == "s1"


def test_deep_analysis_goes_through_answerer():
    service, calls = _service()
    result = service.ask("帮我分析一下 601919")
    assert result.intent == "deep_analysis"
    assert result.symbols == ["601919"]
    assert result.risk_note == RISK_NOTE


# --------------------------------------------------------------------------- #
# 失败隔离（NFR-8）
# --------------------------------------------------------------------------- #
def test_answerer_failure_degrades_gracefully():
    def boom(message, intent, session_id):
        raise RuntimeError("agent down")

    service = ChatReadService(answerer=boom)
    result = service.ask("601919 现在什么情况")
    assert result.degraded is True
    assert "失败" in result.answer
    assert result.risk_note == "", "降级回答不应冒充分析结论"


def test_missing_answerer_degrades():
    # 不注入 answerer，且强制惰性构建失败
    service = ChatReadService()
    service._lazy_failed = True
    result = service.ask("601919 现在什么情况")
    assert result.degraded is True


# --------------------------------------------------------------------------- #
# 纯文本拼装（飞书渠道）
# --------------------------------------------------------------------------- #
def test_full_text_appends_risk_note():
    service, _ = _service()
    result = service.ask("601919 现在什么情况")
    assert RISK_NOTE[:8] in result.full_text
    assert result.answer in result.full_text


def test_full_text_without_risk_note():
    service, _ = _service()
    result = service.ask("帮我买入 601919")
    assert result.full_text == result.answer


# --------------------------------------------------------------------------- #
# 返回体可序列化
# --------------------------------------------------------------------------- #
def test_to_dict_roundtrip():
    service, _ = _service()
    payload: Dict = service.ask("KDJ 是什么").to_dict()
    assert payload["intent"] == "glossary"
    assert isinstance(payload["risk_note"], str)
    assert isinstance(payload["symbols"], list)
