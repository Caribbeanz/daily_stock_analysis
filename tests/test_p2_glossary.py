# -*- coding: utf-8 -*-
"""P2.1 名词表服务单测（纯函数，无 IO）。"""

from __future__ import annotations

from src.services.glossary import (
    GLOSSARY,
    list_entries,
    lookup,
    render_term,
    search,
)


def test_glossary_covers_requirements_v07():
    # 需求 v0.7 §3 共 21 条
    assert len(GLOSSARY) == 21
    terms = {e.term for e in GLOSSARY}
    for must in ("K 线", "MACD", "KDJ", "RSI", "止损", "仓位 / 几成", "折溢价率"):
        assert must in terms


def test_lookup_exact_term():
    entry = lookup("KDJ")
    assert entry is not None
    assert entry.term == "KDJ"
    assert "超买超卖" in entry.explanation


def test_lookup_by_alias():
    assert lookup("BOLL").term.startswith("布林带")
    assert lookup("乖离率").term.startswith("乖离率")
    assert lookup("ma").term.startswith("均线")


def test_lookup_unknown_returns_none():
    assert lookup("量子纠缠") is None
    assert lookup("") is None


def test_search_prefers_longer_keyword():
    # "MACD 金叉" 同时含 MACD 与金叉别名，应命中 MACD 条目而非 MA
    hits = search("MACD 金叉是什么意思")
    assert hits, "应命中名词"
    assert hits[0].term == "MACD"


def test_search_dedupes_and_limits():
    hits = search("止损 止损 止损 止损", limit=3)
    assert len(hits) == 1
    assert hits[0].term == "止损"


def test_search_empty_text():
    assert search("") == []
    assert search(None) == []


def test_render_term_contains_term_and_explanation():
    entry = lookup("ETF")
    text = render_term(entry)
    assert entry.term in text
    assert entry.explanation[:6] in text


def test_list_entries_order_is_stable():
    assert [e.term for e in list_entries()] == [e.term for e in GLOSSARY]
