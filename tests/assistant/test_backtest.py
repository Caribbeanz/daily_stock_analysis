# -*- coding: utf-8 -*-
"""条件计划回合模拟（需求 §9.4.2 / §9.4.3）测试。

重点盯三类容易出错的地方：
1. **保守假设**：同日先止损、跳空按开盘价不追单；
2. **T+1**：买入当日不可卖；
3. **样本不足不给胜率**（§9.4.3），且必须同时给分阶段/分标的（§9.4.2）。
"""

from __future__ import annotations

from datetime import date

import pytest

from src.assistant.backtest import (
    PlanRound,
    RoundConfig,
    pending_plan_rounds,
    simulate_rounds,
    summarize_rounds,
)
from src.assistant.domain import Bar


def bar(day: int, *, o: float, h: float, l: float, c: float) -> Bar:  # noqa: E741
    return Bar(trade_date=date(2026, 6, day), open=o, high=h, low=l, close=c, volume=1_000_000.0)


def plan(**kwargs) -> dict:
    base = {
        "code": "600000",
        "branch_id": "b1",
        "date": date(2026, 6, 1),
        "trigger_type": "price_lte",
        "trigger_price": 10.0,
        "action": "买入",
        "shares": 1000,
        "scenario": "回踩买点",
        "stage": "S1",
    }
    base.update(kwargs)
    return base


BARS = [
    bar(1, o=10.5, h=10.6, l=10.4, c=10.5),
    bar(2, o=10.2, h=10.3, l=9.9, c=10.0),   # 触发 10.0
    bar(3, o=10.0, h=10.8, l=9.9, c=10.7),
    bar(4, o=10.7, h=11.2, l=10.6, c=11.0),  # 触及止盈 11.0
    bar(5, o=11.0, h=11.1, l=10.8, c=10.9),
]


class TestTrigger:
    def test_price_lte_triggers_and_fills(self) -> None:
        rounds = simulate_rounds([plan(take_profit=11.0)], {"600000": BARS})

        row = rounds[0]
        assert row.entry_date == date(2026, 6, 2)
        assert row.entry_price == pytest.approx(10.0)
        assert row.exit_reason == "take_profit"
        assert row.exit_date == date(2026, 6, 4)
        assert row.closed is True

    def test_no_trigger_within_validity(self) -> None:
        rounds = simulate_rounds([plan(trigger_price=9.0, valid_days=1)], {"600000": BARS})

        assert rounds[0].closed is False
        assert "未触发" in rounds[0].note

    def test_gap_down_fills_at_open_not_better(self) -> None:
        bars = [bar(1, o=10.5, h=10.6, l=10.4, c=10.5),
                bar(2, o=9.5, h=9.8, l=9.4, c=9.6)]  # 跳空低开穿过 10.0
        rounds = simulate_rounds([plan()], {"600000": bars})

        assert rounds[0].entry_price == pytest.approx(9.5)  # 按开盘价，不假设拿到 10.0

    def test_price_gte_triggers_on_breakout(self) -> None:
        bars = [bar(1, o=10.5, h=10.6, l=10.4, c=10.5),
                bar(2, o=10.6, h=11.2, l=10.5, c=11.0)]
        rounds = simulate_rounds(
            [plan(trigger_type="price_gte", trigger_price=11.0)], {"600000": bars}
        )

        assert rounds[0].entry_price == pytest.approx(11.0)

    def test_pct_trigger_is_not_guessed(self) -> None:
        """涨跌幅触发需要前收盘折算，模块不自己算，如实不触发。"""
        rounds = simulate_rounds(
            [plan(trigger_type="pct_lte", trigger_price=None, pct_change=-2.0)],
            {"600000": BARS},
        )

        assert rounds[0].closed is False


