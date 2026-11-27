# -*- coding: utf-8 -*-
"""财报事实（FUN-1 / FUN-2 风险与 ``w_fundamental`` 的数据来源）。

为什么单独一个模块：财报不是日频数据，**不能用「报告期」当「可见日」**。东财的
``REPORT_DATE`` 是报告期末（2026-09-30），而三季报实际到 2026-10-31 才公告；若按报告期
判断可见性，回放到 2026-10-01 就会用上还没发布的财报——那是典型的前视偏差，而且它
恰好会让「业绩变脸」在现实中还没发生时就先扣分。所以这里一律按 ``NOTICE_DATE`` 判定
「T 日能不能看见」，缺失公告日的行**不参与判定**（宁可少算，不猜发布时间）。

口径（写清楚才敢让风险层用）：

- **下滑**用同比增速（``TOTALOPERATEREVETZ`` / ``PARENTNETPROFITTZ`` / ``ROEJQTZ``）判，
  不用绝对值：报告期是年内累计数，Q1 与 Q3 直接比大小是季节性错觉。同比 < 0 记一期下滑。
- **连续**要求报告期真的连续：从最新一期往前逐期核对（Q3→Q2→Q1→上年 Q4…），断一节就停——
  缺一期还说「连续 3 期下滑」是编的。
- **经营现金流恶化** = 最新一期每股经营现金流为负**且**同比下降（见 ``_cashflow_worsening``）：
  只看「为负」会把季节性负现金流误判成恶化。
- **审计意见**暂不判定：非标意见在公告/审计报告里，本次没接（M16 未实测），字段恒 False，
  由 ``available`` 与 FUN-3 如实体现「没接」而不是「没问题」。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping, Sequence

import requests

from src.assistant.domain import FundamentalView

__all__ = [
    "FundamentalReport",
    "fetch_fundamental_reports",
    "fundamental_view_from_reports",
    "parse_report_rows",
]

_EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
_REPORT = "RPT_F10_FINANCE_MAINFINADATA"


@dataclass(frozen=True)
class FundamentalReport:
    """一期财报的关键事实（只留判定要用到的字段，不做综合打分）。"""

    code: str
    report_date: date
    #: 公告日；``None`` = 不知道什么时候可见，这种行不参与「T 日可见」判定
    notice_date: date | None = None
    period_label: str = ""
    revenue: float | None = None
    revenue_yoy: float | None = None
    net_profit: float | None = None
    profit_yoy: float | None = None
    roe: float | None = None
    roe_yoy: float | None = None
    ocf_per_share: float | None = None
    ocf_yoy: float | None = None
    debt_ratio: float | None = None
    gross_margin: float | None = None


def _to_day(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text or text in ("None", "nan", "--"):
        return None
    head = text[:10]
    for candidate in (head, text):
        try:
            parsed = datetime.fromisoformat(candidate.replace("/", "-"))
        except ValueError:
            continue
        return parsed.date()
    return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out  # NaN


def parse_report_rows(code: str, rows: Sequence[Mapping[str, Any]]) -> list[FundamentalReport]:
    """东财 ``RPT_F10_FINANCE_MAINFINADATA`` 行 → :class:`FundamentalReport`。

    没有报告期的行直接丢弃（无从判定），其余按报告期倒序返回；同一报告期重复时保留
    第一条（接口按 REPORT_DATE 倒序，第一条是最新的修订版）。
    """
    seen: dict[date, FundamentalReport] = {}
    for row in rows:
        report_date = _to_day(row.get("REPORT_DATE"))
        if report_date is None:
            continue
        if report_date in seen:
            continue
        seen[report_date] = FundamentalReport(
            code=code,
            report_date=report_date,
            notice_date=_to_day(row.get("NOTICE_DATE")),
            period_label=str(row.get("REPORT_DATE_NAME") or ""),
            revenue=_to_float(row.get("TOTALOPERATEREVE")),
            revenue_yoy=_to_float(row.get("TOTALOPERATEREVETZ")),
            net_profit=_to_float(row.get("PARENTNETPROFIT")),
            profit_yoy=_to_float(row.get("PARENTNETPROFITTZ")),
            roe=_to_float(row.get("ROEJQ")),
            roe_yoy=_to_float(row.get("ROEJQTZ")),
            ocf_per_share=_to_float(row.get("MGJYXJJE")),
            ocf_yoy=_to_float(row.get("MGJYXJJETZ")),
            debt_ratio=_to_float(row.get("ZCFZL")),
            gross_margin=_to_float(row.get("XSMLL")),
        )
    return [seen[key] for key in sorted(seen, reverse=True)]


def _prev_period(day: date) -> date | None:
    """上一报告期（A 股为 3/6/9/12 月末）；非常规报告期返回 ``None``（不猜连续性）。"""
    if (day.month, day.day) == (3, 31):
        return date(day.year - 1, 12, 31)
    if (day.month, day.day) == (6, 30):
        return date(day.year, 3, 31)
    if (day.month, day.day) == (9, 30):
        return date(day.year, 6, 30)
    if (day.month, day.day) == (12, 31):
        return date(day.year, 9, 30)
    return None


def _declining_periods(ordered: Sequence[FundamentalReport], field: str) -> int:
    """从最新一期往前数「同比为负」的连续期数；报告期断档即停。"""
    count = 0
    expect: date | None = None
    for report in ordered:
        value = getattr(report, field)
        if value is None:
            break
        if expect is not None and report.report_date != expect:
            break
        if value >= 0:
            break
        count += 1
        expect = _prev_period(report.report_date)
        if expect is None:
            break
    return count


def _cashflow_worsening(report: FundamentalReport) -> bool:
    """经营现金流恶化 = **为负且同比继续变差**（没有同比数据时按「为负」算）。

    只看「为负」会把季节性负现金流（多数公司 Q1/Q3 经营现金流为负是常态）当成恶化，
    实测这一条单独就会多触发 8.6% 的 code-day；只看「同比下滑」又会把仍然健康的
    正现金流也算进来。两个条件同时成立才算「恶化」。
    """
    if report.ocf_per_share is None or report.ocf_per_share >= 0:
        return False
    return report.ocf_yoy is None or report.ocf_yoy < 0


def fundamental_view_from_reports(
    reports: Sequence[FundamentalReport], day: date, *, require_visible: bool = True
) -> FundamentalView:
    """把「T 日**已公告**的财报」折成 :class:`FundamentalView`。

    只有 ``notice_date`` 存在且 ``<= day`` 的报告参与判定；一份都看不见时返回
    ``available=False``（FUN-3 观察级「财报未更新」，**不是**「基本面变坏」）。
    """
    visible = [
        report
        for report in reports
        if (not require_visible or (report.notice_date is not None and report.notice_date <= day))
        and report.report_date <= day
    ]
    if not visible:
        return FundamentalView(available=False)
    ordered = sorted(visible, key=lambda report: report.report_date, reverse=True)
    latest = ordered[0]
    return FundamentalView(
        available=True,
        loss_making=latest.net_profit is not None and latest.net_profit < 0,
        revenue_declining_periods=_declining_periods(ordered, "revenue_yoy"),
        profit_declining_periods=_declining_periods(ordered, "profit_yoy"),
        roe_declining_periods=_declining_periods(ordered, "roe_yoy"),
        cashflow_worsening=_cashflow_worsening(latest),
        # 审计意见未接入（见模块说明）：一律 False，由 available 体现「有没有财报」，
        # 不假装「已核查且没问题」
        audit_opinion_abnormal=False,
        report_period=latest.period_label or latest.report_date.isoformat(),
    )


def fetch_fundamental_reports(code: str, *, limit: int = 40) -> list[FundamentalReport]:
    """取最近 ``limit`` 期财报（一期一行）。取不到返回空列表，**不编造**。"""
    params = {
        "reportName": _REPORT,
        "columns": "ALL",
        "filter": f'(SECURITY_CODE="{code}")',
        "pageNumber": 1,
        "pageSize": limit,
        "sortColumns": "REPORT_DATE",
        "sortTypes": "-1",
    }
    response = requests.get(_EM_URL, params=params, timeout=20, headers=_HEADERS)
    response.raise_for_status()
    payload = response.json()
    rows = ((payload.get("result") or {}).get("data")) or []
    return parse_report_rows(code, rows)
