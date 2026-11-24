# -*- coding: utf-8 -*-
"""调度节拍测试（门控 → 排程 → 同源生成 → 按点投递）。"""

from __future__ import annotations

from datetime import date, datetime

from src.assistant.scheduling.config import NonTradingAction, parse_env_config
from src.assistant.scheduling.delivery import (
    DeliveryOutcome,
    DeliveryPayload,
    MemoryDeliveryLedger,
    MultiSlotDispatcher,
)
from src.assistant.scheduling.gate import GateReason, TradingDayGate
from src.assistant.scheduling.runner import AssistantScheduler

DAY = date(2026, 11, 24)  # 周二


class Builder:
    """假的「生成」：记录被调用次数，返回固定快照。"""

    def __init__(self) -> None:
        self.calls: list[date] = []

    def __call__(self, day: date) -> DeliveryPayload:
        self.calls.append(day)
        return DeliveryPayload(
            data_date=day,
            snapshot_id=f"{day:%Y%m%d}-snap",
            calc_version="c1",
            param_hash="ph-001",
            text="固定正文",
        )


def make(env: dict[str, str] | None, probe, *, action: NonTradingAction = NonTradingAction.SKIP):
    config = parse_env_config(env or {})
    gate = TradingDayGate(action=action, probe=probe)
    sender_calls: list[DeliveryPayload] = []
    dispatcher = MultiSlotDispatcher(MemoryDeliveryLedger(), lambda p: sender_calls.append(p) or True)
    builder = Builder()
    scheduler = AssistantScheduler(
        config=config, gate=gate, dispatcher=dispatcher, generate=builder
    )
    return scheduler, builder, sender_calls


# ---- 交易日正常链路 -------------------------------------------------------
def test_trading_day_generates_once_and_sends_due_slots() -> None:
    scheduler, builder, sent = make({"ASSISTANT_PUSH_SCHEDULE": "19:00,20:00"}, lambda now: True)

    first = scheduler.tick(datetime(2026, 11, 24, 19, 0))
    assert first.generated is True, "首个取数后时点应生成当日快照"
    assert builder.calls == [DAY], "当日只生成一次"
    assert first.sent == 1
    assert len(sent) == 1

    later = scheduler.tick(datetime(2026, 11, 24, 20, 0))
    assert later.generated is False, "第二个时点复用缓存，不得重新生成"
    assert builder.calls == [DAY]
    assert later.sent == 1
    assert len(sent) == 2
    assert sent[0].same_source_as(sent[1]), "两个时点必须同源同文"


def test_no_generation_before_fetch_time() -> None:
    """12:00 早于取数 17:30：不生成当日快照（数据还没到）。"""
    scheduler, builder, sent = make({"ASSISTANT_PUSH_SCHEDULE": "20:00"}, lambda now: True)
    result = scheduler.tick(datetime(2026, 11, 24, 12, 0))
    assert result.generated is False
    assert builder.calls == []
    assert sent == []


def test_pre_fetch_slot_is_skipped_never_pushes_stale_snapshot() -> None:
    """已确认口径（2026-11-24）：早于取数的时点**一律不投递**。

    配 ``08:00,20:00`` 时只有 20:00 真的推——早上拿不到当日快照，推前一交易日的结论
    等于用旧意见冒充当日意见。
    """
    scheduler, builder, sent = make({"ASSISTANT_PUSH_SCHEDULE": "08:00,20:00"}, lambda now: True)

    morning = scheduler.tick(datetime(2026, 11, 24, 8, 5))
    assert morning.sent == 0, "早于取数的时点不得投递"
    assert morning.generated is False, "早于取数也不生成当日快照"
    assert builder.calls == []
    assert any("已跳过早于取数时点" in warning for warning in morning.warnings)

    evening = scheduler.tick(datetime(2026, 11, 24, 20, 0))
    assert evening.generated is True
    assert evening.sent == 1
    assert len(sent) == 1, "整天只应有 20:00 这一条推送"
    assert sent[0].snapshot_id.startswith("20261124")


def test_only_2000_pushes_when_configured_with_earlier_slots() -> None:
    """跨多个早时点反复 tick，仍然只有 20:00 会发。"""
    scheduler, _, sent = make({"ASSISTANT_PUSH_SCHEDULE": "08:00,12:00,20:00"}, lambda now: True)
    for hour, minute in [(8, 5), (12, 0), (17, 0), (19, 59)]:
        assert scheduler.tick(datetime(2026, 11, 24, hour, minute)).sent == 0
    assert scheduler.tick(datetime(2026, 11, 24, 20, 0)).sent == 1
    assert len(sent) == 1


