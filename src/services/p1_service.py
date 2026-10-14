# -*- coding: utf-8 -*-
"""高抛低吸助手 · P1 推送服务控制面（纯代码实现）

不依赖任何操作系统级计划能力（Windows 任务计划 / cron / systemd timer），
后台常驻、停止、查询全部由本模块以代码方式完成。

子命令::

    python -m src.services.p1_service start     # 派生后台常驻进程（幂等）
    python -m src.services.p1_service stop      # 优雅停止（写停止哨兵）
    python -m src.services.p1_service status    # 查看运行状态
    python -m src.services.p1_service restart   # 先停后起

start 透传参数示例::

    python -m src.services.p1_service start --time 20:05 --run-now
    python -m src.services.p1_service start --dry-run
    python -m src.services.p1_service start --no-gate

实现说明
--------
- 子进程通过 ``subprocess.Popen`` 派生并脱离父进程
  （Windows: ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``；POSIX: ``start_new_session``），
  父进程退出不影响服务。
- 停止走**哨兵文件**：服务端调度循环（每 30s）发现后调用 ``Scheduler.stop()`` 优雅退出，
  避免 Windows 上 ``TerminateProcess`` 式强杀。
- 所有状态都以 ``logs/`` 下的 PID / ledger 文件为准，可安全重复执行。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from src.services.p1_scheduler import (
    DEFAULT_LOG_FILE,
    DEFAULT_PID_FILE,
    DEFAULT_STATE_FILE,
    LEDGER_FILE,
    PROJECT_ROOT,
    STOP_SENTINEL,
    parse_hhmm,
    pid_alive,
    read_pid,
    read_state,
    remove_pid,
    resolve_push_time,
)

__all__ = [
    "SERVICE_MODULE",
    "build_start_command",
    "next_run_time",
    "gather_status",
    "start_service",
    "stop_service",
    "status_service",
    "main",
]

SERVICE_MODULE = "src.services.p1_scheduler"
DEFAULT_CONSOLE_LOG = str(PROJECT_ROOT / "logs" / "hslb_service.console.log")


# --------------------------------------------------------------------------- #
# 纯函数（便于测试）
# --------------------------------------------------------------------------- #
def build_start_command(
    python_exe: Optional[str] = None,
    *,
    time_str: Optional[str] = None,
    run_now: bool = False,
    dry_run: bool = False,
    no_gate: bool = False,
    symbols: Optional[Sequence[str]] = None,
    log_file: Optional[str] = DEFAULT_LOG_FILE,
    pid_file: Optional[str] = DEFAULT_PID_FILE,
    no_catch_up: bool = False,
    no_weekly: bool = False,
    weekly_time: Optional[str] = None,
    weekly_weekday: Optional[str] = None,
) -> List[str]:
    """构造派生后台服务所用的命令行（纯函数）。"""
    cmd: List[str] = [python_exe or sys.executable, "-m", SERVICE_MODULE]
    if time_str:
        cmd += ["--time", str(time_str)]
    if run_now:
        cmd.append("--run-now")
    if dry_run:
        cmd.append("--dry-run")
    if no_gate:
        cmd.append("--no-gate")
    if symbols:
        cmd.append("--symbols")
        cmd += [str(s) for s in symbols]
    if log_file:
        cmd += ["--log-file", str(log_file)]
    else:
        cmd.append("--no-log-file")
    if pid_file:
        cmd += ["--pid-file", str(pid_file)]
    else:
        cmd.append("--no-pidfile")
    if no_catch_up:
        cmd.append("--no-catch-up")
    if no_weekly:
        cmd.append("--no-weekly")
    if weekly_time:
        cmd += ["--weekly-time", str(weekly_time)]
    if weekly_weekday not in (None, ""):
        cmd += ["--weekly-weekday", str(weekly_weekday)]
    cmd.append("--no-console")
    return cmd


def next_run_time(push_time: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """计算下一次执行时刻（今天未到则今天，否则明天）；非法时间返回 None。"""
    parsed = parse_hhmm(push_time)
    if parsed is None:
        return None
    current = now or datetime.now()
    hour, minute = parsed
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= current:
        target = target + timedelta(days=1)
    return target


def gather_status(
    *,
    pid_file: str = DEFAULT_PID_FILE,
    ledger: str = LEDGER_FILE,
    log_file: Optional[str] = DEFAULT_LOG_FILE,
    push_time: Optional[str] = None,
    state_file: str = DEFAULT_STATE_FILE,
) -> Dict[str, object]:
    """汇总服务状态（PID / 存活 / 计划时间 / 下次执行 / 最近执行日期）。

    存活时优先采用**进程真实运行参数**（运行态 JSON），未运行时回退到环境变量解析结果。
    """
    pid = read_pid(pid_file)
    alive = bool(pid and pid_alive(pid))

    live_state = read_state(state_file) if alive else None
    resolved = (live_state or {}).get("push_time") or resolve_push_time(push_time)
    effective_log = (live_state or {}).get("log_file") or log_file

    last_run = ""
    try:
        last_run = Path(ledger).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        last_run = ""
    nxt = next_run_time(resolved)
    return {
        "running": alive,
        "pid": pid,
        "pid_file": pid_file,
        "state_file": state_file,
        "push_time": resolved,
        "next_run_at": nxt.strftime("%Y-%m-%d %H:%M:%S") if nxt else None,
        "last_run_date": last_run or None,
        "started_at": (live_state or {}).get("started_at"),
        "require_gate": (live_state or {}).get("require_gate"),
        "dry_run": (live_state or {}).get("dry_run"),
        "log_file": effective_log,
        "console_log": DEFAULT_CONSOLE_LOG,
    }


# --------------------------------------------------------------------------- #
# 子命令实现
# --------------------------------------------------------------------------- #
def start_service(
    *,
    time_str: Optional[str] = None,
    run_now: bool = False,
    dry_run: bool = False,
    no_gate: bool = False,
    symbols: Optional[Sequence[str]] = None,
    pid_file: Optional[str] = DEFAULT_PID_FILE,
    log_file: Optional[str] = DEFAULT_LOG_FILE,
    wait_seconds: float = 20.0,
    no_weekly: bool = False,
    weekly_time: Optional[str] = None,
    weekly_weekday: Optional[str] = None,
) -> Dict[str, object]:
    """派生后台常驻服务；已在运行则直接返回（幂等）。"""
    existing = read_pid(pid_file) if pid_file else None
    if existing and pid_alive(existing):
        return {"ok": True, "started": False, "reason": "already_running", "pid": existing}

    if pid_file and not existing:
        remove_pid(pid_file)  # 清理陈旧文件

    console_log = Path(DEFAULT_CONSOLE_LOG)
    console_log.parent.mkdir(parents=True, exist_ok=True)

    cmd = build_start_command(
        sys.executable,
        time_str=time_str,
        run_now=run_now,
        dry_run=dry_run,
        no_gate=no_gate,
        symbols=symbols,
        log_file=log_file,
        pid_file=pid_file,
        no_weekly=no_weekly,
        weekly_time=weekly_time,
        weekly_weekday=weekly_weekday,
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    popen_kwargs: Dict[str, object]
    if os.name == "nt":  # pragma: no cover - 平台分支
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        popen_kwargs = {"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP}
    else:
        popen_kwargs = {"start_new_session": True}

    log_handle = open(console_log, "a", encoding="utf-8")
    try:
        process = subprocess.Popen(  # noqa: S603 - 受控命令行
            cmd,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            **popen_kwargs,  # type: ignore[arg-type]
        )
    finally:
        log_handle.close()

    # 等待 PID 文件落盘并确认存活
    deadline = time.time() + max(1.0, float(wait_seconds))
    spawned_pid: Optional[int] = None
    while time.time() < deadline:
        spawned_pid = read_pid(pid_file) if pid_file else process.pid
        if spawned_pid and pid_alive(spawned_pid):
            return {
                "ok": True,
                "started": True,
                "pid": spawned_pid,
                "launcher_pid": process.pid,
                "command": cmd,
                "console_log": str(console_log),
            }
        if process.poll() is not None:
            return {
                "ok": False,
                "started": False,
                "reason": f"child_exited_early(rc={process.returncode})",
                "command": cmd,
                "console_log": str(console_log),
            }
        time.sleep(0.5)

    return {
        "ok": False,
        "started": False,
        "reason": "timeout_waiting_for_pid",
        "pid": spawned_pid,
        "command": cmd,
        "console_log": str(console_log),
    }


def stop_service(
    *,
    pid_file: Optional[str] = DEFAULT_PID_FILE,
    sentinel: str = STOP_SENTINEL,
    timeout: float = 90.0,
    force: bool = True,
    progress: bool = True,
) -> Dict[str, object]:
    """优雅停止服务：写停止哨兵，等待进程退出；超时可选强杀。"""
    pid = read_pid(pid_file) if pid_file else None
    if not pid or not pid_alive(pid):
        if pid_file:
            remove_pid(pid_file)
        return {"ok": True, "stopped": False, "reason": "not_running"}

    Path(sentinel).parent.mkdir(parents=True, exist_ok=True)
    Path(sentinel).write_text(f"{datetime.now().isoformat()}\n", encoding="utf-8")

    deadline = time.time() + max(1.0, float(timeout))
    while time.time() < deadline:
        if not pid_alive(pid):
            if pid_file:
                remove_pid(pid_file)
            try:
                Path(sentinel).unlink()
            except OSError:
                pass
            return {"ok": True, "stopped": True, "pid": pid, "graceful": True}
        if progress:
            print(f"... 等待 PID {pid} 优雅退出（哨兵已写入，最多 {int(timeout)}s）", flush=True)
        time.sleep(3.0)

    if force:
        try:
            if os.name == "nt":  # pragma: no cover - 平台分支
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)
            else:
                os.kill(pid, 15)
        except Exception:  # pragma: no cover - 兜底
            pass
        for _ in range(10):
            if not pid_alive(pid):
                if pid_file:
                    remove_pid(pid_file)
                return {"ok": True, "stopped": True, "pid": pid, "graceful": False}
            time.sleep(0.5)

    return {"ok": False, "stopped": False, "reason": "timeout", "pid": pid}


def status_service(
    *,
    pid_file: Optional[str] = DEFAULT_PID_FILE,
    ledger: str = LEDGER_FILE,
    log_file: Optional[str] = DEFAULT_LOG_FILE,
    push_time: Optional[str] = None,
    state_file: str = DEFAULT_STATE_FILE,
) -> Dict[str, object]:
    """查询服务状态。"""
    return gather_status(
        pid_file=pid_file, ledger=ledger, log_file=log_file,
        push_time=push_time, state_file=state_file,
    )


def _print_status(status: Dict[str, object]) -> None:
    online = "运行中" if status.get("running") else "未运行"
    print(f"服务状态 : {online}")
    print(f"PID      : {status.get('pid') or '-'}")
    print(f"计划时间 : 每日 {status.get('push_time')}")
    print(f"下次执行 : {status.get('next_run_at') or '-'}")
    if status.get("started_at"):
        print(f"启动于   : {status.get('started_at')}")
    if status.get("running"):
        gate = "开启" if status.get("require_gate") else "关闭"
        print(f"质量门   : {gate} | dry_run={status.get('dry_run')}")
    print(f"最近执行 : {status.get('last_run_date') or '（无记录）'}")
    print(f"日志文件 : {status.get('log_file') or '-'}")
    print(f"控制台日志: {status.get('console_log') or '-'}")
    print(f"PID 文件 : {status.get('pid_file') or '-'}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="高抛低吸助手 · P1 推送服务控制面（纯代码调度）")
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--pid-file", default=DEFAULT_PID_FILE)
        p.add_argument("--log-file", default=DEFAULT_LOG_FILE)

    p_start = sub.add_parser("start", help="派生后台常驻服务")
    p_start.add_argument("--time", default=None, help="每日推送时间 HH:MM（默认 20:00）")
    p_start.add_argument("--run-now", action="store_true", help="启动时立即执行一次")
    p_start.add_argument("--dry-run", action="store_true", help="只生成不发送")
    p_start.add_argument("--no-gate", action="store_true", help="跳过质量门（仅调试）")
    p_start.add_argument("--symbols", nargs="*", default=None)
    p_start.add_argument("--no-weekly", action="store_true", help="关闭周度复盘（FR-11，默认开启）")
    p_start.add_argument("--weekly-time", default=None, help="周度复盘时间 HH:MM（默认 周六 09:00）")
    p_start.add_argument("--weekly-weekday", default=None, help="周度复盘星期（0~6 或 sat/周六）")
    p_start.add_argument("--wait", type=float, default=20.0, help="等待服务就绪秒数")
    p_start.add_argument("--json", action="store_true")
    _add_common(p_start)

    p_stop = sub.add_parser("stop", help="优雅停止服务")
    p_stop.add_argument("--timeout", type=float, default=90.0, help="优雅退出等待秒数")
    p_stop.add_argument("--no-force", dest="force", action="store_false", default=True,
                        help="超时后不强杀")
    p_stop.add_argument("--json", action="store_true")
    _add_common(p_stop)

    p_status = sub.add_parser("status", help="查看服务状态")
    p_status.add_argument("--time", default=None)
    p_status.add_argument("--json", action="store_true")
    _add_common(p_status)

    p_restart = sub.add_parser("restart", help="先停后起")
    p_restart.add_argument("--time", default=None)
    p_restart.add_argument("--run-now", action="store_true")
    p_restart.add_argument("--dry-run", action="store_true")
    p_restart.add_argument("--no-gate", action="store_true")
    p_restart.add_argument("--timeout", type=float, default=90.0)
    _add_common(p_restart)

    args = parser.parse_args(argv)

    if args.command == "start":
        result = start_service(
            time_str=args.time, run_now=args.run_now, dry_run=args.dry_run,
            no_gate=args.no_gate, symbols=args.symbols,
            pid_file=args.pid_file, log_file=args.log_file, wait_seconds=args.wait,
            no_weekly=args.no_weekly, weekly_time=args.weekly_time,
            weekly_weekday=args.weekly_weekday,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            if result.get("ok") and result.get("started"):
                print(f"服务已启动 ✅  PID={result.get('pid')}  日志={result.get('console_log')}")
            elif result.get("reason") == "already_running":
                print(f"服务已在运行（PID={result.get('pid')}），无需重复启动")
            else:
                print(f"服务启动失败 ❌ 原因={result.get('reason')} 详见 {result.get('console_log')}")
        return 0 if result.get("ok") else 1

    if args.command == "stop":
        result = stop_service(pid_file=args.pid_file, timeout=args.timeout, force=args.force)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            if result.get("stopped"):
                mode = "优雅停止" if result.get("graceful") else "强制停止"
                print(f"服务已{mode} ✅ PID={result.get('pid')}")
            elif result.get("reason") == "not_running":
                print("服务未在运行，无需停止")
            else:
                print(f"停止失败 ❌ 原因={result.get('reason')} PID={result.get('pid')}")
        return 0 if result.get("ok") else 1

    if args.command == "status":
        status = status_service(
            pid_file=args.pid_file, log_file=args.log_file, push_time=args.time
        )
        if args.json:
            print(json.dumps(status, ensure_ascii=False, indent=2))
        else:
            _print_status(status)
        return 0

    if args.command == "restart":
        stopped = stop_service(pid_file=args.pid_file, timeout=args.timeout)
        if not stopped.get("ok"):
            print(f"重启中止：停止失败（{stopped.get('reason')}）")
            return 1
        result = start_service(
            time_str=args.time, run_now=args.run_now, dry_run=args.dry_run,
            no_gate=args.no_gate, pid_file=args.pid_file, log_file=args.log_file,
        )
        if result.get("ok"):
            print(f"服务已重启 ✅ PID={result.get('pid')}")
            return 0
        print(f"重启失败 ❌ 原因={result.get('reason')}")
        return 1

    parser.error(f"未知子命令：{args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
