# -*- coding: utf-8 -*-
"""移动 H5（M1）聚合服务单测。

覆盖：
1. 8 态动作 → 6 类展示口径；「无信号 ≠ 观望」；
2. 列表排序（紧急 → 有操作 → 观望 → 无信号）、行情缺失降级、短缓存；
3. 详情四价透传、距止损/目标计算、五维分析（板块显式不可用）。
"""

from __future__ import annotations

import pytest

from src.services.h5_service import (
    H5Service,
    build_analysis,
    display_action,
)


class FakeWatchlistService:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def list_watchlist(self, *, enabled_only: bool = True):
        self.calls += 1
        return list(self.rows)


class FakeOverviewService:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def load(self, symbol, **kwargs):
        self.calls.append((symbol, kwargs))
        return dict(self.payloads.get(symbol) or {})


class FakeStockService:
    def __init__(self, quotes):
        self.quotes = quotes

    def get_realtime_quote(self, symbol):
        return self.quotes.get(symbol)


def _overview(symbol: str, *, action: str = "watch", signal_id=1, **extra):
    base = {
        "symbol": symbol,
        "name": f"标的{symbol}",
        "as_of": "2026-10-20",
        "generated_at": "2026-10-20T20:00:00",
        "action": action,
        "action_label": action,
        "scenario": "up",
        "scenario_label": "上涨趋势",
        "conclusion": "测试结论",
        "reason": "测试理由",
        "score": 72,
        "confidence": 0.8,
        "is_urgent": action in {"sell", "clear"},
        "entry_low": 10.0,
        "entry_high": 10.5,
        "entry_zone": "10.00~10.50",
        "stop_loss": 9.5,
        "target_price": 12.0,
        "position_ratio": 0.3,
        "position_ratio_label": "30%",
        "position_reason": "首建不超过 3 成",
        "discipline": "仓位建议 30% ｜ 止损 9.50",
        "watch_conditions": "破 9.5 离场",
        "risk_summary": "跌破 MA20 需减仓",
        "factors": [
            {"key": "scenario", "label": "场景判定", "value": "上涨趋势", "tone": "neutral"},
            {"key": "trend", "label": "日线趋势", "value": "向上", "tone": "positive"},
            {"key": "weekly_trend", "label": "周线趋势", "value": "向上", "tone": "positive"},
            {"key": "multi_period_conflict", "label": "多周期共振", "value": "共振（方向一致）", "tone": "positive"},
            {"key": "score", "label": "综合评分", "value": "72/100", "tone": "positive"},
            {"key": "risk_reasons", "label": "风控依据", "value": "跌破 MA20 需减仓", "tone": "warning"},
        ],
        "events": ["【风险】某公司被出具问询函"],
        "quote": {"current_price": 10.8, "change_percent": 1.2, "update_time": "2026-10-20T15:00:00"},
        "distance_to_stop_pct": 13.68,
        "distance_to_target_pct": -10.0,
        "signal_id": signal_id,
        "signal_created_at": "2026-10-20T20:00:12",
        "degraded": [],
        "disclaimer": "免责声明",
    }
    base.update(extra)
    return base


# --------------------------------------------------------------------------- #
# 展示口径
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("action", "has_signal", "expected"),
    [
        ("build", True, "建仓"),
        ("buy", True, "建仓"),
        ("dip_buy", True, "建仓"),
        ("add", True, "加仓"),
        ("hold", True, "持有"),
        ("reduce", True, "减仓"),
        ("sell", True, "清仓"),
        ("clear", True, "清仓"),
        ("wait", True, "观望"),
        ("watch", True, "观望"),
        ("avoid", True, "观望"),
        ("watch", False, "暂无信号"),
    ],
)
def test_display_action_mapping(action, has_signal, expected):
    assert display_action(action, has_signal) == expected


# --------------------------------------------------------------------------- #
# 列表
# --------------------------------------------------------------------------- #
def test_list_sorts_urgent_first_and_marks_no_signal():
    watchlist = FakeWatchlistService(
        [
            {"symbol": "601919", "name": "中远海控", "asset_type": "stock"},
            {"symbol": "513100", "name": "纳指ETF", "asset_type": "etf"},
            {"symbol": "601169", "name": "北京银行", "asset_type": "stock"},
        ]
    )
    overview = FakeOverviewService(
        {
            "601919": _overview("601919", action="reduce"),
            "513100": _overview("513100", action="watch", signal_id=None),
            "601169": _overview("601169", action="sell"),
        }
    )
    quotes = {
        "601919": {"current_price": 15.3, "change_percent": 1.2},
        "513100": {"current_price": 1.234, "change_percent": -0.5},
        "601169": {"current_price": 6.4, "change_percent": 0.3},
    }

    service = H5Service(
        overview_service=overview,
        watchlist_service=watchlist,
        stock_service=FakeStockService(quotes),
        fetch=lambda symbol, days: None,
        cache_ttl_seconds=0,
        market_cache_ttl_seconds=0,
    )
    payload = service.list_watchlist()

    labels = [item["display_action"] for item in payload["items"]]
    assert labels == ["清仓", "减仓", "暂无信号"]
    assert payload["items"][0]["is_urgent"] is True
    assert payload["items"][2]["has_signal"] is False
    assert payload["items"][2]["price"] == 1.234
    # 列表不应聚合事件源（少一次外部调用）
    assert all(call[1].get("with_events") is False for call in overview.calls)


