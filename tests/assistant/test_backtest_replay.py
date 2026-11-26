# -*- coding: utf-8 -*-
"""历史计划回放驱动（D-24）：切片、防抖累积、去重、断点续跑、规模护栏。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.assistant import backtest_replay as module
from src.assistant.backtest import PlanRound
from src.assistant.backtest_replay import (
    ReplayConfig,
    ReplayState,
    _drop_overlaps,
    _exit_levels,
    load_state,
    market_views_from_index,
    replay,
    save_state,
)
from src.assistant.config.overrides import default_param_set
from src.assistant.domain import Bar, KeyLevel
from src.assistant.enums import Action, Stage, TriggerType
from src.assistant.engine.stage import StageDebounceState

DAY0 = date(2026, 11, 2)


def _bar(day: date, *, open_: float, high: float, low: float, close: float) -> Bar:
    return Bar(trade_date=day, open=open_, high=high, low=low, close=close, volume=1_000_000.0)


def _series(days: int = 8, start: float = 10.5) -> list[Bar]:
    """平盘偏弱的日线，便于手工指定触发日的 low/high。"""
    out: list[Bar] = []
    for i in range(days):
        day = DAY0 + timedelta(days=i)
        base = start - 0.02 * i
        out.append(_bar(day, open_=base, high=base + 0.1, low=base - 0.1, close=base))
    return out


def _days(bars: list[Bar]) -> list[date]:
    """可用交易日：首根 K 线不足以判（min_bars=2），从第二根开始。"""
    return [bar.trade_date for bar in bars[1:]]


def _branch(price: float = 10.0, valid_days: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        branch_id="b1",
        scenario="回踩买入",
        trigger_type=TriggerType.PRICE_LTE,
        trigger_price=price,
        pct_change=None,
        action=Action.BUY,
        shares=100,
        valid_days=valid_days,
    )


def _outcome(code: str = "600000", *, plans=None, trend: str = "上升") -> SimpleNamespace:
    return SimpleNamespace(
        result=SimpleNamespace(
            code=code,
            plans=tuple(plans or ()),
            key_levels=(KeyLevel(price=9.4, kind="support", source="重要均线"),),
            stage_result=SimpleNamespace(stage=SimpleNamespace(value=Stage.S1.value)),
            conclusion=SimpleNamespace(trend=trend, advice=Action.BUY),
        ),
        stage_state=StageDebounceState(stage=Stage.S1, as_of=DAY0),
    )


def _patch_chain(monkeypatch, fn) -> None:
    monkeypatch.setattr(module, "run_guard_chain", fn)


class TestGuards:
    def test_rejects_too_short_lookback(self) -> None:
        with pytest.raises(ValueError):
            ReplayConfig(lookback_bars=10, min_bars=60)

    def test_replay_requires_bars(self) -> None:
        with pytest.raises(ValueError):
            replay(days=[DAY0], bars_by_code={}, config=_config())


class TestSlicingAndSkip:
    def test_stale_day_is_skipped_not_guessed(self, monkeypatch) -> None:
        """T 日 K 线未发布 → 该标的当日不跑，绝不拿旧结论冒充当日。"""
        calls: list[date] = []

        def fake_chain(inp):  # noqa: ANN001, ANN202
            calls.append(inp.expected_data_date)
            return _outcome()

        _patch_chain(monkeypatch, fake_chain)
        bars = _series(8)[:5]
        days = _days(bars) + [bars[-1].trade_date + timedelta(days=1)]

        result = replay(days=days, bars_by_code={"600000": bars}, config=_config())

        assert len(calls) == 4
        assert result.counters["skipped_stale_bars"] == 1

    def test_insufficient_bars_skipped(self, monkeypatch) -> None:
        _patch_chain(monkeypatch, lambda inp: _outcome())
        bars = _series(8)

        result = replay(
            days=[bar.trade_date for bar in bars],
            bars_by_code={"600000": bars},
            config=_config(min_bars=10),
        )

        assert result.days == 0, "一根都跑不了的日子不算已回放"
        assert result.counters["skipped_insufficient_bars"] == 8
        assert result.counters["skipped_days"] == 8

    def test_market_missing_counted(self, monkeypatch) -> None:
        _patch_chain(monkeypatch, lambda inp: _outcome())
        bars = _series(8)

        result = replay(days=_days(bars), bars_by_code={"600000": bars}, config=_config())

        assert result.days == 7
        assert result.counters["market_missing_days"] == 7

    def test_guard_runs_use_growing_calendar(self, monkeypatch) -> None:
        """防前视：传给守卫链的交易日只能到当天，不能提前把未来日历塞进去。"""
        seen: list[int] = []

        def fake_chain(inp):  # noqa: ANN001, ANN202
            seen.append(len(inp.trading_days or ()))
            return _outcome()

        _patch_chain(monkeypatch, fake_chain)
        bars = _series(5)

        replay(days=_days(bars), bars_by_code={"600000": bars}, config=_config())

        assert seen == [1, 2, 3, 4], "日历长度跟着日子增长，不能一次给全量"

    def test_generated_at_is_after_close(self, monkeypatch) -> None:
        stamps: list[datetime] = []

        def fake_chain(inp):  # noqa: ANN001, ANN202
            stamps.append(inp.generated_at)
            return _outcome()

        _patch_chain(monkeypatch, fake_chain)
        bars = _series(4)

        replay(days=_days(bars), bars_by_code={"600000": bars}, config=_config())

        assert [stamp.hour for stamp in stamps] == [20, 20, 20]


class TestRounds:
    def test_trigger_and_stop_loss_produce_closed_round(self, monkeypatch) -> None:
        bars = _series(8)
        # 计划在 bars[2] 当日生成（T+1 才生效）：bars[3] 回踩触发，bars[4] 跌破支撑 9.4
        bars[3] = _bar(bars[3].trade_date, open_=10.3, high=10.4, low=9.9, close=10.2)
        bars[4] = _bar(bars[4].trade_date, open_=9.3, high=9.5, low=9.0, close=9.1)
        plan_day = bars[2].trade_date

        def fake_chain(inp):  # noqa: ANN001, ANN202
            if inp.expected_data_date == plan_day:
                return _outcome(plans=[_branch()])
            return _outcome()

        _patch_chain(monkeypatch, fake_chain)

        result = replay(days=_days(bars), bars_by_code={"600000": bars}, config=_config())

        closed = [row for row in result.rounds if row.closed]
        assert len(closed) == 1
        round_ = closed[0]
        assert round_.entry_date == bars[3].trade_date, "计划次日才可触发"
        assert round_.entry_price == 10.0  # 未跳空，按触发价成交
        assert round_.exit_date == bars[4].trade_date
        assert round_.exit_reason == "stop_loss"
        assert round_.exit_price == 9.4  # 出场价取当日关键支撑
        assert round_.net_pnl < 0
        assert result.stats["closed"] == 1
        assert result.stats["sample_sufficient"] is False  # 样本不足不报胜率
        assert result.stats["win_rate"] is None
        assert result.plans == 1
        assert result.stats["triggered"] == 1
        assert result.stats["open_positions"] == 0
        assert result.stats["untriggered"] == 0

    def test_exit_levels_pick_nearest_support_and_resistance(self) -> None:
        result = SimpleNamespace(
            key_levels=(
                KeyLevel(price=8.0, kind="support", source="前低"),
                KeyLevel(price=9.5, kind="support", source="均线"),
                KeyLevel(price=11.0, kind="resistance", source="前高"),
                KeyLevel(price=13.0, kind="resistance", source="箱体上沿"),
            )
        )

        stop_loss, take_profit = _exit_levels(result, 10.0)

        assert stop_loss == 9.5
        assert take_profit == 11.0

    def test_overlap_is_not_double_counted(self, monkeypatch) -> None:
        """同一标的已有未平回合时，后续触发不再建仓（组合层约束的保守近似）。"""
        bars = _series(8)
        for idx in (3, 4):
            bars[idx] = _bar(bars[idx].trade_date, open_=10.3, high=10.4, low=9.9, close=10.2)
        plan_days = {bars[2].trade_date, bars[3].trade_date}

        def fake_chain(inp):  # noqa: ANN001, ANN202
            if inp.expected_data_date in plan_days:
                return _outcome(plans=[_branch(valid_days=3)])
            return _outcome()

        _patch_chain(monkeypatch, fake_chain)

        result = replay(days=_days(bars), bars_by_code={"600000": bars}, config=_config())

        assert result.counters["overlap_skipped"] == 1
        entries = [row.entry_date for row in result.rounds if row.entry_date]
        assert entries == [bars[3].trade_date]


class TestStateAndCheckpoint:
    def test_checkpoint_roundtrip(self, tmp_path) -> None:
        state = ReplayState(
            last_day=DAY0,
            days_done=1,
            rounds=[PlanRound(code="600000", branch_id="b1", closed=True, entry_date=DAY0)],
            stages={"600000": StageDebounceState(stage=Stage.S1, as_of=DAY0)},
            previous={"600000": ("上升", "买入")},
            counters={"guard_runs": 1},
        )
        path = tmp_path / "checkpoint.json"

        save_state(state, path)
        loaded = load_state(path)

        assert loaded is not None
        assert loaded.last_day == DAY0
        assert loaded.rounds[0].code == "600000"
        assert loaded.stages["600000"].stage is Stage.S1
        assert loaded.previous["600000"] == ("上升", "买入")
        assert loaded.counters["guard_runs"] == 1

    def test_broken_checkpoint_is_ignored(self, tmp_path) -> None:
        path = tmp_path / "checkpoint.json"
        path.write_text("{not json", encoding="utf-8")

        assert load_state(path) is None

    def test_missing_checkpoint_is_none(self, tmp_path) -> None:
        assert load_state(tmp_path / "nope.json") is None

    def test_resume_does_not_reprocess_days(self, monkeypatch, tmp_path) -> None:
        _patch_chain(monkeypatch, lambda inp: _outcome())
        bars = _series(8)
        days = _days(bars)
        path = tmp_path / "checkpoint.json"
        cfg = _config()

        first = replay(
            days=days,
            bars_by_code={"600000": bars},
            config=cfg,
            checkpoint_path=path,
            state=ReplayState(),
        )
        assert first.days == 7

        loaded = load_state(path)
        assert loaded is not None
        again = replay(
            days=days,
            bars_by_code={"600000": bars},
            config=cfg,
            checkpoint_path=path,
            state=loaded,
        )

        assert again.days == 0, "已回放过的交易日不能重复跑"
        assert again.counters["guard_runs"] == 7, "续跑不新增回放次数"
        assert loaded.days_done == 7

    def test_max_days_truncates_and_flags(self, monkeypatch) -> None:
        _patch_chain(monkeypatch, lambda inp: _outcome())
        bars = _series(8)

        result = replay(
            days=_days(bars),
            bars_by_code={"600000": bars},
            config=_config(max_days=3),
        )

        assert result.days == 3
        assert result.truncated is True

    def test_checkpoint_written_periodically(self, monkeypatch, tmp_path) -> None:
        _patch_chain(monkeypatch, lambda inp: _outcome())
        bars = _series(8)
        path = tmp_path / "checkpoint.json"

        replay(
            days=_days(bars),
            bars_by_code={"600000": bars},
            config=_config(checkpoint_every=2),
            checkpoint_path=path,
        )

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["last_day"] == bars[-1].trade_date.isoformat()
        assert payload["days_done"] == 7

    def test_stage_state_carries_across_days(self, monkeypatch) -> None:
        """跨日防抖不能每天重置：第二天必须看到第一天的 stage。"""
        seen: list[str | None] = []

        def fake_chain(inp):  # noqa: ANN001, ANN202
            seen.append(inp.stage_state.stage.value if inp.stage_state.stage else None)
            return _outcome()

        _patch_chain(monkeypatch, fake_chain)
        bars = _series(4)

        replay(days=_days(bars), bars_by_code={"600000": bars}, config=_config())

        assert seen[0] is None
        assert seen[1:] == ["S1", "S1"]


class TestIntegration:
    def test_real_guard_chain_runs_over_window(self) -> None:
        """接真守卫链跑一段，确认逐日切片不会因数据不足/参数缺失抛错。"""
        from tests.assistant.helpers import make_bars, trending

        bars = make_bars(trending(200))
        days = [bar.trade_date for bar in bars][-6:]

        result = replay(
            days=days,
            bars_by_code={"600000": bars},
            config=ReplayConfig(min_bars=60, lookback_bars=120, progress_every=0),
        )

        assert result.days == 6
        assert result.stats["calc_version"].startswith("cv1-")
        assert "by_stage" in result.stats and "by_code" in result.stats
        assert result.counters["guard_runs"] == 6
        assert any("组合层资金曲线" in item for item in result.stats["not_modelled"])

    def test_replay_result_serializes(self) -> None:
        from tests.assistant.helpers import make_bars, trending

        bars = make_bars(trending(200))
        result = replay(
            days=[bar.trade_date for bar in bars][-3:],
            bars_by_code={"600000": bars},
            config=ReplayConfig(min_bars=60, lookback_bars=120, progress_every=0),
        )

        payload = json.loads(json.dumps(result.as_dict(), ensure_ascii=False))
        assert payload["days"] == 3
        assert isinstance(payload["stats"], dict)


def _config(**kwargs) -> ReplayConfig:
    base = {
        "lookback_bars": 60,
        "min_bars": 2,
        "params": default_param_set(),
        "progress_every": 0,
    }
    base.update(kwargs)
    return ReplayConfig(**base)


def test_drop_overlaps_keeps_first_entry() -> None:
    first = PlanRound(
        code="600000", branch_id="a", entry_date=DAY0, exit_date=DAY0 + timedelta(days=3), closed=True
    )
    second = PlanRound(
        code="600000",
        branch_id="b",
        entry_date=DAY0 + timedelta(days=1),
        exit_date=DAY0 + timedelta(days=5),
        closed=True,
    )
    third = PlanRound(
        code="600000",
        branch_id="c",
        entry_date=DAY0 + timedelta(days=6),
        exit_date=DAY0 + timedelta(days=8),
        closed=True,
    )

    accepted, dropped = _drop_overlaps([second, third], [first])

    assert [row.branch_id for row in accepted] == ["c"]
    assert dropped == 1


def test_overlap_is_scoped_per_code() -> None:
    """去重必须按标的分组：A 的持仓不能挡住 B 的入场（早年漏了这个过滤，
    10 只 × 460 天只剩 10 笔成交）。"""
    open_a = PlanRound(code="600000", branch_id="a", entry_date=DAY0, closed=False)
    new_b = PlanRound(
        code="000001",
        branch_id="b",
        entry_date=DAY0 + timedelta(days=1),
        exit_date=DAY0 + timedelta(days=4),
        closed=True,
    )

    accepted, dropped = _drop_overlaps([new_b], [open_a])

    assert dropped == 0
    assert accepted == [new_b]


def test_same_code_open_position_blocks_reentry() -> None:
    open_a = PlanRound(code="600000", branch_id="a", entry_date=DAY0, closed=False)
    later_a = PlanRound(code="600000", branch_id="b", entry_date=DAY0 + timedelta(days=2), closed=True)

    accepted, dropped = _drop_overlaps([later_a], [open_a])

    assert dropped == 1
    assert accepted == []


def test_drop_overlaps_keeps_untriggered_rows() -> None:
    """未触发（entry=None）不代表持仓，不能被去重逻辑吃掉。"""
    untriggered = PlanRound(code="600000", branch_id="x", closed=False, note="有效期内未触发")

    accepted, dropped = _drop_overlaps([untriggered], [])

    assert [row.branch_id for row in accepted] == ["x"]
    assert dropped == 0


# ---- 大盘注入（D-30）------------------------------------------------------
def _index_series(count: int, *, start: float = 3000.0, step: float = 6.0,
                  start_day: date = date(2026, 1, 5)) -> list[Bar]:
    """构造一段单调上行的指数日线（MA20 在上方且向上 → 应为偏强档）。"""
    out: list[Bar] = []
    for i in range(count):
        day = start_day + timedelta(days=i)
        close = start + step * i
        out.append(_bar(day, open_=close - 2, high=close + 3, low=close - 4, close=close))
    return out


class TestMarketViews:
    def test_regime_computed_for_each_day(self) -> None:
        bars = _index_series(60)
        days = [bar.trade_date for bar in bars[-3:]]

        views = market_views_from_index(bars, days, symbol="sh000001")

        assert set(views) == set(days)
        first = views[days[0]]
        assert first.index_code == "sh000001"
        assert first.regime, "有效档必须给出档位名，不能留空"
        assert first.regime_degraded is False
        assert first.pct_change > 0

    def test_width_stays_unavailable(self) -> None:
        """涨跌家数回放拿不到：宽度必须留空，别用 0 或 1 假装有数据。"""
        bars = _index_series(60)

        views = market_views_from_index(bars, [bars[-1].trade_date])

        view = views[bars[-1].trade_date]
        assert view.advance_ratio is None
        assert view.width_degraded is True

    def test_no_future_bars_leak(self) -> None:
        """T 只用 ≤T 的日线：把 T 之后的暴跌接在后面，T 的档位不能变。"""
        bars = _index_series(60)
        day = bars[40].trade_date
        crash = [
            _bar(day + timedelta(days=i + 1), open_=1000.0, high=1000.0, low=900.0, close=900.0)
            for i in range(19)
        ]

        plain = market_views_from_index(bars[:41], [day])
        with_future = market_views_from_index(bars[:41] + crash, [day])

        assert plain[day] == with_future[day]

    def test_insufficient_bars_is_omitted_not_guessed(self) -> None:
        bars = _index_series(10)

        views = market_views_from_index(bars, [bars[-1].trade_date], min_bars=30)

        assert views == {}, "根数不够就不放进去，由回放层记成大盘缺失"

    def test_day_without_index_bar_is_omitted(self) -> None:
        bars = _index_series(60)
        missing = bars[-1].trade_date + timedelta(days=5)

        views = market_views_from_index(bars, [missing])

        assert missing not in views
