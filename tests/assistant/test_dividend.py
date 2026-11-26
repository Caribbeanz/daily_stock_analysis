# -*- coding: utf-8 -*-
"""分红送转与「含分红」价格序列：解析口径、复权因子、回放接线。

这一层的三条硬要求，每条都有对应用例：

1. **解析零歧义**：接口给「每 10 股」，``BONUS_IT_RATIO`` 是**送转合计**、``IT_RATIO`` 只是转增，
   直接相加会把纯转增算成双倍；
2. **折算只乘因子、不改相对关系**：跨除权日的涨幅要等于「含分红总回报」；
3. **不猜**：缺前一交易日 K 线的除权事件一律跳过并计数，不拿别的价格顶替。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.assistant.data.dividend import (
    DividendEvent,
    apply_total_return,
    parse_dividend_rows,
)
from src.assistant.data.storage import AssistantStore
from src.assistant.domain import Bar

DAY0 = date(2026, 1, 5)


def _bar(day: date, close: float, *, spread: float = 0.01) -> Bar:
    return Bar(
        trade_date=day,
        open=close * (1 - spread / 2),
        high=close * (1 + spread),
        low=close * (1 - spread),
        close=close,
        volume=1000.0,
    )


def _series(closes: list[float], start: date = DAY0) -> list[Bar]:
    return [_bar(start + timedelta(days=i), c) for i, c in enumerate(closes)]


def _row(**overrides):
    base = {
        "SECURITY_CODE": "600211",
        "ASSIGN_PROGRESS": "实施分配",
        "EX_DIVIDEND_DATE": "2024-04-30 00:00:00",
        "EQUITY_RECORD_DATE": "2024-04-29 00:00:00",
        "PRETAX_BONUS_RMB": 7.37,
        "BONUS_IT_RATIO": 3.0,
        "IT_RATIO": 3.0,
        "IMPL_PLAN_PROFILE": "10转3股派7.37元(含税,扣税后6.633元)",
    }
    base.update(overrides)
    return base


class TestParsing:
    def test_per_ten_becomes_per_share(self) -> None:
        event = parse_dividend_rows("600211", [_row()])[0]

        assert event.ex_date == date(2024, 4, 30)
        assert event.record_date == date(2024, 4, 29)
        assert event.cash_per_share == pytest.approx(0.737)

    def test_transfer_only_not_double_counted(self) -> None:
        """``10转3`` 的接口会把两个字段都填 3.0：送股必须是 0，因子只能是 1.3。"""
        event = parse_dividend_rows("600211", [_row()])[0]

        assert event.bonus_per_share == 0.0
        assert event.transfer_per_share == pytest.approx(0.3)
        assert event.share_factor == pytest.approx(1.3)

    def test_bonus_and_transfer_split(self) -> None:
        """``10送2.4615转2.4615``：合计 4.923、转增 2.4615，因子 1.4923。"""
        event = parse_dividend_rows(
            "000423",
            [_row(PRETAX_BONUS_RMB=0.0, BONUS_IT_RATIO=4.923, IT_RATIO=2.4615,
                  IMPL_PLAN_PROFILE="10送2.4615转2.4615")],
        )[0]

        assert event.bonus_per_share == pytest.approx(0.24615)
        assert event.transfer_per_share == pytest.approx(0.24615)
        assert event.share_factor == pytest.approx(1.4923)

    def test_bonus_only_has_no_transfer(self) -> None:
        event = parse_dividend_rows(
            "600188",
            [_row(PRETAX_BONUS_RMB=14.9, BONUS_IT_RATIO=3.0, IT_RATIO=None,
                  IMPL_PLAN_PROFILE="10送3股派14.90元")],
        )[0]

        assert event.bonus_per_share == pytest.approx(0.3)
        assert event.transfer_per_share == 0.0

    def test_skips_unimplemented_and_undated(self) -> None:
        rows = [
            _row(ASSIGN_PROGRESS="股东大会通过"),
            _row(EX_DIVIDEND_DATE=None),
            _row(PRETAX_BONUS_RMB=0.0, BONUS_IT_RATIO=0.0, IT_RATIO=0.0),
        ]

        assert parse_dividend_rows("600211", rows) == []

    def test_sorted_by_ex_date(self) -> None:
        rows = [
            _row(EX_DIVIDEND_DATE="2026-05-25 00:00:00"),
            _row(EX_DIVIDEND_DATE="2025-04-01 00:00:00"),
        ]

        events = parse_dividend_rows("600211", rows)

        assert [e.ex_date for e in events] == [date(2025, 4, 1), date(2026, 5, 25)]


class TestAdjust:
    def test_cash_dividend_removes_the_gap(self) -> None:
        """除权日理论价 = P - c：折算后跨日涨幅应为 1（税前全额再投）。"""
        bars = _series([10.0, 10.0, 10.0, 9.0, 9.0])
        event = DividendEvent(code="600000", ex_date=bars[3].trade_date, cash_per_share=1.0)

        out = apply_total_return(bars, [event])

        assert out.applied == 1 and out.skipped == 0
        assert out.bars[3].close / out.bars[2].close == pytest.approx(1.0, abs=1e-9)
        assert out.bars[0].adj_factor == pytest.approx(0.9)
        assert out.bars[3].adj_factor == pytest.approx(1.0)

    def test_tax_reduces_total_return(self) -> None:
        bars = _series([10.0, 10.0, 10.0, 9.0, 9.0])
        event = DividendEvent(code="600000", ex_date=bars[3].trade_date, cash_per_share=1.0)

        out = apply_total_return(bars, [event], tax_rate=0.10)

        # 财富从 10 变成 9 + 1*(1-10%) = 9.9
        assert out.bars[3].close / out.bars[2].close == pytest.approx(0.99, abs=1e-9)

    def test_split_is_continuous(self) -> None:
        """10 送 10：价格减半、股数翻倍，含分红序列不该出现 -50%。"""
        bars = _series([10.0, 10.0, 10.0, 5.0, 5.0])
        event = DividendEvent(code="600000", ex_date=bars[3].trade_date, bonus_per_share=1.0)

        out = apply_total_return(bars, [event])

        assert out.bars[3].close / out.bars[2].close == pytest.approx(1.0, abs=1e-9)

    def test_multiple_events_compound(self) -> None:
        bars = _series([10.0, 10.0, 10.0, 9.0, 9.0, 9.0, 8.0, 8.0])
        events = [
            DividendEvent(code="x", ex_date=bars[3].trade_date, cash_per_share=1.0),
            DividendEvent(code="x", ex_date=bars[6].trade_date, cash_per_share=1.0),
        ]

        out = apply_total_return(bars, events)

        assert out.applied == 2
        assert out.cash_per_share == pytest.approx(2.0)
        assert out.bars[3].close / out.bars[2].close == pytest.approx(1.0, abs=1e-9)
        assert out.bars[6].close / out.bars[5].close == pytest.approx(1.0, abs=1e-9)

    def test_event_before_history_is_not_a_failure(self) -> None:
        bars = _series([10.0, 10.0])
        event = DividendEvent(code="x", ex_date=date(2025, 1, 1), cash_per_share=1.0)

        out = apply_total_return(bars, [event])

        assert out.before_history == 1
        assert out.applied == 0 and out.skipped == 0
        assert [bar.close for bar in out.bars] == [bar.close for bar in bars]

    def test_ex_date_without_prior_bar_is_skipped_not_guessed(self) -> None:
        """历史正好从除权日开始：拿不到除权前收盘价，就不折算（不猜）。"""
        bars = _series([9.0, 9.0])
        event = DividendEvent(code="x", ex_date=bars[0].trade_date, cash_per_share=1.0)

        out = apply_total_return(bars, [event])

        assert out.skipped == 1
        assert out.applied == 0
        assert [bar.close for bar in out.bars] == [bar.close for bar in bars]

    def test_stale_previous_bar_is_used(self) -> None:
        """除权日之前最近一根 K 线就够（停牌导致中间空几天也不影响折算）。"""
        bars = _series([10.0, 10.0])
        event = DividendEvent(code="x", ex_date=bars[0].trade_date + timedelta(days=5),
                              cash_per_share=1.0)

        out = apply_total_return(bars, [event])

        assert out.applied == 1
        assert out.skipped == 0

    def test_scaled_bars_stay_valid(self) -> None:
        """缩放后的 high/low 必须仍然包住 close（浮点误差不能把 K 线做成非法）。"""
        bars = _series([3.9492887358271975, 4.0, 4.1])
        event = DividendEvent(code="x", ex_date=bars[2].trade_date, cash_per_share=0.123456789)

        out = apply_total_return(bars, [event])

        for bar in out.bars:
            assert bar.low <= bar.close <= bar.high

    def test_empty_events_is_identity(self) -> None:
        bars = _series([10.0, 9.5])

        out = apply_total_return(bars, [])

        assert [bar.close for bar in out.bars] == [10.0, 9.5]

    def test_rejects_bad_tax_rate(self) -> None:
        with pytest.raises(ValueError):
            apply_total_return(_series([10.0]), [], tax_rate=1.0)


class TestStorage:
    def test_roundtrip(self, tmp_path) -> None:
        store = AssistantStore(tmp_path / "d.db")
        store.create_all()
        events = [
            DividendEvent(code="600211", ex_date=date(2024, 4, 30), cash_per_share=0.737,
                          transfer_per_share=0.3, plan="10转3股派7.37元",
                          record_date=date(2024, 4, 29)),
            DividendEvent(code="600211", ex_date=date(2025, 5, 20), cash_per_share=0.5),
        ]

        assert store.save_dividend_events("600211", events) == 2
        loaded = store.load_dividend_events(["600211"])

        assert [e.ex_date for e in loaded["600211"]] == [date(2024, 4, 30), date(2025, 5, 20)]
        assert loaded["600211"][0].cash_per_share == pytest.approx(0.737)
        assert loaded["600211"][0].transfer_per_share == pytest.approx(0.3)
        assert loaded["600211"][0].record_date == date(2024, 4, 29)

    def test_idempotent_rewrite(self, tmp_path) -> None:
        store = AssistantStore(tmp_path / "d.db")
        store.create_all()
        store.save_dividend_events("600211", [DividendEvent(code="600211", ex_date=date(2024, 4, 30),
                                                            cash_per_share=0.7)])

        store.save_dividend_events("600211", [DividendEvent(code="600211", ex_date=date(2024, 4, 30),
                                                            cash_per_share=0.8)])

        loaded = store.load_dividend_events(["600211"])
        assert len(loaded["600211"]) == 1
        assert loaded["600211"][0].cash_per_share == pytest.approx(0.8)

    def test_old_schema_gets_columns_added(self, tmp_path) -> None:
        """早前预留的老表只有 per_share：``create_all`` 不补列，必须自己迁移。"""
        import sqlite3

        db = tmp_path / "old.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE assistant_dividend_event ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, code VARCHAR(16) NOT NULL,"
            "ex_date DATE NOT NULL, record_date DATE, payout_date DATE,"
            "per_share FLOAT, source_tag VARCHAR(4) NOT NULL DEFAULT 'D1',"
            "CONSTRAINT uq_assistant_dividend_event UNIQUE (code, ex_date))"
        )
        conn.commit()
        conn.close()

        store = AssistantStore(db)
        store.create_all()
        store.save_dividend_events(
            "600211",
            [DividendEvent(code="600211", ex_date=date(2024, 4, 30), cash_per_share=0.737)],
        )

        loaded = store.load_dividend_events(["600211"])
        assert loaded["600211"][0].cash_per_share == pytest.approx(0.737)