def test_list_degrades_when_quote_missing():
    watchlist = FakeWatchlistService([{"symbol": "601919", "name": "中远海控"}])
    overview = FakeOverviewService({"601919": _overview("601919", action="add")})
    service = H5Service(
        overview_service=overview,
        watchlist_service=watchlist,
        stock_service=FakeStockService({}),
        fetch=lambda symbol, days: None,
        cache_ttl_seconds=0,
        market_cache_ttl_seconds=0,
    )

    payload = service.list_watchlist()

    assert payload["items"][0]["price"] is None
    assert payload["items"][0]["display_action"] == "加仓"
    assert any("行情" in note for note in payload["degraded"])


def test_list_uses_short_cache():
    watchlist = FakeWatchlistService([{"symbol": "601919", "name": "中远海控"}])
    overview = FakeOverviewService({"601919": _overview("601919", action="add")})
    service = H5Service(
        overview_service=overview,
        watchlist_service=watchlist,
        stock_service=FakeStockService({"601919": {"current_price": 15.3}}),
        fetch=lambda symbol, days: None,
        market_cache_ttl_seconds=0,
        cache_ttl_seconds=60,
    )

    first = service.list_watchlist()
    second = service.list_watchlist()

    assert first == second
    assert watchlist.calls == 1
    # 强制刷新绕过缓存
    service.list_watchlist(force=True)
    assert watchlist.calls == 2


# --------------------------------------------------------------------------- #
# 详情与分析
# --------------------------------------------------------------------------- #
def test_build_analysis_covers_four_dimensions():
    sections = build_analysis(_overview("601919", action="reduce"))
    by_key = {section["key"]: section for section in sections}

    # 大盘已移出详情页（与首页大盘卡片重复），只保留四个维度
    assert [section["key"] for section in sections] == ["technical", "sector", "risk", "news"]
    assert "market" not in by_key
    assert by_key["technical"]["available"] is True
    assert by_key["technical"]["tone"] == "up"
    assert "日线向上" in by_key["technical"]["verdict"]
    assert by_key["news"]["available"] is True
    assert by_key["news"]["tone"] == "warning"
    assert by_key["risk"]["available"] is True
    # 板块本期未接入：必须显式标记，不得伪造结论
    # 未注入板块数据时如实降级（不伪装成"未接入"或"正常"）
    assert by_key["sector"]["available"] is False
    assert "板块数据暂不可用" in by_key["sector"]["verdict"]


def test_build_analysis_has_no_market_section():
    sections = build_analysis(_overview("601919"))
    assert all(section["key"] != "market" for section in sections)
    assert [section["label"] for section in sections] == ["技术面", "板块", "风险", "消息面"]


class _FakeSeries:
    """最小的 pandas Series 替身：只支持 ``series.iloc[-1]``。"""

    def __init__(self, values):
        self._values = list(values)

    @property
    def iloc(self):
        return self

    def __getitem__(self, index):
        return self._values[index]


class _FakeBars:
    """最小的 DataFrame 替身：``len(bars)`` / ``bars[col].iloc[-1]``。"""

    columns = ("date", "close")

    def __init__(self, dates, closes):
        self._dates = list(dates)
        self._closes = list(closes)

    def __len__(self):
        return len(self._closes)

    def __getitem__(self, key):
        return _FakeSeries(self._closes if key == "close" else self._dates)


def test_detail_passes_through_ohlc_and_computes_distance():
    from datetime import date

    bars = _FakeBars([date(2026, 10, 17), date(2026, 10, 20)], [3180.0, 3200.0])

    watchlist = FakeWatchlistService([])
    overview = FakeOverviewService({"601919": _overview("601919", action="reduce")})
    service = H5Service(
        overview_service=overview,
        watchlist_service=watchlist,
        stock_service=FakeStockService(
            {
                "601919": {
                    "current_price": 10.8,
                    "open": 10.5,
                    "high": 11.0,
                    "low": 10.4,
                    "prev_close": 10.6,
                    "change": 0.2,
                    "change_percent": 1.89,
                    "volume": 1200000,
                    "update_time": "2026-10-20T15:00:00",
                }
            }
        ),
        fetch=lambda symbol, days: bars,
        cache_ttl_seconds=0,
    )

    payload = service.detail("601919")

    assert payload["quote"]["open"] == 10.5
    assert payload["quote"]["high"] == 11.0
    assert payload["quote"]["low"] == 10.4
    assert payload["quote"]["close"] == 10.8
    # 10.8 相对止损 9.5 → +13.68%；相对目标 12 → -10%
    assert payload["distance_to_stop_pct"] == 13.68
    assert payload["distance_to_target_pct"] == -10.0
    # 详情不再返回大盘快照（首页大盘卡片已覆盖，避免重复）
    assert "market" not in payload
    assert [section["key"] for section in payload["analysis"]] == [
        "technical",
        "sector",
        "risk",
        "news",
    ]


def test_detail_without_signal_is_not_watch():
    overview = FakeOverviewService(
        {
            "601919": _overview(
                "601919",
                action="watch",
                signal_id=None,
                conclusion="暂无决策信号",
                degraded=["暂无该标的的决策信号：请先运行每日信号（P1 推送）或等待下一次分析落库"],
            )
        }
    )
    service = H5Service(
        overview_service=overview,
        watchlist_service=FakeWatchlistService([]),
        stock_service=FakeStockService({}),
        fetch=lambda symbol, days: None,
        cache_ttl_seconds=0,
    )

    payload = service.detail("601919")

    assert payload["has_signal"] is False
    assert payload["display_action"] == "暂无信号"
