# -*- coding: utf-8 -*-
"""5 字段 cron 解析与匹配（**不引入新依赖**）。

技术方案 §2.4 给了两个选型（``croniter`` / APScheduler ``CronTrigger``），但把
选型权留到 d 阶段。这里选定**自实现**，理由：

1. 主流程 ``requirements.txt`` 没有 croniter / APScheduler，新增依赖要改部署镜像与
   升级链，而本模块只需要「判断某一分钟是否命中」与「算下一次触发」，能力面很窄；
2. 助手模块的目标是「离线可测、单独可跑」，纯标准库实现最省事；
3. M18 实测确认需求要求的表达力就这几类（``0 20 * * 1-5``、``*/N``、指定星期），
   不需要 croniter 的秒级字段、``@daily`` 宏、``L/W/#`` 扩展。

支持范围（覆盖 FR-9.13「标准 5 字段 cron」）：

- ``*``、``*/N``、``a``、``a-b``、``a-b/N``、逗号列表，以及 ``a/N``（等价 ``a-max/N``）
- 月名 ``JAN-DEC``、星期名 ``SUN-SAT``（大小写不敏感）
- 星期 ``0`` 与 ``7`` 都表示周日（POSIX 兼容）
- **日/周 OR 语义**：两字段都「不以 ``*`` 开头」时命中任一即可；否则按 AND
  （Vixie cron 行为：以首字符 ``*`` 判定是否为「星号字段」，所以 ``*/2`` 也算星号）

不支持（遇到即报错，不静默降级）：秒字段、6/7 字段表达式、``@daily`` 等宏、
``L`` / ``W`` / ``#`` 扩展。这些需求文档均未要求。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

__all__ = ["CronError", "CronExpression", "parse_cron"]


class CronError(ValueError):
    """cron 表达式非法（字段数、取值越界、步长非法等）。"""


_MONTH_NAMES = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_DOW_NAMES = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}

#: (字段名, 下界, 上界, 名称映射)
_FIELD_SPECS = (
    ("分", 0, 59, {}),
    ("时", 0, 23, {}),
    ("日", 1, 31, {}),
    ("月", 1, 12, _MONTH_NAMES),
    ("周", 0, 7, _DOW_NAMES),
)


def _name_value(token: str, names: dict[str, int], field: str, lo: int, hi: int) -> int:
    text = token.strip().upper()
    if text in names:
        return names[text]
    try:
        value = int(text)
    except ValueError as exc:
        raise CronError(f"{field}字段取值非法：{token!r}") from exc
    if not lo <= value <= hi:
        raise CronError(f"{field}字段取值越界：{value}（允许 {lo}-{hi}）")
    return value


def _parse_item(item: str, lo: int, hi: int, names: dict[str, int], field: str) -> set[int]:
    """解析单个「不含逗号」的片段。"""
    body = item
    step = 1
    has_step = "/" in item
    if has_step:
        body, _, step_text = item.partition("/")
        try:
            step = int(step_text.strip())
        except ValueError as exc:
            raise CronError(f"{field}字段步长非法：{item!r}") from exc
        if step < 1:
            raise CronError(f"{field}字段步长必须为正整数：{item!r}")

    body = body.strip()
    if not body:
        raise CronError(f"{field}字段为空：{item!r}")

    if body == "*":
        start, end = lo, hi
    elif "-" in body:
        left, _, right = body.partition("-")
        start = _name_value(left, names, field, lo, hi)
        end = _name_value(right, names, field, lo, hi)
        if start > end:
            raise CronError(f"{field}字段区间倒置：{item!r}")
    else:
        start = _name_value(body, names, field, lo, hi)
        # ``a/N`` 按 Vixie 习惯等价于 ``a-max/N``；无步长时就是单值
        end = hi if has_step else start

    return set(range(start, end + 1, step))


def _parse_field(raw: str, lo: int, hi: int, names: dict[str, int], field: str) -> tuple[frozenset[int], bool]:
    text = raw.strip()
    if not text:
        raise CronError(f"{field}字段为空")
    # Vixie 语义：以首字符 '*' 判定「星号字段」，影响日/周 OR 规则
    star = text.startswith("*")
    values: set[int] = set()
    for item in text.split(","):
        if not item.strip():
            raise CronError(f"{field}字段含空项：{raw!r}")
        values |= _parse_item(item, lo, hi, names, field)
    if not values:
        raise CronError(f"{field}字段未匹配任何取值：{raw!r}")
    return frozenset(values), star


@dataclass(frozen=True)
class CronExpression:
    """已解析的 5 字段 cron 表达式（不可变，可安全缓存/复用）。"""

    raw: str
    minute: frozenset[int]
    hour: frozenset[int]
    day_of_month: frozenset[int]
    month: frozenset[int]
    day_of_week: frozenset[int]
    dom_star: bool
    dow_star: bool

    def __str__(self) -> str:  # pragma: no cover - 仅日志可读性
        return self.raw

    # ------------------------------------------------------------------
    # 匹配
    # ------------------------------------------------------------------
    def day_matches(self, day: date) -> bool:
        """日期是否命中（含日/周 OR 语义）。"""
        if day.month not in self.month:
            return False
        dom_ok = day.day in self.day_of_month
        dow_ok = cron_weekday(day) in self.day_of_week
        if self.dom_star or self.dow_star:
            return dom_ok and dow_ok
        return dom_ok or dow_ok

    def matches(self, when: datetime) -> bool:
        """该分钟是否命中（``when`` 视为配置时区的本地墙上时间）。"""
        return (
            when.minute in self.minute
            and when.hour in self.hour
            and self.day_matches(when.date())
        )

    # ------------------------------------------------------------------
    # 时点计算
    # ------------------------------------------------------------------
    def occurrences_on(self, day: date) -> tuple[datetime, ...]:
        """返回该日的全部触发时点（升序，秒/微秒为 0）。"""
        if not self.day_matches(day):
            return ()
        return tuple(
            datetime.combine(day, time(hour=h, minute=m))
            for h in sorted(self.hour)
            for m in sorted(self.minute)
        )

    def next_after(self, when: datetime, *, horizon_days: int = 400) -> datetime | None:
        """严格大于 ``when`` 的下一个触发时点；``horizon_days`` 内找不到返回 ``None``。

        典型 ``None`` 场景：``0 0 30 2 *``（2 月 30 日）这类永不成立的表达式。
        """
        start = when.replace(second=0, microsecond=0) + timedelta(minutes=1)
        today = start.date()
        for offset in range(horizon_days + 1):
            day = today + timedelta(days=offset)
            if day.month not in self.month or not self.day_matches(day):
                continue
            for hour in sorted(self.hour):
                for minute in sorted(self.minute):
                    candidate = datetime.combine(day, time(hour=hour, minute=minute))
                    if candidate >= start:
                        return candidate
        return None


def cron_weekday(day: date) -> int:
    """``date`` → cron 星期（周日 0 … 周六 6）。"""
    return (day.weekday() + 1) % 7


def parse_cron(expression: str) -> CronExpression:
    """解析 5 字段 cron；非法抛 :class:`CronError`。"""
    text = (expression or "").strip()
    if not text:
        raise CronError("cron 表达式为空")
    parts = text.split()
    if len(parts) != 5:
        raise CronError(
            f"cron 需 5 个字段（分 时 日 月 周），收到 {len(parts)} 个：{expression!r}"
        )

    parsed = []
    for part, (field, lo, hi, names) in zip(parts, _FIELD_SPECS):
        parsed.append(_parse_field(part, lo, hi, names, field))

    (minute, _), (hour, _), (dom, dom_star), (month, _), (dow, dow_star) = parsed

    # 星期 7 归一为 0（POSIX 两种写法等价）
    normalized_dow = frozenset(0 if value == 7 else value for value in dow)

    return CronExpression(
        raw=text,
        minute=minute,
        hour=hour,
        day_of_month=dom,
        month=month,
        day_of_week=normalized_dow,
        dom_star=dom_star,
        dow_star=dow_star,
    )
