# -*- coding: utf-8 -*-
"""板块强度三档（需求 §5.1 / §4.6 口径，D-20 剩余部分）。

需求对板块层有两条互相约束的硬规则，这里逐条落地：

1. **单日值不得判档**（§4.6 硬约束）：``sector_strength`` 必须用**多日序列** + 同款 2×2
   框架判档。所以入参是板块指数日线，不是当日涨跌幅。序列不足 ``min_valid_bars`` 时
   返回 ``中性 + degraded``——按 §5.10「数据 degraded → 注记 + 置信度下调，档位不变」。
2. **``rs_t`` 不得进评分**（§4.6 硬约束）：它是「板块当日涨跌 − 大盘当日涨跌」的单日
   算术差，只做展示分档（:attr:`SectorStrength.rs_label`），既不参与三档，也不回传评分层。

与大盘 M1 是**同款框架、独立实现**：需求 §4.6 明确「板块层 2×2 为板块独立规则，不复用
指数函数实现」，因此这里自带阈值与参数对象，方便板块单独调参而不牵动大盘。

- 因子 A（收盘 vs MA）：``close > MA×(1+eps)`` → above；``close < MA×(1−eps)`` → below；
  缓冲带 → near，沿用最近一个**非 near 交易日**的有效档，没有则降级。
- 因子 B（MA 斜率）：``MA_t / MA_{t−k} − 1 > theta`` → up，否则 not_up（严格二分，
  走平归 not_up，四格闭合）。
- 象限 → 三档：Q1(above,up)=偏强、Q4(below,not_up)=偏弱、Q2/Q3=中性。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from src.assistant.domain import Bar

__all__ = [
    "STRENGTH_NEUTRAL",
    "STRENGTH_STRONG",
    "STRENGTH_WEAK",
    "SectorStrength",
    "SectorStrengthParams",
    "rs_label_from_gap",
    "sector_strength_from_bars",
]

#: 三档取值（与评分层 `sector_strength` 口径一致，全局只有这三个字面量）
STRENGTH_STRONG = "偏强"
STRENGTH_NEUTRAL = "中性"
STRENGTH_WEAK = "偏弱"


@dataclass(frozen=True)
class SectorStrengthParams:
    """板块 2×2 阈值（初值；板块独立调参，不牵动大盘 M1）。"""

    ma_window: int = 20
    slope_lookback_k: int = 5
    slope_theta: float = 0.0
    ma_buffer_eps: float = 0.005
    min_valid_bars: int = 26  # ma_window + slope_lookback_k + 1
    #: ``|rs_t|`` 超过该值才分「强于/弱于大盘」档，否则算同步；仅展示用
    rs_band: float = 1.0

    def __post_init__(self) -> None:
        if self.ma_window < 2:
            raise ValueError("ma_window 至少为 2")
        if self.slope_lookback_k < 1:
            raise ValueError("slope_lookback_k 至少为 1")
        if self.min_valid_bars < self.ma_window:
            raise ValueError("min_valid_bars 不能小于 ma_window")


@dataclass(frozen=True)
class SectorStrength:
    """板块强度判定结果（只描述结构，不给买卖建议：需求 NG-7）。"""

    strength: str = STRENGTH_NEUTRAL
    #: 板块当日涨跌 − 大盘当日涨跌（百分点），单日算术差，仅展示
    rs_t: float | None = None
    #: ``rs_t`` 的展示分档
    rs_label: str = ""
    #: 2×2 因子取值，如 ``"above+up"``，用于留痕与解释
    factors: str = ""
    #: 序列最后一根的交易日（板块指数常比一览晚一天发布，展示层据此标「截至 MM-DD」）
    as_of: "date | None" = None
    degraded: bool = False
    note: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "strength": self.strength,
            "rs_t": self.rs_t,
            "rs_label": self.rs_label,
            "factors": self.factors,
            "as_of": self.as_of,
            "degraded": self.degraded,
            "note": self.note,
        }


def rs_label_from_gap(gap: float | None, *, band: float = 1.0) -> str:
    """相对大盘单日差 → 展示分档；**不参与三档判定**（§4.6 硬约束）。"""
    if gap is None:
        return ""
    if gap >= band:
        return "强于大盘"
    if gap <= -band:
        return "弱于大盘"
    return "同步"


def _ma(closes: Sequence[float], end: int, window: int) -> float | None:
    start = end - window
    if start < 0:
        return None
    window_values = closes[start:end]
    if len(window_values) != window:
        return None
    return sum(window_values) / window


def _side_of(close: float, ma: float, eps: float) -> str | None:
    if close > ma * (1 + eps):
        return "above"
    if close < ma * (1 - eps):
        return "below"
    return None  # near：缓冲带，沿用历史有效档


def _regime_of(side: str, slope: str) -> str:
    if side == "above" and slope == "up":
        return STRENGTH_STRONG
    if side == "below" and slope == "not_up":
        return STRENGTH_WEAK
    return STRENGTH_NEUTRAL


def _ordered_valid(bars: Sequence[Bar]) -> list[Bar] | None:
    """按日期升序、收盘价有效的日线；任一非有限值即整体不可用（不猜、不补值）。"""
    ordered = sorted(bars, key=lambda item: item.trade_date)
    for bar in ordered:
        if not math.isfinite(bar.close) or bar.close <= 0:
            return None
    return list(ordered)


def sector_strength_from_bars(
    bars: Sequence[Bar],
    *,
    pct_change: float | None = None,
    market_pct: float | None = None,
    as_of_day: date | None = None,
    params: SectorStrengthParams | None = None,
) -> SectorStrength:
    """板块指数日线 → 三档；板块当日涨跌幅 + 大盘当日涨跌幅 → ``rs_t``。

    两个入参分工是刻意的（2026-11-26 实测）：同花顺**板块一览有当日涨跌幅，板块指数
    日线却晚一天发布**。若用序列自己算当日差，``rs_t`` 会变成昨日的差、甚至与今日涨跌
    反向。因此：

    - ``rs_t`` 只认调用方给的**当日** ``pct_change``（板块一览）与 ``market_pct``（指数），
      两者缺一就不给值，**不拿序列末日顶替**；
    - 三档只认**多日序列**，不用当日涨跌幅判档（需求 §4.6）；
    - ``as_of_day`` 用来判断序列是否滞后：``bars`` 末日早于它即 ``degraded`` 并在
      ``note`` 写明「截至 MM-DD」，档位照旧但明确标注按上一交易日。

    序列不足、含非有限值、落在缓冲带且无历史有效档时一律 ``degraded=True`` + ``中性``。
    """
    cfg = params or SectorStrengthParams()
    ordered = _ordered_valid(bars)
    if ordered is None:
        return SectorStrength(degraded=True, note="板块指数含无效收盘价")

    closes = [bar.close for bar in ordered]
    rs_t = None if (pct_change is None or market_pct is None) else pct_change - market_pct
    rs_label = rs_label_from_gap(rs_t, band=cfg.rs_band)
    as_of = ordered[-1].trade_date if ordered else None
    lag_note = ""
    if as_of is not None and as_of_day is not None and as_of < as_of_day:
        lag_note = f"板块指数截至 {as_of:%m-%d}，当日未发布"

    if len(closes) < cfg.min_valid_bars:
        return SectorStrength(
            rs_t=rs_t,
            rs_label=rs_label,
            as_of=as_of,
            degraded=True,
            note=f"板块序列不足 {cfg.min_valid_bars} 根（现有 {len(closes)} 根），本次不判档"
            + (f"；{lag_note}" if lag_note else ""),
        )

    side: str | None = None
    slope: str | None = None
    for end in range(len(closes), cfg.ma_window - 1, -1):
        ma_t = _ma(closes, end, cfg.ma_window)
        ma_prev = _ma(closes, end - cfg.slope_lookback_k, cfg.ma_window)
        if ma_t is None or ma_prev is None or ma_t <= 0:
            continue
        current_side = _side_of(closes[end - 1], ma_t, cfg.ma_buffer_eps)
        if current_side is None:
            continue  # near：往前找最近一个有效档；找不到就降级
        side = current_side
        slope = "up" if (ma_t / ma_prev - 1) > cfg.slope_theta else "not_up"
        break

    if side is None or slope is None:
        return SectorStrength(
            rs_t=rs_t,
            rs_label=rs_label,
            as_of=as_of,
            degraded=True,
            note="价格落在 MA 缓冲带且无历史有效档" + (f"；{lag_note}" if lag_note else ""),
        )

    return SectorStrength(
        strength=_regime_of(side, slope),
        rs_t=rs_t,
        rs_label=rs_label,
        factors=f"{side}+{slope}",
        as_of=as_of,
        degraded=bool(lag_note),
        note=lag_note,
    )
