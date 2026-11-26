# -*- coding: utf-8 -*-
"""板块快照测试：2×2 三档、`rs_t` 口径、滞后与降级、装配层选板与落库。

关键用例都对着一件真实事故写：同花顺**板块一览有当日涨跌幅，板块指数日线晚一天发布**，
2026-11-26 实测「贵金属 +4.22%」若用序列自算当日差会得到 `rs_t=-3.35`（方向反了）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace

import pytest

from src.assistant.data.fetcher import SectorOverview, SectorRow
from src.assistant.data.storage import AssistantStore
from src.assistant.engine.sector import (
    SectorStrengthParams,
    rs_label_from_gap,
    sector_strength_from_bars,
)
from src.assistant.enums import SourceTag
from src.assistant.pipeline import AssistantPipeline, PipelineSettings
from tests.assistant.helpers import make_bars

DAY = date(2026, 11, 26)


def up(n: int = 40) -> list[float]:
    return [100.0 + 2.0 * i for i in range(n)]


def down(n: int = 40) -> list[float]:
    return [200.0 - 2.0 * i for i in range(n)]


class TestStrength:
    def test_uptrend_is_strong(self) -> None:
        assert sector_strength_from_bars(make_bars(up())).strength == "偏强"

    def test_downtrend_is_weak(self) -> None:
        assert sector_strength_from_bars(make_bars(down())).strength == "偏弱"

    def test_flat_is_neutral(self) -> None:
        assert sector_strength_from_bars(make_bars([100.0] * 40)).strength == "中性"

    def test_short_series_never_judges(self) -> None:
        """单日/短序列不得判档（需求 §4.6）：中性 + degraded + 原因。"""
        outcome = sector_strength_from_bars(make_bars(up(10)))

        assert outcome.strength == "中性"
        assert outcome.degraded is True
        assert "序列不足" in outcome.note

    def test_nan_degrades(self) -> None:
        # Bar 自身拒绝 NaN，用鸭子类型的行情行验证防御分支
        source = make_bars(up())
        bars = [
            SimpleNamespace(
                trade_date=bar.trade_date,
                close=float("nan") if idx == len(source) - 1 else bar.close,
            )
            for idx, bar in enumerate(source)
        ]
        outcome = sector_strength_from_bars(bars)

        assert outcome.degraded is True
        assert outcome.strength == "中性"

    def test_near_carries_last_valid_side(self) -> None:
        closes = up()
        # 解 close = MA20(含自身) → 正好落在缓冲带内
        closes[-1] = sum(closes[20:39]) / 19
        outcome = sector_strength_from_bars(make_bars(closes))

        assert outcome.degraded is False
        assert outcome.strength == "偏强"


class TestRelativeStrength:
    def test_uses_same_day_pct_not_series_tail(self) -> None:
        """`rs_t` 用调用方给的当日涨跌幅，不用序列末日（否则会与今日方向相反）。"""
        bars = make_bars(down(30) + [up(1)[0]])  # 序列在跌，但当日涨
        outcome = sector_strength_from_bars(bars, pct_change=4.22, market_pct=0.01)

        assert outcome.rs_t == pytest.approx(4.21)
        assert outcome.rs_label == "强于大盘"

    def test_missing_market_pct_gives_no_value(self) -> None:
        outcome = sector_strength_from_bars(make_bars(up()), pct_change=4.22)

        assert outcome.rs_t is None
        assert outcome.rs_label == ""

    def test_label_bands(self) -> None:
        assert rs_label_from_gap(1.0) == "强于大盘"
        assert rs_label_from_gap(-1.0) == "弱于大盘"
        assert rs_label_from_gap(0.4) == "同步"
        assert rs_label_from_gap(None) == ""


class TestLag:
    def test_lagged_series_is_flagged(self) -> None:
        """板块指数停在 T-1：档位照算但标 degraded + 注明截止日。"""
        bars = make_bars(up())
        as_of = bars[-1].trade_date
        outcome = sector_strength_from_bars(bars, pct_change=1.0, market_pct=0.0, as_of_day=DAY)

        assert outcome.degraded is True
        assert as_of.strftime("%m-%d") in outcome.note
        assert outcome.as_of == as_of

    def test_same_day_series_not_flagged(self) -> None:
        bars = make_bars(up())
        outcome = sector_strength_from_bars(
            bars, pct_change=1.0, market_pct=0.0, as_of_day=bars[-1].trade_date
        )

        assert outcome.degraded is False
        assert outcome.note == ""

    def test_params_guard(self) -> None:
        with pytest.raises(ValueError):
            SectorStrengthParams(ma_window=1)


@dataclass
class _FakeMarketFetcher:
    """假板块源：一览 + 逐板块指数日线，全部离线。"""

    rows: tuple[SectorRow, ...]
    bars_by_name: dict[str, list]
    index_calls: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.index_calls is None:
            self.index_calls = []

    def fetch_sectors(self) -> SectorOverview:
        return SectorOverview(rows=self.rows, source_tag=SourceTag.D1, source_name="fake")

    def fetch_sector_index(self, name: str, start: date, end: date):  # noqa: ANN201
        self.index_calls.append(name)
        return tuple(self.bars_by_name.get(name, ()))


def _overview() -> tuple[SectorRow, ...]:
    return (
        SectorRow(sector_code="881001", sector_name="强板块", pct_change=4.2, up_count=14, down_count=0,
                  lead_stock="甲"),
        SectorRow(sector_code="881002", sector_name="中板块", pct_change=0.5, up_count=6, down_count=5,
                  lead_stock="乙"),
        SectorRow(sector_code="881003", sector_name="弱板块", pct_change=-3.1, up_count=0, down_count=12,
                  lead_stock="丙"),
        SectorRow(sector_code="", sector_name="缺代码板块", pct_change=9.9),
    )


def _pipeline(store: AssistantStore, fetcher: _FakeMarketFetcher, **kwargs) -> AssistantPipeline:
    return AssistantPipeline(
        store=store,
        settings=PipelineSettings(total_capital=100_000.0, **kwargs),
        market_fetcher=fetcher,
    )


class TestPipelineSectors:
    def setup_method(self) -> None:
        self.store = AssistantStore(":memory:")
        self.store.create_all()

    def test_picks_both_ends_and_skips_codeless(self) -> None:
        fetcher = _FakeMarketFetcher(
            rows=_overview(),
            bars_by_name={"强板块": make_bars(up()), "中板块": make_bars([100.0] * 40),
                          "弱板块": make_bars(down())},
        )
        pipeline = _pipeline(self.store, fetcher, sector_top_n=1, sector_weak_n=1)

        rows = pipeline.sector_rows(DAY)

        assert [row["sector_name"] for row in rows] == ["强板块", "弱板块"]
        assert rows[0]["sector_strength"] == "偏强"
        assert rows[1]["sector_strength"] == "偏弱"
        assert all(row["sector_code"] for row in rows)  # 缺代码的不入选
        assert "缺代码板块" not in fetcher.index_calls

    def test_same_day_cached_once(self) -> None:
        fetcher = _FakeMarketFetcher(rows=_overview(), bars_by_name={"强板块": make_bars(up())})
        pipeline = _pipeline(self.store, fetcher, sector_top_n=1, sector_weak_n=0)

        first = pipeline.sector_rows(DAY)
        calls = len(fetcher.index_calls)
        second = pipeline.sector_rows(DAY)

        assert first == second
        assert len(fetcher.index_calls) == calls  # 第二次没再请求

    def test_switch_off_returns_empty(self) -> None:
        fetcher = _FakeMarketFetcher(rows=_overview(), bars_by_name={})
        pipeline = _pipeline(self.store, fetcher, sector_enabled=False)

        assert pipeline.sector_rows(DAY) == ()

    def test_fetch_failure_is_empty_not_faked(self) -> None:
        class _Broken:
            def fetch_sectors(self) -> SectorOverview:
                raise RuntimeError("boom")

        pipeline = AssistantPipeline(
            store=self.store,
            settings=PipelineSettings(total_capital=100_000.0),
            market_fetcher=_Broken(),
        )

        assert pipeline.sector_rows(DAY) == ()

    def test_persist_is_idempotent(self) -> None:
        rows = [
            {
                "sector_code": "881001",
                "sector_name": "强板块",
                "pct_change": 4.2,
                "sector_strength": "偏强",
                "rs_label": "强于大盘",
                "rs_t": 4.19,
                "up_count": 14,
                "down_count": 0,
                "lead_stock": "甲",
                "as_of": date(2026, 11, 25),
                "degraded": True,
            }
        ]

        assert self.store.save_sector_snapshots("snap-1", rows) == 1
        assert self.store.save_sector_snapshots("snap-1", rows) == 0  # 同日不重写

        readback = self.store.snapshot_sectors("snap-1")[0]
        assert readback["as_of"] == "2026-11-25"
        assert readback["degraded"] is True
        assert readback["rs_t"] == pytest.approx(4.19)

    def test_readback_keeps_write_order(self) -> None:
        """按写入顺序读回（强 → 弱）；若按 sector_code 排，会把最强排到最弱后面。"""
        rows = [
            {"sector_code": "881900", "sector_name": "最强", "pct_change": 5.0, "sector_strength": "偏强"},
            {"sector_code": "881100", "sector_name": "最弱", "pct_change": -5.0, "sector_strength": "偏弱"},
        ]

        self.store.save_sector_snapshots("snap-3", rows)

        assert [row["sector_name"] for row in self.store.snapshot_sectors("snap-3")] == ["最强", "最弱"]

    def test_persist_empty_writes_nothing(self) -> None:
        assert self.store.save_sector_snapshots("snap-2", []) == 0
        assert self.store.snapshot_sectors("snap-2") == []


class TestSettingsEnv:
    def test_defaults(self) -> None:
        settings = PipelineSettings.from_env({"ASSISTANT_TOTAL_CAPITAL": "100000"})

        assert settings.sector_enabled is True
        assert settings.sector_top_n == 6
        assert settings.sector_weak_n == 3

    def test_override_and_clamp(self) -> None:
        settings = PipelineSettings.from_env(
            {
                "ASSISTANT_TOTAL_CAPITAL": "100000",
                "ASSISTANT_SECTOR_ENABLED": "false",
                "ASSISTANT_SECTOR_TOP_N": "99",
                "ASSISTANT_SECTOR_WEAK_N": "abc",
            }
        )

        assert settings.sector_enabled is False
        assert settings.sector_top_n == 20  # 夹到上限
        assert settings.sector_weak_n == 3  # 非法值回默认
