# -*- coding: utf-8 -*-
"""大盘快照落库（D-20 前半）：写在哪、什么时候不写、重复写怎么办。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from src.assistant.data.storage import AssistantStore
from src.assistant.pipeline import AssistantPipeline, PipelineSettings
from tests.assistant.helpers import make_bars, trending


def build_pipeline(tmp_path, *, market_bars=None) -> tuple[AssistantPipeline, AssistantStore, date]:
    store = AssistantStore(tmp_path / "market.db")
    store.create_all()
    bars = make_bars(trending(150))
    store.save_bars("600000", bars)
    store.append_watchlist("600000", "add", effective_date=date(2026, 5, 1))
    pipeline = AssistantPipeline(
        store=store,
        settings=PipelineSettings(total_capital=100_000.0, index_code="sh000001"),
        clock=lambda: datetime(2026, 11, 25, 18, 0),
        market_bars=market_bars,
    )
    return pipeline, store, bars[-1].trade_date


def test_snapshot_writes_market_row(tmp_path) -> None:
    # 指数与个股取同样根数：末根日期相同，才不是「指数滞后」而 degraded
    index_bars = make_bars(trending(150, drift=0.006, base=3000.0))
    pipeline, store, day = build_pipeline(tmp_path, market_bars=index_bars)

    outcome = pipeline.ensure_snapshot(day)
    market = store.snapshot_market(outcome.snapshot_id)

    assert market is not None
    assert market["index_code"] == "sh000001"
    assert market["index_name"] == "上证指数"
    assert market["pct_change"] is not None
    assert market["market_regime"] in {"偏强", "中性", "偏弱"}
    assert market["regime_cells"] in {"above+up", "above+not_up", "below+up", "below+not_up"}
    assert market["degraded"] is False


def test_market_row_carries_data_cutoff(tmp_path) -> None:
    """指数最后一根早于请求日 → degraded（页面据此标「截至 MM-DD」，不把旧值说成今天）。"""
    index_bars = make_bars(trending(80, drift=0.006, base=3000.0))
    pipeline, store, day = build_pipeline(tmp_path, market_bars=index_bars)

    outcome = pipeline.ensure_snapshot(day + timedelta(days=3))
    market = store.snapshot_market(outcome.snapshot_id)

    assert market is not None
    assert market["degraded"] is True


def test_unavailable_market_writes_nothing(tmp_path) -> None:
    """没有指数数据时**不写行**：读方按「未写入」处理，不编造档位。"""
    pipeline, store, day = build_pipeline(tmp_path)

    outcome = pipeline.ensure_snapshot(day)

    assert store.snapshot_market(outcome.snapshot_id) is None


def test_second_write_is_skipped(tmp_path) -> None:
    index_bars = make_bars(trending(80, drift=0.006, base=3000.0))
    pipeline, store, day = build_pipeline(tmp_path, market_bars=index_bars)
    outcome = pipeline.ensure_snapshot(day)

    written = store.save_market_snapshot(
        outcome.snapshot_id, index_code="sh000001", index_name="别的名字", market_regime="偏强"
    )

    assert written is False
    assert store.snapshot_market(outcome.snapshot_id)["index_name"] == "上证指数"


def test_market_failure_does_not_lose_conclusions(tmp_path) -> None:
    """大盘表异常只降级为「该行缺失」，不能带走当天已算好的个股结论。"""

    class BrokenStore(AssistantStore):
        def save_market_snapshot(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("表异常")

    store = BrokenStore(tmp_path / "broken.db")
    store.create_all()
    bars = make_bars(trending(150))
    store.save_bars("600000", bars)
    store.append_watchlist("600000", "add", effective_date=date(2026, 5, 1))
    pipeline = AssistantPipeline(
        store=store,
        settings=PipelineSettings(total_capital=100_000.0),
        clock=lambda: datetime(2026, 11, 25, 18, 0),
        market_bars=make_bars(trending(80, drift=0.006, base=3000.0)),
    )

    outcome = pipeline.ensure_snapshot(bars[-1].trade_date)

    assert outcome.codes == ("600000",)
    assert store.snapshot_market(outcome.snapshot_id) is None


@pytest.mark.parametrize("bad", [None, []])
def test_empty_market_bars_stay_unavailable(tmp_path, bad) -> None:
    pipeline, store, day = build_pipeline(tmp_path, market_bars=bad)
    outcome = pipeline.ensure_snapshot(day)
    assert store.snapshot_market(outcome.snapshot_id) is None
