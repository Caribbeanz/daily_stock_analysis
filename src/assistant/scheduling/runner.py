# -*- coding: utf-8 -*-
"""调度节拍（一次 ``tick`` 的完整决策）。

把四件事按固定顺序串起来，顺序本身就是要保护的口径：

1. **门控先行**：先问交易日（``TradingDayGate``），判不出来就终止（fail-closed）。
   这一步必须在「读配置 / 触发生成」之前——否则非交易日也会白跑一遍生成。
2. **按日排程**：交易日才解析当天时点（``ScheduleConfig.plan_day``）。
3. **只在取数后生成并推送**：``now`` 早于 ``fetch_time`` 时不生成当日快照；
   **早于取数的时点一律不投递**（2026-11-24 确认口径：仅取数后的时点推送）。
4. **当日只生成一次、各时点同源**：``SameSourceProvider`` 按日缓存 payload；
   已投过的时点由台账拦下，进程重启也不重投。

关于第 3 条的边界：R24 允许把时点配到 ``08:00``（原文举例 ``08:00,20:00``），但当日
快照要 17:30 取数后才存在。此时若硬推，只能推前一交易日的结论——那等于用旧结论冒充
当日意见。**已确认的做法是跳过并告警**，即实际只在取数后的时点（默认 20:00）推送。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Optional

from src.assistant.scheduling.config import DayPlan, ScheduleConfig
from src.assistant.scheduling.delivery import (
    DeliveryPayload,
    DispatchResult,
    MultiSlotDispatcher,
    SameSourceProvider,
)
from src.assistant.scheduling.gate import GateDecision, TradingDayGate

__all__ = ["AssistantScheduler", "TickResult"]


@dataclass(frozen=True)
class TickResult:
    """一次节拍的结果（可直接序列化进日志）。"""

    now: datetime
    gate: GateDecision
    plan: Optional[DayPlan] = None
    payload: Optional[DeliveryPayload] = None
    dispatches: tuple[DispatchResult, ...] = ()
    notified: bool = False
    #: 本次是否**新建**了当日快照（同日第二次 tick 为 False）
    generated: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def sent(self) -> int:
        return sum(1 for item in self.dispatches if item.outcome.value == "sent")


@dataclass
class AssistantScheduler:
    """调度节拍器。

    - ``generate(day)``：生成当日快照并返回 payload（装配层注入，须幂等）
    - ``notify_non_trading(day, decision)``：``notify_skip`` 下的短提醒
    """

    config: ScheduleConfig
    gate: TradingDayGate
    dispatcher: MultiSlotDispatcher
    generate: Callable[[date], DeliveryPayload]
    notify_non_trading: Optional[Callable[[date, GateDecision], bool]] = None
    _provider: SameSourceProvider = field(init=False, repr=False)
    _notified_days: set[date] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        self._provider = SameSourceProvider(self.generate)

    def tick(self, now: Optional[datetime] = None) -> TickResult:
        decision = self.gate.decide(now)
        local = decision.market_local_time
        wall_clock = local.replace(tzinfo=None)  # 计划里是本地墙上时间（naive）
        day = wall_clock.date()
        warnings: list[str] = list(decision.warnings) + list(self.config.warnings)

        # 1) 非交易日 / 判不出来：零产出，必要时发一条短提醒
        if not decision.should_generate:
            notified = False
            if decision.should_notify and day not in self._notified_days:
                notified = self._notify(day, decision)
                if notified:
                    self._notified_days.add(day)
            return TickResult(now=local, gate=decision, notified=notified, warnings=tuple(warnings))

        # 2) 交易日：排当天时点
        plan = self.config.plan_day(day)
        warnings.extend(plan.warnings)
        if not plan.points:  # pragma: no cover - 交易日必有配置时点
            return TickResult(now=local, gate=decision, plan=plan, warnings=tuple(warnings))

        data_ready = wall_clock.time() >= self.config.fetch_time
        fresh_points = tuple(point for point in plan.points if point.source == "fresh")
        pre_points = tuple(point for point in plan.points if point.source == "pre_fetch")

        dispatches: list[DispatchResult] = []
        payload: Optional[DeliveryPayload] = None
        generated = False

        # 3a) 早于取数的时点：**一律不投递**（已确认口径：只有取数后的时点推送）。
        #     理由：那时当日快照还不存在，能拿到的只有前一交易日的结论；把它当「今天的
        #     推送」发出去，等于用旧结论冒充当日意见。宁可不发，也不混淆时点语义。
        if pre_points and any(point.when <= wall_clock for point in pre_points):
            warnings.append(
                f"已跳过早于取数时点 {self.config.fetch_time:%H:%M} 的推送"
                "（仅取数后的时点投递，避免用旧快照冒充当日结论）"
            )

        # 3b) 取数后的时点：当日快照只生成一次
        if fresh_points:
            if data_ready:
                generated = not self._provider.has(day)
                current = self._provider.payload_for(day)
                payload = current
                dispatches.extend(self.dispatcher.dispatch_day(fresh_points, wall_clock, current))
            elif any(point.when <= wall_clock for point in fresh_points):  # pragma: no cover
                warnings.append("尚未到取数时点，当日快照未生成")

        warnings.extend(item.reason for item in dispatches if item.outcome.value == "blocked")

        return TickResult(
            now=local,
            gate=decision,
            plan=plan,
            payload=payload,
            dispatches=tuple(dispatches),
            generated=generated,
            warnings=tuple(warnings),
        )

    def _notify(self, day: date, decision: GateDecision) -> bool:
        if self.notify_non_trading is None:
            return False
        try:
            return bool(self.notify_non_trading(day, decision))
        except Exception:  # noqa: BLE001 - 提醒失败不能影响主链路
            return False
