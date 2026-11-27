# -*- coding: utf-8 -*-
"""板块层接进回放（D-29）：板块日线 → 每日三档/rs_t，L1 读写，整年缺失补齐。"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from src.assistant import backtest_replay as module
from src.assistant.backtest_replay import (
    ReplayConfig,
    SectorSeries,
    replay,
    sector_view_for_day,
)
from src.assistant.data.fetcher import ReferenceFetcher
from src.assistant.data.storage import AssistantStore
from src.assistant.domain import Bar
from src.assistant.engine.stage import StageDebounceState
from src.assistant.enums import Action, Stage

DAY0 = date(2026, 3, 2)


def _bar(day: date, close: float, *, spread: float = 0.5, volume: float = 1_000_000.0) -> Bar:
    return Bar(
        trade_date=day,
        open=close,
        high=close + spread,
        low=close - spread,
        close=close,
        volume=volume,
    )


def _trend(days: int = 40, start: float = 1000.0, step: float = 1.0) -> list[Bar]:
    """逐日上涨（自然日跳着走，回放按 trade_date 切片，不需要真交易日历）。"""
    return [_bar(DAY0 + timedelta(days=i), start + step * i) for i in range(days)]


def _index_pct(bars: list[Bar], step_pct: float = 0.1) -> dict[date, float]:
    return {bar.trade_date: step_pct for bar in bars[1:]}


class TestSectorViewForDay:
    def test_strong_sector_from_series(self) -> None:
        """收盘在 MA 上、MA 上行 → 偏强；当日 rs_t 进历史末位。"""
        bars = _trend()
        series = SectorSeries.build("银行", "881155", bars)
        day = bars[-1].trade_date
        view = sector_view_for_day(series, day, _index_pct(bars, 0.05), depth=10)

        assert view is not None
        assert view.strength == "偏强"
        assert view.sector_code == "881155"
        assert view.rs_t is not None and view.rs_t > 0
        assert view.history_rs_t[-1] == pytest.approx(round(view.rs_t, 4))
        assert len(view.history_rs_t) == 10
        assert view.degraded is False

    def test_weak_sector_from_series(self) -> None:
        bars = [_bar(DAY0 + timedelta(days=i), 1000.0 - 1.0 * i) for i in range(40)]
        series = SectorSeries.build("煤炭开采加工", "881101", bars)
        day = bars[-1].trade_date
        view = sector_view_for_day(series, day, _index_pct(bars, -0.05), depth=5)

        assert view is not None
        assert view.strength == "偏弱"
        assert view.rs_t is not None and view.rs_t < 0
        assert len(view.history_rs_t) == 5

    def test_no_future_lookahead(self) -> None:
        """切片只到 T：T 之后的板块 K 线不得影响 T 日的档位。"""
        bars = _trend(days=40)
        series = SectorSeries.build("银行", "881155", bars)
        cut = bars[30].trade_date

        spaced = series.upto(cut)

        assert [bar.trade_date for bar in spaced] == [bar.trade_date for bar in bars[:31]]

    def test_stale_sector_bar_is_degraded_and_no_rs(self) -> None:
        """板块指数停在 T−1：不给 rs_t / 涨跌幅，标 degraded，但三档照旧（§5.10）。"""
        bars = _trend(days=40)
        series = SectorSeries.build("银行", "881155", bars)
        day = bars[-1].trade_date + timedelta(days=1)  # 当天没有板块 K 线

        view = sector_view_for_day(series, day, {day: 0.8}, depth=5)

        assert view is not None
        assert view.degraded is True
        assert view.rs_t is None
        assert view.pct_change == 0.0
        assert view.strength == "偏强"  # 档位按 T−1 序列判，不因缺当日而变中性

    def test_short_series_stays_neutral_and_degraded(self) -> None:
        bars = _trend(days=10)
        series = SectorSeries.build("银行", "881155", bars)
        view = sector_view_for_day(series, bars[-1].trade_date, {}, depth=5)

        assert view is not None
        assert view.strength == "中性"
        assert view.degraded is True
        assert view.rs_t is None

    def test_history_skips_days_without_index_pct(self) -> None:
        """任一侧缺该日就不取那个差，不插值、不补零。"""
        bars = _trend(days=40)
        series = SectorSeries.build("银行", "881155", bars)
        pct = _index_pct(bars, 0.05)
        for bar in bars[-5:-2]:
            pct.pop(bar.trade_date)
        day = bars[-1].trade_date

        view = sector_view_for_day(series, day, pct, depth=10)

        assert view is not None
        assert len(view.history_rs_t) == 7  # 10 天里去掉 3 天

    def test_empty_series_returns_none(self) -> None:
        series = SectorSeries.build("银行", "881155", ())
        assert sector_view_for_day(series, DAY0, {}) is None


class TestReplayWiring:
    @staticmethod
    def _stub_outcome(code: str) -> SimpleNamespace:
        return SimpleNamespace(
            result=SimpleNamespace(
                code=code,
                plans=(),
                key_levels=(),
                stage_result=SimpleNamespace(stage=SimpleNamespace(value=Stage.S1.value)),
                conclusion=SimpleNamespace(trend="上升", advice=Action.BUY),
            ),
            stage_state=StageDebounceState(stage=Stage.S1, as_of=DAY0),
        )

    def test_sector_view_reaches_guard_chain(self, monkeypatch) -> None:
        captured: list = []

        def fake_chain(inp):
            captured.append(inp)
            return self._stub_outcome(inp.view.code)

        monkeypatch.setattr(module, "run_guard_chain", fake_chain)
        days = [DAY0 + timedelta(days=i) for i in range(70)]
        stock = [_bar(day, 20.0) for day in days]

        result = replay(
            days=days,
            bars_by_code={"600000": stock, "600001": stock},
            config=ReplayConfig(min_bars=60, lookback_bars=70),
            market_by_day={},
            sector_by_code={"600000": "银行"},
            sector_bars_by_name={"银行": _trend(days=70, start=1000.0)},
            sector_code_by_name={"银行": "881155"},
            index_bars=[_bar(day, 3000.0) for day in days],
        )

        sectors = [inp.sector for inp in captured if inp.view.code == "600000"]
        assert sectors and all(item is not None for item in sectors)
        assert sectors[-1].sector_name == "银行"
        assert result.stats["sector"]["enabled"] is True
        assert result.stats["sector"]["mapped_codes"] == 1
        assert result.stats["sector"]["unmapped_codes"] == ["600001"]
        # 前 59 天不足 min_bars 被跳过；剩下每天只有映射了板块的那只有视图，
        # 未映射那只天天记缺失
        ran_days = len(days) - 59
        assert result.counters["sector_view_days"] == ran_days
        assert result.counters["sector_missing_code_days"] == ran_days

    def test_unmapped_code_is_marked_not_mapped(self, monkeypatch) -> None:
        captured: list = []

        def fake_chain(inp):
            captured.append(inp)
            return TestReplayWiring._stub_outcome(inp.view.code)

        monkeypatch.setattr(module, "run_guard_chain", fake_chain)
        days = [DAY0 + timedelta(days=i) for i in range(65)]
        stock = [_bar(day, 20.0) for day in days]

        replay(
            days=days,
            bars_by_code={"600001": stock},
            config=ReplayConfig(min_bars=60, lookback_bars=65),
            sector_by_code={"600000": "银行"},  # 本次跑的标的没被映射
            sector_bars_by_name={"银行": _trend(days=65)},
        )

        assert all(inp.sector_mapped is False for inp in captured)

    def test_without_sector_layer_keeps_old_behaviour(self, monkeypatch) -> None:
        captured: list = []

        def fake_chain(inp):
            captured.append(inp)
            return TestReplayWiring._stub_outcome(inp.view.code)

        monkeypatch.setattr(module, "run_guard_chain", fake_chain)
        days = [DAY0 + timedelta(days=i) for i in range(65)]

        result = replay(
            days=days,
            bars_by_code={"600000": [_bar(day, 20.0) for day in days]},
            config=ReplayConfig(min_bars=60, lookback_bars=65),
        )

        assert all(inp.sector is None for inp in captured)
        assert all(inp.sector_mapped is True for inp in captured)  # 未接入不冒「映射失败」
        assert result.stats["sector"]["enabled"] is False


class TestSectorStorage:
    def test_save_and_load_roundtrip(self, tmp_path) -> None:
        store = AssistantStore(tmp_path / "s.db")
        store.create_all()
        bars = _trend(days=5)

        assert store.save_sector_bars("881155", "银行", bars) == 5
        loaded = store.load_sector_bars("881155")

        assert [bar.trade_date for bar in loaded] == [bar.trade_date for bar in bars]
        assert loaded[0].close == pytest.approx(bars[0].close)
        assert store.sector_code_map() == {"银行": "881155"}
        assert list(store.load_sector_bars_by_name()) == ["银行"]

    def test_requires_sector_code(self, tmp_path) -> None:
        store = AssistantStore(tmp_path / "s.db")
        store.create_all()
        with pytest.raises(ValueError, match="没有代码"):
            store.save_sector_bars("", "银行", _trend(days=2))

    def test_rows_without_close_are_not_returned(self, tmp_path) -> None:
        """老行只有当日涨跌幅、没有收盘价：不塞 0 冒充收盘，按缺失处理。"""
        from sqlalchemy import text

        store = AssistantStore(tmp_path / "s.db")
        store.create_all()
        with store.session() as sess:
            sess.execute(
                text(
                    "INSERT INTO assistant_sector_daily "
                    "(sector_code, sector_name, trade_date, pct_change, source_tag) "
                    "VALUES (:sector_code, :sector_name, :trade_date, :pct_change, :source_tag)"
                ),
                {
                    "sector_code": "881155",
                    "sector_name": "银行",
                    "trade_date": DAY0.isoformat(),
                    "pct_change": 1.2,
                    "source_tag": "D1",
                },
            )

        assert store.load_sector_bars("881155") == []
        assert store.load_sector_bars_by_name() == {}

    def test_old_schema_gets_ohlc_columns(self, tmp_path) -> None:
        """老库建表时没有 OHLC 列：``create_all`` 必须追加，否则写入报 no column。"""
        db = tmp_path / "old.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE assistant_sector_daily ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, sector_code VARCHAR(16) NOT NULL,"
            "sector_name VARCHAR(64) NOT NULL DEFAULT '', trade_date DATE NOT NULL,"
            "pct_change FLOAT, up_count INTEGER, down_count INTEGER,"
            "lead_stock VARCHAR(32), source_tag VARCHAR(4) NOT NULL DEFAULT 'D1',"
            "CONSTRAINT uq_assistant_sector_daily UNIQUE (sector_code, trade_date))"
        )
        conn.commit()
        conn.close()

        store = AssistantStore(db)
        store.create_all()
        bars = _trend(days=3)
        assert store.save_sector_bars("881155", "银行", bars) == 3
        assert len(store.load_sector_bars("881155")) == 3


class TestSectorYearGapRepair:
    def test_missing_year_is_refetched(self, monkeypatch) -> None:
        """接口按年下载且失败会静默跳过：整体结果缺整年时，必须按年补一次。"""
        calls: list[tuple[date, date]] = []
        full = [_bar(date(2023, 12, 29), 1000.0), _bar(date(2024, 1, 2), 1001.0),
                _bar(date(2025, 1, 2), 1002.0)]

        def fake_year(_name, start, end):
            calls.append((start, end))
            if start.year == end.year == 2024:
                return tuple(bar for bar in full if bar.trade_date.year == 2024)
            if start.year == 2023 and end.year == 2025:
                return tuple(bar for bar in full if bar.trade_date.year != 2024)
            return ()

        monkeypatch.setattr(ReferenceFetcher, "_fetch_sector_index_year", staticmethod(fake_year))
        out = ReferenceFetcher().fetch_sector_index(
            "银行", date(2023, 12, 1), date(2025, 1, 31)
        )

        assert [bar.trade_date for bar in out] == [bar.trade_date for bar in full]
        assert any(s == date(2024, 1, 1) and e == date(2024, 12, 31) for s, e in calls)
