# -*- coding: utf-8 -*-
"""发送接线测试（payload → 既有通知链）。

三类断言：

1. **参数翻译**：路由类型、去重键＝``snapshot_id``、``allow_repeat``、纯文本降级
   变体是否按口径传下去；
2. **失败不伪装成功**：正文为空或服务返回 ``False`` 时必须返回 ``False``，让台账释放；
3. **降噪语义**：``allow_repeat`` 只跳过重复抑制（去重/冷却），静默时段照旧生效。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from types import SimpleNamespace

from src.assistant.notify import (
    ASSISTANT_ROUTE_TYPE,
    make_sender,
    non_trading_text,
    notify_non_trading,
    send_payload,
)
from src.assistant.scheduling.config import EntryKind, FirePoint, ScheduleEntry
from src.assistant.scheduling.delivery import (
    DeliveryOutcome,
    DeliveryPayload,
    MemoryDeliveryLedger,
    MultiSlotDispatcher,
)
from src.assistant.scheduling.gate import GateReason
from src.notification_noise import (
    evaluate_notification_noise,
    record_notification_noise,
    reset_notification_noise_state,
)
from src.notification_routing import NOTIFICATION_ROUTE_CONFIGS

DAY = date(2026, 11, 27)
TEXT = "📊 **2026-11-27** 收盘\n✅ ➕ 加仓 600000"


def payload(text: str = TEXT, *, snapshot_id: str = "20261127-abc123") -> DeliveryPayload:
    return DeliveryPayload(
        data_date=DAY,
        snapshot_id=snapshot_id,
        calc_version="rules-v1/p1",
        param_hash="p1",
        text=text,
        bar_date=DAY,
    )


@dataclass
class FakeService:
    """通知服务替身：记录调用参数，可按需返回失败。"""

    ok: bool = True
    calls: list[dict] = field(default_factory=list)

    def send(self, content: str, **kwargs) -> bool:
        self.calls.append({"content": content, **kwargs})
        return self.ok


class TestSendPayload:
    def test_translates_payload_to_route_and_dedupe_key(self) -> None:
        service = FakeService()
        assert send_payload(payload(), service=service) is True

        call = service.calls[0]
        assert call["content"] == TEXT
        assert call["route_type"] == ASSISTANT_ROUTE_TYPE == "assistant_daily"
        assert call["dedup_key"] == "20261127-abc123"
        assert call["allow_repeat"] is True

    def test_fallback_variant_has_markdown_stripped(self) -> None:
        service = FakeService()
        send_payload(payload(), service=service)

        fallback = service.calls[0]["fallback_content"]
        assert "**" not in fallback
        assert "加仓 600000" in fallback  # 只剥标记，不动内容

    def test_empty_text_is_not_sent(self) -> None:
        service = FakeService()
        assert send_payload(payload("   \n"), service=service) is False
        assert service.calls == []

    def test_service_failure_is_reported_as_failure(self) -> None:
        service = FakeService(ok=False)
        assert send_payload(payload(), service=service) is False

    def test_make_sender_matches_dispatcher_signature(self) -> None:
        service = FakeService()
        sender = make_sender(service)
        assert sender(payload()) is True
        assert service.calls[0]["route_type"] == ASSISTANT_ROUTE_TYPE


class TestDispatcherWiring:
    def test_each_slot_delivers_once(self) -> None:
        service = FakeService()
        ledger = MemoryDeliveryLedger()
        entry = ScheduleEntry(raw="20:00", kind=EntryKind.HHMM, at=time(20, 0))
        points = (
            FirePoint(entry=entry, when=datetime(2026, 11, 27, 20, 0), source="fresh"),
            FirePoint(
                entry=ScheduleEntry(raw="20:30", kind=EntryKind.HHMM, at=time(20, 30)),
                when=datetime(2026, 11, 27, 20, 30),
                source="fresh",
            ),
        )
        dispatcher = MultiSlotDispatcher(ledger=ledger, sender=make_sender(service))

        results = dispatcher.dispatch_day(points, datetime(2026, 11, 27, 21, 0), payload())

        assert [item.outcome for item in results] == [DeliveryOutcome.SENT, DeliveryOutcome.SENT]
        assert len(service.calls) == 2  # 两个时点各投一次，内容同源

    def test_second_call_on_same_slot_is_duplicate(self) -> None:
        service = FakeService()
        ledger = MemoryDeliveryLedger()
        point = FirePoint(
            entry=ScheduleEntry(raw="20:00", kind=EntryKind.HHMM, at=time(20, 0)),
            when=datetime(2026, 11, 27, 20, 0),
            source="fresh",
        )
        dispatcher = MultiSlotDispatcher(ledger=ledger, sender=make_sender(service))

        assert dispatcher.dispatch(point, payload()).outcome is DeliveryOutcome.SENT
        assert dispatcher.dispatch(point, payload()).outcome is DeliveryOutcome.DUPLICATE
        assert len(service.calls) == 1


class TestNonTradingNotify:
    def test_text_says_no_conclusion(self) -> None:
        text = non_trading_text(DAY)
        assert "2026-11-27" in text
        assert "非交易日" in text
        assert "20:00" in text  # 交代何时恢复

    def test_dedupe_key_is_per_day(self) -> None:
        service = FakeService()
        decision = SimpleNamespace(reason=GateReason.NON_TRADING)

        assert notify_non_trading(DAY, decision, service=service) is True
        call = service.calls[0]
        assert call["dedup_key"] == "assistant-skip-2026-11-27"
        assert call["route_type"] == ASSISTANT_ROUTE_TYPE
        assert "**" not in call["fallback_content"]


class TestRouteRegistration:
    def test_route_registered_with_env_key(self) -> None:
        route = NOTIFICATION_ROUTE_CONFIGS["assistant_daily"]
        assert route["env_key"] == "NOTIFICATION_ASSISTANT_CHANNELS"
        assert route["config_attr"] == "notification_assistant_channels"

    def test_existing_routes_untouched(self) -> None:
        assert set(NOTIFICATION_ROUTE_CONFIGS) == {
            "report",
            "alert",
            "system_error",
            "assistant_daily",
        }


class TestNoiseAllowRepeat:
    """``allow_repeat`` 只跳过重复抑制，静默时段与最低级别照旧。"""

    def setup_method(self) -> None:
        reset_notification_noise_state()

    def teardown_method(self) -> None:
        reset_notification_noise_state()

    @staticmethod
    def config(**kwargs):
        base = {
            "notification_dedup_ttl_seconds": 600,
            "notification_cooldown_seconds": 600,
            "notification_quiet_hours": "",
            "notification_timezone": "",
            "notification_min_severity": "",
        }
        base.update(kwargs)
        return SimpleNamespace(**base)

    def test_plain_repeat_is_deduped(self) -> None:
        config = self.config()
        now = datetime(2026, 11, 27, 20, 0)
        first = evaluate_notification_noise(
            config, content=TEXT, route_type=ASSISTANT_ROUTE_TYPE,
            dedup_key="20261127-abc123", now=now,
        )
        # 真实链路里由 NotificationService 在发送成功后落状态
        record_notification_noise(first, now=now)
        second = evaluate_notification_noise(
            config, content=TEXT, route_type=ASSISTANT_ROUTE_TYPE,
            dedup_key="20261127-abc123", now=now,
        )

        assert first.should_send is True
        assert second.should_send is False
        assert second.reason_code == "dedup"

    def test_allow_repeat_bypasses_dedup_and_cooldown(self) -> None:
        config = self.config()
        now = datetime(2026, 11, 27, 20, 0)
        for _ in range(3):
            decision = evaluate_notification_noise(
                config, content=TEXT, route_type=ASSISTANT_ROUTE_TYPE,
                dedup_key="20261127-abc123", allow_repeat=True, now=now,
            )
            assert decision.should_send is True

    def test_allow_repeat_still_respects_quiet_hours(self) -> None:
        config = self.config(notification_quiet_hours="19:00-21:00")
        decision = evaluate_notification_noise(
            config, content=TEXT, route_type=ASSISTANT_ROUTE_TYPE,
            dedup_key="20261127-abc123", allow_repeat=True,
            now=datetime(2026, 11, 27, 20, 0),
        )

        assert decision.should_send is False
        assert decision.reason_code == "quiet_hours"

    def test_allow_repeat_still_respects_min_severity(self) -> None:
        config = self.config(notification_min_severity="warning")
        decision = evaluate_notification_noise(
            config, content=TEXT, route_type=ASSISTANT_ROUTE_TYPE,
            dedup_key="20261127-abc123", allow_repeat=True,
            now=datetime(2026, 11, 27, 20, 0),
        )

        assert decision.should_send is False
        assert decision.reason_code == "min_severity"
