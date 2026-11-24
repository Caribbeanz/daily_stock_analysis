# -*- coding: utf-8 -*-
"""测试用的确定性行情构造器（无随机、无网络）。"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Sequence

from src.assistant.config.overrides import ParamSet, default_param_set
from src.assistant.domain import Bar, PortfolioView, PositionView, StockView
from src.assistant.enums import SourceTag

START = date(2026, 5, 4)


def make_bars(closes: Sequence[float], volumes: Sequence[float] | None = None, start: date = START) -> list[Bar]:
    """按收盘序列生成 K 线（开高低由收盘派生，保证 high ≥ max(open, close)）。"""
    bars: list[Bar] = []
    day = start
    for idx, close in enumerate(closes):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        volume = volumes[idx] if volumes else 1_000_000.0
        bars.append(
            Bar(
                trade_date=day,
                open=round(close * 0.998, 4),
                high=round(close * 1.006, 4),
                low=round(close * 0.994, 4),
                close=round(close, 4),
                volume=volume,
            )
        )
        day += timedelta(days=1)
    return bars


def trending(n: int = 150, drift: float = 0.005, base: float = 10.0) -> list[float]:
    """单边趋势（drift 为正=上升，为负=下跌），带轻微正弦扰动。"""
    out: list[float] = []
    close = base
    for i in range(n):
        close *= 1 + drift + 0.0015 * math.sin(i)
        out.append(round(close, 2))
    return out


def boxed(n: int = 150, low: float = 100.0, high: float = 108.0, last_at: str = "high") -> list[float]:
    """箱体震荡：10 日一个周期，3 日贴下沿 / 3 日贴上沿 / 4 日在中间。"""
    out: list[float] = []
    for i in range(n):
        phase = i % 10
        if phase < 3:
            out.append(low)
        elif phase < 6:
            out.append(high)
        else:
            out.append(round((low + high) / 2, 2))
    if not out:
        return out
    if last_at == "low":
        out[-3:] = [low + 0.5, low + 0.2, low + 0.1]
    elif last_at == "high":
        out[-3:] = [high - 0.1, high - 0.05, high]
    return out


def wide_oscillation(n: int = 200, center: float = 115.0, amplitude: float = 15.0, period: int = 20) -> list[float]:
    """宽幅震荡：箱体宽度远超 12%，MA20 走平 → 应落 S5（无结构）。"""
    return [round(center + amplitude * math.sin(2 * math.pi * i / period), 2) for i in range(n)]


def make_view(
    closes: Sequence[float],
    *,
    code: str = "600000",
    name: str = "测试标的",
    holding: int = 0,
    cost_basis: float | None = None,
    volumes: Sequence[float] | None = None,
    source_tag: SourceTag = SourceTag.D1,
) -> StockView:
    bars = make_bars(closes, volumes)
    position = PositionView(shares=holding, cost_basis=cost_basis if cost_basis is not None else closes[-1] * 0.95)
    return StockView(code=code, name=name, bars=bars, position=position, source_tag=source_tag)


def make_portfolio(total: float = 500_000.0, cash: float = 400_000.0, holding_value: float = 0.0) -> PortfolioView:
    return PortfolioView(total_capital=total, cash_available=cash, holding_market_value=holding_value)


def std_params() -> ParamSet:
    return default_param_set()


def now() -> datetime:
    return datetime(2026, 11, 24, 18, 0, 0)
