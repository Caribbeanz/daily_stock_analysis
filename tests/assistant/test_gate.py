# -*- coding: utf-8 -*-
"""交易日门控测试（需求 FR-8.6 / FR-8.7，技术方案 §2.4）。

重点验 **fail-closed**：日历说「不知道」时必须按非交易日处理，不能因为配置的时点
命中就产出内容。这是与主流程 ``is_market_open()``（fail-open）的分水岭。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.assistant.scheduling.config import NonTradingAction
from src.assistant.scheduling.gate import GateReason, TradingDayGate

LOCAL_NOON = datetime(2026, 11, 24, 12, 0)


def gate(probe, action: NonTradingAction = NonTradingAction.SKIP) -> TradingDayGate:
    return TradingDayGate(action=action, probe=probe)


# ---- 交易日 ---------------------------------------------------------------
def test_trading_day_enters_pipeline() -> None:
    decision = gate(lambda now: True).decide(LOCAL_NOON)
    assert decision.should_generate is True
    assert decision.should_notify is False
    assert decision.reason is GateReason.TRADING_DAY
    assert decision.is_trading_day is True
    assert decision.warnings == ()


# ---- 非交易日 -------------------------------------------------------------
def test_non_trading_day_skip_produces_nothing() -> None:
    decision = gate(lambda now: False, NonTradingAction.SKIP).decide(LOCAL_NOON)
    assert decision.should_generate is False
    assert decision.should_notify is False
    assert decision.reason is GateReason.NON_TRADING
    assert decision.is_trading_day is False


def test_non_trading_day_notify_skip_only_notifies() -> None:
    decision = gate(lambda now: False, NonTradingAction.NOTIFY_SKIP).decide(LOCAL_NOON)
    assert decision.should_generate is False, "notify_skip 也不得生成快照/卡片"
    assert decision.should_notify is True
    assert decision.reason is GateReason.NON_TRADING


# ---- fail-closed ----------------------------------------------------------
def test_unknown_calendar_fails_closed() -> None:
    decision = gate(lambda now: None).decide(LOCAL_NOON)
    assert decision.should_generate is False
    assert decision.should_notify is False, "判不出来时连提醒都不发"
    assert decision.reason is GateReason.CALENDAR_UNAVAILABLE
    assert decision.is_trading_day is None
    assert decision.warnings and "fail-closed" in decision.warnings[0]


def test_calendar_exception_fails_closed() -> None:
    def boom(now: datetime) -> bool:
        raise RuntimeError("exchange_calendars 未安装")

    decision = gate(boom).decide(LOCAL_NOON)
    assert decision.should_generate is False
    assert decision.reason is GateReason.CALENDAR_UNAVAILABLE
    assert "exchange_calendars" in decision.warnings[0]


def test_fail_closed_ignores_notify_skip() -> None:
    """日历不可用时按非交易日处理，但「提醒」本身也不该发——内容都不存在。"""
    decision = gate(lambda now: None, NonTradingAction.NOTIFY_SKIP).decide(LOCAL_NOON)
    assert decision.should_generate is False
    assert decision.should_notify is False


def test_default_probe_is_fail_closed_when_calendar_libs_missing() -> None:
    """不注入探针时走真实 ``trading_calendar``；缺依赖必须回 UNKNOWN → fail-closed。"""
    try:
        import pandas  # noqa: F401
    except ImportError:  # pragma: no cover - 无 pandas 时直接跳过
        pytest.skip("未安装 pandas，默认探针无法导入")
    decision = TradingDayGate().decide(LOCAL_NOON)
    # 有日历库时返回 True/False；缺 exchange_calendars 时返回 UNKNOWN —— 两种都不许抛异常
    assert decision.reason in {
        GateReason.TRADING_DAY,
        GateReason.NON_TRADING,
        GateReason.CALENDAR_UNAVAILABLE,
    }
    if decision.reason is GateReason.CALENDAR_UNAVAILABLE:
        assert decision.should_generate is False


# ---- 时区 -----------------------------------------------------------------
def test_naive_time_gets_configured_timezone() -> None:
    seen: list[datetime] = []

    def probe(now: datetime) -> bool:
        seen.append(now)
        return True

    decision = gate(probe).decide(datetime(2026, 11, 24, 20, 0))
    assert seen[0].tzinfo is not None
    assert seen[0].utcoffset().total_seconds() == 8 * 3600
    assert decision.market_local_time.hour == 20


def test_utc_time_is_converted_to_market_local() -> None:
    """服务器 UTC 20:00 = 北京时间次日 04:00，交易日判定必须按本地日期（FR-8.7 ④）。"""
    seen: list[datetime] = []

    def probe(now: datetime) -> bool:
        seen.append(now)
        return True

    gate(probe).decide(datetime(2026, 11, 24, 20, 0, tzinfo=timezone.utc))
    assert seen[0].date().isoformat() == "2026-11-25"
    assert seen[0].hour == 4


def test_custom_timezone_applied() -> None:
    seen: list[datetime] = []
    g = TradingDayGate(timezone="Asia/Tokyo", probe=lambda now: seen.append(now) or True)
    g.decide(datetime(2026, 11, 24, 20, 0, tzinfo=timezone.utc))
    assert seen[0].hour == 5  # UTC 20:00 → 东京次日 05:00
    assert seen[0].date().isoformat() == "2026-11-25"


def test_decision_serialises_to_dict() -> None:
    payload = gate(lambda now: False).decide(LOCAL_NOON).as_dict()
    assert payload["should_generate"] is False
    assert payload["reason"] == "non_trading"
    assert payload["non_trading_action"] == "skip"
    assert "market_local_time" in payload
