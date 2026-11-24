# -*- coding: utf-8 -*-
"""调度与交易日门控（技术方案 §2.4 / §6.3）。

四个子模块，全部**不依赖第三方库**（cron 自实现、时区用标准库 ``zoneinfo``）：

- :mod:`.cron`     5 字段 cron 解析与匹配
- :mod:`.config`   时点配置解析（``HH:MM`` 简写 + cron 混用）、按日生成触发计划
- :mod:`.gate`     交易日门控，**fail-closed**（判不出来按非交易日处理）
- :mod:`.delivery` 多时点同源投递与去重台账

交易日历本身复用主流程 ``src/core/trading_calendar.py``，但只在
:func:`TradingDayGate.decide` 内部延迟 import，因此本包可脱离 pandas /
exchange-calendars 单独测试。
"""

from src.assistant.scheduling.config import (
    DEFAULT_FETCH_TIME,
    DEFAULT_PUSH_TIME,
    DEFAULT_TIMEZONE,
    DayPlan,
    EntryKind,
    FirePoint,
    NonTradingAction,
    ScheduleConfig,
    ScheduleEntry,
    parse_env_config,
)
from src.assistant.scheduling.cron import CronError, CronExpression, parse_cron
from src.assistant.scheduling.delivery import (
    DeliveryOutcome,
    DeliveryPayload,
    DispatchResult,
    MemoryDeliveryLedger,
    MultiSlotDispatcher,
    SameSourceProvider,
)
from src.assistant.scheduling.gate import CalendarProbe, GateDecision, GateReason, TradingDayGate
from src.assistant.scheduling.runner import AssistantScheduler, TickResult

__all__ = [
    "AssistantScheduler",
    "CalendarProbe",
    "CronError",
    "CronExpression",
    "DEFAULT_FETCH_TIME",
    "DEFAULT_PUSH_TIME",
    "DEFAULT_TIMEZONE",
    "DayPlan",
    "DeliveryOutcome",
    "DeliveryPayload",
    "DispatchResult",
    "EntryKind",
    "FirePoint",
    "GateDecision",
    "GateReason",
    "MemoryDeliveryLedger",
    "MultiSlotDispatcher",
    "NonTradingAction",
    "SameSourceProvider",
    "ScheduleConfig",
    "ScheduleEntry",
    "TickResult",
    "TradingDayGate",
    "parse_cron",
    "parse_env_config",
]
