# -*- coding: utf-8 -*-
"""时点配置解析测试（需求 FR-9.13 / R24，技术方案 §2.4）。"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from src.assistant.scheduling.config import (
    DEFAULT_FETCH_TIME,
    DEFAULT_TIMEZONE,
    ENV_FALLBACK,
    ENV_FETCH_TIME,
    ENV_NON_TRADING_ACTION,
    ENV_PUSH_SCHEDULE,
    ENV_TIMEZONE,
    EntryKind,
    NonTradingAction,
    ScheduleConfig,
    parse_env_config,
)

TUESDAY = date(2026, 11, 24)
SATURDAY = date(2026, 11, 28)


# ---- 默认值 ---------------------------------------------------------------
def test_empty_env_falls_back_to_single_20_00_push() -> None:
    config = parse_env_config({})
    assert len(config.entries) == 1
    assert config.entries[0].kind is EntryKind.HHMM
    assert config.entries[0].at == time(20, 0)
    assert config.fetch_time == time(17, 30)
    assert config.timezone == DEFAULT_TIMEZONE
    assert config.non_trading_action is NonTradingAction.SKIP
    assert config.warnings == ()


def test_explicit_default_matches_implicit_default() -> None:
    """B50 ①：默认配置与 B49 的单推口径逐字一致。"""
    implicit = parse_env_config({})
    explicit = parse_env_config({ENV_PUSH_SCHEDULE: "20:00"})
    assert implicit.entries == explicit.entries
    assert implicit.entries[0].raw == "20:00"


# ---- HH:MM 简写 -----------------------------------------------------------
def test_multiple_hhmm_sorted_and_deduped() -> None:
    config = parse_env_config({ENV_PUSH_SCHEDULE: "20:00,08:00,20:00"})
    assert [entry.raw for entry in config.entries] == ["20:00", "08:00"]
    points = config.fire_points_on(TUESDAY)
    assert [point.when.strftime("%H:%M") for point in points] == ["08:00", "20:00"]


def test_pre_fetch_slot_flagged_and_warned() -> None:
    """08:00 早于取数 17:30：允许配置，但必须标 pre_fetch 并告警。"""
    config = parse_env_config({ENV_PUSH_SCHEDULE: "08:00,20:00"})
    plan = config.plan_day(TUESDAY)
    assert [point.source for point in plan.points] == ["pre_fetch", "fresh"]
    assert any("早于取数时点" in warning for warning in plan.warnings)


def test_all_slots_after_fetch_have_no_pre_fetch_warning() -> None:
    config = parse_env_config({ENV_PUSH_SCHEDULE: "19:00,20:00"})
    plan = config.plan_day(TUESDAY)
    assert {point.source for point in plan.points} == {"fresh"}
    assert plan.warnings == ()


# ---- cron -----------------------------------------------------------------
def test_cron_weekdays_only() -> None:
    config = parse_env_config({ENV_PUSH_SCHEDULE: "0 20 * * 1-5"})
    assert config.entries[0].kind is EntryKind.CRON
    assert len(config.fire_points_on(TUESDAY)) == 1
    assert config.fire_points_on(SATURDAY) == ()
    assert config.plan_day(SATURDAY).warnings, "该日无触发时点应告警"


def test_mixed_hhmm_and_cron() -> None:
    """FR-9.13 ②：两种写法可混用。"""
    config = parse_env_config({ENV_PUSH_SCHEDULE: "08:00,0 20 * * 1-5"})
    assert len(config.entries) == 2
    assert [entry.kind for entry in config.entries] == [EntryKind.HHMM, EntryKind.CRON]
    points = config.fire_points_on(TUESDAY)
    assert [point.when.strftime("%H:%M") for point in points] == ["08:00", "20:00"]


def test_same_minute_from_two_entries_deduped() -> None:
    """``20:00`` 与 ``0 20 * * *`` 同一分钟，只算一个时点（避免同分钟重复推送）。"""
    config = parse_env_config({ENV_PUSH_SCHEDULE: "20:00,0 20 * * *"})
    points = config.fire_points_on(TUESDAY)
    assert len(points) == 1
    assert points[0].slot_key == "2026-11-24T20:00"


def test_cron_firing_multiple_times_per_day() -> None:
    config = parse_env_config({ENV_PUSH_SCHEDULE: "0 8,20 * * *"})
    points = config.fire_points_on(TUESDAY)
    assert len(points) == 2
    assert {point.slot_key for point in points} == {"2026-11-24T08:00", "2026-11-24T20:00"}


# ---- 非法值与逐条回退 ------------------------------------------------------
def test_invalid_hhmm_falls_back_per_entry_without_killing_valid_ones() -> None:
    """FR-9.13 ⑤：非法条目回退默认 20:00 并告警，其余合法条目照常生效。"""
    config = parse_env_config({ENV_PUSH_SCHEDULE: "25:00,19:30"})
    assert len(config.entries) == 2
    fallback = config.entries[0]
    assert fallback.fallback_applied
    assert fallback.at == time(20, 0)
    assert any("25:00" in warning and "回退" in warning for warning in config.warnings)
    assert config.entries[1].raw == "19:30"


def test_all_invalid_entries_collapse_to_single_default() -> None:
    config = parse_env_config({ENV_PUSH_SCHEDULE: "bad,0 20 * *"})
    # 字段不足 5 的残片被合并成一条非法表达式，回退后只剩一条默认时点
    assert len(config.entries) == 1
    assert config.entries[0].at == time(20, 0)
    assert config.entries[0].fallback_applied
    assert config.warnings


def test_invalid_timezone_falls_back() -> None:
    config = parse_env_config({ENV_TIMEZONE: "Mars/Olympus"})
    assert config.timezone == DEFAULT_TIMEZONE
    assert any("IANA" in warning for warning in config.warnings)


def test_valid_timezone_kept() -> None:
    config = parse_env_config({ENV_TIMEZONE: "Asia/Tokyo"})
    assert config.timezone == "Asia/Tokyo"


def test_invalid_fetch_time_falls_back() -> None:
    config = parse_env_config({ENV_FETCH_TIME: "25:00"})
    assert config.fetch_time == time(17, 30)
    assert any("17:30" in warning for warning in config.warnings)


@pytest.mark.parametrize(
    "raw,expected",
    [("skip", NonTradingAction.SKIP), ("notify_skip", NonTradingAction.NOTIFY_SKIP), ("NOTIFY_SKIP", NonTradingAction.NOTIFY_SKIP)],
)
def test_non_trading_action_parsed(raw: str, expected: NonTradingAction) -> None:
    config = parse_env_config({ENV_NON_TRADING_ACTION: raw})
    assert config.non_trading_action is expected


def test_invalid_non_trading_action_falls_back_to_skip() -> None:
    config = parse_env_config({ENV_NON_TRADING_ACTION: "always"})
    assert config.non_trading_action is NonTradingAction.SKIP
    assert any("notify_skip" in warning for warning in config.warnings)


def test_unknown_fallback_policy_warns_but_keeps_warn_semantics() -> None:
    config = parse_env_config({ENV_FALLBACK: "silent"})
    assert config.entries, "不得静默停摆"
    assert any("warn" in warning for warning in config.warnings)


def test_fetch_later_than_first_push_warns() -> None:
    config = parse_env_config({ENV_PUSH_SCHEDULE: "09:00", ENV_FETCH_TIME: "17:30"})
    assert any("晚于首推时点" in warning for warning in config.warnings)


# ---- next_fire ------------------------------------------------------------
def test_next_fire_picks_earliest_across_entries() -> None:
    config = parse_env_config({ENV_PUSH_SCHEDULE: "08:00,20:00"})
    nxt = config.next_fire(datetime(2026, 11, 24, 9, 0))
    assert nxt is not None
    assert nxt.when == datetime(2026, 11, 24, 20, 0)
    assert nxt.source == "fresh"

    nxt2 = config.next_fire(datetime(2026, 11, 24, 21, 0))
    assert nxt2 is not None
    assert nxt2.when == datetime(2026, 11, 25, 8, 0)
    assert nxt2.source == "pre_fetch"


def test_next_fire_uses_cron_next_occurrence() -> None:
    config = parse_env_config({ENV_PUSH_SCHEDULE: "0 20 * * 1-5"})
    # 周五收盘后 → 下一个工作日周一
    nxt = config.next_fire(datetime(2026, 11, 27, 21, 0))
    assert nxt is not None
    assert nxt.when == datetime(2026, 11, 30, 20, 0)


def test_default_config_classmethod_matches_env_defaults() -> None:
    config = ScheduleConfig.default()
    assert config.entries[0].at == time(20, 0)
    assert config.fetch_time == time(int(DEFAULT_FETCH_TIME[:2]), int(DEFAULT_FETCH_TIME[3:]))
