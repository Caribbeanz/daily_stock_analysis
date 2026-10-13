# -*- coding: utf-8 -*-
"""高抛低吸助手 · P0 指标计算（纯函数，无 IO）

职责：
1. 在现有 ``alert_indicators`` 阈值判定之外，提供**数值型**指标计算
   （MA / EMA / MACD / KDJ / RSI / BOLL / BIAS），供场景引擎与风控引擎消费。
2. 提供 ``build_indicator_snapshot`` 一次性产出"最新 + 前一交易日"的关键值，
   便于做金叉/死叉、放大/缩小、超买超卖等判定。
3. 提供 ``resample_weekly`` 由日线聚合周线（FR-3.3 多周期）。

约定（与国内行情软件口径一致，可配置）：
- MACD 柱 = 2 × (DIF − DEA)
- KDJ：RSV 的 SMA(n,1) 递推（K = 2/3·K_prev + 1/3·RSV），初值 K=D=50
- BOLL 使用总体标准差（ddof=0）
- BIAS = (close − MA(n)) / MA(n) × 100

本模块**不依赖数据库、不联网**，便于单测与回测复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

import pandas as pd

__all__ = [
    "InsufficientDataError",
    "IndicatorSnapshot",
    "sma",
    "ema",
    "macd",
    "kdj",
    "rsi",
    "boll",
    "bias",
    "atr",
    "resample_weekly",
    "build_indicator_snapshot",
]


class InsufficientDataError(ValueError):
    """数据不足以计算指标（例如新股、停牌、数据缺失）。"""


DEFAULT_MA_PERIODS: Tuple[int, ...] = (5, 10, 20, 60)
DEFAULT_BIAS_PERIODS: Tuple[int, ...] = (6, 12, 24)
DEFAULT_MACD = (12, 26, 9)
DEFAULT_KDJ = (9, 3, 3)
DEFAULT_RSI_PERIOD = 14
DEFAULT_BOLL = (20, 2.0)
DEFAULT_ATR_PERIOD = 14


# --------------------------------------------------------------------------- #
# 基础指标
# --------------------------------------------------------------------------- #
def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def macd(
    close: pd.Series,
    fast: int = DEFAULT_MACD[0],
    slow: int = DEFAULT_MACD[1],
    signal: int = DEFAULT_MACD[2],
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """返回 (DIF, DEA, MACD柱)，柱 = 2 × (DIF − DEA)。"""
    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, signal)
    hist = (dif - dea) * 2.0
    return dif, dea, hist


def kdj(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    n: int = DEFAULT_KDJ[0],
    k_period: int = DEFAULT_KDJ[1],
    d_period: int = DEFAULT_KDJ[2],
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """返回 (K, D, J)。采用国内常用递推（等价于 RSV 的 SMA(n,1)）。"""
    lowest = low.rolling(window=n, min_periods=n).min()
    highest = high.rolling(window=n, min_periods=n).max()
    span = (highest - lowest).replace(0.0, pd.NA)
    rsv = ((close - lowest) / span * 100.0).astype("float64")

    k_values = []
    d_values = []
    prev_k = 50.0
    prev_d = 50.0
    for value in rsv.tolist():
        if value is None or pd.isna(value):
            k_values.append(prev_k)  # 数据未齐时沿用初值，避免 NaN 传播
            d_values.append(prev_d)
            continue
        current_k = (2.0 / 3.0) * prev_k + (1.0 / 3.0) * float(value)
        current_d = (2.0 / 3.0) * prev_d + (1.0 / 3.0) * current_k
        k_values.append(current_k)
        d_values.append(current_d)
        prev_k, prev_d = current_k, current_d

    k_series = pd.Series(k_values, index=close.index, dtype="float64")
    d_series = pd.Series(d_values, index=close.index, dtype="float64")
    j_series = 3.0 * k_series - 2.0 * d_series
    return k_series, d_series, j_series


def rsi(close: pd.Series, period: int = DEFAULT_RSI_PERIOD) -> pd.Series:
    """Wilder RSI。"""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, pd.NA)
    result = 100.0 - (100.0 / (1.0 + rs))
    # avg_loss 为 0（一路涨）时 RSI = 100
    return result.where(avg_loss != 0.0, 100.0).astype("float64")


def boll(
    close: pd.Series,
    period: int = DEFAULT_BOLL[0],
    num_std: float = DEFAULT_BOLL[1],
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """返回 (中轨, 上轨, 下轨)，使用总体标准差（ddof=0）。"""
    mid = close.rolling(window=period, min_periods=period).mean()
    std = close.rolling(window=period, min_periods=period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    return mid, upper, lower


def bias(close: pd.Series, period: int) -> pd.Series:
    ma = sma(close, period)
    return (close - ma) / ma * 100.0


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = DEFAULT_ATR_PERIOD,
) -> pd.Series:
    """平均真实波幅（Wilder 口径）。

    TR = max(high-low, |high-prev_close|, |low-prev_close|)
    ATR = TR 的 Wilder 平滑（α = 1/period）

    用途：为高波动标的提供**自适应止损**基准（止损 = close − k × ATR），
    避免固定技术位（如贴脸 MA5）在正常波动中被反复扫损。
    """
    prev_close = close.shift(1)
    true_range = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1.0 / max(1, int(period)), adjust=False).mean()


def resample_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """由日线聚合周线（按自然周，取每周最后一个交易日）。

    要求 df 含 ``date``(可转 datetime) 与 open/high/low/close/volume 列。
    """
    if df is None or df.empty:
        return pd.DataFrame()
    frame = df.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"]).sort_values("date")
    if frame.empty:
        return pd.DataFrame()
    indexed = frame.set_index("date")
    agg = {
        column: ("first" if column == "open" else "last" if column == "close" else "max" if column == "high" else "min" if column == "low" else "sum")
        for column in ("open", "high", "low", "close", "volume")
        if column in indexed.columns
    }
    if not agg:
        return pd.DataFrame()
    weekly = indexed.resample("W-FRI").agg(agg).dropna(subset=["close"]).reset_index()
    return weekly


# --------------------------------------------------------------------------- #
# 快照
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class IndicatorSnapshot:
    """一次性打包"最新 + 前一交易日"关键指标值。"""

    close: float
    prev_close: float
    ma: Dict[int, float] = field(default_factory=dict)
    prev_ma: Dict[int, float] = field(default_factory=dict)
    dif: float = float("nan")
    dea: float = float("nan")
    macd_hist: float = float("nan")
    prev_macd_hist: float = float("nan")
    k: float = float("nan")
    d: float = float("nan")
    j: float = float("nan")
    prev_k: float = float("nan")
    prev_d: float = float("nan")
    rsi: float = float("nan")
    prev_rsi: float = float("nan")
    boll_mid: float = float("nan")
    boll_upper: float = float("nan")
    boll_lower: float = float("nan")
    bias: Dict[int, float] = field(default_factory=dict)
    volume: float = float("nan")
    avg_volume_5: float = float("nan")
    high_20: float = float("nan")
    low_20: float = float("nan")
    atr: float = float("nan")
    as_of: Optional[str] = None

    # -- 便捷判定 ---------------------------------------------------------- #
    def ma_slope_up(self, period: int = 20) -> bool:
        prev = self.prev_ma.get(period)
        curr = self.ma.get(period)
        if prev is None or curr is None:
            return False
        return curr > prev

    def macd_golden_cross(self) -> bool:
        if any(pd.isna(v) for v in (self.prev_macd_hist, self.macd_hist)):
            return False
        return self.prev_macd_hist <= 0 < self.macd_hist

    def macd_dead_cross(self) -> bool:
        if any(pd.isna(v) for v in (self.prev_macd_hist, self.macd_hist)):
            return False
        return self.prev_macd_hist >= 0 > self.macd_hist

    def macd_hist_shrinking(self) -> bool:
        if any(pd.isna(v) for v in (self.prev_macd_hist, self.macd_hist)):
            return False
        return abs(self.macd_hist) < abs(self.prev_macd_hist)

    def kdj_golden_cross(self) -> bool:
        if any(pd.isna(v) for v in (self.prev_k, self.prev_d, self.k, self.d)):
            return False
        return self.prev_k <= self.prev_d and self.k > self.d

    def volume_ratio(self) -> float:
        if not self.avg_volume_5 or pd.isna(self.avg_volume_5) or self.avg_volume_5 <= 0:
            return float("nan")
        return self.volume / self.avg_volume_5

    def is_bullish_alignment(self) -> bool:
        ma5, ma10, ma20 = self.ma.get(5), self.ma.get(10), self.ma.get(20)
        if None in (ma5, ma10, ma20):
            return False
        return ma5 >= ma10 >= ma20


def build_indicator_snapshot(
    df: pd.DataFrame,
    *,
    ma_periods: Tuple[int, ...] = DEFAULT_MA_PERIODS,
    bias_periods: Tuple[int, ...] = DEFAULT_BIAS_PERIODS,
    macd_params: Tuple[int, int, int] = DEFAULT_MACD,
    kdj_params: Tuple[int, int, int] = DEFAULT_KDJ,
    rsi_period: int = DEFAULT_RSI_PERIOD,
    boll_params: Tuple[int, float] = DEFAULT_BOLL,
    atr_period: int = DEFAULT_ATR_PERIOD,
    min_bars: int = 21,
) -> IndicatorSnapshot:
    """从 OHLCV DataFrame 构建指标快照。

    df 需含 date/open/high/low/close（volume 可选）。数据不足 ``min_bars`` 抛
    ``InsufficientDataError``。
    """
    if df is None or df.empty:
        raise InsufficientDataError("empty data frame")
    required = ("open", "high", "low", "close")
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise InsufficientDataError(f"missing columns: {', '.join(missing)}")
    if len(df) < min_bars:
        raise InsufficientDataError(f"need >= {min_bars} bars, got {len(df)}")

    frame = df.reset_index(drop=True)
    close = pd.to_numeric(frame["close"], errors="coerce")
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    volume = (
        pd.to_numeric(frame["volume"], errors="coerce")
        if "volume" in frame.columns
        else pd.Series([float("nan")] * len(frame))
    )

    def _last(series: pd.Series, index: int = -1) -> float:
        try:
            value = float(series.iloc[index])
        except (IndexError, TypeError, ValueError):
            return float("nan")
        return value

    ma_series = {p: sma(close, p) for p in ma_periods}
    ma_latest = {p: _last(s) for p, s in ma_series.items()}
    ma_prev = {p: _last(s, -2) for p, s in ma_series.items()}

    dif, dea, hist = macd(close, *macd_params)
    k_series, d_series, j_series = kdj(high, low, close, *kdj_params)
    rsi_series = rsi(close, rsi_period)
    mid, upper, lower = boll(close, *boll_params)
    atr_series = atr(high, low, close, atr_period)
    bias_latest = {p: _last(bias(close, p)) for p in bias_periods}

    as_of: Optional[str] = None
    if "date" in frame.columns and len(frame) > 0:
        as_of = str(frame["date"].iloc[-1])[:10]

    return IndicatorSnapshot(
        close=_last(close),
        prev_close=_last(close, -2),
        ma=ma_latest,
        prev_ma=ma_prev,
        dif=_last(dif),
        dea=_last(dea),
        macd_hist=_last(hist),
        prev_macd_hist=_last(hist, -2),
        k=_last(k_series),
        d=_last(d_series),
        j=_last(j_series),
        prev_k=_last(k_series, -2),
        prev_d=_last(d_series, -2),
        rsi=_last(rsi_series),
        prev_rsi=_last(rsi_series, -2),
        boll_mid=_last(mid),
        boll_upper=_last(upper),
        boll_lower=_last(lower),
        bias=bias_latest,
        volume=_last(volume),
        avg_volume_5=_last(volume.rolling(window=5, min_periods=1).mean()),
        high_20=_last(high.rolling(window=20, min_periods=1).max()),
        low_20=_last(low.rolling(window=20, min_periods=1).min()),
        atr=_last(atr_series),
        as_of=as_of,
    )
