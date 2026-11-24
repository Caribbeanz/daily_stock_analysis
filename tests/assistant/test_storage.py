# -*- coding: utf-8 -*-
"""存储层测试（L1 幂等 / L3 append-only / L4 变更重放）。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from src.assistant.config import default_param_set
from src.assistant.data.storage import AssistantStore
from src.assistant.domain import Bar
from src.assistant.engine.guard_chain import GuardChainInput, run_guard_chain
from tests.assistant.helpers import make_portfolio, make_view, now, trending


@pytest.fixture()
def store(tmp_path) -> AssistantStore:
    s = AssistantStore(tmp_path / "assistant_test.db")
    s.create_all()
    return s


def _bars(closes: list[float], start: date = date(2026, 11, 2)) -> list[Bar]:
    out: list[Bar] = []
    day = start
    for close in closes:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        out.append(Bar(trade_date=day, open=close * 0.99, high=close * 1.02, low=close * 0.98, close=close, volume=1000.0))
        day += timedelta(days=1)
    return out


def test_tables_created_with_prefix(store: AssistantStore) -> None:
    from sqlalchemy import inspect

    names = set(inspect(store._engine).get_table_names())
    assert "assistant_snapshot" in names
    assert "assistant_stock_daily" in names
    assert "assistant_param_override" in names
    # 不得触碰主流程表结构
    assert "stock_daily" not in names


def test_save_bars_idempotent(store: AssistantStore) -> None:
    bars = _bars([10.0, 10.5, 11.0])
    assert store.save_bars("600000", bars) == 3
    assert store.bar_count("600000") == 3
    # 重写同一批（修正数据）不产生重复行
    fixed = _bars([10.0, 10.5, 11.5])
    store.save_bars("600000", fixed)
    assert store.bar_count("600000") == 3
    assert store.load_bars("600000")[-1].close == pytest.approx(11.5)


def test_load_bars_order_and_limit(store: AssistantStore) -> None:
    store.save_bars("600000", _bars([10.0, 11.0, 12.0, 13.0]))
    all_bars = store.load_bars("600000")
    assert [b.close for b in all_bars] == [10.0, 11.0, 12.0, 13.0]
    tail = store.load_bars("600000", limit=2)
    assert [b.close for b in tail] == [12.0, 13.0], "limit 取最后 N 根且仍按升序"


def test_weekly_monthly_aggregation(store: AssistantStore) -> None:
    """周线 OHLC 由日线聚合：开=首日开，收=末日收，高/低取极值。"""
    store.save_bars("600000", _bars([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0]))
    weeks, months = store.rebuild_weekly_monthly("600000")
    assert weeks >= 2 and months >= 1

    from sqlalchemy import select

    from src.assistant.data.models import StockWeekly

    with store.session() as sess:
        rows = sess.execute(select(StockWeekly).order_by(StockWeekly.week_end_date.asc())).scalars().all()
    first = rows[0]
    # 2026-11-02 是周一，首周含 5 根（10.0 → 14.0）
    assert first.open == pytest.approx(10.0 * 0.99)
    assert first.high == pytest.approx(14.0 * 1.02)
    assert first.low == pytest.approx(10.0 * 0.98)
    assert first.close == pytest.approx(14.0)
    assert first.volume == pytest.approx(5000.0)


def test_gap_days(store: AssistantStore) -> None:
    bars = _bars([10.0, 10.1, 10.2])
    store.save_bars("600000", bars)
    last = bars[-1].trade_date
    assert store.gap_days("600000", last) == 0
    assert store.gap_days("600000", last + timedelta(days=2)) == 2


def test_snapshot_append_only(store: AssistantStore) -> None:
    """同日两次生成得到两个 snapshot_id，历史不被覆盖（硬约束 2）。"""
    view = make_view(trending(150, 0.005), holding=1000)
    params = default_param_set()

    def _result():
        return run_guard_chain(
            GuardChainInput(view=view, portfolio=make_portfolio(), params=params, calc_version="c1", generated_at=now())
        ).result

    sid1 = store.write_snapshot(_result())
    sid2 = store.write_snapshot(_result())
    assert sid1 != sid2
    assert store.snapshot_exists(sid1) and store.snapshot_exists(sid2)
    latest = store.latest_snapshot()
    assert latest is not None and latest["snapshot_id"] in (sid1, sid2)


def test_param_change_replay_and_rollback(store: AssistantStore) -> None:
    ps = default_param_set()
    c1 = ps.make_change("advice_margin", 0.12, op_time=datetime(2026, 11, 24, 19, 0), effective_date=date(2026, 11, 25))
    store.record_param_change(c1)
    assert store.current_overrides() == {"advice_margin": 0.12}

    rollback = ps.make_change(
        "advice_margin", 0.10, op_time=datetime(2026, 11, 24, 20, 0), effective_date=date(2026, 11, 25), op_type="rollback"
    )
    store.record_param_change(rollback)
    assert store.current_overrides() == {}, "回滚后覆盖应清空"
    history = store.param_history("advice_margin")
    assert len(history) == 2, "回滚也是一条记录，不删除历史"


def test_override_values_keep_types(store: AssistantStore) -> None:
    """存储层不能把 ``False`` 解成 ``0``、把 ``0.12`` 解成字符串。"""
    ps = default_param_set()
    store.record_param_change(
        ps.make_change("probe_enabled", False, op_time=datetime(2026, 11, 24, 19, 0), effective_date=date(2026, 11, 25))
    )
    store.record_param_change(
        ps.make_change("advice_margin", 0.12, op_time=datetime(2026, 11, 24, 19, 1), effective_date=date(2026, 11, 25))
    )
    overrides = store.current_overrides()
    assert overrides["probe_enabled"] is False
    assert overrides["advice_margin"] == pytest.approx(0.12)


def test_watchlist_replay(store: AssistantStore) -> None:
    store.append_watchlist("600000", "add")
    store.append_watchlist("000001", "add")
    store.append_watchlist("600000", "remove")
    assert store.effective_watchlist() == {"000001"}


def test_holding_upsert(store: AssistantStore) -> None:
    store.upsert_holding("600000", 1000, 10.5, date(2026, 5, 1))
    store.upsert_holding("600000", 2000, 10.8)
    holdings = store.load_holdings()
    assert holdings["600000"]["shares"] == 2000
    assert holdings["600000"]["cost_basis"] == pytest.approx(10.8)


def test_llm_text_written_once(store: AssistantStore) -> None:
    view = make_view(trending(150, 0.005), holding=1000)
    result = run_guard_chain(
        GuardChainInput(view=view, portfolio=make_portfolio(), params=default_param_set(), calc_version="c1", generated_at=now())
    ).result
    sid = store.write_snapshot(result)
    store.save_llm_text(sid, "summary", "第一版", model="m1")
    store.save_llm_text(sid, "summary", "第二版", model="m2")

    from sqlalchemy import select

    from src.assistant.data.models import LlmTextCacheRow

    with store.session() as sess:
        rows = sess.execute(select(LlmTextCacheRow).where(LlmTextCacheRow.snapshot_id == sid)).scalars().all()
    assert len(rows) == 1
    assert rows[0].text == "第一版", "同快照同位置只生成一次，不覆盖"
