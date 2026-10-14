# -*- coding: utf-8 -*-
"""P3 · 周六复盘调度单测（时间 / 星期解析、窗口判定、任务构造）。"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.services.p1_scheduler import (
    DEFAULT_WEEKLY_TIME,
    DEFAULT_WEEKLY_WEEKDAY,
    build_weekly_task,
    mark_ran,
    resolve_weekly_time,
    resolve_weekly_weekday,
    should_weekly_run,
    weekly_enabled,
)

SAT = datetime(2026, 10, 17, 10, 0)  # 周六
FRI = datetime(2026, 10, 16, 10, 0)  # 周五


def test_defaults():
    assert DEFAULT_WEEKLY_TIME == "09:00"
    assert DEFAULT_WEEKLY_WEEKDAY == 5  # 周六


def test_resolve_weekly_time_priority(monkeypatch):
    monkeypatch.delenv("HSLB_WEEKLY_REVIEW_TIME", raising=False)
    assert resolve_weekly_time() == "09:00"
    monkeypatch.setenv("HSLB_WEEKLY_REVIEW_TIME", "08:30")
    assert resolve_weekly_time() == "08:30"
    assert resolve_weekly_time("07:15") == "07:15"


def test_resolve_weekly_weekday(monkeypatch):
    monkeypatch.delenv("HSLB_WEEKLY_REVIEW_WEEKDAY", raising=False)
    assert resolve_weekly_weekday() == 5
    assert resolve_weekly_weekday("sun") == 6
    assert resolve_weekly_weekday("周日") == 6
    assert resolve_weekly_weekday("0") == 0
    assert resolve_weekly_weekday("9") == 5  # 越界回退默认
    assert resolve_weekly_weekday("monday") == 5  # 未知回退默认


def test_weekly_enabled(monkeypatch):
    monkeypatch.delenv("HSLB_WEEKLY_REVIEW_ENABLED", raising=False)
    assert weekly_enabled() is True
    monkeypatch.setenv("HSLB_WEEKLY_REVIEW_ENABLED", "false")
    assert weekly_enabled() is False
    monkeypatch.setenv("HSLB_WEEKLY_REVIEW_ENABLED", "on")
    assert weekly_enabled() is True
    assert weekly_enabled(False) is False


def test_should_weekly_run_windows(tmp_path):
    ledger = str(tmp_path / "weekly.last")

    assert should_weekly_run("09:00", 5, now=SAT.replace(hour=8), ledger=ledger) is False  # 未到点
    assert should_weekly_run("09:00", 5, now=SAT, ledger=ledger) is True
    assert should_weekly_run("09:00", 5, now=FRI, ledger=ledger) is False  # 非周六

    mark_ran(ledger, now=SAT)
    assert should_weekly_run("09:00", 5, now=SAT, ledger=ledger) is False  # 当日已跑


def test_should_weekly_run_bad_time(tmp_path):
    ledger = str(tmp_path / "weekly.last")
    assert should_weekly_run("25:99", 5, now=SAT, ledger=ledger) is False


def test_build_weekly_task_marks_ledger(tmp_path, monkeypatch):
    ledger = str(tmp_path / "weekly.last")
    calls = {"n": 0}

    class FakeService:
        def __init__(self, **_kwargs):
            pass

        def run(self, *, symbols=None, dry_run=False):
            calls["n"] += 1

            class R:
                class review:  # noqa: N801
                    week_key = "2026-10-12"
                    signal_summary = type("S", (), {"total": 3})()
                    positions = [1, 2]

                sent_text = False
                skipped_reason = "dry_run"
                errors = []

            return R()

    monkeypatch.setattr("src.services.weekly_review_service.WeeklyReviewService", FakeService)
    task = build_weekly_task(dry_run=True, ledger=ledger)
    task()
    assert calls["n"] == 1
    from pathlib import Path

    assert Path(ledger).read_text(encoding="utf-8").strip() == datetime.now().strftime("%Y-%m-%d")


def test_run_service_accepts_weekly_params(monkeypatch):
    """run_service 应能接收周任务参数（非法推送时间时提前返回 2，不启动调度）。"""
    from src.services.p1_scheduler import run_service

    code = run_service(push_time="bad-time", weekly=True, weekly_time="09:00", weekly_weekday="sat")
    assert code == 2
