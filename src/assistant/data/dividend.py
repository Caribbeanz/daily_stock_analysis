# -*- coding: utf-8 -*-
"""分红送转与「含分红」价格序列（内存派生，不落库）。

**为什么需要它**：回放用 L1 不复权日线（需求 §4.6.4），而红利类标的每年除权 1–2 次。
不复权序列在除权日会凭空跌掉一个股息，后果有两层：

1. **收益被低估**：股息是真实现金流入，序列里看不见；
2. **止损被误触发**：「跌了 6%」其实只是除权，却按结构位/比例止损打出去——
   对高股息标的（本样本里吉比特 10 派 60 元）这一条足以把结论带偏。

做法是把除权事件折算成**乘在除权日之前所有 K 线上的因子**（即前复权口径，锚在最新价）：

    m = (P - c) / (k * (P - c * t))

- ``P`` 除权前一交易日收盘价（原始价）
- ``c`` 每股税前现金分红（元/股）
- ``k = 1 + 每股送股 + 每股转增``
- ``t`` 红利税率（0 = 税前全额）

推导：除权理论价 ``P_ex = (P - c) / k``；持有 1 股的财富从 ``P`` 变成
``k * P_ex + c * (1 - t) = P - c * t``；要让派生序列的跨除权日涨幅等于这个财富比，
就只能把**除权日之前**的价格统一乘 ``m``。``t = 0`` 时退化成标准前复权因子
``(P - c) / (k * P)``。

**不落库**：派生序列按存储层硬约束 §3.3 第 3 条只在内存里存在；
落库的只有原始事件（除权日 / 每股派息 / 送转比例）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

from src.assistant.domain import Bar

__all__ = [
    "AdjustOutcome",
    "DividendEvent",
    "DividendFacts",
    "apply_total_return",
    "dividend_facts",
    "fetch_dividend_events",
    "parse_dividend_rows",
]

logger = logging.getLogger(__name__)

_EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}

#: 东财「分红送配」里状态为「实施分配」的才算真除过权
_IMPLEMENTED = "实施分配"


@dataclass(frozen=True)
class DividendEvent:
    """一次除权除息。

    比例均为**每股**口径（接口给的是「每 10 股」，解析时已除以 10）。
    """

    code: str
    ex_date: date
    cash_per_share: float = 0.0
    bonus_per_share: float = 0.0
    transfer_per_share: float = 0.0
    plan: str = ""
    #: 股权登记日（有就存，仅用于审计核对，不参与折算）
    record_date: date | None = None

    @property
    def share_factor(self) -> float:
        """送转后股数倍数（10 送 3 转 2 → 1.5）。"""
        return 1.0 + self.bonus_per_share + self.transfer_per_share

    @property
    def is_material(self) -> bool:
        """有现金分红或送转才算一次真事件。"""
        return self.cash_per_share > 0 or (self.bonus_per_share + self.transfer_per_share) > 0

    def as_row(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "ex_date": self.ex_date,
            "cash_per_share": self.cash_per_share,
            "bonus_per_share": self.bonus_per_share,
            "transfer_per_share": self.transfer_per_share,
            "plan": self.plan,
            "record_date": self.record_date,
        }


@dataclass(frozen=True)
class AdjustOutcome:
    """派生结果：调整后的 K 线 + 事件处理计数（不猜、不静默丢）。"""

    bars: tuple[Bar, ...]
    applied: int = 0
    #: 除权日在序列首日之前：序列里没有需要调整的 K 线，属正常情况而非失败
    before_history: int = 0
    #: 除权日有、但序列里找不到它之前那根 K 线（L1 缺数据）——如实计数，不猜价格
    skipped: int = 0
    #: 本次折算掉的每股现金分红合计（仅统计真正应用了的事件）
    cash_per_share: float = 0.0


@dataclass(frozen=True)
class DividendFacts:
    """分红事实（进 ``ValuationView``：评分的 `w_dividend` 与 DIV-1/2 风险共用）。

    口径都写在这里，避免「连续 5 年」在不同地方有不同数法：

    - **连续年数**按**完整年度**数（``day.year - 1`` 起往前），当年未实施完的分红
      不参与——否则每年 1–6 月会把「还没到除权日」误判成中断；
    - **中断**只有在「事件覆盖到了那一年、而那年确实没有现金分红」时才成立；
      覆盖不足一律 ``None``（不猜），由风险层按「数据不足」注记；
    - **股息率**是近 365 天每股现金分红之和 ÷ 最新收盘价（百分数，与主项目
      ``ttm_dividend_yield_pct`` 同单位），不做税费调整（税前口径）。
    """

    continuity_years: int | None = None
    interrupted: bool = False
    #: 未来最近的除权日（事件表里有才算；东财实施分配表通常只有已实施记录）
    ex_dividend_date: date | None = None
    ttm_cash_per_share: float = 0.0
    #: 百分比（5.2 表示 5.2%）
    dividend_yield: float | None = None
    #: 事件覆盖起点，便于核对「为什么判不了连续 N 年」
    covered_from: date | None = None
    note: str = ""

    @property
    def has_data(self) -> bool:
        return (
            self.continuity_years is not None
            or self.interrupted
            or self.dividend_yield is not None
            or self.ex_dividend_date is not None
        )


def dividend_facts(
    events: Iterable["DividendEvent"],
    day: date,
    last_close: float | None,
    *,
    window: int = 10,
) -> DividendFacts:
    """由除权事件算分红事实；**只用 ``ex_date <= day`` 的事件**（不碰未来）。"""
    past = sorted((e for e in events if e.ex_date <= day), key=lambda e: e.ex_date)
    upcoming = sorted((e for e in events if e.ex_date > day), key=lambda e: e.ex_date)
    covered_from = past[0].ex_date if past else None
    cash = [e for e in past if e.cash_per_share > 0]

    continuity: int | None = None
    interrupted = False
    note = ""
    last_full = day.year - 1
    if not cash:
        if covered_from is None:
            note = "无除权事件记录（按无分红处理）"
        else:
            interrupted = True
            continuity = 0
            note = f"事件覆盖自 {covered_from.isoformat()}，其间无现金分红"
    elif last_full in {e.ex_date.year for e in cash}:
        years = {e.ex_date.year for e in cash}
        count = 0
        year = last_full
        while year in years and count < window:
            count += 1
            year -= 1
        continuity = count
        if count >= window:
            note = f"连续分红 ≥{window} 年（窗口上限）"
        elif covered_from is not None and covered_from.year <= year:
            note = f"{year} 年无现金分红（断点）"
        else:
            note = f"事件覆盖自 {covered_from.year if covered_from else '未知'} 年，连续年数为下界"
    elif covered_from is not None and covered_from.year <= last_full:
        interrupted = True
        continuity = 0
        note = f"{last_full} 年无现金分红"
    else:
        note = "除权事件覆盖不足，无法判断连续性（不猜）"

    ttm_cash = sum(e.cash_per_share for e in cash if (day - e.ex_date).days <= 365)
    yield_pct = None
    if ttm_cash > 0 and last_close and last_close > 0:
        yield_pct = round(ttm_cash / last_close * 100, 4)
    return DividendFacts(
        continuity_years=continuity,
        interrupted=interrupted,
        ex_dividend_date=upcoming[0].ex_date if upcoming else None,
        ttm_cash_per_share=round(ttm_cash, 6),
        dividend_yield=yield_pct,
        covered_from=covered_from,
        note=note,
    )


def _to_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _per_share(value: Any) -> float:
    """接口给的「每 10 股」数值 → 每股。"""
    try:
        return max(0.0, float(value) / 10.0)
    except (TypeError, ValueError):
        return 0.0


def parse_dividend_rows(code: str, rows: Iterable[Mapping[str, Any]]) -> list[DividendEvent]:
    """把东财 ``RPT_SHAREBONUS_DET`` 的原始行解析成事件列表（按除权日升序）。

    只认 ``ASSIGN_PROGRESS == 实施分配`` 且除权日可解析的行：预案、股东大会通过但
    尚未实施的分红**不算**——那些价格还没除权，折进去等于凭空多给一笔股息。
    """
    events: list[DividendEvent] = []
    for row in rows:
        if str(row.get("ASSIGN_PROGRESS") or "") != _IMPLEMENTED:
            continue
        ex_date = _to_date(row.get("EX_DIVIDEND_DATE"))
        if ex_date is None:
            continue
        # 接口字段含义实测（2026-11-26，20 只样本逐条与 IMPL_PLAN_PROFILE 交叉核对）：
        #   ``BONUS_IT_RATIO`` = **送转合计**（每 10 股），``IT_RATIO`` = 其中**转增**部分。
        # 直接把两个字段相加会把「纯转增」算成双倍（10 转 3 派 7.37 → 因子 1.6 而非 1.3），
        # 因此这里先取合计再拆出送股：送 = 合计 - 转增。
        total_share_ratio = _per_share(row.get("BONUS_IT_RATIO"))
        transfer_ratio = min(total_share_ratio, _per_share(row.get("IT_RATIO")))
        event = DividendEvent(
            code=str(row.get("SECURITY_CODE") or code).zfill(6),
            ex_date=ex_date,
            cash_per_share=_per_share(row.get("PRETAX_BONUS_RMB")),
            bonus_per_share=max(0.0, total_share_ratio - transfer_ratio),
            transfer_per_share=transfer_ratio,
            plan=str(row.get("IMPL_PLAN_PROFILE") or ""),
            record_date=_to_date(row.get("EQUITY_RECORD_DATE")),
        )
        if event.is_material:
            events.append(event)
    events.sort(key=lambda item: item.ex_date)
    return events


def fetch_dividend_events(
    code: str, *, timeout: float = 15.0, url: str = _EM_URL
) -> list[DividendEvent]:  # pragma: no cover - 真实取数
    """从东财 datacenter 取一只标的的分红送转历史。

    走 ``datacenter-web``（不碰 ``push2``）：实测 ``push2`` 系列在批量请求下会被限流，
    而 datacenter 的报表接口稳定。
    """
    import requests  # noqa: PLC0415 - 仅真实取数时需要

    params = {
        "reportName": "RPT_SHAREBONUS_DET",
        "columns": "ALL",
        "filter": f'(SECURITY_CODE="{code}")',
        "pageNumber": 1,
        "pageSize": 200,
        "sortColumns": "EX_DIVIDEND_DATE",
        "sortTypes": "-1",
        "source": "WEB",
        "client": "WEB",
    }
    response = requests.get(url, params=params, timeout=timeout, headers=_HEADERS)
    response.raise_for_status()
    payload = response.json()
    rows = ((payload.get("result") or {}).get("data")) or []
    return parse_dividend_rows(code, rows)


def _scaled(bar: Bar, new_close: float) -> Bar:
    """按 ``new_close / close`` 等比缩放整根 K 线（含 ``adj_factor``）。

    四个价必须用**同一个因子**乘：分别算会因浮点误差让 close 落到 low 外一丝，
    而 ``Bar.__post_init__`` 会直接拒绝。缩放后仍把 high/low 夹到包住 close，
    保证构造结果永远合法（缩放只可能把差距缩到 1 ulp）。
    """
    if bar.close <= 0:
        return bar
    factor = new_close / bar.close
    close = bar.close * factor
    return replace(
        bar,
        open=bar.open * factor,
        high=max(bar.high * factor, close),
        low=min(bar.low * factor, close),
        close=close,
        adj_factor=factor,
    )


def apply_total_return(
    bars: Sequence[Bar],
    events: Sequence[DividendEvent],
    *,
    tax_rate: float = 0.0,
) -> AdjustOutcome:
    """把不复权序列折算成「含分红」序列（前复权口径，锚在最新价）。

    ``tax_rate`` 是红利税率：0 = 税前全额再投。差别化税率下持股 1 个月–1 年按 10%，
    1 年以内是最常见区间，因此做敏感性时用 0.10 即可；这里不按持有期逐笔算——
    回放层没有逐笔持仓期，硬算只能靠猜。

    序列按除权日升序处理，同一除权日出现多条时因子相乘；找不到「除权日之前那根 K 线」
    的事件一律跳过（计数留痕），**不拿别的价格顶替**。
    """
    if tax_rate < 0 or tax_rate >= 1:
        raise ValueError("tax_rate 必须落在 [0, 1)")
    ordered = sorted(bars, key=lambda bar: bar.trade_date)
    if not ordered or not events:
        return AdjustOutcome(bars=tuple(ordered))

    prices = [bar.close for bar in ordered]
    applied = before_history = skipped = 0
    cash_total = 0.0

    # 同一除权日的多个事件合成一个因子：分两次乘与一次乘的结果必须一致，
    # 否则「同一天派息 + 送股」会被重复折算
    grouped: dict[date, list[DividendEvent]] = {}
    for event in sorted(events, key=lambda item: item.ex_date):
        if not event.is_material:
            continue
        grouped.setdefault(event.ex_date, []).append(event)

    for ex_date in sorted(grouped):
        index = None
        for i, bar in enumerate(ordered):
            if bar.trade_date < ex_date:
                index = i
            else:
                break
        if index is None:
            if ordered[0].trade_date > ex_date:
                before_history += 1
            else:
                skipped += 1
            continue
        prev_close = prices[index]
        factor = 1.0
        cash_here = 0.0
        usable = True
        for event in grouped[ex_date]:
            cash = event.cash_per_share
            share_factor = event.share_factor
            if prev_close <= 0 or prev_close - cash <= 0:
                usable = False
                break
            denominator = share_factor * (prev_close - cash * tax_rate)
            if denominator <= 0:
                usable = False
                break
            factor *= (prev_close - cash) / denominator
            cash_here += cash
        if not usable:
            logger.warning("%s %s 的除权事件无法折算（前收 %.4f），已跳过", ordered[0].trade_date, ex_date, prev_close)
            skipped += 1
            continue
        for i in range(index + 1):
            prices[i] *= factor
        applied += 1
        cash_total += cash_here

    adjusted = tuple(_scaled(bar, prices[i]) for i, bar in enumerate(ordered))
    return AdjustOutcome(
        bars=adjusted,
        applied=applied,
        before_history=before_history,
        skipped=skipped,
        cash_per_share=cash_total,
    )
