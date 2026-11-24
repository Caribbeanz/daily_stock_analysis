# -*- coding: utf-8 -*-
"""取数层测试（多源降级 / 空响应 / 熔断 / 归一化 / 回补幂等），全部离线。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

import pytest

from src.assistant.data.fetcher import (
    CircuitBreaker,
    DailyFetcher,
    FetchError,
    ReferenceFetcher,
    normalize_rows,
    post_adjusted_closes,
    to_index_symbol,
    to_market_symbol,
    to_plain_code,
)
from src.assistant.data.storage import AssistantStore
from src.assistant.domain import Bar
from src.assistant.enums import SourceTag

START = date(2026, 11, 2)
END = date(2026, 11, 24)


def rows_of(closes: Sequence[float], start: date = START) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    day = start
    for close in closes:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        out.append({"date": day, "open": close, "high": close, "low": close, "close": close, "volume": 1000})
        day += timedelta(days=1)
    return out


class FakeSource:
    """可编程假数据源：记录调用次数，可返回数据 / 空表 / 抛错。"""

    def __init__(self, name: str, tag: SourceTag, rows=None, *, error: Exception | None = None,
                 fail_times: int = 0) -> None:
        self.name = name
        self.tag = tag
        self._rows = rows or []
        self._error = error
        self._fail_times = fail_times
        self.calls = 0

    def fetch(self, code: str, start: date, end: date) -> Sequence[Mapping[str, Any]]:
        self.calls += 1
        if self._fail_times > 0:
            self._fail_times -= 1
            raise ConnectionError("模拟瞬时失败")
        if self._error is not None:
            raise self._error
        return self._rows


def fetcher(sources, store=None, clock=None, **kwargs) -> DailyFetcher:
    return DailyFetcher(
        sources,
        store=store,
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=60, clock=clock),
        backoff_seconds=0.0,
        sleeper=lambda _s: None,
        clock=clock,
        **kwargs,
    )


# ---- 归一化 ----------------------------------------------------------------
def test_normalize_handles_chinese_columns_and_units() -> None:
    rows = [
        {"日期": "2026-11-20", "开盘": "10.00", "最高": "10.50", "最低": "9.80", "收盘": "10.20", "成交量": "1,234,500", "成交额": "1.2e7"},
        {"日期": "2026-11-21", "开盘": 10.2, "最高": 10.3, "最低": 10.0, "收盘": 10.25, "成交量": 900},
    ]
    bars, dropped = normalize_rows(rows)
    assert dropped == 0
    assert [b.trade_date for b in bars] == [date(2026, 11, 20), date(2026, 11, 21)]
    assert bars[0].volume == pytest.approx(1_234_500)
    assert bars[0].amount == pytest.approx(1.2e7)


def test_normalize_clamps_inconsistent_high_low() -> None:
    """高低于开收不自洽时夹紧，不丢行也不编造方向。"""
    rows = [{"date": "2026-11-20", "open": 10.0, "high": 9.5, "low": 10.6, "close": 10.2, "volume": 100}]
    bars, dropped = normalize_rows(rows)
    assert dropped == 0
    assert bars[0].high == pytest.approx(10.2)
    assert bars[0].low == pytest.approx(10.0)


def test_normalize_drops_invalid_and_dedupes() -> None:
    rows = [
        {"date": "2026-11-20", "close": 10.0},
        {"date": "", "close": 10.0},
        {"date": "2026-11-21", "close": 0},
        {"date": "2026-11-22", "close": 11.0},
        {"date": "2026-11-22", "close": 11.5},  # 同日修正值
    ]
    bars, dropped = normalize_rows(rows)
    assert dropped == 2
    assert len(bars) == 2
    assert bars[-1].close == pytest.approx(11.5)


def test_normalize_accepts_pandas_like_timestamp() -> None:
    rows = [{"date": datetime(2026, 11, 20, 15, 0), "close": 10.0}]
    bars, _ = normalize_rows(rows)
    assert bars[0].trade_date == date(2026, 11, 20)


# ---- 代码规范化 ------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [("600000", "600000"), ("sh600000", "600000"), ("600000.SH", "600000")])
def test_to_plain_code(raw: str, expected: str) -> None:
    assert to_plain_code(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("600000", "sh600000"), ("000001", "sz000001"), ("300750", "sz300750"), ("830799", "bj830799")],
)
def test_to_market_symbol(raw: str, expected: str) -> None:
    assert to_market_symbol(raw) == expected


def test_invalid_code_rejected() -> None:
    with pytest.raises(ValueError):
        to_market_symbol("ABC")


# ---- 降级链 ----------------------------------------------------------------
def test_primary_success_not_degraded() -> None:
    primary = FakeSource("eastmoney", SourceTag.D1, rows_of([10.0, 10.1, 10.2]))
    backup = FakeSource("sina", SourceTag.D2, rows_of([9.0]))
    result = fetcher([primary, backup]).fetch_daily("600000", START, END)
    assert result.source_tag is SourceTag.D1
    assert result.degraded is False
    assert result.rows == 3
    assert backup.calls == 0


def test_empty_payload_treated_as_failure_and_falls_back() -> None:
    """东财 push2 静默风控返回空表 → 必须降级，不能当成「无行情」。"""
    primary = FakeSource("eastmoney", SourceTag.D1, rows=[])
    backup = FakeSource("sina", SourceTag.D2, rows_of([10.0, 10.1]))
    result = fetcher([primary, backup]).fetch_daily("600000", START, END)
    assert result.source_tag is SourceTag.D2
    assert result.degraded is True
    assert "降级" in result.note
    assert any("空响应" in a for a in result.attempts)


def test_exception_falls_back_and_records_attempt() -> None:
    primary = FakeSource("eastmoney", SourceTag.D1, error=ConnectionError("ProxyError"))
    backup = FakeSource("sina", SourceTag.D2, rows_of([10.0]))
    result = fetcher([primary, backup]).fetch_daily("600000", START, END)
    assert result.source_tag is SourceTag.D2
    assert any("ProxyError" in a for a in result.attempts)


def test_retry_then_success_keeps_primary() -> None:
    primary = FakeSource("eastmoney", SourceTag.D1, rows_of([10.0, 10.1]), fail_times=1)
    result = fetcher([primary], retries=2).fetch_daily("600000", START, END)
    assert result.source_tag is SourceTag.D1
    assert primary.calls == 2, "第 1 次失败后应重试并成功"


def test_out_of_range_rows_filtered() -> None:
    rows = rows_of([10.0]) + [{"date": date(2020, 1, 2), "close": 5.0}]
    primary = FakeSource("eastmoney", SourceTag.D1, rows)
    result = fetcher([primary]).fetch_daily("600000", START, END)
    assert [b.trade_date for b in result.bars] == [date(2026, 11, 2)]


def test_all_sources_fail_without_cache_raises() -> None:
    sources = [
        FakeSource("eastmoney", SourceTag.D1, error=ConnectionError("proxy")),
        FakeSource("sina", SourceTag.D2, error=TimeoutError("timeout")),
    ]
    with pytest.raises(FetchError) as excinfo:
        fetcher(sources).fetch_daily("600000", START, END)
    assert "proxy" in str(excinfo.value)
    assert "timeout" in str(excinfo.value)


def test_cache_fallback_marks_d3(tmp_path) -> None:
    store = AssistantStore(tmp_path / "t.db")
    store.create_all()
    store.save_bars("600000", [Bar(trade_date=date(2026, 11, 20), open=10, high=10, low=10, close=10, volume=1)])
    sources = [FakeSource("eastmoney", SourceTag.D1, error=ConnectionError("proxy"))]
    result = fetcher(sources, store=store).fetch_daily("600000", START, END, allow_cache=True)
    assert result.source_tag is SourceTag.D3
    assert result.degraded is True
    assert "本地缓存" in result.note


# ---- 熔断 ------------------------------------------------------------------
def test_breaker_opens_after_threshold_and_skips() -> None:
    now = [0.0]
    primary = FakeSource("eastmoney", SourceTag.D1, error=ConnectionError("blocked"))
    backup = FakeSource("sina", SourceTag.D2, rows_of([10.0]))
    f = fetcher([primary, backup], clock=lambda: now[0], retries=0)

    for _ in range(3):
        f.fetch_daily("600000", START, END)
    assert primary.calls == 3

    # 冷却期内：东财被跳过，不再发起请求
    result = f.fetch_daily("600000", START, END)
    assert primary.calls == 3, "熔断期间不得再请求该源"
    assert any("熔断跳过" in a for a in result.attempts)

    # 冷却结束后恢复尝试
    now[0] = 61.0
    f.fetch_daily("600000", START, END)
    assert primary.calls == 4


def test_breaker_resets_on_success() -> None:
    now = [0.0]
    flaky = FakeSource("eastmoney", SourceTag.D1, rows_of([10.0]), fail_times=2)
    f = fetcher([flaky], clock=lambda: now[0], retries=0)
    for _ in range(2):
        with pytest.raises(FetchError):
            f.fetch_daily("600000", START, END)
    assert f.breaker.snapshot()["eastmoney"]["failures"] == 2
    result = f.fetch_daily("600000", START, END)
    assert result.source_tag is SourceTag.D1
    assert f.breaker.snapshot() == {}, "成功一次后失败计数应清零"


# ---- 回补 ------------------------------------------------------------------
def test_backfill_saves_and_is_idempotent(tmp_path) -> None:
    store = AssistantStore(tmp_path / "t.db")
    store.create_all()
    code = "600000"
    # 覆盖到 END 的连续交易日，保证「已更新」判定可触发
    source = FakeSource("eastmoney", SourceTag.D1, rows_of([10.0 + i * 0.01 for i in range(20)]))

    f = fetcher([source], store=store)
    results = f.backfill([code], years=1, end=END)
    assert results[0].rows > 10
    assert store.bar_count(code) == results[0].rows

    # 第二次：本地已更新到 END 附近 → 跳过，不再请求
    before = source.calls
    second = f.backfill([code], years=1, end=END + timedelta(days=1))
    assert "跳过回补" in second[0].note
    assert source.calls == before


def test_backfill_failure_does_not_raise(tmp_path) -> None:
    store = AssistantStore(tmp_path / "t.db")
    store.create_all()
    source = FakeSource("eastmoney", SourceTag.D1, error=ConnectionError("blocked"))
    results = fetcher([source], store=store).backfill(["600000"], years=1, end=END)
    assert results[0].degraded is True
    assert results[0].rows == 0
    assert "取数失败" in results[0].note


def test_backfill_requires_store() -> None:
    with pytest.raises(ValueError):
        fetcher([FakeSource("eastmoney", SourceTag.D1, rows_of([10.0]))]).backfill(["600000"])


# ---- 复权派生 --------------------------------------------------------------
def test_post_adjusted_closes_uses_factor() -> None:
    bars = [
        Bar(trade_date=date(2026, 11, 20), open=10, high=10, low=10, close=10, volume=1, adj_factor=1.0),
        Bar(trade_date=date(2026, 11, 21), open=9, high=9, low=9, close=9, volume=1, adj_factor=1.2),
    ]
    assert post_adjusted_closes(bars) == pytest.approx([10.0, 10.8])


def test_derive_adj_factor_from_qfq_ratio() -> None:
    from src.assistant.data.fetcher import derive_adj_factor_from_qfq

    raw = [Bar(trade_date=date(2026, 11, 20), open=10, high=10, low=10, close=10, volume=1)]
    qfq = [{"date": "2026-11-20", "close": 12.0}]
    out = derive_adj_factor_from_qfq(raw, qfq)
    assert out[0].adj_factor == pytest.approx(1.2)
    assert out[0].close == pytest.approx(10.0), "原始价不得被改成前复权价"
    assert out[0].source_tag is SourceTag.D2


# ---- 指数（撞号防护）-----------------------------------------------------
@pytest.mark.parametrize("raw,expected", [("sh000001", "sh000001"), ("000001.SH", "sh000001"), ("sz399001", "sz399001")])
def test_to_index_symbol(raw: str, expected: str) -> None:
    assert to_index_symbol(raw) == expected


def test_index_bare_code_rejected() -> None:
    """裸 ``000001`` 会被个股规则判成平安银行，必须拒绝而不是猜。"""
    with pytest.raises(ValueError):
        to_index_symbol("000001")
    with pytest.raises(ValueError):
        ReferenceFetcher().fetch_index("000001", START, END)


def test_reference_fetcher_passes_prefixed_symbol() -> None:
    seen: list[str] = []

    class IndexFake(FakeSource):
        def fetch(self, code: str, start: date, end: date):
            seen.append(code)
            return rows_of([3548.0])

    result = ReferenceFetcher([IndexFake("sina_index", SourceTag.D1)]).fetch_index("000001.SH", START, END)
    assert seen == ["sh000001"], "指数必须按 sh 前缀请求，不能转成 sz000001"
    assert result.bars[0].close == pytest.approx(3548.0)
    assert result.code == "sh000001"


def test_reference_fetcher_degrades_on_empty() -> None:
    empty = FakeSource("sina_index", SourceTag.D1, rows=[])
    with pytest.raises(FetchError):
        ReferenceFetcher([empty]).fetch_index("sh000001", START, END)
