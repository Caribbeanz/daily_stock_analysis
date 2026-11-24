# -*- coding: utf-8 -*-
"""技术指标计算（L2 派生层，**纯函数 + 不落库**）。

设计约束：
- 只接受**不复权**原始价（需求 §4.6.4）；需要连续序列时用 :attr:`Bar.post_adj_close`，
  由调用方显式传入，避免「悄悄用前复权」。
- 全部为确定性计算，无随机、无 LLM、无网络；同一输入必得同一输出（DP-5）。
- 阈值一律来自 :class:`~src.assistant.config.overrides.ParamSet`，不在此处硬编码。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from src.assistant.domain import Bar, KeyLevel


def sma(values: Sequence[float], window: int) -> list[float | None]:
    """简单移动平均，前 ``window-1`` 位为 ``None``。"""
    if window <= 0:
        raise ValueError(f"window 必须为正：{window}")
    out: list[float | None] = []
    running = 0.0
    for idx, val in enumerate(values):
        running += val
        if idx >= window:
            running -= values[idx - window]
        out.append(running / window if idx >= window - 1 else None)
    return out


def relative_slope(series: Sequence[float], k: int) -> float:
    """日均相对斜率：``(v[t] - v[t-k]) / (v[t-k] × k)``。

    θ=0.003 即「日均 0.3%」，与需求 §5.14.2 的量级一致。
    序列不足 ``k+1`` 位或基准为 0 时返回 ``0.0``（视作走平，交给 S5 兜底，不编造方向）。
    """
    if k <= 0:
        raise ValueError(f"k 必须为正：{k}")
    if len(series) < k + 1:
        return 0.0
    base = series[-1 - k]
    if base == 0:
        return 0.0
    return (series[-1] - base) / (base * k)


def percentile_rank(values: Sequence[float], current: float) -> float:
    """``current`` 在窗口内的分位（0–1，按「≤ 当前值」的占比）。"""
    if not values:
        raise ValueError("分位计算需要非空序列")
    hits = sum(1 for val in values if val <= current)
    return hits / len(values)


def average(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("平均值计算需要非空序列")
    return sum(values) / len(values)


def ma_alignment_score(closes: Sequence[float], ma_window: int) -> int:
    """均线排列同向条数（0–3）：MA5 > MA10、MA10 > MA20、MA5 > MA20 各计 1 分。

    需求 §5.14.2「均线排列 MA5>MA10>MA20（至少 2 项成立）」——用条数而非布尔值，
    便于置信度按「同向证据条数」分级。
    """
    if len(closes) < ma_window:
        return 0
    ma5 = average(closes[-5:]) if len(closes) >= 5 else None
    ma10 = average(closes[-10:]) if len(closes) >= 10 else None
    ma20 = average(closes[-ma_window:])
    score = 0
    if ma5 is not None and ma10 is not None and ma5 > ma10:
        score += 1
    if ma10 is not None and ma10 > ma20:
        score += 1
    if ma5 is not None and ma5 > ma20:
        score += 1
    return score


@dataclass(frozen=True)
class Box:
    """箱体（≈ 简化中枢，需求 §5.14.4）。"""

    high: float
    low: float
    width: float
    up_touches: int
    down_touches: int
    lookback: int
    width_limit: float
    valid: bool

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2

    def as_dict(self) -> dict[str, float | int | bool]:
        return {
            "high": self.high,
            "low": self.low,
            "width": round(self.width, 6),
            "up_touches": self.up_touches,
            "down_touches": self.down_touches,
            "lookback": self.lookback,
            "width_limit": self.width_limit,
            "valid": self.valid,
        }


def detect_box(
    bars: Sequence[Bar],
    *,
    lookback: int,
    width_max: float,
    touch_min: int,
    touch_tol: float,
) -> Box | None:
    """箱体判定：宽度 ≤ 上限 且 上下沿各触碰 ≥ 次数（需求 §5.14.2）。

    返回 ``None`` 表示序列不足；返回 ``valid=False`` 表示有箱体形状但不成立（→ 归 S5）。
    """
    if len(bars) < 2:
        return None
    window = list(bars[-lookback:]) if lookback > 0 else list(bars)
    if len(window) < 2:
        return None
    box_high = max(bar.high for bar in window)
    box_low = min(bar.low for bar in window)
    if box_low <= 0:
        return None
    width = (box_high - box_low) / box_low
    up_tol = box_high * (1 - touch_tol)
    down_tol = box_low * (1 + touch_tol)
    up_touches = sum(1 for bar in window if bar.high >= up_tol)
    down_touches = sum(1 for bar in window if bar.low <= down_tol)
    valid = width <= width_max and up_touches >= touch_min and down_touches >= touch_min
    return Box(
        high=box_high,
        low=box_low,
        width=width,
        up_touches=up_touches,
        down_touches=down_touches,
        lookback=len(window),
        width_limit=width_max,
        valid=valid,
    )


def price_position(bars: Sequence[Bar], window: int) -> float:
    """``pos20``：最新收盘在近 N 日收盘区间内的分位（需求 §5.14.2 二分依据）。"""
    closes = [bar.close for bar in bars[-window:]]
    return percentile_rank(closes, closes[-1])


def build_key_levels(
    bars: Sequence[Bar],
    *,
    ma: float | None,
    box: Box | None,
    lookback: int,
    touch_tol: float,
) -> tuple[KeyLevel, ...]:
    """生成关键价位（需求 §5.3：支撑 1–2 / 压力 1–2 / 失效位 1，**每个都带来源**）。

    去重策略：同一价格的多个来源合并为一条，``source`` 用「+」连接，保留可追溯性。
    """
    if not bars:
        return ()
    close = bars[-1].close
    window = list(bars[-lookback:]) if lookback > 0 else list(bars)
    recent_high = max(bar.high for bar in window)
    recent_low = min(bar.low for bar in window)

    raw: list[tuple[float, str, str, dict[str, float]]] = []
    if box is not None and box.valid:
        raw.append((box.high, "resistance", "箱体上沿", {"box_high": box.high, "window": box.lookback}))
        raw.append((box.low, "support", "箱体下沿", {"box_low": box.low, "window": box.lookback}))
    raw.append((recent_high, "resistance", "近 N 日最高", {"window": len(window)}))
    raw.append((recent_low, "support", "近 N 日最低", {"window": len(window)}))
    if ma is not None and ma > 0:
        kind = "support" if ma <= close else "resistance"
        raw.append((ma, kind, "MA20 折算", {"ma_window": len(window)}))

    merged: dict[tuple[str, float], KeyLevel] = {}
    for price, kind, source, detail in raw:
        if price <= 0:
            continue
        key = (kind, round(price, 2))
        if key in merged:
            prev = merged[key]
            merged[key] = KeyLevel(
                price=prev.price,
                kind=kind,
                source=f"{prev.source}+{source}",
                detail={**dict(prev.detail), **detail},
            )
        else:
            merged[key] = KeyLevel(price=round(price, 2), kind=kind, source=source, detail=detail)

    # 失效位：箱体成立用箱体下沿，否则用近 N 日最低（均带容差）
    invalid_base = box.low if (box is not None and box.valid) else recent_low
    invalid = round(invalid_base * (1 - touch_tol), 2)
    levels = list(merged.values())
    levels.append(
        KeyLevel(
            price=invalid,
            kind="invalid",
            source="箱体下沿-容差" if (box is not None and box.valid) else "近 N 日最低-容差",
            detail={"base": invalid_base, "tol": touch_tol},
        )
    )

    # 排序：支撑由高到低（离现价近的在前）、压力由低到高，失效位垫底
    supports = sorted((lv for lv in levels if lv.kind == "support"), key=lambda lv: -lv.price)
    resists = sorted((lv for lv in levels if lv.kind == "resistance"), key=lambda lv: lv.price)
    invalid_levels = [lv for lv in levels if lv.kind == "invalid"]
    return tuple(supports[:2] + resists[:2] + invalid_levels)


def returns_pct(closes: Sequence[float], days: int) -> float:
    """N 日涨跌幅（``closes[-1]/closes[-1-days] - 1``）；不足返回 0。"""
    if days <= 0 or len(closes) <= days:
        return 0.0
    base = closes[-1 - days]
    if base == 0:
        return 0.0
    return closes[-1] / base - 1


def consecutive(predicates: Iterable[bool]) -> int:
    """从末尾往前数，连续满足的条数。"""
    count = 0
    for flag in reversed(list(predicates)):
        if not flag:
            break
        count += 1
    return count
