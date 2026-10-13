# -*- coding: utf-8 -*-
"""P1 · 事件源（FR-20）离线单测：stub 情报/预警源，不联网、不读写真实库。"""

from __future__ import annotations

import pytest

from src.services.event_source import EventSource


# --------------------------- 测试替身 --------------------------- #
class _StubWatchlist:
    def __init__(self, items):
        self._items = items

    def list_watchlist(self, *, enabled_only=True):
        return list(self._items)


class _StubIntelligence:
    def __init__(self, items=None, *, boom=False):
        self._items = items or []
        self._boom = boom
        self.calls = []

    def list_items(self, **filters):
        self.calls.append(filters)
        if self._boom:
            raise RuntimeError("intelligence down")
        return {"items": list(self._items), "total": len(self._items)}


class _StubAlert:
    def __init__(self, items=None, *, boom=False):
        self._items = items or []
        self._boom = boom

    def list_triggers(self, **filters):
        if self._boom:
            raise RuntimeError("alert down")
        return {"items": list(self._items), "total": len(self._items)}


def _watchlist():
    return _StubWatchlist(
        [
            {"symbol": "601919", "name": "中远海控"},
            {"symbol": "513100", "name": "纳指ETF(国泰)"},
        ]
    )


def _source(intel_items=None, alert_items=None, **kwargs):
    return EventSource(
        watchlist_service=_watchlist(),
        intelligence_service=_StubIntelligence(intel_items),
        alert_service=_StubAlert(alert_items),
        **kwargs,
    )


# --------------------------- 情报命中 --------------------------- #
def test_match_by_name():
    src = _source([{"title": "中远海控发布三季报，净利大增", "source_name": "财联社", "published_at": "2026-10-12"}])
    events = src.collect()
    assert len(events) == 1
    assert "中远海控" in events[0]
    assert "财联社" in events[0]


def test_match_by_symbol():
    src = _source([{"title": "513100 溢价率升至 3%", "source_name": "东方财富", "published_at": "2026-10-12"}])
    events = src.collect()
    assert len(events) == 1
    assert "纳指ETF" in events[0]


def test_match_by_scope_value():
    src = _source(
        [
            {
                "title": "公司公告",
                "scope_type": "symbol",
                "scope_value": "601919",
                "source_name": "巨潮",
                "published_at": "2026-10-12",
            }
        ]
    )
    events = src.collect()
    assert len(events) == 1


def test_unrelated_item_is_skipped():
    src = _source([{"title": "某无关公司新闻", "source_name": "X", "published_at": "2026-10-12"}])
    assert src.collect() == []


# --------------------------- 风险 / 日历标记 --------------------------- #
def test_risk_keyword_marks_risk_prefix():
    src = _source([{"title": "中远海控遭监管问询", "source_name": "证券时报", "published_at": "2026-10-12"}])
    events = src.collect()
    assert "风险" in events[0]


def test_calendar_keyword_marks_calendar_prefix():
    src = _source([{"title": "中远海控预约披露三季报", "source_name": "交易所", "published_at": "2026-10-12"}])
    events = src.collect()
    assert "日历" in events[0]


def test_risk_only_filters_plain_items():
    src = _source(
        [
            {"title": "中远海控召开股东大会", "source_name": "A", "published_at": "2026-10-12"},
            {"title": "中远海控遭减持", "source_name": "B", "published_at": "2026-10-11"},
        ],
        risk_only=True,
    )
    events = src.collect()
    assert len(events) == 1
    assert "减持" in events[0]


# --------------------------- 去重 / 排序 / 截断 --------------------------- #
def test_duplicate_titles_deduped():
    src = _source(
        [
            {"title": "中远海控利好", "source_name": "A", "published_at": "2026-10-12"},
            {"title": "中远海控利好", "source_name": "B", "published_at": "2026-10-12"},
        ]
    )
    assert len(src.collect()) == 1


def test_risk_sorted_before_plain():
    src = _source(
        [
            {"title": "中远海控分红到账", "source_name": "A", "published_at": "2026-10-13"},
            {"title": "中远海控被立案调查", "source_name": "B", "published_at": "2026-10-10"},
        ]
    )
    events = src.collect()
    assert "风险" in events[0]


def test_max_events_truncates():
    items = [
        {"title": f"中远海控消息{i}", "source_name": "X", "published_at": f"2026-10-{10+i:02d}"}
        for i in range(1, 10)
    ]
    src = _source(items, max_events=3)
    assert len(src.collect()) == 3


# --------------------------- 预警源 --------------------------- #
def test_alert_trigger_collected():
    src = _source(alert_items=[{"target": "601919", "message": "跌破止损位", "created_at": "2026-10-12"}])
    events = src.collect()
    assert len(events) == 1
    assert "止损" in events[0]


# --------------------------- 异常隔离 / 空输入 --------------------------- #
def test_intelligence_failure_degrades_empty():
    src = EventSource(
        watchlist_service=_watchlist(),
        intelligence_service=_StubIntelligence(boom=True),
        alert_service=_StubAlert(boom=True),
    )
    assert src.collect() == []


def test_no_watchlist_returns_empty():
    src = EventSource(
        watchlist_service=_StubWatchlist([]),
        intelligence_service=_StubIntelligence([{"title": "任意"}]),
        alert_service=_StubAlert(),
    )
    assert src.collect() == []


def test_symbols_filter_limits_scope():
    src = _source(
        [
            {"title": "中远海控消息", "source_name": "A", "published_at": "2026-10-12"},
            {"title": "纳指ETF大涨", "source_name": "B", "published_at": "2026-10-12"},
        ]
    )
    events = src.collect(["601919"])
    assert len(events) == 1
    assert "中远海控" in events[0]


def test_as_provider_returns_callable():
    src = _source([{"title": "中远海控消息", "source_name": "A", "published_at": "2026-10-12"}])
    provider = src.as_provider()
    assert callable(provider)
    assert len(provider()) == 1
