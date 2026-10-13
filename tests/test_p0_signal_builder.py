# -*- coding: utf-8 -*-
"""P0 信号编排离线端到端测试（不联网、不落库）。

用假的数据源与假的信号服务，验证：
- 指标 → 场景 → 风控 → payload 组装的完整链路；
- 失败隔离（单只标的异常不影响整批）；
- payload 关键字段符合 ``decision_signals`` 契约。
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.services.p0_signal_builder import P0SignalBuilder


def _uptrend_df(rows: int = 120) -> pd.DataFrame:
    closes = [8.0]
    for i in range(1, rows):
        # 前段上涨，最后一段回踩 MA20 附近（制造"回踩不破"买点）
        closes.append(closes[-1] + (0.05 if i < rows * 0.8 else -0.02))
    return pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=rows, freq="D"),
            "open": closes,
            "high": [c * 1.012 for c in closes],
            "low": [c * 0.988 for c in closes],
            "close": closes,
            "volume": [1000.0] * (rows - 1) + [2200.0],
        }
    )


class _FakeOutcome:
    def __init__(self):
        self.item = {"id": 42}
        self.created = True
        self.duplicate = False


class _FakeSignalService:
    def __init__(self):
        self.payloads = []

    def create_signal_with_outcome(self, payload):
        self.payloads.append(payload)
        return _FakeOutcome()


class _FakeWatchlist:
    def __init__(self, items):
        self._items = items

    def ensure_ready(self):
        return {"seeded": 0, "synced": 0, "total": len(self._items)}

    def list_watchlist(self, *, enabled_only=True):
        return list(self._items)


@pytest.fixture()
def builder(monkeypatch):
    items = [
        {"symbol": "601919", "name": "中远海控", "asset_type": "stock", "market": "cn"},
        {"symbol": "513100", "name": "纳指ETF(国泰)", "asset_type": "etf", "market": "cn", "track_index": "纳斯达克100"},
    ]
    fake_signals = _FakeSignalService()
    instance = P0SignalBuilder(
        watchlist_service=_FakeWatchlist(items),
        signal_service=fake_signals,
    )
    monkeypatch.setattr(P0SignalBuilder, "_fetch_daily", staticmethod(lambda symbol, days: _uptrend_df()))
    monkeypatch.setattr(P0SignalBuilder, "load_positions", staticmethod(lambda: {}))
    instance.fake_signals = fake_signals
    return instance


def test_run_builds_payload_for_each_symbol(builder):
    summary = builder.run(ensure_ready=True)
    assert summary.total == 2
    assert summary.succeeded == 2
    assert summary.failed == 0
    assert len(builder.fake_signals.payloads) == 2


def test_payload_matches_decision_signal_contract(builder):
    builder.run()
    payload = builder.fake_signals.payloads[0]

    assert payload["market"] == "cn"
    assert payload["source_type"] == "analysis"
    assert payload["trigger_source"] == "p0_engine"
    assert payload["market_phase"] == "postmarket"
    assert payload["action"] in {"buy", "add", "hold", "reduce", "sell", "watch"}
    assert 0.0 <= payload["confidence"] <= 1.0
    assert 0 <= payload["score"] <= 100

    metadata = payload["metadata"]
    assert metadata["engine"] == "p0_engine"
    assert metadata["scenario"] in {"up", "down", "sideways", "breakout", "volatile"}
    assert "position_advice" in metadata
    assert isinstance(metadata["take_profit_plan"], list)

    evidence = payload["evidence"]
    assert evidence["scenario"]["rules_version"] == 1
    assert evidence["risk_reasons"] is not None


def test_entry_range_order_is_valid(builder):
    builder.run()
    for payload in builder.fake_signals.payloads:
        low, high = payload.get("entry_low"), payload.get("entry_high")
        if low is not None and high is not None:
            assert low <= high


def test_etf_payload_carries_track_index(builder):
    builder.run()
    etf_payload = next(p for p in builder.fake_signals.payloads if p["stock_code"] == "513100")
    assert etf_payload["metadata"]["asset_type"] == "etf"
    assert etf_payload["metadata"]["track_index"] == "纳斯达克100"


def test_fetch_daily_unpacks_tuple(monkeypatch):
    """回归：统一取数入口返回 (DataFrame, source_name)，必须解包取 DataFrame。"""
    import data_provider

    frame = _uptrend_df(30)

    class _FakeManager:
        def get_daily_data(self, symbol, days=30):
            return frame, "TencentFetcher"

    monkeypatch.setattr(data_provider, "DataFetcherManager", _FakeManager)
    result = P0SignalBuilder._fetch_daily("601919", 30)
    assert isinstance(result, pd.DataFrame)
    assert len(result) == 30


def test_failure_isolation(monkeypatch):
    """一只标的取数失败，不应影响另一只。"""

    def _flaky_fetch(symbol, days):
        if symbol == "601919":
            raise RuntimeError("boom")
        return _uptrend_df()

    items = [
        {"symbol": "601919", "name": "中远海控", "asset_type": "stock", "market": "cn"},
        {"symbol": "513100", "name": "纳指ETF(国泰)", "asset_type": "etf", "market": "cn"},
    ]
    fake_signals = _FakeSignalService()
    instance = P0SignalBuilder(watchlist_service=_FakeWatchlist(items), signal_service=fake_signals)
    monkeypatch.setattr(P0SignalBuilder, "_fetch_daily", staticmethod(_flaky_fetch))
    monkeypatch.setattr(P0SignalBuilder, "load_positions", staticmethod(lambda: {}))

    summary = instance.run()
    assert summary.total == 2
    assert summary.succeeded == 1
    assert summary.failed == 1
    failed = next(r for r in summary.results if not r.ok)
    assert failed.symbol == "601919"
    assert "boom" in (failed.error or "")
