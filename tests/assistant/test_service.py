# -*- coding: utf-8 -*-
"""助手运行时装配与 `main.py` 接线决策测试。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

import pytest

from src.assistant.pipeline import PipelineError
from src.assistant.scheduling.config import EntryKind, NonTradingAction
from src.assistant.service import (
    assistant_mode_decision,
    build_runtime,
    run_loop,
    run_once,
)
from tests.assistant.helpers import make_bars, trending

BASE_ENV = {"ASSISTANT_TOTAL_CAPITAL": "100000"}


@dataclass
class FakeService:
    ok: bool = True
    calls: list[dict] = field(default_factory=list)

    def send(self, content: str, **kwargs: Any) -> bool:
        self.calls.append({"content": content, **kwargs})
        return self.ok


class TestAssistantModeDecision:
    def test_no_request_stays_in_main_flow(self) -> None:
        decision = assistant_mode_decision(
            assistant_flag=False, assistant_once=False, env_enabled=False, start_serve=False
        )
        assert decision.enter is False

    def test_flag_enters_standalone_mode(self) -> None:
        decision = assistant_mode_decision(
            assistant_flag=True, assistant_once=False, env_enabled=False, start_serve=False
        )
        assert decision.enter is True
        assert decision.message == ""

    def test_env_flag_also_enters(self) -> None:
        decision = assistant_mode_decision(
            assistant_flag=False, assistant_once=False, env_enabled=True, start_serve=False
        )
        assert decision.enter is True

    def test_explicit_flag_with_web_service_is_error(self) -> None:
        decision = assistant_mode_decision(
            assistant_flag=True, assistant_once=False, env_enabled=False, start_serve=True
        )
        assert decision.enter is False
        assert decision.level == "error"
        assert "--serve" in decision.message

    def test_env_with_web_service_is_warning_and_web_wins(self) -> None:
        decision = assistant_mode_decision(
            assistant_flag=False, assistant_once=False, env_enabled=True, start_serve=True
        )
        assert decision.enter is False
        assert decision.level == "warning"


class TestBuildRuntime:
    def test_missing_capital_fails_loudly(self) -> None:
        with pytest.raises(PipelineError, match="ASSISTANT_TOTAL_CAPITAL"):
            build_runtime(env={}, db_path=":memory:")

    def test_wires_store_schedule_gate_and_notify(self) -> None:
        service = FakeService()
        runtime = build_runtime(
            env={**BASE_ENV, "ASSISTANT_NON_TRADING_ACTION": "notify_skip"},
            db_path=":memory:",
            service=service,
        )

        assert runtime.schedule.non_trading_action is NonTradingAction.NOTIFY_SKIP
        assert runtime.schedule.entries[0].raw == "20:00"
        assert runtime.scheduler.gate.action is NonTradingAction.NOTIFY_SKIP
        assert runtime.scheduler.dispatcher.ledger is runtime.store  # type: ignore[attr-defined]
        assert runtime.pipeline.market_fetcher is not None
        assert runtime.scheduler.config is runtime.schedule

    def test_custom_env_reaches_schedule_and_pipeline(self) -> None:
        runtime = build_runtime(
            env={
                **BASE_ENV,
                "ASSISTANT_PUSH_SCHEDULE": "20:30",
                "ASSISTANT_INDEX_CODE": "sz399001",
                "ASSISTANT_PARAM_PRESET": "strict",
            },
            db_path=":memory:",
        )
        assert runtime.schedule.entries[0].kind is EntryKind.HHMM
        assert runtime.schedule.entries[0].raw == "20:30"
        assert runtime.pipeline.settings.index_code == "sz399001"
        assert runtime.pipeline.settings.preset.value == "strict"


class TestTickThroughRuntime:
    @staticmethod
    def runtime(service: FakeService):
        runtime = build_runtime(env=BASE_ENV, db_path=":memory:", service=service)
        bars = make_bars(trending(150))
        day = bars[-1].trade_date
        runtime.store.save_bars("600000", bars)
        runtime.store.append_watchlist("600000", "add", effective_date=date(2026, 5, 1))
        runtime.scheduler.gate.probe = lambda now: True  # 离线固定为交易日
        return runtime, day

    def test_once_sends_one_slot_at_20(self) -> None:
        service = FakeService()
        runtime, day = self.runtime(service)

        result = run_once(runtime, now=datetime.combine(day, time(20, 0)))

        assert result.sent == 1
        assert result.generated is True
        assert service.calls[0]["route_type"] == "assistant_daily"
        assert service.calls[0]["dedup_key"].startswith(day.strftime("%Y%m%d"))

    def test_second_tick_same_slot_is_duplicate(self) -> None:
        service = FakeService()
        runtime, day = self.runtime(service)
        point = datetime.combine(day, time(20, 0))

        assert run_once(runtime, now=point).sent == 1
        second = run_once(runtime, now=point)
        assert second.sent == 0
        assert len(service.calls) == 1

    def test_before_fetch_time_no_delivery(self) -> None:
        service = FakeService()
        runtime, day = self.runtime(service)

        result = run_once(runtime, now=datetime.combine(day, time(8, 0)))

        assert result.sent == 0
        assert service.calls == []


class TestRunLoop:
    def test_stops_on_keyboard_interrupt(self) -> None:
        runtime = build_runtime(env=BASE_ENV, db_path=":memory:", service=FakeService())
        ticks: list[datetime] = []
        now = datetime(2026, 11, 27, 8, 0)

        def fake_sleep(_seconds: float) -> None:
            raise KeyboardInterrupt

        def fake_clock() -> datetime:
            return now

        def counting_tick(when: datetime | None = None):
            ticks.append(when or now)
            return runtime.scheduler.tick(when)

        runtime.tick = counting_tick  # type: ignore[method-assign]
        run_loop(runtime, sleep=fake_sleep, clock=fake_clock)

        assert len(ticks) == 1
