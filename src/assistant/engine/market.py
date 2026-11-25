# -*- coding: utf-8 -*-
"""大盘一句（D-16）：指数日线 → 涨跌幅 + M1 2×2 环境档。

只做需求 §5.1「环境评级 M1」里**指数日线能独立算的部分**：

- 因子 A（价格相对 MA）：``close > MA×(1+eps)`` → above；``close < MA×(1−eps)``
  → below；缓冲带 → near（沿用最近一个**非 near 交易日**的有效档，没有则降级）。
- 因子 B（均线斜率）：``MA_t / MA_{t-k} − 1 > theta`` → up，否则 not_up（严格二分，
  走平归 not_up，四格闭合）。
- 象限 → 三档：Q1(above,up)=偏强、Q4(below,not_up)=偏弱、Q2/Q3=中性。
- 量能只做确认标注，**不进硬判定**——这里没有展示确认位，因此不输出它。
- 市场宽度/风格/指数共振属于别的数据能力，缺失时**只减少展示项**，不编造档位。

数据不足（序列长度不够、非有限值、near 且无历史档）一律返回 ``available=False``，
由渲染层写「大盘数据缺失（本次不判断环境档）」——**不猜档**。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Sequence

from src.assistant.domain import Bar, MarketLine

__all__ = ["MarketRegimeParams", "market_line_from_bars"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketRegimeParams:
    """M1 阈值（初值；纸面回看确定后可改，数值进 `calc_version`）。"""

    ma_window: int = 20
    slope_lookback_k: int = 5
    slope_theta: float = 0.0
    ma_buffer_eps: float = 0.005
    min_valid_bars: int = 26  # ma_window + slope_lookback_k + 1

    def __post_init__(self) -> None:
        if self.ma_window < 2:
            raise ValueError("ma_window 至少为 2")
        if self.slope_lookback_k < 1:
            raise ValueError("slope_lookback_k 至少为 1")
        if self.min_valid_bars < self.ma_window:
            raise ValueError("min_valid_bars 不能小于 ma_window")


def _ma(closes: Sequence[float], end: int, window: int) -> float | None:
    """``closes`` 前闭后开区间 ``[end-window, end)`` 的均值。"""
    start = end - window
    if start < 0:
        return None
    window_values = closes[start:end]
    if len(window_values) != window:
        return None
    return sum(window_values) / window


def _factor_a(close: float, ma: float, eps: float) -> str | None:
    if close > ma * (1 + eps):
        return "above"
    if close < ma * (1 - eps):
        return "below"
    return None  # 缓冲带：near，沿用历史有效档


def _quadrant_regime(side: str, slope: str) -> str:
    if side == "above" and slope == "up":
        return "偏强"
    if side == "below" and slope == "not_up":
        return "偏弱"
    return "中性"  # Q2 / Q3


def _valid_closes(bars: Sequence[Bar]) -> list[Bar] | None:
    """按日期升序、收盘价有效的 K 线；任一非有限值即整体不可用（不猜、不补值）。"""
    ordered = sorted(bars, key=lambda item: item.trade_date)
    for bar in ordered:
        if not math.isfinite(bar.close) or bar.close <= 0:
            return None
    return list(ordered)


def market_line_from_bars(
    bars: Sequence[Bar],
    *,
    index_name: str = "上证指数",
    params: MarketRegimeParams | None = None,
) -> MarketLine:
    """从指数日线算出一句大盘描述；数据不足返回 ``available=False``。

    ``bars`` 允许包含请求日之后的行（调用方按 end 取数时不会），这里不使用未来数据：
    只按交易日升序取全部行的最后一根作为 t。
    """
    cfg = params or MarketRegimeParams()
    ordered = _valid_closes(bars)
    if ordered is None or len(ordered) < cfg.min_valid_bars:
        return MarketLine(available=False)

    closes = [bar.close for bar in ordered]
    n = len(closes)

    # 从 t 往前找最近一个非 near 的交易日，作为「沿用上一交易日有效档」的来源
    side: str | None = None
    slope: str | None = None
    regime = ""
    for end in range(n, cfg.ma_window - 1, -1):
        ma_t = _ma(closes, end, cfg.ma_window)
        ma_prev = _ma(closes, end - cfg.slope_lookback_k, cfg.ma_window)
        if ma_t is None or ma_prev is None or ma_t <= 0:
            continue
        current_side = _factor_a(closes[end - 1], ma_t, cfg.ma_buffer_eps)
        if current_side is None:
            continue  # near：继续往前找有效档；找不到就降级
        current_slope = "up" if (ma_t / ma_prev - 1) > cfg.slope_theta else "not_up"
        side, slope = current_side, current_slope
        regime = _quadrant_regime(side, slope)
        break

    if side is None or slope is None or not regime:
        return MarketLine(available=False)

    pct_change = (closes[-1] / closes[-2] - 1) * 100 if n >= 2 else None
    return MarketLine(
        index_name=index_name,
        pct_change=pct_change,
        regime=regime,
        available=True,
    )
