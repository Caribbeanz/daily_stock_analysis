# -*- coding: utf-8 -*-
"""P4 大屏看板 · 服务层单测（FR-14.1）

覆盖：符号解析、卡片组装、持仓汇总、指数口径、事件与复盘摘要的降级隔离、
数据截止时间、动作分布、快照缓存与并发/串行两条取数分支。

依赖全部注入假件，**不触网、不读库**。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from src.core.risk_engine import PositionView
from src.services.screen_service import (
    ACTION_ORDER,
    SCREEN_PATH,
    WEEKLY_REVIEW_PATH,
    ScreenCard,
    ScreenService,
    summarize_portfolio,
)


# --------------------------------------------------------------------------- #
# 假件
# --------------------------------------------------------------------------- #
def make_bars(closes, dates=None):
    dates = dates or [f"2026-10-{10 + i:02d}" for i in range(len(closes))]
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1000] * len(closes),
        }
    )


class FakeOverview:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def load(self, symbol, *, with_quote=True):
        self.calls.append((symbol, with_quote))
        payload = self.payloads.get(symbol)
        if isinstance(payload, Exception):
            raise payload
        return payload or {}


class FakeWatchlist:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error

    def list_watchlist(self, *, enabled_only=True):
        if self.error is not None:
            raise self.error
        return list(self.rows)


class FakePositions:
    def __init__(self, views=None, error=None):
        self.views = views or {}
        self.error = error

    def list_positions(self):
        if self.error is not None:
            raise self.error
        return dict(self.views)


class FakeEvents:
    def __init__(self, items=None, error=None):
        self.items = items if items is not None else []
        self.error = error
        self.seen = None

    def collect(self, symbols):
        self.seen = list(symbols)
        if self.error is not None:
            raise self.error
        return list(self.items)


class FakeReview:
    week_key = "2026-10-12"
    h5_base_url = ""
    position_week_pnl = 128.5
    signal_summary = SimpleNamespace(
        evaluated=10, correct=6, unevaluated=2, win_rate=60.0, avg_return_pct=1.25
    )

    def build(self):
        return self

    def headline(self):
        return "本周胜率 60%（6/10）"

    def execution_counts(self):
        return {"executed": 3, "ignored": 1}


def overview_payload(symbol, **overrides):
    payload = {
        "symbol": symbol,
        "name": f"标的{symbol}",
        "action": "reduce",
        "action_label": "减仓",
        "scenario_label": "上涨趋势",
        "conclusion": "减仓（上涨趋势）：触及止盈位",
        "is_urgent": False,
        "score": 58,
        "entry_zone": "12.10~12.50",
        "stop_loss": 11.0,
        "target_price": 14.0,
        "position_ratio_label": "30%",
        "distance_to_stop_pct": -8.5,
        "distance_to_target_pct": 16.2,
        "signal_created_at": "2026-10-13 20:05:00",
        "quote": {
            "current_price": 12.04,
            "change_percent": 1.86,
            "update_time": "2026-10-14 15:00:00",
        },
        "degraded": [],
    }
    payload.update(overrides)
    return payload


def build_service(**kwargs):
    defaults = dict(
        overview_service=FakeOverview({}),
        watchlist_service=FakeWatchlist([]),
        position_service=FakePositions({}),
        event_source=FakeEvents([]),
        review_service=FakeReview(),
        fetch=lambda symbol, days: None,
        max_workers=4,
        now=datetime(2026, 10, 14, 15, 30, 0),
    )
    defaults.update(kwargs)
    return ScreenService(**defaults)


# --------------------------------------------------------------------------- #
# summarize_portfolio（纯函数）
# --------------------------------------------------------------------------- #
def test_summarize_portfolio_full_priced():
    positions = {
        "601169": PositionView(symbol="601169", avg_cost=6.0, quantity=1000),
        "513100": PositionView(symbol="513100", avg_cost=2.0, quantity=500),
    }
    cards = [
        ScreenCard(symbol="601169", price=6.6),
        ScreenCard(symbol="513100", price=1.8),
    ]
    summary, notes = summarize_portfolio(positions, cards)

    assert summary.position_count == 2
    assert summary.priced_count == 2
    assert summary.complete is True
    assert summary.total_cost == pytest.approx(6000 + 1000, abs=0.01)
    assert summary.market_value == pytest.approx(6600 + 900, abs=0.01)
    assert summary.pnl_amount == pytest.approx(500.0, abs=0.01)
    assert summary.pnl_pct == pytest.approx(500 / 7000 * 100, abs=1e-3)
    assert summary.winners == 1 and summary.losers == 1
    assert notes == []


def test_summarize_portfolio_partial_quote_is_honest():
    """部分持仓无行情时：不臆造盈亏，complete=False 并给出降级说明。"""
    positions = {
        "601169": PositionView(symbol="601169", avg_cost=6.0, quantity=1000),
        "601818": PositionView(symbol="601818", avg_cost=3.0, quantity=2000),
    }
    cards = [ScreenCard(symbol="601169", price=6.6)]

    summary, notes = summarize_portfolio(positions, cards)

    assert summary.position_count == 2
    assert summary.priced_count == 1
    assert summary.complete is False
    assert summary.market_value is None and summary.pnl_amount is None and summary.pnl_pct is None
    assert summary.total_cost is not None
    assert notes and "601818" in notes[0]


def test_summarize_portfolio_empty_and_zero_quantity():
    summary, notes = summarize_portfolio({}, [])
    assert summary.position_count == 0
    assert summary.total_cost is None and summary.pnl_amount is None
    assert notes == []

    positions = {"601169": PositionView(symbol="601169", avg_cost=6.0, quantity=0)}
    summary, notes = summarize_portfolio(positions, [])
    assert summary.position_count == 0


# --------------------------------------------------------------------------- #
# 符号解析
# --------------------------------------------------------------------------- #
def test_resolve_symbols_merges_watchlist_and_positions():
    service = build_service(
        watchlist_service=FakeWatchlist(
            [{"symbol": "601169", "name": "北京银行"}, {"symbol": "513100", "name": "纳指ETF"}]
        ),
        position_service=FakePositions(
            {"601818": PositionView(symbol="601818", avg_cost=3.0, quantity=100)}
        ),
        overview_service=FakeOverview({}),
    )
    positions = service.position_service.list_positions()
    resolved, names = service._resolve_symbols(None, positions, [])

    assert resolved == ["601169", "513100", "601818"]
    assert names["513100"] == "纳指ETF"


def test_resolve_symbols_explicit_wins_and_normalizes():
    service = build_service(
        watchlist_service=FakeWatchlist([{"symbol": "601169", "name": "北京银行"}]),
    )
    resolved, _ = service._resolve_symbols(["601919", "601919", " 513100 "], {}, [])
    assert resolved == ["601919", "513100"]


def test_resolve_symbols_watchlist_failure_degrades():
    service = build_service(watchlist_service=FakeWatchlist(error=RuntimeError("boom")))
    degraded = []
    positions = {"601818": PositionView(symbol="601818", avg_cost=3.0, quantity=100)}
    resolved, _ = service._resolve_symbols(None, positions, degraded)

    assert resolved == ["601818"]
    assert degraded and "自选股读取失败" in degraded[0]


def test_position_read_failure_degrades():
    service = build_service(position_service=FakePositions(error=RuntimeError("db down")))
    degraded = []
    positions = service._load_positions(degraded)

    assert positions == {}
    assert degraded and "持仓读取失败" in degraded[0]


# --------------------------------------------------------------------------- #
# build：卡片 / 指数 / 事件 / 复盘
# --------------------------------------------------------------------------- #
def test_build_cards_map_overview_and_position():
    service = build_service(
        overview_service=FakeOverview({"601169": overview_payload("601169")}),
        position_service=FakePositions(
            {"601169": PositionView(symbol="601169", avg_cost=6.0, quantity=2000)}
        ),
        fetch=lambda symbol, days: make_bars([10.0, 11.0]),
    )
    payload = service.build(["601169"], with_weekly=False)

    assert len(payload["cards"]) == 1
    card = payload["cards"][0]
    assert card["name"] == "标的601169"
    assert card["price"] == pytest.approx(12.04)
    assert card["change_pct"] == pytest.approx(1.86)
    assert card["action_label"] == "减仓"
    assert card["entry_zone"] == "12.10~12.50"
    assert card["has_position"] is True
    assert card["quantity"] == pytest.approx(2000)
    assert card["pnl_pct"] == pytest.approx((12.04 - 6.0) / 6.0 * 100, abs=1e-3)
    assert card["position_value"] == pytest.approx(12.04 * 2000, abs=0.01)
    assert payload["quote_as_of"] == "2026-10-14 15:00:00"
    assert payload["generated_at"] == "2026-10-14 15:30:00"
    assert payload["urgent_count"] == 0
    assert payload["action_counts"] == {"reduce": 1}
    assert payload["disclaimer"]


def test_build_falls_back_to_watchlist_name_when_overview_empty():
    service = build_service(
        overview_service=FakeOverview({}),
        watchlist_service=FakeWatchlist([{"symbol": "601169", "name": "北京银行"}]),
        fetch=lambda symbol, days: None,
    )
    payload = service.build(None, with_weekly=False)

    assert payload["cards"][0]["symbol"] == "601169"
    assert payload["cards"][0]["name"] == "北京银行"
    # 无行情 → 涨跌为空，但卡片仍应存在
    assert payload["cards"][0]["price"] is None


def test_build_indices_and_market_date():
    frames = {
        "sh000001": make_bars([3800.0, 3842.0], ["2026-10-13", "2026-10-14"]),
        "sz399001": make_bars([12000.0, 11940.0], ["2026-10-13", "2026-10-14"]),
    }
    service = build_service(fetch=lambda symbol, days: frames.get(symbol))
    payload = service.build([], with_weekly=False)

    by_code = {item["code"]: item for item in payload["indices"]}
    assert by_code["sh000001"]["value"] == pytest.approx(3842.0)
    assert by_code["sh000001"]["change_pct"] == pytest.approx((3842 - 3800) / 3800 * 100, abs=1e-3)
    assert by_code["sz399001"]["change_pct"] < 0
    # 创业板指未取到 → 保留占位、值为空
    assert by_code["sz399006"]["value"] is None
    assert payload["market_date"] == "2026-10-14"


def test_build_indices_all_missing_degrades():
    service = build_service(fetch=lambda symbol, days: None)
    payload = service.build([], with_weekly=False)

    assert payload["market_date"] == ""
    assert any("指数行情暂不可用" in note for note in payload["degraded"])


def test_build_index_fetch_exception_is_isolated():
    def boom(symbol, days):
        raise RuntimeError("network down")

    service = build_service(fetch=boom)
    payload = service.build([], with_weekly=False)

    assert all(item["value"] is None for item in payload["indices"])
    assert any("指数行情暂不可用" in note for note in payload["degraded"])


def test_build_events_limited_and_degraded():
    service = build_service(
        overview_service=FakeOverview({"601169": overview_payload("601169")}),
        event_source=FakeEvents(["事件A", "事件B", "事件C", "事件D"]),
        fetch=lambda symbol, days: None,
    )
    payload = service.build(["601169"], with_weekly=False, limit_events=2)

    assert payload["events"] == ["事件A", "事件B"]

    broken = build_service(
        overview_service=FakeOverview({"601169": overview_payload("601169")}),
        event_source=FakeEvents(error=RuntimeError("no source")),
        fetch=lambda symbol, days: None,
    )
    payload2 = broken.build(["601169"], with_weekly=False)
    assert payload2["events"] == []
    assert any("事件源不可用" in note for note in payload2["degraded"])


def test_build_weekly_summary_and_degradation():
    service = build_service(
        overview_service=FakeOverview({"601169": overview_payload("601169")}),
        fetch=lambda symbol, days: None,
    )
    payload = service.build(["601169"])

    weekly = payload["weekly"]
    assert weekly["week_key"] == "2026-10-12"
    assert weekly["headline"] == "本周胜率 60%（6/10）"
    assert weekly["win_rate"] == pytest.approx(60.0)
    assert weekly["evaluated"] == 10
    assert weekly["position_week_pnl"] == pytest.approx(128.5)
    assert weekly["execution_counts"] == {"executed": 3, "ignored": 1}
    assert weekly["detail_url"] == WEEKLY_REVIEW_PATH


def test_build_weekly_can_be_disabled_and_failure_isolated():
    service = build_service(fetch=lambda symbol, days: None)
    assert service.build([], with_weekly=False)["weekly"] == {}

    class BrokenReview:
        def build(self):
            raise RuntimeError("review down")

    broken = build_service(review_service=BrokenReview(), fetch=lambda symbol, days: None)
    payload = broken.build([])
    assert payload["weekly"] == {}
    assert any("本周复盘摘要暂不可用" in note for note in payload["degraded"])


def test_build_weekly_uses_h5_base_url_for_detail_link():
    class ReviewWithBase(FakeReview):
        h5_base_url = "http://192.168.1.10:8000"

    service = build_service(review_service=ReviewWithBase(), fetch=lambda symbol, days: None)
    payload = service.build([])
    assert payload["weekly"]["detail_url"] == f"http://192.168.1.10:8000{WEEKLY_REVIEW_PATH}"


def test_card_failure_is_isolated_and_others_kept():
    service = build_service(
        overview_service=FakeOverview(
            {
                "601169": overview_payload("601169"),
                "601818": RuntimeError("boom"),
            }
        ),
        fetch=lambda symbol, days: None,
    )
    payload = service.build(["601169", "601818"], with_weekly=False)

    symbols = [card["symbol"] for card in payload["cards"]]
    assert symbols == ["601169", "601818"]
    broken = payload["cards"][1]
    assert broken["price"] is None
    assert broken["degraded"] and "聚合失败" in broken["degraded"][0]
    assert any("601818 聚合失败" in note for note in payload["degraded"])


def test_build_serial_branch_keeps_order():
    """``max_workers=1`` 走串行分支，顺序必须与入参一致。"""
    overview = FakeOverview(
        {code: overview_payload(code) for code in ("601169", "601818", "601919")}
    )
    service = build_service(overview_service=overview, fetch=lambda symbol, days: None, max_workers=1)
    payload = service.build(["601919", "601169", "601818"], with_weekly=False)

    assert [card["symbol"] for card in payload["cards"]] == ["601919", "601169", "601818"]
    assert overview.calls[0][0] == "601919"


def test_action_counts_order_and_urgent_count():
    payloads = {
        "A": overview_payload("A", action="watch", action_label="观望", is_urgent=False),
        "B": overview_payload("B", action="sell", action_label="清仓", is_urgent=True),
        "C": overview_payload("C", action="build", action_label="建仓", is_urgent=False),
    }
    service = build_service(overview_service=FakeOverview(payloads), fetch=lambda symbol, days: None)
    payload = service.build(["A", "B", "C"], with_weekly=False)

    keys = list(payload["action_counts"].keys())
    assert keys == sorted(keys, key=lambda action: ACTION_ORDER.index(action))
    assert payload["action_counts"] == {"build": 1, "sell": 1, "watch": 1}
    assert payload["urgent_count"] == 1


def test_portfolio_incomplete_propagates_to_degraded():
    service = build_service(
        overview_service=FakeOverview({"601169": overview_payload("601169")}),
        position_service=FakePositions(
            {
                "601169": PositionView(symbol="601169", avg_cost=6.0, quantity=100),
                "601818": PositionView(symbol="601818", avg_cost=3.0, quantity=100),
            }
        ),
        fetch=lambda symbol, days: None,
    )
    payload = service.build(["601169"], with_weekly=False)

    assert payload["portfolio"]["complete"] is False
    assert any("盈亏暂不展示" in note for note in payload["degraded"])


def test_with_quote_false_is_forwarded():
    overview = FakeOverview({"601169": overview_payload("601169")})
    service = build_service(overview_service=overview, fetch=lambda symbol, days: None)
    service.build(["601169"], with_quote=False, with_weekly=False)

    assert overview.calls == [("601169", False)]


def test_snapshot_cache_ttl_and_clear():
    overview = FakeOverview({"601169": overview_payload("601169")})
    service = ScreenService(
        overview_service=overview,
        watchlist_service=FakeWatchlist([]),
        position_service=FakePositions({}),
        event_source=FakeEvents([]),
        review_service=FakeReview(),
        fetch=lambda symbol, days: None,
        max_workers=1,
        cache_ttl_seconds=60,
        now=datetime(2026, 10, 14, 15, 30, 0),
    )
    first = service.build(["601169"], with_weekly=False)
    second = service.build(["601169"], with_weekly=False)

    assert len(overview.calls) == 1  # 命中缓存，未再取数
    assert second == first

    service.clear_cache()
    service.build(["601169"], with_weekly=False)
    assert len(overview.calls) == 2


def test_snapshot_cache_disabled_by_default():
    overview = FakeOverview({"601169": overview_payload("601169")})
    service = build_service(overview_service=overview, fetch=lambda symbol, days: None, max_workers=1)
    service.build(["601169"], with_weekly=False)
    service.build(["601169"], with_weekly=False)
    assert len(overview.calls) == 2


def test_quote_as_of_normalizes_iso_timestamp():
    """行情源返回 ISO（带 T 与微秒）时，数据截止时间应归一成可读串。"""
    service = build_service(
        overview_service=FakeOverview(
            {
                "601169": overview_payload(
                    "601169",
                    quote={
                        "current_price": 5.6,
                        "change_percent": -0.5,
                        "update_time": "2026-10-14T16:41:58.541498",
                    },
                )
            }
        ),
        fetch=lambda symbol, days: None,
    )
    payload = service.build(["601169"], with_weekly=False)
    assert payload["quote_as_of"] == "2026-10-14 16:41:58"


def test_screen_paths_constants():
    assert SCREEN_PATH == "/screen"
    assert WEEKLY_REVIEW_PATH == "/review"
