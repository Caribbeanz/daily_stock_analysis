# -*- coding: utf-8 -*-
"""估值历史与**只用过去**的分位（需求 §5.8 / DIV-3 的数据来源）。

为什么分位要「只用过去算」：`pe_percentile` / `pb_percentile` 会进评分（`w_valuation`）
和 DIV-3 风险（估值分位高位 + 价格位置高位）。若拿全样本算分位，回放到某一天时
等于用上了那天之后的数据——分位会平滑得不像当时能看到的东西。所以这里按交易日
逐日推进、只用 ≤T 的样本，与「不给未来数据」是同一条纪律。

口径（写清楚才敢让评分用）：

- 分位 = 「严格小于当日值的样本数 / (样本数 − 1)」，取值 0–1；样本全相同给 0.5。
- 样本不足 ``min_obs`` 根时**不给分位**（`None`），由评分层按「估值缺失」处理；
  硬塞一个基于 30 个点的分位，等于把噪声当信号。
- ``PE <= 0``（亏损）与 ``PB <= 0``（资不抵债）**不参与分位**：负值排序没有经济含义。
  值本身保留，纸面回看能看到「亏损」这个事实。
"""

from __future__ import annotations

import logging
from bisect import bisect_left, insort
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Sequence

from src.assistant.domain import ValuationView

__all__ = [
    "ValuationPoint",
    "ValuationRow",
    "fetch_valuation_history",
    "percentile_series",
    "valuation_view",
    "valuation_view_from_rows",
]

logger = logging.getLogger(__name__)

#: 分位最少样本数（约两年交易日）：需求要求首次回补 ≥3 年，样本更短时不给分位
MIN_OBS = 500


@dataclass(frozen=True)
class ValuationPoint:
    """一天的估值原值（未算分位）。"""

    trade_date: date
    pe: float | None = None
    pb: float | None = None


@dataclass(frozen=True)
class ValuationRow:
    """一天的估值 + 当日分位（分位只用 ≤该日的样本算出）。"""

    trade_date: date
    pe: float | None = None
    pb: float | None = None
    pe_pctile: float | None = None
    pb_pctile: float | None = None


def _to_day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN → None


def percentile_series(
    points: Sequence[ValuationPoint], *, min_obs: int = MIN_OBS
) -> list[ValuationRow]:
    """逐日推进算分位（只用 ≤该日的样本），返回与入参同长的行。"""
    ordered = sorted(points, key=lambda item: item.trade_date)
    pe_values: list[float] = []
    pb_values: list[float] = []
    out: list[ValuationRow] = []
    for point in ordered:
        pe = point.pe if (point.pe is not None and point.pe > 0) else None
        pb = point.pb if (point.pb is not None and point.pb > 0) else None
        if pe is not None:
            insort(pe_values, pe)
        if pb is not None:
            insort(pb_values, pb)
        out.append(
            ValuationRow(
                trade_date=point.trade_date,
                pe=point.pe,
                pb=point.pb,
                pe_pctile=_pctile(pe_values, pe, min_obs),
                pb_pctile=_pctile(pb_values, pb, min_obs),
            )
        )
    return out


def _pctile(sorted_values: Sequence[float], value: float | None, min_obs: int) -> float | None:
    if value is None or len(sorted_values) < max(2, min_obs):
        return None
    if sorted_values[0] == sorted_values[-1]:
        return 0.5
    rank = bisect_left(sorted_values, value)
    return round(rank / (len(sorted_values) - 1), 4)


def fetch_valuation_history(
    code: str, start: date, end: date
) -> tuple[list[ValuationPoint], str]:
    """取某只标的的 PE/PB 日序列，返回 ``(points, 来源名)``。

    主源东财 ``stock_value_em``（2018 起、每日 PE(TTM)/市净率，且当天就有数据）；
    取不到时退到百度 ``stock_zh_valuation_baidu``（只有近五年、且 PE/PB 要分两次请求）。
    两条都失败就是空列表——**不猜、不用市值反推**。
    """
    if start > end:
        raise ValueError(f"起始日 {start} 晚于结束日 {end}")
    points = _fetch_from_eastmoney(code)
    if points:
        return ([p for p in points if start <= p.trade_date <= end], "eastmoney:stock_value_em")
    points = _fetch_from_baidu(code)
    kept = [p for p in points if start <= p.trade_date <= end]
    return (kept, "baidu:stock_zh_valuation_baidu")


