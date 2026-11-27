# -*- coding: utf-8 -*-
"""股息率历史分位（D-38）：TTM 口径、逐日推进不打未来、样本门槛。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.assistant.data.dividend import (
    DividendEvent,
    yield_percentile_series,
    yield_series,
)
from src.assistant.domain import Bar


def _bar(day: date, close: float) -> Bar:
    return Bar(trade_date=day, open=close, high=close, low=close, close=close, volume=1.0)


def _days(count: int, start: date = date(2025, 1, 1)) -> list[date]:
    return [start + timedelta(days=i) for i in range(count)]


class TestYieldSeries:
    def test_ttm_window_and_division(self) -> None:
        days = _days(3)
        bars = [_bar(day, 20.0) for day in days]
        events = [DividendEvent(code="600188", ex_date=days[1], cash_per_share=1.0)]

        series = yield_series(bars, events)

        assert series[days[1]] == pytest.approx(5.0)  # 1.0 / 20.0 * 100
        assert series[days[2]] == pytest.approx(5.0)  # TTM 仍含着这笔

    def test_event_older_than_window_is_excluded(self) -> None:
        bars = [_bar(date(2026, 6, 1), 20.0)]
        events = [DividendEvent(code="600188", ex_date=date(2024, 6, 1), cash_per_share=5.0)]

        assert yield_series(bars, events)[date(2026, 6, 1)] == pytest.approx(0.0)

    def test_event_before_its_own_date_is_not_used(self) -> None:
        """除权日之前不能算这笔股息（分子要用**已除权**的事件）。"""
        days = _days(3)
        bars = [_bar(day, 20.0) for day in days]
        events = [DividendEvent(code="600188", ex_date=days[2], cash_per_share=1.0)]

        series = yield_series(bars, events)

        assert series[days[0]] == pytest.approx(0.0)
        assert series[days[2]] == pytest.approx(5.0)

    def test_zero_close_is_skipped(self) -> None:
        series = yield_series([_bar(date(2026, 6, 1), 0.0)], [])

        assert series == {}

    def test_no_events_gives_empty(self) -> None:
        assert yield_series([_bar(date(2026, 6, 1), 10.0)], []) == {}


class TestYieldPercentile:
    def test_needs_min_obs(self) -> None:
        series = {date(2026, 1, 1): 3.0, date(2026, 1, 2): 4.0}

        assert yield_percentile_series(series, min_obs=10) == {}

    def test_percentile_uses_only_past(self) -> None:
        """把未来的高股息接在后面，过去那天的分位不能变。"""
        base = {date(2026, 1, 1) + timedelta(days=i): 3.0 + i * 0.01 for i in range(20)}
        future = {date(2026, 3, 1) + timedelta(days=i): 50.0 for i in range(20)}

        plain = yield_percentile_series(base, min_obs=5)
        with_future = yield_percentile_series({**base, **future}, min_obs=5)

        assert plain[date(2026, 1, 20)] == with_future[date(2026, 1, 20)]

    def test_high_yield_reads_high_percentile(self) -> None:
        series = {date(2026, 1, 1) + timedelta(days=i): 3.0 for i in range(20)}
        series[date(2026, 2, 1)] = 9.0

        pcts = yield_percentile_series(series, min_obs=5)

        assert pcts[date(2026, 2, 1)] == pytest.approx(1.0)
