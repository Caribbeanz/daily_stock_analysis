# -*- coding: utf-8 -*-
"""cron 解析与匹配测试（需求 FR-9.13「标准 5 字段 cron」）。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from src.assistant.scheduling.cron import CronError, cron_weekday, parse_cron


def dt(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


# ---- 基本字段 ---------------------------------------------------------------
def test_weekday_alias_parses_and_matches() -> None:
    expr = parse_cron("0 20 * * 1-5")
    assert expr.minute == frozenset({0})
    assert expr.hour == frozenset({20})
    # 2026-11-24 是周二
    assert expr.matches(dt("2026-11-24 20:00"))
    assert expr.matches(dt("2026-11-27 20:00"))  # 周五
    assert not expr.matches(dt("2026-11-28 20:00"))  # 周六
    assert not expr.matches(dt("2026-11-29 20:00"))  # 周日
    assert not expr.matches(dt("2026-11-24 20:01"))


def test_step_expression() -> None:
    expr = parse_cron("*/15 9-11 * * *")
    assert expr.minute == frozenset({0, 15, 30, 45})
    assert expr.hour == frozenset({9, 10, 11})
    assert expr.matches(dt("2026-11-24 09:30"))
    assert not expr.matches(dt("2026-11-24 09:10"))


def test_list_expression() -> None:
    expr = parse_cron("0 8,12,20 * * *")
    assert expr.hour == frozenset({8, 12, 20})
    assert expr.matches(dt("2026-11-24 12:00"))


def test_range_with_step() -> None:
    expr = parse_cron("0-30/10 20 * * *")
    assert expr.minute == frozenset({0, 10, 20, 30})


def test_open_ended_step_takes_upper_bound() -> None:
    """``a/N`` 按 Vixie 习惯等价 ``a-max/N``。"""
    expr = parse_cron("50/5 20 * * *")
    assert expr.minute == frozenset({50, 55})


def test_month_names_case_insensitive() -> None:
    expr = parse_cron("0 20 * jan-mar *")
    assert expr.month == frozenset({1, 2, 3})
    assert expr.matches(dt("2026-02-10 20:00"))
    assert not expr.matches(dt("2026-04-10 20:00"))


def test_dow_names() -> None:
    expr = parse_cron("0 20 * * mon-fri")
    assert expr.matches(dt("2026-11-24 20:00"))
    assert not expr.matches(dt("2026-11-28 20:00"))


def test_dow_seven_equals_sunday() -> None:
    expr = parse_cron("0 20 * * 7")
    assert expr.day_of_week == frozenset({0})
    assert expr.matches(dt("2026-11-29 20:00"))  # 周日
    assert not expr.matches(dt("2026-11-24 20:00"))


# ---- 日/周 OR 语义（最容易写错）---------------------------------------------
def test_dom_and_dow_both_restricted_use_or_semantics() -> None:
    """两者都不是星号字段时命中任一即可（Vixie cron）。"""
    expr = parse_cron("0 20 1 * 1")
    assert expr.matches(dt("2026-12-01 20:00")), "每月的 1 号应命中（周二）"
    assert expr.matches(dt("2026-11-30 20:00")), "周一应命中（30 号）"
    assert not expr.matches(dt("2026-11-24 20:00")), "周二且非 1 号，不该命中"


def test_star_dow_makes_dom_authoritative() -> None:
    expr = parse_cron("0 20 1 * *")
    assert expr.matches(dt("2026-12-01 20:00"))
    assert not expr.matches(dt("2026-11-02 20:00"))  # 2 号不是 1 号


def test_star_dom_makes_dow_authoritative() -> None:
    expr = parse_cron("0 20 * * 1")
    assert expr.matches(dt("2026-11-30 20:00"))
    assert not expr.matches(dt("2026-12-01 20:00"))


def test_step_in_dom_counts_as_star_field() -> None:
    """``*/2`` 首字符是 ``*``，按 Vixie 属星号字段，不触发 OR 语义。

    「日」字段 ``*/2`` 从下界 1 起算 → 命中奇数日。
    """
    expr = parse_cron("0 20 */2 * 1")
    assert expr.dom_star and not expr.dow_star
    # 2026-11-23 是周一且 23 为奇数 → 命中
    assert expr.matches(dt("2026-11-23 20:00"))
    # 2026-11-30 是周一但 30 为偶数 → 按 AND 不命中（若误用 OR 会命中）
    assert not expr.matches(dt("2026-11-30 20:00"))
    # 2026-11-25 是周三、日条件满足但周条件不满足 → 不命中
    assert not expr.matches(dt("2026-11-25 20:00"))


# ---- 时点计算 ---------------------------------------------------------------
def test_occurrences_on_weekday() -> None:
    expr = parse_cron("0 20 * * 1-5")
    assert expr.occurrences_on(date(2026, 11, 24)) == (datetime(2026, 11, 24, 20, 0),)
    assert expr.occurrences_on(date(2026, 11, 28)) == ()


def test_occurrences_on_multiple_times_per_day() -> None:
    expr = parse_cron("0 8,20 * * *")
    assert expr.occurrences_on(date(2026, 11, 24)) == (
        datetime(2026, 11, 24, 8, 0),
        datetime(2026, 11, 24, 20, 0),
    )


def test_next_after_is_strictly_later() -> None:
    expr = parse_cron("0 20 * * 1-5")
    # 恰好在 20:00 时，下一次应是下一个工作日
    assert expr.next_after(dt("2026-11-24 20:00")) == dt("2026-11-25 20:00")
    assert expr.next_after(dt("2026-11-24 19:59")) == dt("2026-11-24 20:00")
    # 周五 20:00 之后跨周末
    assert expr.next_after(dt("2026-11-27 20:00")) == dt("2026-11-30 20:00")


def test_next_after_impossible_expression_returns_none() -> None:
    """2 月 30 日永不成立——必须返回 None 而不是死循环。"""
    assert parse_cron("0 0 30 2 *").next_after(dt("2026-01-01 00:00")) is None


def test_cron_weekday_convention() -> None:
    assert cron_weekday(date(2026, 11, 29)) == 0  # 周日
    assert cron_weekday(date(2026, 11, 24)) == 2  # 周二


# ---- 非法输入 ---------------------------------------------------------------
@pytest.mark.parametrize(
    "expr, keyword",
    [
        ("", "为空"),
        ("0 20 * *", "5 个字段"),
        ("0 20 * * 1-5 extra", "5 个字段"),
        ("60 20 * * *", "越界"),
        ("0 24 * * *", "越界"),
        ("0 0 0 * *", "越界"),
        ("0 0 * 13 *", "越界"),
        ("*/0 20 * * *", "步长"),
        ("5-1 20 * * *", "倒置"),
        ("0 20 * * FOO", "取值非法"),
        ("0 20 * * 1,,5", "空项"),
    ],
)
def test_invalid_expressions_raise(expr: str, keyword: str) -> None:
    with pytest.raises(CronError) as exc:
        parse_cron(expr)
    assert keyword in str(exc.value)
