# -*- coding: utf-8 -*-
"""多时点同源投递（需求 B50 ③ / FR-9.13 ③④，技术方案 §6.3）。

三条不变量：

1. **当日快照只生成一次**：payload 由 :class:`SameSourceProvider` 按 ``data_date``
   缓存，各时点拿到的是**同一个对象**（``snapshot_id`` / ``text`` / ``calc_version``
   逐字相同）。
2. **各时点内容逐字相同**：若某时点传来的 ``snapshot_id`` 与当日已投递的不一致，
   说明中途重算过 → 直接 ``BLOCKED`` 并留痕，不投递，不把两份不同结论都发出去。
3. **每个时点各投一次、不重复也不互吞**：去重键精确到分钟
   （``2026-11-24T20:00``），所以 ``20:00`` 与 ``20:30`` 是两个时点、各投一次；
   同一时点因进程重启二次触发则判 ``DUPLICATE``。

台账接口是 duck-typed（``claim_delivery`` / ``mark_delivery_sent`` /
``release_delivery`` / ``delivery_done`` / ``delivered_snapshot_for``）：内存实现见
本模块，SQLite 实现见 :class:`src.assistant.data.storage.AssistantStore`（重启后
仍能防重，这是内存实现做不到的）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Callable, Protocol, Optional

from src.assistant.scheduling.config import FirePoint

__all__ = [
    "DeliveryOutcome",
    "DeliveryPayload",
    "DispatchResult",
    "MemoryDeliveryLedger",
    "MultiSlotDispatcher",
    "SameSourceProvider",
]


@dataclass(frozen=True)
class DeliveryPayload:
    """一次投递的内容。``data_date`` 是**数据截止日**（T-1 收盘口径见需求 §5）。"""

    data_date: date
    snapshot_id: str
    calc_version: str
    param_hash: str
    text: str
    #: 数据不是当日新取的（缓存兜底 / 早于取数时点复用旧快照）
    stale: bool = False

    def same_source_as(self, other: DeliveryPayload) -> bool:
        """同源判定：快照与规则版本必须完全一致（正文也须逐字相同）。"""
        return (
            self.data_date == other.data_date
            and self.snapshot_id == other.snapshot_id
            and self.calc_version == other.calc_version
            and self.param_hash == other.param_hash
            and self.text == other.text
        )


class DeliveryOutcome(str, Enum):
    SENT = "sent"
    DUPLICATE = "duplicate"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True)
class DispatchResult:
    outcome: DeliveryOutcome
    slot_key: str
    reason: str
    payload: Optional[DeliveryPayload] = None


class DeliveryLedger(Protocol):
    """投递台账最小接口。"""

    def claim_delivery(
        self, slot_key: str, *, data_date: date, snapshot_id: str, slot_label: str
    ) -> bool:
        """领取投递权；已发送过返回 ``False``。"""

    def mark_delivery_sent(self, slot_key: str) -> None:
        """标记已发送。"""

    def release_delivery(self, slot_key: str, error: str) -> None:
        """发送失败，释放供下次重试。"""

    def delivery_done(self, slot_key: str) -> bool:
        """该时点是否已成功投递。"""

    def delivered_snapshot_for(self, data_date: date) -> Optional[str]:
        """当日已成功投递的快照 id（同源校验用）。"""


@dataclass
class _Record:
    slot_key: str
    data_date: date
    snapshot_id: str
    slot_label: str
    status: str  # claimed / sent / failed
    attempts: int = 1
    error: str = ""


@dataclass
class MemoryDeliveryLedger:
    """进程内台账（测试与单机短跑可用；重启后失效）。"""

    _records: dict[str, _Record] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def claim_delivery(
        self, slot_key: str, *, data_date: date, snapshot_id: str, slot_label: str
    ) -> bool:
        with self._lock:
            return self._claim_locked(slot_key, data_date, snapshot_id, slot_label)

    def _claim_locked(self, slot_key: str, data_date: date, snapshot_id: str, slot_label: str) -> bool:
        existing = self._records.get(slot_key)
        if existing is not None and existing.status == "sent":
            return False
        if existing is not None:
            existing.attempts += 1
            existing.status = "claimed"
            return True
        self._records[slot_key] = _Record(slot_key, data_date, snapshot_id, slot_label, "claimed")
        return True

    def mark_delivery_sent(self, slot_key: str) -> None:
        with self._lock:
            record = self._records.get(slot_key)
            if record is not None:
                record.status = "sent"
                record.error = ""

    def release_delivery(self, slot_key: str, error: str) -> None:
        with self._lock:
            record = self._records.get(slot_key)
            if record is not None:
                record.status = "failed"
                record.error = error

    def delivery_done(self, slot_key: str) -> bool:
        with self._lock:
            record = self._records.get(slot_key)
            return record is not None and record.status == "sent"

    def delivered_snapshot_for(self, data_date: date) -> Optional[str]:
        with self._lock:
            for record in self._records.values():
                if record.data_date == data_date and record.status == "sent":
                    return record.snapshot_id
            return None

    # 便于测试与排查
    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {
                key: {
                    "status": rec.status,
                    "attempts": rec.attempts,
                    "snapshot_id": rec.snapshot_id,
                    "error": rec.error,
                }
                for key, rec in self._records.items()
            }


@dataclass
class SameSourceProvider:
    """按 ``data_date`` 缓存 payload，保证同一天各时点拿到**同一份**内容。"""

    build: Callable[[date], DeliveryPayload]
    _cache: dict[date, DeliveryPayload] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def payload_for(self, day: date) -> DeliveryPayload:
        with self._lock:
            cached = self._cache.get(day)
            if cached is not None:
                return cached
            payload = self.build(day)
            self._cache[day] = payload
            return payload

    def has(self, day: date) -> bool:
        """当日是否已生成（用于区分「本次新建」与「复用缓存」）。"""
        with self._lock:
            return day in self._cache

    def clear(self, day: Optional[date] = None) -> None:
        with self._lock:
            if day is None:
                self._cache.clear()
            else:
                self._cache.pop(day, None)


@dataclass
class MultiSlotDispatcher:
    """把「某时点该不该投、投什么」这件事收敛到一处。"""

    ledger: DeliveryLedger
    sender: Callable[[DeliveryPayload], bool]

    def dispatch(self, point: FirePoint, payload: DeliveryPayload) -> DispatchResult:
        slot_key = point.slot_key

        if self.ledger.delivery_done(slot_key):
            return DispatchResult(
                DeliveryOutcome.DUPLICATE, slot_key, "该时点已投递过同一份快照", payload
            )

        # 同源不变量：当日已投递的 snapshot_id 必须与本次一致，否则说明中途重算
        delivered = self.ledger.delivered_snapshot_for(payload.data_date)
        if delivered is not None and delivered != payload.snapshot_id:
            return DispatchResult(
                DeliveryOutcome.BLOCKED,
                slot_key,
                f"当日已投递快照 {delivered}，本次为 {payload.snapshot_id}（当日快照应只生成一次）",
                payload,
            )

        if not self.ledger.claim_delivery(
            slot_key,
            data_date=payload.data_date,
            snapshot_id=payload.snapshot_id,
            slot_label=point.entry.raw,
        ):
            return DispatchResult(
                DeliveryOutcome.DUPLICATE, slot_key, "该时点已被其它进程领取", payload
            )

        try:
            ok = bool(self.sender(payload))
        except Exception as exc:  # noqa: BLE001 - 发送异常不应带走调度循环
            self.ledger.release_delivery(slot_key, f"{type(exc).__name__}: {exc}")
            return DispatchResult(
                DeliveryOutcome.FAILED, slot_key, f"发送异常：{type(exc).__name__}", payload
            )

        if not ok:
            self.ledger.release_delivery(slot_key, "sender 返回 False")
            return DispatchResult(DeliveryOutcome.FAILED, slot_key, "发送失败，已释放供下次重试", payload)

        self.ledger.mark_delivery_sent(slot_key)
        return DispatchResult(DeliveryOutcome.SENT, slot_key, "已投递", payload)

    def dispatch_day(
        self, points: tuple[FirePoint, ...], now: datetime, payload: DeliveryPayload
    ) -> tuple[DispatchResult, ...]:
        """投递 ``now`` 之前（含）尚未处理的时点，供「错过的时点补投」使用。"""
        results: list[DispatchResult] = []
        for point in points:
            if point.when > now:
                continue
            results.append(self.dispatch(point, payload))
        return tuple(results)
