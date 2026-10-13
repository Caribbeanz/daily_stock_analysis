# -*- coding: utf-8 -*-
"""P0 指标模块单元测试（纯函数，无 IO）。"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from src.core.indicators import (
    InsufficientDataError,
    atr,
    bias,
    boll,
    build_indicator_snapshot,
    ema,
    kdj,
    macd,
    resample_weekly,
    rsi,
    sma,
)


def _make_df(closes, volumes=None, highs=None, lows=None):
    closes = list(closes)
    n = len(closes)
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    volumes = volumes or [1000.0] * n
    return pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=n, freq="D"),
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )


def test_sma_and_ema_basic():
    series = pd.Series([1, 2, 3, 4, 5], dtype="float64")
    assert sma(series, 3).iloc[-1] == pytest.approx(4.0)
    assert math.isfinite(ema(series, 3).iloc[-1])


def test_macd_hist_is_twice_diff():
    series = pd.Series([10 + i * 0.5 for i in range(60)], dtype="float64")
    dif, dea, hist = macd(series)
    assert hist.iloc[-1] == pytest.approx((dif.iloc[-1] - dea.iloc[-1]) * 2.0)


def test_kdj_in_reasonable_range():
    n = 40
    close = pd.Series([10 + math.sin(i / 3) for i in range(n)], dtype="float64")
    high = close + 0.2
    low = close - 0.2
    k, d, j = kdj(high, low, close)
    assert 0 <= k.iloc[-1] <= 100
    assert 0 <= d.iloc[-1] <= 100


def test_rsi_all_up_is_100():
    close = pd.Series([float(i) for i in range(1, 40)], dtype="float64")
    value = rsi(close, 14).iloc[-1]
    assert value == pytest.approx(100.0)


def test_boll_bands_ordering():
    close = pd.Series([10 + (i % 5) * 0.3 for i in range(40)], dtype="float64")
    mid, upper, lower = boll(close, 20, 2.0)
    assert lower.iloc[-1] < mid.iloc[-1] < upper.iloc[-1]


def test_bias_positive_when_above_ma():
    close = pd.Series([float(i) for i in range(1, 40)], dtype="float64")
    assert bias(close, 6).iloc[-1] > 0


def test_snapshot_uptrend_flags():
    closes = [10 + i * 0.2 for i in range(60)]
    snap = build_indicator_snapshot(_make_df(closes))
    assert snap.is_bullish_alignment() is True
    assert snap.ma_slope_up(20) is True
    assert snap.close > snap.ma[5] > snap.ma[20]


def test_snapshot_insufficient_data_raises():
    with pytest.raises(InsufficientDataError):
        build_indicator_snapshot(_make_df([1, 2, 3]))


def test_resample_weekly_reduces_rows():
    closes = [10 + i * 0.1 for i in range(60)]
    df = _make_df(closes)
    weekly = resample_weekly(df)
    assert not weekly.empty
    assert len(weekly) < len(df)
    assert {"open", "high", "low", "close", "volume"}.issubset(weekly.columns)


# --------------------------- ATR（止损自适应基准） --------------------------- #
def test_atr_positive_and_tracks_range():
    closes = [10.0] * 40
    high = pd.Series([10.6] * 40, dtype="float64")
    low = pd.Series([9.6] * 40, dtype="float64")
    close = pd.Series(closes, dtype="float64")
    series = atr(high, low, close, 14)
    assert series.iloc[-1] > 0
    # 恒定 1.0 振幅（high-low）→ ATR 收敛到 1.0 附近
    assert series.iloc[-1] == pytest.approx(1.0, abs=0.05)


def test_atr_widens_with_volatility():
    n = 60
    calm_high = pd.Series([10.1] * n, dtype="float64")
    calm_low = pd.Series([9.9] * n, dtype="float64")
    close = pd.Series([10.0] * n, dtype="float64")
    calm = atr(calm_high, calm_low, close, 14).iloc[-1]

    wild_high = pd.Series([11.0] * n, dtype="float64")
    wild_low = pd.Series([9.0] * n, dtype="float64")
    wild = atr(wild_high, wild_low, close, 14).iloc[-1]
    assert wild > calm


def test_snapshot_exposes_atr():
    closes = [10 + (i % 7) * 0.2 for i in range(60)]
    snap = build_indicator_snapshot(_make_df(closes))
    assert math.isfinite(snap.atr)
    assert snap.atr > 0
