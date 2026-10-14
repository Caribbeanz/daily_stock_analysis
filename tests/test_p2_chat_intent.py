# -*- coding: utf-8 -*-
"""P2.1 意图解析单测（纯规则 + 可注入 LLM，离线）。"""

from __future__ import annotations

import pytest

from src.agent.intent import IntentKind, resolve


# --------------------------------------------------------------------------- #
# 名词解释
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    ["KDJ 是什么", "MACD 金叉是什么意思", "布林带怎么理解", "解释一下乖离率", "ETF"],
)
def test_glossary_intents(text):
    intent = resolve(text)
    assert intent.kind is IntentKind.GLOSSARY, text
    assert intent.terms, text


# --------------------------------------------------------------------------- #
# 越界（FR-14.3.9）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    ["帮我买入 601919", "直接卖出 601919", "市价单买 100 股 600519", "一键清仓 601919"],
)
def test_out_of_scope_intents(text):
    intent = resolve(text)
    assert intent.kind is IntentKind.OUT_OF_SCOPE, text


def test_ask_about_order_term_is_not_out_of_scope():
    # "什么是挂单" 是提问，不应被判为越界下单
    assert resolve("什么是挂单").kind is not IntentKind.OUT_OF_SCOPE


# --------------------------------------------------------------------------- #
# 写意图（P2.1 只识别、不执行）
# --------------------------------------------------------------------------- #
def test_write_watchlist_add():
    intent = resolve("添加 600519 到自选股")
    assert intent.kind is IntentKind.WRITE_INTENT
    assert [op.op for op in intent.write_ops] == ["watchlist_add"]
    assert "600519" in intent.symbols


def test_write_watchlist_remove():
    intent = resolve("把 601919 从自选里删除")
    assert intent.kind is IntentKind.WRITE_INTENT
    assert "watchlist_remove" in [op.op for op in intent.write_ops]


def test_write_stop_loss():
    intent = resolve("把 600519 止损设为 15 元")
    assert intent.kind is IntentKind.WRITE_INTENT
    assert "set_stop_loss" in [op.op for op in intent.write_ops]


def test_write_compound_multiple_ops():
    intent = resolve("把技术面权重调到 60% 并把 600519 止损设为 15 元")
    assert intent.kind is IntentKind.WRITE_INTENT
    ops = {op.op for op in intent.write_ops}
    assert {"set_weight", "set_stop_loss"} <= ops


# --------------------------------------------------------------------------- #
# 深度分析 / 组合体检 / 日常问答
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", ["帮我分析一下 601919", "601919 该不该买", "601919 值不值得持有"])
def test_deep_analysis_intents(text):
    intent = resolve(text)
    assert intent.kind is IntentKind.DEEP_ANALYSIS, text


@pytest.mark.parametrize(
    "text",
    ["我的持仓体检一下", "整体风险怎么样", "我持有的股票情况", "帮我看看全部持仓"],
)
def test_portfolio_review_intents(text):
    assert resolve(text).kind is IntentKind.PORTFOLIO_REVIEW, text


def test_default_is_qa():
    intent = resolve("601919 现在什么情况")
    assert intent.kind is IntentKind.QA
    assert intent.source == "default"
    assert "601919" in intent.symbols


def test_empty_text_is_qa_low_confidence():
    intent = resolve("   ")
    assert intent.kind is IntentKind.QA
    assert intent.confidence < 0.5


# --------------------------------------------------------------------------- #
# symbol_hint 注入（个股详情页「就地问」）
# --------------------------------------------------------------------------- #
def test_symbol_hint_is_merged():
    intent = resolve("现在什么情况", symbol_hint="601919")
    assert "601919" in intent.symbols


# --------------------------------------------------------------------------- #
# LLM 兜底
# --------------------------------------------------------------------------- #
def test_llm_fallback_used_when_rules_miss():
    def fake_llm(text: str):
        return {"kind": "deep_analysis", "symbols": ["600519"], "reason": "fake"}

    intent = resolve("帮我看看这只票", llm=fake_llm)
    assert intent.kind is IntentKind.DEEP_ANALYSIS
    assert intent.source == "llm"
    assert "600519" in intent.symbols


def test_llm_failure_degrades_to_qa():
    def boom(text: str):
        raise RuntimeError("llm down")

    intent = resolve("随便说点什么", llm=boom)
    assert intent.kind is IntentKind.QA
    assert intent.source == "default"


def test_rules_win_over_llm():
    called = {"n": 0}

    def fake_llm(text: str):
        called["n"] += 1
        return {"kind": "qa"}

    intent = resolve("KDJ 是什么", llm=fake_llm)
    assert intent.kind is IntentKind.GLOSSARY
    assert called["n"] == 0, "规则已判定时不应外呼 LLM"


# --------------------------------------------------------------------------- #
# P2.2 写指令：参数抽取 + 复合指令逐条拆分
# --------------------------------------------------------------------------- #
def _ops(text: str):
    from src.agent.intent.resolver import parse_write_ops

    return parse_write_ops(text)


def test_parse_weight_op_with_factor_and_value():
    ops = _ops("把技术面权重调到 60%")
    assert len(ops) == 1
    assert ops[0].op == "set_weight"
    assert ops[0].params["factor"] == "technical"
    assert abs(ops[0].params["value"] - 0.6) < 1e-9


def test_parse_weight_accepts_plain_ratio():
    ops = _ops("技术面权重设为 0.6，大盘 0.2，消息 0.1，基本面 0.1")
    factors = {op.params.get("factor") for op in ops}
    assert "technical" in factors


def test_parse_stop_loss_op():
    ops = _ops("把 600519 止损设为 15 元")
    assert ops[0].op == "set_stop_loss"
    assert ops[0].params["symbol"] == "600519"
    assert abs(ops[0].params["value"] - 15.0) < 1e-9


def test_parse_position_op_with_cost_and_quantity():
    ops = _ops("把 601919 持仓成本设为 12.9 数量 200 股")
    assert ops[0].op == "set_position"
    assert ops[0].params["symbol"] == "601919"
    assert abs(ops[0].params["cost"] - 12.9) < 1e-9
    assert abs(ops[0].params["quantity"] - 200.0) < 1e-9


def test_parse_compound_two_instructions():
    ops = _ops("把技术面权重调到 60% 并把 600519 止损设为 15 元")
    assert [op.op for op in ops] == ["set_weight", "set_stop_loss"]
    assert ops[0].params["factor"] == "technical"
    assert ops[1].params["symbol"] == "600519"


def test_parse_compound_with_pause_punctuation():
    ops = _ops("添加 600519 到自选，同时把基本面权重调到 20%")
    assert {op.op for op in ops} == {"watchlist_add", "set_weight"}


def test_watchlist_add_single_op_not_over_split():
    ops = _ops("添加 600519 到自选股")
    assert [op.op for op in ops] == ["watchlist_add"]
    assert ops[0].params["symbol"] == "600519"


def test_parse_write_ops_empty_input():
    assert _ops("") == []
    assert _ops("今天天气不错") == []
