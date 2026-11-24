# -*- coding: utf-8 -*-
"""交易日门控（需求 FR-8.7 / FR-8.6，技术方案 §2.4）。

**核心是 fail-closed**：判不出交易日就按「非交易日」处理并告警，绝不因为配置的时点
命中就产出内容。这是与 ``is_market_open()`` 的关键区别——后者在日历不可用时返回
``True``（fail-open），节假日会误推送，方案明确禁止用它做门控。

判定链：

1. 调 ``build_market_phase_context(market="cn")`` 取 ``is_trading_day``；
2. ``True`` → 交易日，进入生成与投递；
3. ``False`` → 非交易日，按 ``non_trading_action`` 处置（两值都**不生成**快照/卡片）；
4. ``None`` / 抛异常 / 日历库缺失 → **按非交易日处理** + 告警（fail-closed）。

时区：``now`` 为 naive 时按配置时区补全，避免服务器 UTC 与其跨日不一致（FR-8.7 ④）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from src.assistant.scheduling.config import DEFAULT_TIMEZONE, NonTradingAction

__all__ = ["GateReason", "GateDecision", "TradingDayGate", "CalendarProbe"]

#: 交易日探针：输入本地时间，返回 True/False/None（None = 判不出来）
CalendarProbe = Callable[[datetime], Optional[bool]]


class GateReason(str, Enum):
    TRADING_DAY = "trading_day"
    NON_TRADING = "non_trading"
    CALENDAR_UNAVAILABLE = "calendar_unavailable"


@dataclass(frozen=True)
class GateDecision:
    """门控结论。"""

    is_trading_day: Optional[bool]
    should_generate: bool
    should_notify: bool
    reason: GateReason
    action: NonTradingAction
    market_local_time: datetime
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "is_trading_day": self.is_trading_day,
            "should_generate": self.should_generate,
            "should_notify": self.should_notify,
            "reason": self.reason.value,
            "non_trading_action": self.action.value,
            "market_local_time": self.market_local_time.isoformat(),
            "warnings": list(self.warnings),
        }


def _default_probe(market: str) -> CalendarProbe:
    """默认探针：复用主流程 ``src/core/trading_calendar.py``。

    延迟 import——该模块依赖 pandas / exchange-calendars，调度层其余部分不需要。
    """

    def probe(now: datetime) -> Optional[bool]:
        from src.core.trading_calendar import build_market_phase_context

        ctx = build_market_phase_context(market=market, current_time=now)
        return ctx.is_trading_day

    return probe


@dataclass
class TradingDayGate:
    """交易日门控。``probe`` 可注入（测试与离线环境用假日历）。"""

    market: str = "cn"
    action: NonTradingAction = NonTradingAction.SKIP
    timezone: str = DEFAULT_TIMEZONE
    probe: Optional[CalendarProbe] = None
    _warnings: list[str] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.probe is None:
            self.probe = _default_probe(self.market)

    def now(self, now: Optional[datetime] = None) -> datetime:
        """统一到配置时区的本地时间（naive 视为本地墙上时间）。"""
        tz = ZoneInfo(self.timezone)
        if now is None:
            return datetime.now(tz)
        if now.tzinfo is None:
            return now.replace(tzinfo=tz)
        return now.astimezone(tz)

    def decide(self, now: Optional[datetime] = None) -> GateDecision:
        local = self.now(now)
        assert self.probe is not None  # __post_init__ 保证

        try:
            is_trading_day = self.probe(local)
        except Exception as exc:  # noqa: BLE001 - 任何取历失败都必须 fail-closed
            return self._unavailable(local, f"交易日历调用失败：{type(exc).__name__}: {exc}")

        if is_trading_day is None:
            return self._unavailable(local, "交易日历返回 UNKNOWN（日历不可用或市场未识别）")

        if is_trading_day:
            return GateDecision(
                is_trading_day=True,
                should_generate=True,
                should_notify=False,  # 交易日不发「非交易日提醒」
                reason=GateReason.TRADING_DAY,
                action=self.action,
                market_local_time=local,
            )

        # 非交易日：两值都不生成快照/卡片，区别只在要不要发一条短提醒
        notify = self.action is NonTradingAction.NOTIFY_SKIP
        return GateDecision(
            is_trading_day=False,
            should_generate=False,
            should_notify=notify,
            reason=GateReason.NON_TRADING,
            action=self.action,
            market_local_time=local,
        )

    def _unavailable(self, local: datetime, message: str) -> GateDecision:
        """fail-closed：判不出来 → 按非交易日处理 + 告警。"""
        warning = f"{message}；按非交易日处理（fail-closed），本次不生成、不推送"
        return GateDecision(
            is_trading_day=None,
            should_generate=False,
            should_notify=False,
            reason=GateReason.CALENDAR_UNAVAILABLE,
            action=self.action,
            market_local_time=local,
            warnings=(warning,),
        )