def _fetch_from_eastmoney(code: str) -> list[ValuationPoint]:
    import akshare as ak  # noqa: PLC0415

    try:
        frame = ak.stock_value_em(symbol=code)
    except Exception as exc:  # noqa: BLE001 - 主源失败只降级
        logger.warning("估值取数 %s 东财失败：%s: %s", code, type(exc).__name__, exc)
        return []
    if frame is None or getattr(frame, "empty", True):
        return []
    out: list[ValuationPoint] = []
    for row in frame.to_dict("records"):
        day = _to_day(row.get("数据日期"))
        if day is None:
            continue
        out.append(
            ValuationPoint(
                trade_date=day,
                pe=_to_float(row.get("PE(TTM)")),
                pb=_to_float(row.get("市净率")),
            )
        )
    return out


def _fetch_from_baidu(code: str) -> list[ValuationPoint]:
    import akshare as ak  # noqa: PLC0415

    merged: dict[date, dict[str, float | None]] = {}
    for indicator, key in (("市盈率(TTM)", "pe"), ("市净率", "pb")):
        try:
            frame = ak.stock_zh_valuation_baidu(symbol=code, indicator=indicator, period="近五年")
        except Exception as exc:  # noqa: BLE001
            logger.warning("估值取数 %s 百度 %s 失败：%s: %s", code, indicator, type(exc).__name__, exc)
            continue
        if frame is None or getattr(frame, "empty", True):
            continue
        for row in frame.to_dict("records"):
            day = _to_day(row.get("date"))
            if day is None:
                continue
            merged.setdefault(day, {})[key] = _to_float(row.get("value"))
    return [
        ValuationPoint(trade_date=day, pe=parts.get("pe"), pb=parts.get("pb"))
        for day, parts in sorted(merged.items())
    ]


def valuation_view_from_rows(
    rows: Sequence[ValuationRow], day: date
) -> ValuationView | None:
    """取 ``trade_date <= day`` 的最近一条，转成评分用的 ``ValuationView``。

    **当日没有估值行时取上一条**（估值按收盘更新，T 日盘后未必已入库），但只回退
    不超过 5 个自然日的记录，更早的按缺失处理——拿三个月前的分位当今天用，比缺失更误导。
    """
    best: ValuationRow | None = None
    for row in rows:
        if row.trade_date <= day and (best is None or row.trade_date > best.trade_date):
            best = row
    if best is None or (day - best.trade_date).days > 5:
        return None
    return ValuationView(pe_percentile=best.pe_pctile, pb_percentile=best.pb_pctile)


def valuation_view(
    rows: Sequence[ValuationRow],
    events: Iterable[Any] | None,
    day: date,
    last_close: float | None,
) -> ValuationView:
    """估值分位 + 分红事实合成一份 ``ValuationView``（评分与风险共用）。

    分红部分调用 :func:`src.assistant.data.dividend.dividend_facts`，只用 ``ex_date <= day``
    的事件；两者都缺时返回空视图（`is_missing` 为真），评分与风险各自按「缺失」注记。
    """
    from src.assistant.data.dividend import dividend_facts  # noqa: PLC0415 - 避免循环导入

    base = valuation_view_from_rows(rows, day) or ValuationView()
    facts = dividend_facts(list(events or ()), day, last_close)
    return ValuationView(
        pe_percentile=base.pe_percentile,
        pb_percentile=base.pb_percentile,
        dividend_yield=facts.dividend_yield,
        dividend_yield_percentile=None,
        dividend_continuity_years=facts.continuity_years,
        dividend_interrupted=facts.interrupted,
        ex_dividend_date=facts.ex_dividend_date,
    )
