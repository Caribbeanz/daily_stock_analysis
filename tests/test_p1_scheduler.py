# -*- coding: utf-8 -*-
"""P1 · 定时推送服务与控制面单测（纯代码调度：PID / 哨兵 / 漏跑补偿 / 命令行构造）。"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

import pytest

from src.services import p1_service, p1_scheduler


# --------------------------------------------------------------------------- #
# 时间解析
# --------------------------------------------------------------------------- #
def test_resolve_push_time_precedence(monkeypatch):
    monkeypatch.delenv("HSLB_SCHEDULE_TIME", raising=False)
    monkeypatch.delenv("SCHEDULE_TIME", raising=False)
    assert p1_scheduler.resolve_push_time() == "20:00"
    monkeypatch.setenv("SCHEDULE_TIME", "18:30")
    assert p1_scheduler.resolve_push_time() == "18:30"
    monkeypatch.setenv("HSLB_SCHEDULE_TIME", "20:05")
    assert p1_scheduler.resolve_push_time() == "20:05"
    assert p1_scheduler.resolve_push_time("21:00") == "21:00"


@pytest.mark.parametrize(
    "text,expected",
    [("20:00", (20, 0)), ("00:00", (0, 0)), ("23:59", (23, 59)),
     ("24:00", None), ("20:60", None), ("2000", None), ("", None), ("ab:cd", None)],
)
def test_parse_hhmm(text, expected):
    assert p1_scheduler.parse_hhmm(text) == expected


# --------------------------------------------------------------------------- #
# PID 文件
# --------------------------------------------------------------------------- #
def test_pid_roundtrip_and_cleanup(tmp_path):
    pid_file = str(tmp_path / "svc.pid")
    assert p1_scheduler.read_pid(pid_file) is None
    p1_scheduler.write_pid(pid_file, 4321)
    assert p1_scheduler.read_pid(pid_file) == 4321
    p1_scheduler.remove_pid(pid_file)
    assert p1_scheduler.read_pid(pid_file) is None
    p1_scheduler.remove_pid(pid_file)  # 幂等


def test_read_pid_tolerates_junk(tmp_path):
    pid_file = tmp_path / "svc.pid"
    pid_file.write_text("not-a-pid\n", encoding="utf-8")
    assert p1_scheduler.read_pid(str(pid_file)) is None
    pid_file.write_text("-5\n", encoding="utf-8")
    assert p1_scheduler.read_pid(str(pid_file)) is None


def test_pid_alive_self_and_bogus():
    assert p1_scheduler.pid_alive(os.getpid()) is True
    assert p1_scheduler.pid_alive(0) is False
    assert p1_scheduler.pid_alive(-1) is False
    assert p1_scheduler.pid_alive("abc") is False


def test_state_roundtrip_and_cleanup(tmp_path):
    state_file = str(tmp_path / "svc.state.json")
    assert p1_scheduler.read_state(state_file) is None
    p1_scheduler.write_state(state_file, {"pid": 1, "push_time": "20:00"})
    assert p1_scheduler.read_state(state_file) == {"pid": 1, "push_time": "20:00"}
    p1_scheduler.remove_state(state_file)
    assert p1_scheduler.read_state(state_file) is None
    p1_scheduler.remove_state(state_file)  # 幂等


def test_read_state_tolerates_broken_json(tmp_path):
    state_file = tmp_path / "svc.state.json"
    state_file.write_text("{not json", encoding="utf-8")
    assert p1_scheduler.read_state(str(state_file)) is None
    state_file.write_text("[1,2,3]", encoding="utf-8")
    assert p1_scheduler.read_state(str(state_file)) is None


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def test_setup_logging_writes_file_and_is_idempotent(tmp_path):
    root = logging.getLogger()
    saved = list(root.handlers)
    for handler in saved:
        root.removeHandler(handler)
    try:
        log_file = str(tmp_path / "svc.log")
        p1_scheduler.setup_logging(log_file=log_file, console=False)
        p1_scheduler.setup_logging(log_file=log_file, console=False)
        file_handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
        assert len(file_handlers) == 1
        logging.getLogger("test.hslb").info("hello-scheduler")
        for handler in root.handlers:
            handler.flush()
        assert "hello-scheduler" in open(log_file, encoding="utf-8").read()
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        for handler in saved:
            root.addHandler(handler)


# --------------------------------------------------------------------------- #
# 漏跑补偿
# --------------------------------------------------------------------------- #
def test_should_catch_up_before_schedule_time(tmp_path):
    ledger = str(tmp_path / "last_run")
    now = datetime(2026, 10, 13, 19, 30)
    assert p1_scheduler.should_catch_up("20:00", now=now, ledger=ledger) is False


def test_should_catch_up_after_time_without_ledger(tmp_path):
    ledger = str(tmp_path / "last_run")
    now = datetime(2026, 10, 13, 20, 30)
    assert p1_scheduler.should_catch_up("20:00", now=now, ledger=ledger) is True


def test_should_catch_up_skips_when_already_ran_today(tmp_path):
    ledger = str(tmp_path / "last_run")
    now = datetime(2026, 10, 13, 20, 30)
    p1_scheduler.mark_ran(ledger, now=now)
    assert p1_scheduler.should_catch_up("20:00", now=now, ledger=ledger) is False


def test_should_catch_up_when_ledger_is_stale(tmp_path):
    ledger = str(tmp_path / "last_run")
    now = datetime(2026, 10, 13, 20, 30)
    p1_scheduler.mark_ran(ledger, now=now - timedelta(days=1))
    assert p1_scheduler.should_catch_up("20:00", now=now, ledger=ledger) is True


def test_should_catch_up_invalid_time(tmp_path):
    assert p1_scheduler.should_catch_up("25:00", now=datetime(2026, 10, 13, 20, 30),
                                        ledger=str(tmp_path / "x")) is False


# --------------------------------------------------------------------------- #
# 任务包装（ledger 落盘）
# --------------------------------------------------------------------------- #
class _FakeResult:
    counts = {"total": 8}
    sent_text = True
    sent_images = 0
    skipped_reason = ""
    errors = []
    gate_passed = True
    gate_detail = {}


class _FakePushService:
    def __init__(self, **_kwargs):
        pass

    def run(self, **_kwargs):
        return _FakeResult()


def test_build_task_marks_ledger(monkeypatch, tmp_path):
    import src.services.p1_push_service as push_module

    ledger = str(tmp_path / "last_run")
    monkeypatch.setattr(push_module, "P1PushService", _FakePushService)

    task = p1_scheduler.build_task(ledger=ledger)
    task()
    assert open(ledger, encoding="utf-8").read().strip() == datetime.now().strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# 服务控制面（纯函数）
# --------------------------------------------------------------------------- #
def test_build_start_command_contains_module_and_flags():
    cmd = p1_service.build_start_command(
        "python", time_str="20:05", run_now=True, dry_run=True, no_gate=True,
        symbols=["601919", "601288"],
    )
    assert cmd[:3] == ["python", "-m", p1_service.SERVICE_MODULE]
    assert "--time" in cmd and "20:05" in cmd
    assert "--run-now" in cmd and "--dry-run" in cmd and "--no-gate" in cmd
    assert cmd[cmd.index("--symbols") + 1: cmd.index("--symbols") + 3] == ["601919", "601288"]
    assert cmd[-1] == "--no-console"


def test_build_start_command_no_log_and_no_pidfile():
    cmd = p1_service.build_start_command("python", log_file=None, pid_file=None)
    assert "--no-log-file" in cmd
    assert "--no-pidfile" in cmd


def test_next_run_time_today_or_tomorrow():
    now = datetime(2026, 10, 13, 19, 0)
    assert p1_service.next_run_time("20:00", now=now) == datetime(2026, 10, 13, 20, 0)
    assert p1_service.next_run_time("18:00", now=now) == datetime(2026, 10, 14, 18, 0)
    assert p1_service.next_run_time("bad", now=now) is None


def test_gather_status_not_running(tmp_path):
    status = p1_service.gather_status(
        pid_file=str(tmp_path / "none.pid"),
        ledger=str(tmp_path / "none.ledger"),
        log_file=str(tmp_path / "none.log"),
    )
    assert status["running"] is False
    assert status["pid"] is None
    assert status["last_run_date"] is None


def test_gather_status_prefers_live_state(monkeypatch, tmp_path):
    state_file = tmp_path / "svc.state.json"
    p1_scheduler.write_state(
        str(state_file),
        {"pid": os.getpid(), "push_time": "21:21", "require_gate": True, "dry_run": False},
    )
    monkeypatch.setattr(p1_service, "read_pid", lambda _p: os.getpid())
    monkeypatch.setattr(p1_service, "pid_alive", lambda _p: True)

    status = p1_service.gather_status(
        pid_file="ignored.pid", ledger=str(tmp_path / "none.ledger"),
        log_file=None, state_file=str(state_file),
    )
    assert status["running"] is True
    assert status["push_time"] == "21:21"   # 以进程真实参数为准，而非环境变量
    assert status["require_gate"] is True


def test_stop_service_when_not_running(tmp_path):
    result = p1_service.stop_service(pid_file=str(tmp_path / "none.pid"))
    assert result["ok"] is True
    assert result["stopped"] is False
    assert result["reason"] == "not_running"
