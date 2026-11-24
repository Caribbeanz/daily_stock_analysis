# -*- coding: utf-8 -*-
"""推送时点配置解析（需求 FR-9.13 / R24，技术方案 §2.4）。

配置项（键名与方案一致，沿用「只加键不改读取链」）：

===============================  =============  ============================================
键                               默认           含义
===============================  =============  ============================================
``ASSISTANT_PUSH_SCHEDULE``      ``20:00``      ``HH:MM`` 逗号简写 / 5 字段 cron，可混用
``ASSISTANT_TRADE_FETCH_TIME``   ``17:30``      取数时点（须早于首推）
``ASSISTANT_NON_TRADING_ACTION`` ``skip``       ``skip`` | ``notify_skip``
``ASSISTANT_SCHEDULE_TIMEZONE``  ``Asia/Shanghai``  IANA 时区
``ASSISTANT_SCHEDULE_FALLBACK``  ``warn``       解析失败动作：回退默认 20:00 并告警
===============================  =============  ============================================

三条硬口径：

1. **非法条目逐条回退、不静默停摆**（FR-9.13 ⑤）：某条解析失败只把该条退为默认
   20:00 并记警告，其余合法条目照常生效；全部失败才整体回退。
2. **多时点同源**（B50 ③）：本模块只负责「今天有哪些时点」，同一份 payload 由
   :mod:`src.assistant.scheduling.delivery` 保证复用，这里不生成任何内容。
3. **早于取数时点的配置只告警、不投递**：配置 ``08:00`` 这类早于取数时点的时点拿不到
   当日新快照。R24 允许这样写，但**实际只有取数后的时点会推送**（2026-11-24 确认：
   仅 20:00 推送）；本模块把这类时点标成 ``pre_fetch`` 并在计划里告警，调度层据此跳过。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.assistant.scheduling.cron import CronError, CronExpression, parse_cron

__all__ = [
    "DEFAULT_FETCH_TIME",
    "DEFAULT_PUSH_TIME",
    "DEFAULT_TIMEZONE",
    "ENV_FETCH_TIME",
    "ENV_FALLBACK",
    "ENV_NON_TRADING_ACTION",
    "ENV_PUSH_SCHEDULE",
    "ENV_TIMEZONE",
    "DayPlan",
    "EntryKind",
    "FirePoint",
    "NonTradingAction",
    "ScheduleConfig",
    "ScheduleEntry",
    "parse_env_config",
]

DEFAULT_PUSH_TIME = "20:00"
DEFAULT_FETCH_TIME = "17:30"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_NON_TRADING_ACTION = "skip"

ENV_PUSH_SCHEDULE = "ASSISTANT_PUSH_SCHEDULE"
ENV_FETCH_TIME = "ASSISTANT_TRADE_FETCH_TIME"
ENV_NON_TRADING_ACTION = "ASSISTANT_NON_TRADING_ACTION"
ENV_TIMEZONE = "ASSISTANT_SCHEDULE_TIMEZONE"
ENV_FALLBACK = "ASSISTANT_SCHEDULE_FALLBACK"

_HHMM_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


class NonTradingAction(str, Enum):
    """非交易日处置（FR-8.6；两值都不生成快照/卡片）。"""

    SKIP = "skip"
    NOTIFY_SKIP = "notify_skip"


class EntryKind(str, Enum):
    HHMM = "hhmm"
    CRON = "cron"


def _parse_hhmm(text: str) -> time:
    hour, minute = text.split(":")
    return time(hour=int(hour), minute=int(minute))


@dataclass(frozen=True)
class ScheduleEntry:
    """一条时点配置。

    ``raw`` 保留用户原样输入（告警、留痕、去重键都用它）；``fallback_applied`` 为真
    表示该条本身非法、已回退为默认 20:00。
    """

    raw: str
    kind: EntryKind
    at: time | None = None
    cron: CronExpression | None = None
    fallback_applied: bool = False

    def occurrences_on(self, day: date) -> tuple[datetime, ...]:
        if self.kind is EntryKind.HHMM:
            assert self.at is not None  # 构造期保证
            return (datetime.combine(day, self.at),)
        assert self.cron is not None
        return self.cron.occurrences_on(day)

    def next_after(self, when: datetime) -> datetime | None:
        if self.kind is EntryKind.HHMM:
            assert self.at is not None
            today = datetime.combine(when.date(), self.at)
            if today > when.replace(second=0, microsecond=0):
                return today
            return today + timedelta(days=1)
        assert self.cron is not None
        return self.cron.next_after(when)


@dataclass(frozen=True)
class FirePoint:
    """某一天的一个触发时点。"""

    when: datetime
    entry: ScheduleEntry
    #: ``fresh`` = 该时点在取数之后，可投当日新快照；``pre_fetch`` = 早于取数，
    #: **不投递**（当日新快照尚未生成，旧快照不能冒充当日结论）
    source: str = "fresh"

    @property
    def slot_key(self) -> str:
        """去重键：精确到分钟，天然兼容同一分钟的多条配置。"""
        return self.when.strftime("%Y-%m-%dT%H:%M")


@dataclass(frozen=True)
class DayPlan:
    """某一天的计划：全部触发时点 + 告警。"""

    day: date
    points: tuple[FirePoint, ...]
    warnings: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.points)


@dataclass(frozen=True)
class ScheduleConfig:
    """解析后的调度配置。"""

    entries: tuple[ScheduleEntry, ...]
    timezone: str
    non_trading_action: NonTradingAction
    fetch_time: time
    warnings: tuple[str, ...] = ()

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    @classmethod
    def default(cls) -> ScheduleConfig:
        return cls(
            entries=(ScheduleEntry(DEFAULT_PUSH_TIME, EntryKind.HHMM, at=_parse_hhmm(DEFAULT_PUSH_TIME)),),
            timezone=DEFAULT_TIMEZONE,
            non_trading_action=NonTradingAction.SKIP,
            fetch_time=_parse_hhmm(DEFAULT_FETCH_TIME),
        )

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    # ------------------------------------------------------------------
    # 计划
    # ------------------------------------------------------------------
    def fire_points_on(self, day: date) -> tuple[FirePoint, ...]:
        """该日全部触发时点（升序、按分钟去重）。"""
        seen: set[str] = set()
        points: list[FirePoint] = []
        for entry in self.entries:
            for when in entry.occurrences_on(day):
                source = "fresh" if when.time() >= self.fetch_time else "pre_fetch"
                point = FirePoint(when=when, entry=entry, source=source)
                if point.slot_key in seen:
                    continue
                seen.add(point.slot_key)
                points.append(point)
        return tuple(sorted(points, key=lambda item: item.when))

    def plan_day(self, day: date) -> DayPlan:
        points = self.fire_points_on(day)
        warnings: list[str] = []
        if not points:
            warnings.append(f"{day.isoformat()} 无触发时点（表达式在该日不成立）")
        for point in points:
            if point.source == "pre_fetch":
                warnings.append(
                    f"{point.when:%H:%M} 早于取数时点 {self.fetch_time:%H:%M}，"
                    "该时点不会推送（仅取数后的时点投递）"
                )
        return DayPlan(day=day, points=points, warnings=tuple(warnings))

    def next_fire(self, after: datetime) -> FirePoint | None:
        """严格晚于 ``after`` 的最近一个触发时点。"""
        best: datetime | None = None
        chosen: ScheduleEntry | None = None
        for entry in self.entries:
            candidate = entry.next_after(after)
            if candidate is None:
                continue
            if best is None or candidate < best:
                best, chosen = candidate, entry
        if best is None or chosen is None:
            return None
        source = "fresh" if best.time() >= self.fetch_time else "pre_fetch"
        return FirePoint(when=best, entry=chosen, source=source)


def _split_entries(raw: str) -> list[str]:
    """把配置串切成「条目」列表，兼容 ``HH:MM`` 简写与 5 字段 cron 混用。

    难点：**逗号是双重语义**——既是 ``HH:MM`` 列表的分隔符（``08:00,20:00``），也是
    cron 字段的列表运算符（``0 8,20 * * *``）。任何「先按逗号 split」的做法都会把
    后者切坏（``0 8,20`` → ``0 8`` + ``20 * * *``），这是实测踩到的坑。

    因此按**空格分词**，再按「片段是否含冒号」区分：

    - 含冒号的片段 = ``HH:MM`` 候选（非法值留给 :func:`_parse_entry` 逐条回退）
    - 不含冒号的片段 = cron 字段（同一 token 内的多个片段用逗号拼回一个字段）
    - 累积满 5 个 cron 字段即成一整条表达式
    """

    entries: list[str] = []
    cron_fields: list[str] = []

    for token in raw.split():
        colon_parts: list[str] = []
        plain_parts: list[str] = []
        for piece in token.split(","):
            if not piece:
                continue
            (colon_parts if ":" in piece else plain_parts).append(piece)

        entries.extend(colon_parts)
        if plain_parts:
            cron_fields.append(",".join(plain_parts))
        if len(cron_fields) >= 5:
            entries.append(" ".join(cron_fields))
            cron_fields.clear()

    if cron_fields:
        # 字段数不足 5：整条当作非法表达式，交给解析器报错并回退
        entries.append(" ".join(cron_fields))

    return entries


def parse_env_config(env: Mapping[str, str]) -> ScheduleConfig:
    """从环境变量映射解析配置；非法条目逐条回退并累计告警。"""
    warnings: list[str] = []

    raw_schedule = env.get(ENV_PUSH_SCHEDULE)
    items = _split_entries(raw_schedule or "")
    if not items:
        if raw_schedule:
            warnings.append(f"{ENV_PUSH_SCHEDULE}={raw_schedule!r} 解析为空，回退默认 {DEFAULT_PUSH_TIME}")
        items = [DEFAULT_PUSH_TIME]

    entries: list[ScheduleEntry] = []
    seen_raw: set[str] = set()
    for item in items:
        if item in seen_raw:
            continue
        seen_raw.add(item)
        entry = _parse_entry(item, warnings)
        entries.append(entry)

    if not entries:  # pragma: no cover - _parse_entry 永远返回可用条目
        entries = [_fallback_entry(warnings, reason="无有效条目")]

    fetch_raw = (env.get(ENV_FETCH_TIME) or "").strip() or DEFAULT_FETCH_TIME
    if _HHMM_RE.fullmatch(fetch_raw):
        fetch_time = _parse_hhmm(fetch_raw)
    else:
        warnings.append(f"{ENV_FETCH_TIME}={fetch_raw!r} 非法（需 HH:MM），回退 {DEFAULT_FETCH_TIME}")
        fetch_time = _parse_hhmm(DEFAULT_FETCH_TIME)

    tz_raw = (env.get(ENV_TIMEZONE) or "").strip() or DEFAULT_TIMEZONE
    try:
        ZoneInfo(tz_raw)
        timezone = tz_raw
    except (ZoneInfoNotFoundError, ValueError):
        warnings.append(f"{ENV_TIMEZONE}={tz_raw!r} 不是有效 IANA 时区，回退 {DEFAULT_TIMEZONE}")
        timezone = DEFAULT_TIMEZONE

    action_raw = (env.get(ENV_NON_TRADING_ACTION) or "").strip().lower() or DEFAULT_NON_TRADING_ACTION
    try:
        action = NonTradingAction(action_raw)
    except ValueError:
        warnings.append(
            f"{ENV_NON_TRADING_ACTION}={action_raw!r} 非法（需 skip | notify_skip），"
            f"回退 {DEFAULT_NON_TRADING_ACTION}"
        )
        action = NonTradingAction.SKIP

    fallback_raw = (env.get(ENV_FALLBACK) or "").strip().lower()
    if fallback_raw and fallback_raw != "warn":
        warnings.append(f"{ENV_FALLBACK}={fallback_raw!r} 未识别，按 warn 处理（回退默认并告警）")

    config = ScheduleConfig(
        entries=tuple(entries),
        timezone=timezone,
        non_trading_action=action,
        fetch_time=fetch_time,
        warnings=tuple(warnings),
    )
    # 取数晚于首推：给出运维告警，但不改配置（R24 允许早时点）
    first = config.fire_points_on(date(2026, 1, 5))  # 任意工作日；只用于比较时刻
    if first and config.fetch_time > first[0].when.time():
        warnings = list(warnings) + [
            f"取数时点 {config.fetch_time:%H:%M} 晚于首推时点 {first[0].when:%H:%M}，"
            "首推时点早于取数，实际不会推送（仅取数后的时点投递）"
        ]
        config = ScheduleConfig(
            entries=config.entries,
            timezone=config.timezone,
            non_trading_action=config.non_trading_action,
            fetch_time=config.fetch_time,
            warnings=tuple(warnings),
        )
    return config


def _fallback_entry(warnings: list[str], *, reason: str) -> ScheduleEntry:
    warnings.append(f"{reason}：回退默认推送时点 {DEFAULT_PUSH_TIME}")
    return ScheduleEntry(
        raw=DEFAULT_PUSH_TIME,
        kind=EntryKind.HHMM,
        at=_parse_hhmm(DEFAULT_PUSH_TIME),
        fallback_applied=True,
    )


def _parse_entry(item: str, warnings: list[str]) -> ScheduleEntry:
    if _HHMM_RE.fullmatch(item):
        return ScheduleEntry(raw=item, kind=EntryKind.HHMM, at=_parse_hhmm(item))
    try:
        cron = parse_cron(item)
    except CronError as exc:
        return _fallback_entry(warnings, reason=f"{item!r} 无法解析（{exc}）")
    return ScheduleEntry(raw=item, kind=EntryKind.CRON, cron=cron)