class TestExit:
    def test_same_day_stop_and_target_prefers_stop(self) -> None:
        """同日既破止损又摸止盈：按止损算（保守，§9.4.3）。"""
        bars = [bar(1, o=10.5, h=10.6, l=10.4, c=10.5),
                bar(2, o=10.2, h=10.3, l=9.9, c=10.0),
                bar(3, o=10.0, h=11.5, l=9.0, c=9.5)]
        rounds = simulate_rounds([plan(stop_loss=9.5, take_profit=11.0)], {"600000": bars})

        assert rounds[0].exit_reason == "stop_loss"
        assert rounds[0].exit_price == pytest.approx(9.5)
        assert rounds[0].net_pnl < 0

    def test_t_plus_one_blocks_same_day_exit(self) -> None:
        bars = [bar(1, o=10.5, h=10.6, l=10.4, c=10.5),
                bar(2, o=10.0, h=10.1, l=9.0, c=9.2)]  # 当日即破止损，但 T+1 不能卖
        rounds = simulate_rounds([plan(stop_loss=9.8)], {"600000": bars}, config=RoundConfig(max_hold_days=1))

        assert rounds[0].entry_date == date(2026, 6, 2)
        assert rounds[0].exit_date is None  # 没有次日数据 → 未闭合
        assert rounds[0].closed is False

    def test_max_hold_forces_exit(self) -> None:
        rounds = simulate_rounds([plan()], {"600000": BARS}, config=RoundConfig(max_hold_days=2))

        assert rounds[0].exit_reason == "max_hold"
        assert rounds[0].exit_date == date(2026, 6, 4)

    def test_cost_applied_both_sides(self) -> None:
        rounds = simulate_rounds(
            [plan(take_profit=11.0)], {"600000": BARS}, config=RoundConfig(cost_bps=10.0)
        )

        row = rounds[0]
        expected = 10.0 * 1000 * 0.001 + 11.0 * 1000 * 0.001
        assert row.cost == pytest.approx(expected)
        assert row.net_pnl == pytest.approx(row.gross_pnl - expected)


class TestSellBranches:
    def test_sell_branch_does_not_create_round(self) -> None:
        rounds = simulate_rounds([plan(action="减仓")], {"600000": BARS})

        assert rounds[0].closed is False
        assert "卖出类分支" in rounds[0].note

    def test_zero_shares_recorded_but_flagged(self) -> None:
        rounds = simulate_rounds([plan(shares=0)], {"600000": BARS})

        assert "股数为 0" in rounds[0].note


class TestSummary:
    def test_small_sample_reports_no_win_rate(self) -> None:
        rounds = simulate_rounds([plan(take_profit=11.0)], {"600000": BARS})

        summary = summarize_rounds(rounds)

        assert summary["win_rate"] is None
        assert summary["sample_sufficient"] is False
        assert "样本不足" in summary["note"]
        assert summary["closed"] == 1

    def test_win_rate_when_enough(self) -> None:
        rows = [
            PlanRound(code="600000", branch_id=f"b{i}", stage="S1", shares=100,
                      entry_price=10.0, exit_price=11.0, closed=True, net_pnl=90.0, days_held=3)
            for i in range(30)
        ]
        rows += [
            PlanRound(code="600000", branch_id="c1", stage="S2", shares=100,
                      entry_price=10.0, exit_price=9.0, closed=True, net_pnl=-110.0, days_held=4)
        ]

        summary = summarize_rounds(rows)

        assert summary["sample_sufficient"] is True
        assert summary["win_rate"] == pytest.approx(30 / 31, rel=1e-3)
        assert summary["payoff"] == round(90 / 110, 2)  # 指标落库统一保留两位
        # 分阶段必须给；单桶不足 30 时该桶不报胜率
        assert summary["by_stage"]["S1"]["win_rate"] is not None
        assert summary["by_stage"]["S2"]["win_rate"] is None
        assert summary["by_code"]["600000"]["closed"] == 31

    def test_open_rounds_excluded_from_denominator(self) -> None:
        rounds = [
            PlanRound(code="600000", branch_id="b1", shares=100, closed=True, net_pnl=50.0),
            PlanRound(code="600000", branch_id="b2", shares=100, closed=False),
        ]

        summary = summarize_rounds(rounds, config=RoundConfig(min_closed_rounds=1))

        assert summary["closed"] == 1
        assert summary["open"] == 1
        assert summary["win_rate"] == 1.0
        assert len(pending_plan_rounds(rounds)) == 1

    def test_reports_not_modelled_items(self) -> None:
        summary = summarize_rounds([])

        assert summary["not_modelled"]
        assert any("做 T" in item for item in summary["not_modelled"])


class TestConfig:
    @pytest.mark.parametrize("kwargs", [{"cost_bps": -1}, {"max_hold_days": 0}])
    def test_invalid_config_rejected(self, kwargs) -> None:
        with pytest.raises(ValueError):
            RoundConfig(**kwargs)

    def test_missing_bars_skipped(self) -> None:
        assert simulate_rounds([plan()], {}) == []
