# -*- coding: utf-8 -*-
"""估值分位与分红事实（D-34）：滚动分位不打未来、连续性判定、接线到评分。"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from src.assistant import backtest_replay as replay_module
from src.assistant.backtest_replay import ReplayConfig, replay
from src.assistant.data.dividend import DividendEvent, dividend_facts
from src.assistant.data.storage import AssistantStore
from src.assistant.data.valuation import (
    ValuationPoint,
    ValuationRow,
    percentile_series,
    valuation_view,
    valuation_view_from_rows,
)
from src.assistant.engine.scoring import dividend_adjustment
from src.assistant.engine.stage import StageDebounceState
from src.assistant.domain import ValuationView
from src.assistant.enums import Action, Stage

DAY = date(2026, 11, 26)


class TestDividendScoringOff:
    """分红退出评分（w_dividend=0）：0 分，但事实要留在说明里。"""

    def test_weight_zero_gives_no_points_but_keeps_facts(self) -> None:
        view = ValuationView(dividend_continuity_years=10, dividend_yield=3.07)

        adj, notes = dividend_adjustment(view, 0.0)

        assert adj == 0.0
        joined = "；".join(notes)
        assert "不进评分" in joined
        assert "连续分红 10 年" in joined
        assert "3.07%" in joined

    def test_weight_zero_still_reports_interruption_as_fact(self) -> None:
        view = ValuationView(dividend_continuity_years=0, dividend_interrupted=True)

        adj, notes = dividend_adjustment(view, 0.0)

        assert adj == 0.0
        assert any("DIV-1" in note for note in notes)

    def test_missing_data_is_still_flagged_when_weight_is_zero(self) -> None:
        adj, notes = dividend_adjustment(None, 0.0)

        assert adj == 0.0
        assert any("缺失" in note for note in notes)


def _points(values: list[float]) -> list[ValuationPoint]:
    start = date(2024, 1, 2)
    return [
        ValuationPoint(trade_date=start + timedelta(days=i), pe=value, pb=value / 5)
        for i, value in enumerate(values)
    ]


class TestPercentileSeries:
    def test_no_percentile_before_min_obs(self) -> None:
        """样本不足就不给分位：30 个点的分位等于噪声。"""
        rows = percentile_series(_points([10, 11, 12, 13]), min_obs=3)

        assert rows[0].pe_pctile is None
        assert rows[1].pe_pctile is None
        assert rows[2].pe_pctile is not None  # 第 3 个点起样本够

    def test_uses_only_past_samples(self) -> None:
        """分位只用 ≤当日 的样本：插入一个更低的未来值不能改写历史分位。"""
        rows = percentile_series(_points([10, 20, 30]), min_obs=2)
        forward = percentile_series(_points([10, 20, 30, 5]), min_obs=2)

        assert [r.pe_pctile for r in rows] == [r.pe_pctile for r in forward[:3]]

    def test_rank_math(self) -> None:
        rows = percentile_series(_points([10, 20, 30, 20]), min_obs=2)

        # 第 4 个点：样本 [10,20,20,30]，严格小于 20 的有 1 个 → 1/3
        assert rows[3].pe_pctile == pytest.approx(round(1 / 3, 4))

    def test_non_positive_pe_does_not_enter_percentile(self) -> None:
        rows = percentile_series(_points([10, -5, 20]), min_obs=2)

        assert rows[1].pe == -5          # 值保留（亏损是事实）
        assert rows[1].pe_pctile is None  # 但不参与分位
        assert rows[2].pe_pctile is not None

    def test_flat_series_is_half(self) -> None:
        rows = percentile_series(_points([10, 10, 10]), min_obs=2)

        assert rows[-1].pe_pctile == 0.5


class TestValuationView:
    def test_takes_latest_row_on_or_before_day(self) -> None:
        rows = [
            ValuationRow(trade_date=date(2026, 11, 20), pe_pctile=0.3),
            ValuationRow(trade_date=date(2026, 11, 25), pe_pctile=0.7),
            ValuationRow(trade_date=date(2026, 11, 27), pe_pctile=0.9),
        ]

        view = valuation_view_from_rows(rows, DAY)

        assert view is not None
        assert view.pe_percentile == 0.7

    def test_stale_row_beyond_five_days_is_missing(self) -> None:
        rows = [ValuationRow(trade_date=date(2026, 10, 1), pe_pctile=0.3)]

        assert valuation_view_from_rows(rows, DAY) is None

    def test_merges_dividend_facts(self) -> None:
        rows = [ValuationRow(trade_date=DAY, pe_pctile=0.2, pb_pctile=0.4)]
        events = [
            DividendEvent(code="600188", ex_date=date(2025, 6, 20), cash_per_share=1.0),
            DividendEvent(code="600188", ex_date=date(2024, 6, 20), cash_per_share=1.0),
            DividendEvent(code="600188", ex_date=date(2026, 6, 20), cash_per_share=0.5),
        ]

        view = valuation_view(rows, events, DAY, 20.0)

        assert view.pe_percentile == 0.2
        assert view.dividend_continuity_years == 2
        assert view.dividend_interrupted is False
        assert view.dividend_yield == pytest.approx(round(0.5 / 20.0 * 100, 4))


class TestDividendFacts:
    def test_consecutive_years_counted_back_from_last_full_year(self) -> None:
        events = [
            DividendEvent(code="X", ex_date=date(y, 6, 1), cash_per_share=0.5)
            for y in (2023, 2024, 2025)
        ]

        facts = dividend_facts(events, DAY, 10.0)

        assert facts.continuity_years == 3
        assert facts.interrupted is False

    def test_current_year_event_does_not_break_continuity(self) -> None:
        """当年还没到除权日不算中断：按完整年度数，不按「今年有没有」数。"""
        events = [
            DividendEvent(code="X", ex_date=date(2024, 6, 1), cash_per_share=0.5),
            DividendEvent(code="X", ex_date=date(2025, 6, 1), cash_per_share=0.5),
        ]

        facts = dividend_facts(events, date(2026, 3, 1), 10.0)

        assert facts.interrupted is False
        assert facts.continuity_years == 2

    def test_missing_last_year_is_interrupted(self) -> None:
        events = [
            DividendEvent(code="X", ex_date=date(2023, 6, 1), cash_per_share=0.5),
            DividendEvent(code="X", ex_date=date(2024, 6, 1), cash_per_share=0.5),
        ]

        facts = dividend_facts(events, DAY, 10.0)

        assert facts.interrupted is True
        assert facts.continuity_years == 0

    def test_insufficient_coverage_does_not_guess(self) -> None:
        """事件只有今年一条：去年到底有没有分红无从判断 → None，不判中断。"""
        events = [DividendEvent(code="X", ex_date=date(2026, 6, 1), cash_per_share=0.5)]

        facts = dividend_facts(events, DAY, 10.0)

        assert facts.continuity_years is None
        assert facts.interrupted is False
        assert "覆盖不足" in facts.note

    def test_future_event_is_not_used(self) -> None:
        events = [
            DividendEvent(code="X", ex_date=date(2024, 6, 1), cash_per_share=0.5),
            DividendEvent(code="X", ex_date=date(2027, 6, 1), cash_per_share=9.0),
        ]

        facts = dividend_facts(events, DAY, 10.0)

        assert facts.ttm_cash_per_share == 0.0
        assert facts.ex_dividend_date == date(2027, 6, 1)

    def test_ttm_yield_uses_last_365_days(self) -> None:
        events = [
            DividendEvent(code="X", ex_date=date(2025, 1, 10), cash_per_share=1.0),  # 超出 365 天
            DividendEvent(code="X", ex_date=date(2026, 6, 1), cash_per_share=0.6),
        ]

        facts = dividend_facts(events, DAY, 20.0)

        assert facts.ttm_cash_per_share == pytest.approx(0.6)
        assert facts.dividend_yield == pytest.approx(3.0)

    def test_no_events_is_not_interrupted(self) -> None:
        """完全没有事件表覆盖，不能判「中断」——那是数据缺失，不是公司不分红。"""
        facts = dividend_facts([], DAY, 10.0)

        assert facts.interrupted is False
        assert facts.continuity_years is None
        assert facts.has_data is False


class TestValuationStorage:
    def test_roundtrip_keeps_percentiles(self, tmp_path) -> None:
        store = AssistantStore(tmp_path / "v.db")
        store.create_all()
        rows = percentile_series(_points([10, 12, 14, 13]), min_obs=2)

        assert store.save_valuation_rows("600188", rows) == 4
        loaded = store.load_valuation_rows("600188")

        assert [r.trade_date for r in loaded] == [r.trade_date for r in rows]
        assert loaded[-1].pe_pctile == pytest.approx(rows[-1].pe_pctile)

    def test_latest_valuation_respects_day(self, tmp_path) -> None:
        store = AssistantStore(tmp_path / "v.db")
        store.create_all()
        store.save_valuation_rows(
            "600188",
            [
                ValuationRow(trade_date=date(2026, 11, 20), pe_pctile=0.3),
                ValuationRow(trade_date=date(2026, 11, 25), pe_pctile=0.7),
            ],
        )

        assert store.latest_valuation("600188", DAY).pe_pctile == 0.7
        assert store.latest_valuation("600188", date(2026, 11, 1)) is None

    def test_idempotent_rewrite(self, tmp_path) -> None:
        store = AssistantStore(tmp_path / "v.db")
        store.create_all()
        store.save_valuation_rows("600188", [ValuationRow(trade_date=DAY, pe_pctile=0.3)])
        store.save_valuation_rows("600188", [ValuationRow(trade_date=DAY, pe_pctile=0.8)])

        loaded = store.load_valuation_rows("600188")
        assert len(loaded) == 1
        assert loaded[0].pe_pctile == 0.8


class TestReplayWiring:
    def test_valuation_reaches_stock_view(self, monkeypatch) -> None:
        captured: list = []

        def fake_chain(inp):
            captured.append(inp)
            return SimpleNamespace(
                result=SimpleNamespace(
                    code=inp.view.code,
                    plans=(),
                    key_levels=(),
                    stage_result=SimpleNamespace(stage=SimpleNamespace(value=Stage.S1.value)),
                    conclusion=SimpleNamespace(trend="上升", advice=Action.BUY),
                ),
                stage_state=StageDebounceState(stage=Stage.S1, as_of=DAY),
            )

        monkeypatch.setattr(replay_module, "run_guard_chain", fake_chain)
        start = date(2026, 9, 1)
        days = [start + timedelta(days=i) for i in range(70)]
        bars = [
            SimpleNamespace(
                trade_date=day, open=20.0, high=20.5, low=19.5, close=20.0,
                volume=1_000_000.0, amount=0.0, adj_factor=1.0,
            )
            for day in days
        ]

        result = replay(
            days=days,
            bars_by_code={"600188": bars},
            config=ReplayConfig(min_bars=60, lookback_bars=70),
            valuations_by_code={
                "600188": [ValuationRow(trade_date=days[-1], pe_pctile=0.9, pb_pctile=0.9)]
            },
            dividend_events={
                "600188": [DividendEvent(code="600188", ex_date=date(2025, 6, 1),
                                         cash_per_share=1.0)]
            },
        )

        valued = [inp.view.valuation for inp in captured if inp.view.valuation.pe_percentile]
        assert valued, "估值行没有进 StockView"
        assert valued[-1].pe_percentile == 0.9
        assert result.stats["valuation"]["codes_with_rows"] == 1
        assert result.stats["valuation"]["codes_with_events"] == 1
