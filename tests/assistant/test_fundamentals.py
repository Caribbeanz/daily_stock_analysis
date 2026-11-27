# -*- coding: utf-8 -*-
"""财报事实（D-38）：公告日决定可见性、连续下滑要真连续、存储往返。"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from src.assistant.data.fundamentals import (
    FundamentalReport,
    fundamental_view_from_reports,
    parse_report_rows,
)
from src.assistant import backtest_replay as replay_module
from src.assistant.backtest_replay import ReplayConfig, replay
from src.assistant.data.storage import AssistantStore
from src.assistant.engine.stage import StageDebounceState
from src.assistant.enums import Action, Stage


def _row(
    report_date: str,
    notice_date: str | None,
    *,
    revenue_yoy: float | None = 5.0,
    profit_yoy: float | None = 5.0,
    roe_yoy: float | None = 5.0,
    net_profit: float = 1e8,
    ocf: float = 1.0,
    ocf_yoy: float | None = None,
) -> dict:
    return {
        "REPORT_DATE": f"{report_date} 00:00:00",
        "NOTICE_DATE": f"{notice_date} 00:00:00" if notice_date else None,
        "REPORT_DATE_NAME": report_date.replace("-", "")[:6],
        "TOTALOPERATEREVE": 1e9,
        "TOTALOPERATEREVETZ": revenue_yoy,
        "PARENTNETPROFIT": net_profit,
        "PARENTNETPROFITTZ": profit_yoy,
        "ROEJQ": 8.0,
        "ROEJQTZ": roe_yoy,
        "MGJYXJJE": ocf,
        "MGJYXJJETZ": ocf_yoy,
        "ZCFZL": 50.0,
        "XSMLL": 30.0,
    }


class TestParsing:
    def test_dates_and_numbers(self) -> None:
        reports = parse_report_rows("600188", [_row("2026-09-30", "2026-10-31")])

        assert len(reports) == 1
        assert reports[0].report_date == date(2026, 9, 30)
        assert reports[0].notice_date == date(2026, 10, 31)
        assert reports[0].revenue_yoy == pytest.approx(5.0)

    def test_row_without_report_date_is_dropped(self) -> None:
        assert parse_report_rows("600188", [{"NOTICE_DATE": "2026-10-31"}]) == []

    def test_duplicate_period_keeps_first(self) -> None:
        rows = [_row("2026-09-30", "2026-10-31"), _row("2026-09-30", "2026-11-05")]

        reports = parse_report_rows("600188", rows)

        assert len(reports) == 1
        assert reports[0].notice_date == date(2026, 10, 31)


class TestVisibility:
    def test_not_visible_before_notice_date(self) -> None:
        """三季报 10-31 公告：10-01 看不到它——这是最容易被写错的前视偏差。"""
        reports = parse_report_rows("600188", [_row("2026-09-30", "2026-10-31")])

        before = fundamental_view_from_reports(reports, date(2026, 10, 1))
        after = fundamental_view_from_reports(reports, date(2026, 10, 31))

        assert before.available is False
        assert before.report_period == ""
        assert after.available is True
        assert "202609" in after.report_period

    def test_report_without_notice_date_never_used(self) -> None:
        """不知道什么时候公告的财报不参与判定（宁可缺失，不猜发布时间）。"""
        reports = parse_report_rows("600188", [_row("2026-09-30", None)])

        assert fundamental_view_from_reports(reports, date(2027, 1, 1)).available is False

    def test_latest_visible_period_wins(self) -> None:
        reports = parse_report_rows(
            "600188",
            [
                _row("2026-09-30", "2026-10-31", revenue_yoy=6.0),
                _row("2026-06-30", "2026-08-20", revenue_yoy=-3.0),
            ],
        )

        view = fundamental_view_from_reports(reports, date(2026, 9, 1))

        # 9-01 能看到的只有半年报
        assert "202606" in view.report_period
        assert view.revenue_declining_periods == 1


class TestDeclineCounts:
    def _reports(self, **kwargs) -> list[FundamentalReport]:
        return parse_report_rows("600188", [_row("2026-09-30", "2026-10-31", **kwargs)])

    def test_three_consecutive_quarters(self) -> None:
        reports = parse_report_rows(
            "600188",
            [
                _row("2026-09-30", "2026-10-31", revenue_yoy=-1.0),
                _row("2026-06-30", "2026-08-20", revenue_yoy=-2.0),
                _row("2026-03-31", "2026-04-25", revenue_yoy=-3.0),
            ],
        )

        assert fundamental_view_from_reports(reports, date(2026, 11, 1)).revenue_declining_periods == 3

    def test_positive_growth_breaks_the_streak(self) -> None:
        reports = parse_report_rows(
            "600188",
            [
                _row("2026-09-30", "2026-10-31", revenue_yoy=-1.0),
                _row("2026-06-30", "2026-08-20", revenue_yoy=2.0),
                _row("2026-03-31", "2026-04-25", revenue_yoy=-3.0),
            ],
        )

        assert fundamental_view_from_reports(reports, date(2026, 11, 1)).revenue_declining_periods == 1

    def test_gap_in_periods_stops_counting(self) -> None:
        """缺一期就不能说「连续 N 期下滑」——断档即停，不跨期累加。"""
        reports = parse_report_rows(
            "600188",
            [
                _row("2026-09-30", "2026-10-31", revenue_yoy=-1.0),
                # 少了 2026-06-30
                _row("2026-03-31", "2026-04-25", revenue_yoy=-3.0),
            ],
        )

        assert fundamental_view_from_reports(reports, date(2026, 11, 1)).revenue_declining_periods == 1

    def test_missing_field_stops_counting(self) -> None:
        reports = parse_report_rows(
            "600188",
            [
                _row("2026-09-30", "2026-10-31", roe_yoy=None),
                _row("2026-06-30", "2026-08-20", roe_yoy=-2.0),
            ],
        )

        assert fundamental_view_from_reports(reports, date(2026, 11, 1)).roe_declining_periods == 0

    def test_cashflow_needs_both_negative_and_worsening(self) -> None:
        """「恶化」= 为负且同比继续变差；只看为负会把季节性负现金流当恶化。"""
        improving = parse_report_rows(
            "600188", [_row("2026-09-30", "2026-10-31", ocf=-0.5, ocf_yoy=12.0)]
        )
        still_positive = parse_report_rows(
            "600188", [_row("2026-09-30", "2026-10-31", ocf=0.5, ocf_yoy=-20.0)]
        )
        unknown_yoy = parse_report_rows("600188", [_row("2026-09-30", "2026-10-31", ocf=-0.5)])

        assert fundamental_view_from_reports(improving, date(2026, 11, 1)).cashflow_worsening is False
        assert (
            fundamental_view_from_reports(still_positive, date(2026, 11, 1)).cashflow_worsening
            is False
        )
        # 没有同比数据时按「为负」算（不因为缺同比就放过一个负现金流的报告）
        assert fundamental_view_from_reports(unknown_yoy, date(2026, 11, 1)).cashflow_worsening is True

    def test_loss_and_cashflow_flags(self) -> None:
        reports = parse_report_rows(
            "600188", [_row("2026-09-30", "2026-10-31", net_profit=-1e7, ocf=-0.5)]
        )

        view = fundamental_view_from_reports(reports, date(2026, 11, 1))

        assert view.loss_making is True
        assert view.cashflow_worsening is True
        # 审计意见未接入：一律 False，不能假装「已核查且没问题」
        assert view.audit_opinion_abnormal is False


class TestStorage:
    def test_roundtrip_and_rewrite(self, tmp_path) -> None:
        store = AssistantStore(tmp_path / "s.db")
        store.create_all()
        reports = parse_report_rows(
            "600188",
            [_row("2026-09-30", "2026-10-31"), _row("2026-06-30", "2026-08-20")],
        )

        assert store.save_fundamental_reports("600188", reports) == 2
        loaded = store.load_fundamental_reports(["600188"])["600188"]

        assert [item.report_date for item in loaded] == [date(2026, 9, 30), date(2026, 6, 30)]
        assert loaded[0].notice_date == date(2026, 10, 31)
        assert loaded[0].revenue_yoy == pytest.approx(5.0)

        # 幂等：同一报告期再写一次只更新，不新增
        changed = parse_report_rows("600188", [_row("2026-09-30", "2026-11-05", revenue_yoy=9.0)])
        store.save_fundamental_reports("600188", changed)
        again = store.load_fundamental_reports(["600188"])["600188"]

        assert len(again) == 2
        assert again[0].notice_date == date(2026, 11, 5)
        assert again[0].revenue_yoy == pytest.approx(9.0)


class TestReplayWiring:
    def test_reports_reach_stock_view_by_notice_date(self, monkeypatch) -> None:
        """回放里财报也按公告日切片：公告前 available=False，公告后才变 True。"""
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
                stage_state=StageDebounceState(stage=Stage.S1, as_of=inp.expected_data_date),
            )

        monkeypatch.setattr(replay_module, "run_guard_chain", fake_chain)
        start = date(2026, 9, 1)
        days = [start + timedelta(days=i) for i in range(40)]
        bars = [
            SimpleNamespace(
                trade_date=day, open=20.0, high=20.5, low=19.5, close=20.0,
                volume=1_000_000.0, amount=0.0, adj_factor=1.0,
            )
            for day in days
        ]
        notice = date(2026, 9, 20)
        reports = parse_report_rows("600188", [_row("2026-06-30", notice.isoformat())])

        result = replay(
            days=days,
            bars_by_code={"600188": bars},
            config=ReplayConfig(min_bars=10, lookback_bars=40),
            fundamentals_by_code={"600188": reports},
        )

        seen = [(inp.expected_data_date, inp.view.fundamental.available) for inp in captured]
        before = [flag for day, flag in seen if day < notice]
        after = [flag for day, flag in seen if day > notice]
        assert before and not any(before), "公告日之前的财报不该可见"
        assert after and all(after), "公告日之后应该用上这份财报"
        assert result.stats["fundamental"]["codes_with_reports"] == 1

    def test_yield_percentile_reaches_view_after_min_obs(self, monkeypatch) -> None:
        captured: list = []

        def fake_chain(inp):
            captured.append(inp)
            return SimpleNamespace(
                result=SimpleNamespace(
                    code=inp.view.code, plans=(), key_levels=(),
                    stage_result=SimpleNamespace(stage=SimpleNamespace(value=Stage.S1.value)),
                    conclusion=SimpleNamespace(trend="上升", advice=Action.BUY),
                ),
                stage_state=StageDebounceState(stage=Stage.S1, as_of=inp.expected_data_date),
            )

        monkeypatch.setattr(replay_module, "run_guard_chain", fake_chain)
        start = date(2025, 1, 1)
        days = [start + timedelta(days=i) for i in range(300)]
        bars = [
            SimpleNamespace(
                trade_date=day, open=20.0, high=20.5, low=19.5, close=20.0,
                volume=1_000_000.0, amount=0.0, adj_factor=1.0,
            )
            for day in days
        ]
        from src.assistant.data.dividend import DividendEvent

        result = replay(
            days=days[-30:],
            bars_by_code={"600188": bars},
            config=ReplayConfig(min_bars=10, lookback_bars=300),
            dividend_events={
                "600188": [DividendEvent(code="600188", ex_date=days[-60], cash_per_share=0.8)]
            },
        )

        assert result.stats["valuation"]["codes_with_yield_pctile"] == 1
        last = captured[-1].view.valuation
        assert last.dividend_yield is not None
        assert last.dividend_yield_percentile is not None


def test_missing_column_is_added_on_create_all(tmp_path) -> None:
    """老库（建表时没有 ocf_yoy）再跑 create_all 要能自动补列，否则写入报 no column。"""
    from sqlalchemy import inspect, text

    store = AssistantStore(tmp_path / "s.db")
    store.create_all()
    with store._engine.begin() as conn:
        conn.execute(text("ALTER TABLE assistant_fundamental_report DROP COLUMN ocf_yoy"))
    assert "ocf_yoy" not in {c["name"] for c in inspect(store._engine).get_columns("assistant_fundamental_report")}

    store.create_all()

    assert "ocf_yoy" in {c["name"] for c in inspect(store._engine).get_columns("assistant_fundamental_report")}
