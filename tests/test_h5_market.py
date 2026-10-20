# -*- coding: utf-8 -*-
"""首页大盘（M2-A）纯函数单测：评级矩阵、指标格式、总结自洽性。"""

from __future__ import annotations

import pytest

from src.services.h5_service import (
    REGIME_UNKNOWN,
    build_market_block,
    build_market_indicators,
    build_market_summary,
    judge_market_regime,
)


# --------------------------------------------------------------------------- #
# 环境评级：2×2 矩阵必须覆盖四个象限
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("close", "ma20", "ma20_prev", "volume_ratio", "expected_level", "expected_tone"),
    [
        (110.0, 100.0, 99.0, 1.2, "bullish", "up"),
        (110.0, 100.0, 99.0, 0.8, "bullish_quiet", "up"),
        (110.0, 100.0, 101.0, 1.0, "rebound", "warning"),
        (95.0, 100.0, 99.0, 1.0, "pullback", "warning"),
        (95.0, 100.0, 101.0, 1.0, "bear", "down"),
    ],
)
def test_regime_matrix(close, ma20, ma20_prev, volume_ratio, expected_level, expected_tone):
    regime = judge_market_regime(close=close, ma20=ma20, ma20_prev=ma20_prev, volume_ratio=volume_ratio)
    assert regime["level"] == expected_level
    assert regime["tone"] == expected_tone
    assert regime["advice"]


def test_regime_boundaries():
    # 价格正好等于 MA20：归入"上方"分支（不误判为破位）
    equal = judge_market_regime(close=100.0, ma20=100.0, ma20_prev=99.0, volume_ratio=1.0)
    assert equal["level"] == "bullish"

    # 量能比恰好 1.0 视为放量
    exact = judge_market_regime(close=110.0, ma20=100.0, ma20_prev=99.0, volume_ratio=1.0)
    assert exact["level"] == "bullish"

    # MA20 持平：不算上行，落入反弹分支
    flat = judge_market_regime(close=110.0, ma20=100.0, ma20_prev=100.0, volume_ratio=1.0)
    assert flat["level"] == "rebound"


def test_regime_unknown_when_data_missing():
    for kwargs in (
        {"close": None, "ma20": 100.0, "ma20_prev": 99.0, "volume_ratio": 1.0},
        {"close": 110.0, "ma20": None, "ma20_prev": 99.0, "volume_ratio": 1.0},
        {"close": 110.0, "ma20": 100.0, "ma20_prev": None, "volume_ratio": 1.0},
    ):
        assert judge_market_regime(**kwargs)["level"] == REGIME_UNKNOWN["level"]


# --------------------------------------------------------------------------- #
# 指标格式
# --------------------------------------------------------------------------- #
def test_indicator_formatting():
    rows = build_market_indicators(
        ma_dev_pct=1.2,
        ma_align="bull",
        volume_ratio=0.86,
        rsi=58.0,
        range20=0.72,
        chg5=2.1,
    )
    by_key = {row["key"]: row for row in rows}

    assert [row["key"] for row in rows] == ["ma20", "ma_align", "volume", "rsi", "range20", "chg5"]
    assert by_key["ma20"]["value"] == "上方 +1.20%"
    assert by_key["ma20"]["tone"] == "up"
    assert by_key["ma_align"]["value"] == "多头排列"
    assert by_key["volume"]["value"] == "0.86×（缩量）"
    assert by_key["rsi"]["value"] == "58（中性）"
    assert by_key["range20"]["value"] == "72%（偏上）"
    assert by_key["chg5"]["value"] == "+2.10%"
    assert by_key["chg5"]["tone"] == "up"


def test_indicator_missing_values_degrade_to_dash():
    rows = build_market_indicators(
        ma_dev_pct=None, ma_align=None, volume_ratio=None, rsi=None, range20=None, chg5=None
    )
    assert all(row["value"] == "-" for row in rows)
    assert all(row["tone"] == "neutral" for row in rows)


def test_rsi_extremes_are_flagged():
    hot = build_market_indicators(
        ma_dev_pct=2.0, ma_align="bull", volume_ratio=1.3, rsi=72.0, range20=0.9, chg5=3.0
    )
    assert next(row for row in hot if row["key"] == "rsi")["tone"] == "warning"
    cold = build_market_indicators(
        ma_dev_pct=-2.0, ma_align="bear", volume_ratio=0.7, rsi=25.0, range20=0.1, chg5=-4.0
    )
    assert next(row for row in cold if row["key"] == "rsi")["value"] == "25（超卖）"


