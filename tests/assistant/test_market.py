# -*- coding: utf-8 -*-
"""D-16 大盘一句测试：M1 环境档 + 装配层取数与降级。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace

import pytest

from src.assistant.data.storage import AssistantStore
from src.assistant.engine.market import (
    MarketRegimeParams,
    _quadrant_regime,  # noqa: SLF001 - 单测锁定四格闭合
    market_line_from_bars,
)
from src.assistant.pipeline import AssistantPipeline, PipelineSettings
from tests.assistant.helpers import make_bars

DAY = date(2026, 11, 27)


def up(n: int = 40) -> list[float]:
    return [100.0 + 2.0 * i for i in range(n)]


def down(n: int = 40) -> list[float]:
    return [200.0 - 2.0 * i for i in range(n)]


class TestQuadrants:
    def test_four_cells_closed(self) -> None:
        assert _quadrant_regime("above", "up") == "偏强"
        assert _quadrant_regime("above", "not_up") == "中性"
        assert _quadrant_regime("below", "up") == "中性"
        assert _quadrant_regime("below", "not_up") == "偏弱"

    def test_q1_strong(self) -> None:
        line = market_line_from_bars(make_bars(up()), index_name="测试指数")
        assert line.available is True
        assert line.regime == "偏强"
        assert line.pct_change is not None and line.pct_change > 0
        assert line.index_name == "测试指数"

    def test_q4_weak(self) -> None:
        line = market_line_from_bars(make_bars(down()), index_name="测试指数")
        assert line.available is True
        assert line.regime == "偏弱"
        assert line.pct_change is not None and line.pct_change < 0

    def test_q2_above_but_slope_down_is_neutral(self) -> None:
        closes = [100.0 + 2.0 * i for i in range(35)]
        closes += [170.0 - 1.5 * i for i in range(12)]
        closes += [170.0 - 1.5 * 12 + 5.0]
        line = market_line_from_bars(make_bars(closes))
        assert line.available is True
        assert line.regime == "中性"

    def test_q3_below_but_slope_up_is_neutral(self) -> None:
        closes = [100.0 + 2.0 * i for i in range(40)] + [150.0]
        line = market_line_from_bars(make_bars(closes))
        assert line.available is True
        assert line.regime == "中性"

    def test_near_carries_last_valid_regime(self) -> None:
        closes = up()
        # 解 close = MA20(含自身) → 正好落在缓冲带内
        closes[-1] = sum(closes[20:39]) / 19
        line = market_line_from_bars(make_bars(closes))
        assert line.available is True
        assert line.regime == "偏强"  # 沿用上一交易日有效档

    def test_sentence_renders_direction_with_arrow(self) -> None:
        line = market_line_from_bars(make_bars(up()), index_name="上证指数")
        assert "▲" in line.sentence()
        assert "偏强" in line.sentence()

    def test_missing_sentence_says_no_judgement(self) -> None:
        assert "不判断环境档" in market_line_from_bars([]).sentence()


class TestDegrade:
    def test_too_few_bars_degrades(self) -> None:
        line = market_line_from_bars(make_bars(up(10)))
        assert line.available is False
        assert line.pct_change is None

    def test_flat_series_carries_no_history(self) -> None:
        # 全程 near、无任何有效历史档 → 不猜档
        assert market_line_from_bars(make_bars([100.0] * 40)).available is False

    def test_non_finite_close_degrades(self) -> None:
        from math import nan

        # Bar 本身拒绝 NaN，用鸭子类型的行情行验证防御分支
        source = make_bars(up())
        bars = [
            SimpleNamespace(
                trade_date=bar.trade_date,
                close=nan if idx == len(source) - 1 else bar.close,
            )
            for idx, bar in enumerate(source)
        ]
        assert market_line_from_bars(bars).available is False  # type: ignore[arg-type]

    def test_bad_params_rejected(self) -> None:
        with pytest.raises(ValueError):
            MarketRegimeParams(ma_window=1)
        with pytest.raises(ValueError):
            MarketRegimeParams(slope_lookback_k=0)


class TestPipelineMarketLine:
    @staticmethod
    def pipeline(**kwargs) -> AssistantPipeline:
        store = AssistantStore(":memory:")
        store.create_all()
        return AssistantPipeline(
            store=store,
            settings=PipelineSettings(total_capital=100_000.0),
            **kwargs,
        )

    def test_injected_bars_fill_market_line(self) -> None:
        pipeline = self.pipeline(market_bars=make_bars(up()))
        line = pipeline.market_line(DAY)
        assert line.available is True
        assert line.regime == "偏强"

    def test_lagging_index_bar_is_labelled(self) -> None:
        bars = make_bars(up(), start=date(2026, 9, 1))
        pipeline = self.pipeline(market_bars=bars)
        line = pipeline.market_line(DAY)
        assert line.available is True
        assert "截至" in line.index_name  # 不把旧数据说成今天

    def test_fetcher_called_once_per_day(self) -> None:
        @dataclass
        class Fetcher:
            bars: tuple
            calls: int = 0

            def fetch_index(self, code, start, end):
                self.calls += 1
                assert code == "sh000001"
                assert start <= end
                return SimpleNamespace(bars=self.bars)

        fetcher = Fetcher(tuple(make_bars(up())))
        pipeline = self.pipeline(market_fetcher=fetcher)

        first = pipeline.market_line(DAY)
        second = pipeline.market_line(DAY)
        assert first == second
        assert fetcher.calls == 1  # 同日缓存：同源与逐字相同

    def test_fetch_failure_degrades_honestly(self) -> None:
        class Broken:
            def fetch_index(self, code, start, end):
                raise RuntimeError("指数源不可用")

        pipeline = self.pipeline(market_fetcher=Broken())
        line = pipeline.market_line(DAY)
        assert line.available is False
        assert "不判断环境档" in line.sentence()

    def test_no_fetcher_offline_degrades(self) -> None:
        assert self.pipeline().market_line(DAY).available is False