def test_repeated_tick_after_last_slot_sends_nothing() -> None:
    scheduler, builder, sent = make({}, lambda now: True)
    scheduler.tick(datetime(2026, 11, 24, 21, 0))
    again = scheduler.tick(datetime(2026, 11, 24, 21, 5))
    assert again.sent == 0
    assert len(sent) == 1
    assert builder.calls == [DAY]


def test_before_first_slot_nothing_sent_but_generation_may_warm() -> None:
    """未到点时也先生成（供 Web 查看），但不投递。"""
    scheduler, builder, sent = make({"ASSISTANT_PUSH_SCHEDULE": "20:00"}, lambda now: True)
    result = scheduler.tick(datetime(2026, 11, 24, 18, 0))
    assert builder.calls == [DAY]
    assert result.sent == 0
    assert sent == []


# ---- 非交易日 -------------------------------------------------------------
def test_non_trading_day_skip_produces_nothing() -> None:
    scheduler, builder, sent = make({}, lambda now: False)
    result = scheduler.tick(datetime(2026, 11, 28, 20, 0))
    assert result.gate.reason is GateReason.NON_TRADING
    assert result.generated is False
    assert builder.calls == []
    assert sent == []
    assert result.notified is False


def test_non_trading_day_notify_skip_notifies_once_per_day() -> None:
    notices: list[date] = []

    def notify(day: date, decision) -> bool:
        notices.append(day)
        return True

    config = parse_env_config({})
    dispatcher = MultiSlotDispatcher(MemoryDeliveryLedger(), lambda p: True)
    builder = Builder()
    scheduler = AssistantScheduler(
        config=config,
        gate=TradingDayGate(action=NonTradingAction.NOTIFY_SKIP, probe=lambda now: False),
        dispatcher=dispatcher,
        generate=builder,
        notify_non_trading=notify,
    )
    first = scheduler.tick(datetime(2026, 11, 28, 20, 0))
    second = scheduler.tick(datetime(2026, 11, 28, 20, 30))
    assert first.notified is True
    assert second.notified is False, "同日只提醒一次"
    assert notices == [date(2026, 11, 28)]
    assert builder.calls == [], "notify_skip 也不得生成"


# ---- fail-closed ----------------------------------------------------------
def test_calendar_unavailable_produces_nothing() -> None:
    scheduler, builder, sent = make({}, lambda now: None)
    result = scheduler.tick(datetime(2026, 11, 24, 20, 0))
    assert result.gate.reason is GateReason.CALENDAR_UNAVAILABLE
    assert result.generated is False
    assert builder.calls == []
    assert sent == []
    assert any("fail-closed" in warning for warning in result.warnings)


def test_calendar_exception_produces_nothing() -> None:
    def boom(now: datetime) -> bool:
        raise RuntimeError("日历库崩了")

    scheduler, builder, _ = make({}, boom)
    result = scheduler.tick(datetime(2026, 11, 24, 20, 0))
    assert result.generated is False
    assert builder.calls == []


# ---- 异常与重投 -----------------------------------------------------------
def test_send_failure_can_be_retried_on_next_tick() -> None:
    config = parse_env_config({})
    attempts: list[int] = []
    payloads: list[DeliveryPayload] = []

    def sender(payload: DeliveryPayload) -> bool:
        payloads.append(payload)
        attempts.append(len(payloads))
        return len(payloads) > 1  # 第一次失败、第二次成功

    scheduler = AssistantScheduler(
        config=config,
        gate=TradingDayGate(probe=lambda now: True),
        dispatcher=MultiSlotDispatcher(MemoryDeliveryLedger(), sender),
        generate=Builder(),
    )
    first = scheduler.tick(datetime(2026, 11, 24, 20, 0))
    assert [r.outcome for r in first.dispatches] == [DeliveryOutcome.FAILED]

    second = scheduler.tick(datetime(2026, 11, 24, 20, 1))
    assert [r.outcome for r in second.dispatches] == [DeliveryOutcome.SENT]


def test_warnings_are_merged_from_config_and_plan() -> None:
    scheduler, _, _ = make({"ASSISTANT_PUSH_SCHEDULE": "08:00,20:00"}, lambda now: True)
    result = scheduler.tick(datetime(2026, 11, 24, 12, 0))
    assert any("早于取数时点" in warning for warning in result.warnings)
