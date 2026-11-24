# -*- coding: utf-8 -*-
"""多时点同源投递测试（需求 B50 ③ / FR-9.13 ③④）。"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from src.assistant.data.storage import AssistantStore
from src.assistant.scheduling.config import EntryKind, FirePoint, ScheduleEntry
from src.assistant.scheduling.delivery import (
    DeliveryOutcome,
    DeliveryPayload,
    MemoryDeliveryLedger,
    MultiSlotDispatcher,
    SameSourceProvider,
)

DAY = date(2026, 11, 23)


def payload(snapshot_id: str = "20261123-aaaa", text: str = "固定正文") -> DeliveryPayload:
    return DeliveryPayload(
        data_date=DAY,
        snapshot_id=snapshot_id,
        calc_version="c1",
        param_hash="ph-001",
        text=text,
    )


def point(hour: int, minute: int = 0, raw: str | None = None) -> FirePoint:
    entry = ScheduleEntry(
        raw=raw or f"{hour:02d}:{minute:02d}", kind=EntryKind.HHMM, at=time(hour, minute)
    )
    return FirePoint(when=datetime(2026, 11, 23, hour, minute), entry=entry)


class Sender:
    def __init__(self, result: bool = True) -> None:
        self.sent: list[DeliveryPayload] = []
        self.result = result
        self.error: Exception | None = None

    def __call__(self, item: DeliveryPayload) -> bool:
        if self.error is not None:
            raise self.error
        if self.result:
            self.sent.append(item)
        return self.result


# ---- 同源 provider --------------------------------------------------------
def test_same_source_provider_builds_once_per_day() -> None:
    calls: list[date] = []

    def build(day: date) -> DeliveryPayload:
        calls.append(day)
        return payload()

    provider = SameSourceProvider(build)
    first = provider.payload_for(DAY)
    second = provider.payload_for(DAY)
    assert calls == [DAY], "同一天只应生成一次"
    assert first is second, "必须返回同一对象，杜绝任何字段漂移"


def test_same_source_provider_rebuilds_for_new_day() -> None:
    provider = SameSourceProvider(lambda day: payload(snapshot_id=f"{day:%Y%m%d}-x"))
    a = provider.payload_for(DAY)
    b = provider.payload_for(date(2026, 11, 24))
    assert a.snapshot_id != b.snapshot_id
    assert not a.same_source_as(b)


# ---- 单时点去重 -----------------------------------------------------------
def test_first_dispatch_sends_and_second_is_duplicate() -> None:
    ledger = MemoryDeliveryLedger()
    sender = Sender()
    dispatcher = MultiSlotDispatcher(ledger, sender)

    first = dispatcher.dispatch(point(20), payload())
    assert first.outcome is DeliveryOutcome.SENT
    assert ledger.delivery_done("2026-11-23T20:00")

    again = dispatcher.dispatch(point(20), payload())
    assert again.outcome is DeliveryOutcome.DUPLICATE
    assert len(sender.sent) == 1, "同一时点不得重复投递"


# ---- 多时点同源 -----------------------------------------------------------
def test_two_slots_send_identical_payload() -> None:
    ledger = MemoryDeliveryLedger()
    sender = Sender()
    dispatcher = MultiSlotDispatcher(ledger, sender)
    provider = SameSourceProvider(lambda day: payload())

    for slot in (point(8), point(20)):
        result = dispatcher.dispatch(slot, provider.payload_for(DAY))
        assert result.outcome is DeliveryOutcome.SENT

    assert len(sender.sent) == 2
    assert sender.sent[0].same_source_as(sender.sent[1])
    assert {item.snapshot_id for item in sender.sent} == {"20261123-aaaa"}
    assert ledger.delivered_snapshot_for(DAY) == "20261123-aaaa"


def test_two_slots_at_same_minute_dedupe_via_slot_key() -> None:
    ledger = MemoryDeliveryLedger()
    sender = Sender()
    dispatcher = MultiSlotDispatcher(ledger, sender)
    a = point(20, raw="20:00")
    b = point(20, raw="0 20 * * *")
    assert dispatcher.dispatch(a, payload()).outcome is DeliveryOutcome.SENT
    assert dispatcher.dispatch(b, payload()).outcome is DeliveryOutcome.DUPLICATE
    assert len(sender.sent) == 1


def test_different_snapshot_same_day_is_blocked() -> None:
    """当日已投递快照 A，第二个时点却给出快照 B → 说明中途重算，必须拦下留痕。"""
    ledger = MemoryDeliveryLedger()
    sender = Sender()
    dispatcher = MultiSlotDispatcher(ledger, sender)

    assert dispatcher.dispatch(point(8), payload("20261123-aaaa")).outcome is DeliveryOutcome.SENT
    blocked = dispatcher.dispatch(point(20), payload("20261123-bbbb"))

    assert blocked.outcome is DeliveryOutcome.BLOCKED
    assert "20261123-aaaa" in blocked.reason
    assert len(sender.sent) == 1, "不得把两份不同结论都发出去"
    assert ledger.delivered_snapshot_for(DAY) == "20261123-aaaa"


# ---- 失败与重试 -----------------------------------------------------------
def test_sender_exception_releases_slot_for_retry() -> None:
    ledger = MemoryDeliveryLedger()
    sender = Sender()
    dispatcher = MultiSlotDispatcher(ledger, sender)

    sender.error = RuntimeError("webhook 超时")
    failed = dispatcher.dispatch(point(20), payload())
    assert failed.outcome is DeliveryOutcome.FAILED
    assert "webhook 超时" not in failed.reason  # 不泄露长堆栈，但要能定位类型
    assert "RuntimeError" in failed.reason
    assert not ledger.delivery_done("2026-11-23T20:00")

    sender.error = None
    retried = dispatcher.dispatch(point(20), payload())
    assert retried.outcome is DeliveryOutcome.SENT
    assert ledger.snapshot()["2026-11-23T20:00"]["attempts"] == 2


def test_sender_false_releases_slot() -> None:
    ledger = MemoryDeliveryLedger()
    dispatcher = MultiSlotDispatcher(ledger, Sender(result=False))
    assert dispatcher.dispatch(point(20), payload()).outcome is DeliveryOutcome.FAILED
    assert not ledger.delivery_done("2026-11-23T20:00")


# ---- 补投 ----------------------------------------------------------------
def test_dispatch_day_only_fires_points_up_to_now() -> None:
    ledger = MemoryDeliveryLedger()
    sender = Sender()
    dispatcher = MultiSlotDispatcher(ledger, sender)
    points = (point(8), point(20))

    results = dispatcher.dispatch_day(points, datetime(2026, 11, 23, 12, 0), payload())
    assert [r.outcome for r in results] == [DeliveryOutcome.SENT]
    assert len(sender.sent) == 1

    results = dispatcher.dispatch_day(points, datetime(2026, 11, 23, 20, 5), payload())
    outcomes = [r.outcome for r in results]
    assert outcomes == [DeliveryOutcome.DUPLICATE, DeliveryOutcome.SENT]


# ---- SQLite 台账（重启后仍防重）-------------------------------------------
@pytest.fixture()
def store(tmp_path) -> AssistantStore:
    s = AssistantStore(tmp_path / "delivery.db")
    s.create_all()
    return s


def test_store_ledger_persists_across_instances(tmp_path) -> None:
    db = tmp_path / "delivery.db"
    first = AssistantStore(db)
    first.create_all()
    assert first.claim_delivery(
        "2026-11-23T20:00", data_date=DAY, snapshot_id="20261123-aaaa", slot_label="20:00"
    )
    first.mark_delivery_sent("2026-11-23T20:00")
    assert first.delivery_done("2026-11-23T20:00")

    reopened = AssistantStore(db)
    assert reopened.delivery_done("2026-11-23T20:00"), "进程重启后仍需防重"
    assert reopened.delivered_snapshot_for(DAY) == "20261123-aaaa"
    assert not reopened.claim_delivery(
        "2026-11-23T20:00", data_date=DAY, snapshot_id="20261123-aaaa", slot_label="20:00"
    )


def test_store_ledger_allows_retry_after_release() -> None:
    store = AssistantStore(":memory:")
    store.create_all()
    key = "2026-11-23T20:00"
    assert store.claim_delivery(key, data_date=DAY, snapshot_id="s1", slot_label="20:00")
    store.release_delivery(key, "webhook 500")
    assert not store.delivery_done(key)
    assert store.claim_delivery(key, data_date=DAY, snapshot_id="s1", slot_label="20:00")
    store.mark_delivery_sent(key)
    assert store.delivery_done(key)
    log = store.delivery_log(DAY)
    assert log[0]["attempts"] == 2
    assert log[0]["status"] == "sent"


def test_dispatcher_works_with_store_ledger() -> None:
    store = AssistantStore(":memory:")
    store.create_all()
    sender = Sender()
    dispatcher = MultiSlotDispatcher(store, sender)
    assert dispatcher.dispatch(point(20), payload()).outcome is DeliveryOutcome.SENT
    assert dispatcher.dispatch(point(20), payload()).outcome is DeliveryOutcome.DUPLICATE
    assert len(sender.sent) == 1