# --------------------------------------------------------------------------- #
# 总结：数字必须自洽
# --------------------------------------------------------------------------- #
def _counts(**kwargs):
    base = {"reduce": 0, "increase": 0, "hold": 0, "watch": 0, "none": 0}
    base.update(kwargs)
    return base


def test_summary_counts_are_self_consistent():
    summary = build_market_summary(
        index={"name": "上证指数", "close": 3268.42, "change_pct": 0.42},
        regime={"label": "偏多（缩量）", "advice": "缩量上行，不追高"},
        trend_text="站上 MA20 且 MA20 上行",
        volume_text="量能 0.86 倍（缩量）",
        counts=_counts(reduce=2, increase=2, watch=3, none=1),
    )
    assert "3268.42" in summary
    assert "+0.42%" in summary
    assert "偏多（缩量）" in summary
    assert "个股仍按各自纪律执行" in summary
    # 2 + 2 + 3 + 1 = 8
    assert "自选股 8 只中" in summary
    assert "2 只需减/清" in summary
    assert "1 只暂无信号" in summary


def test_summary_skips_counts_sentence_when_empty():
    summary = build_market_summary(
        index={"name": "上证指数", "close": 3268.42, "change_pct": None},
        regime={"label": "偏空", "advice": "控制仓位"},
        trend_text="跌破 MA20 且 MA20 下行",
        volume_text="量能 1.10 倍（平量）",
        counts=_counts(),
    )
    assert "自选股" not in summary
    assert "环境偏空" in summary


def test_summary_degrades_without_index():
    summary = build_market_summary(
        index={}, regime=REGIME_UNKNOWN, trend_text="", volume_text="", counts=_counts(reduce=1)
    )
    assert summary == "大盘数据暂不可用。"


def test_market_block_strips_internal_fields():
    block = build_market_block(
        {
            "index": {"name": "上证指数", "close": 3268.42, "change_pct": 0.42},
            "others": [],
            "indicators": [],
            "regime": {"level": "bullish_quiet", "label": "偏多（缩量）", "tone": "up", "advice": "等回踩"},
            "advice": "等回踩",
            "alerts": [],
            "as_of": "2026-10-20",
            "degraded": [],
            "trend_text": "站上 MA20 且 MA20 上行",
            "volume_text": "量能 0.86 倍（缩量）",
        },
        ["减仓", "观望", "暂无信号"],
    )
    assert "trend_text" not in block
    assert "volume_text" not in block
    assert block["counts"]["reduce"] == 1
    assert block["counts"]["none"] == 1
    assert "自选股 3 只中" in block["summary"]


# --------------------------------------------------------------------------- #
# 黄金样本：固定 120 根指数日线 → 指标与评级必须稳定（防后续调参悄悄改口径）
# --------------------------------------------------------------------------- #
def test_market_core_golden_sample():
    pd = pytest.importorskip("pandas", reason="指标快照依赖 pandas，轻量环境跳过")

    from src.services.h5_service import H5Service

    closes = [3000.0 + index * 2.0 for index in range(120)]
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2026-04-01", periods=120, freq="B"),
            "open": [value - 1 for value in closes],
            "high": [value + 5 for value in closes],
            "low": [value - 5 for value in closes],
            "close": closes,
            "volume": [1.0e8] * 120,
        }
    )

    class NoQuote:
        def get_realtime_quote(self, symbol):
            return None

    service = H5Service(
        overview_service=None,
        watchlist_service=None,
        stock_service=NoQuote(),
        fetch=lambda symbol, days: frame,
        cache_ttl_seconds=0,
        market_cache_ttl_seconds=0,
    )
    core = service._market_core()

    # 连续上行 + 量能持平 → 偏多（放量分支，因量比恰为 1.0）
    assert core["regime"]["level"] == "bullish"
    assert core["index"]["close"] == pytest.approx(closes[-1])
    by_key = {row["key"]: row for row in core["indicators"]}
    assert by_key["ma20"]["tone"] == "up"
    assert by_key["ma_align"]["value"] == "多头排列"
    assert by_key["volume"]["value"] == "1.00×（平量）"
    assert by_key["chg5"]["value"].startswith("+")
