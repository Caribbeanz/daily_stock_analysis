# -*- coding: utf-8 -*-
"""高抛低吸助手 · P1 定时推送服务（默认 20:00）

以**项目自身代码**实现每日定时推送，不依赖任何操作系统级计划能力
（Windows 任务计划 / cron / systemd timer）。

设计要点
--------
1. **常驻进程**：调度循环由 :class:`src.scheduler.Scheduler`（schedule 库）驱动，
   与既有 ``main.py --schedule`` 同一套底座。
2. **优雅退出**：SIGINT / SIGTERM + **停止哨兵文件**（跨平台，Windows 亦可靠）。
3. **单实例**：PID 文件互斥，重复启动会被拒绝（退出码 3）。
4. **可观测**：滚动文件日志 ``logs/hslb_scheduler.log``（5MB × 5）+ 控制台。
5. **漏跑补偿**：进程在计划时刻之后才启动、且当天尚未跑过 → 立即补跑一次，
   并把日期写入 ledger，避免重复补跑。
6. **质量门**：默认开启（回测不达标则不推送）。

用法::

    python -m src.services.p1_scheduler                 # 前台常驻（默认 20:00）
    python -m src.services.p1_scheduler --time 20:05    # 自定义时间
    python -m src.services.p1_scheduler --run-now       # 启动时先跑一次
    python -m src.services.p1_scheduler --dry-run       # 只生成不发送
    python -m src.services.p1_scheduler --no-catch-up   # 关闭漏跑补偿
    python -m src.services.p1_scheduler --no-gate       # 跳过质量门（仅调试）

后台常驻 / 停止 / 查询请用控制面 :mod:`src.services.p1_service`::

    python -m src.services.p1_service start|stop|status|restart

时间优先级：``--time`` > 环境变量 ``HSLB_SCHEDULE_TIME`` > ``SCHEDULE_TIME`` > ``20:00``。
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "PROJECT_ROOT",
    "DEFAULT_PUSH_TIME",
    "DEFAULT_WEEKLY_TIME",
    "DEFAULT_WEEKLY_WEEKDAY",
    "DEFAULT_LOG_FILE",
    "DEFAULT_PID_FILE",
    "DEFAULT_STATE_FILE",
    "STOP_SENTINEL",
    "LEDGER_FILE",
    "WEEKLY_LEDGER_FILE",
    "resolve_push_time",
    "resolve_weekly_time",
    "resolve_weekly_weekday",
    "weekly_enabled",
    "should_weekly_run",
    "pid_alive",
    "read_pid",
    "write_pid",
    "remove_pid",
    "write_state",
    "read_state",
    "remove_state",
    "setup_logging",
    "parse_hhmm",
    "should_catch_up",
    "mark_ran",
    "build_task",
    "build_weekly_task",
    "run_service",
    "main",
]

# 项目根目录：src/services/p1_scheduler.py -> parents[0]=services, [1]=src, [2]=repo root
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_PUSH_TIME = "20:00"
# P3 周度复盘（FR-11）：默认**周六 09:00**（需求"周末复盘固定周六"）
DEFAULT_WEEKLY_TIME = "09:00"
DEFAULT_WEEKLY_WEEKDAY = 5  # Python weekday()：周一=0 … 周六=5
_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_LOG_FILE = str(_LOG_DIR / "hslb_scheduler.log")
DEFAULT_PID_FILE = str(_LOG_DIR / "hslb_scheduler.pid")
DEFAULT_STATE_FILE = str(_LOG_DIR / "hslb_scheduler.state.json")
STOP_SENTINEL = str(_LOG_DIR / "hslb_scheduler.stop")
LEDGER_FILE = str(_LOG_DIR / "hslb_scheduler.last_run")
WEEKLY_LEDGER_FILE = str(_LOG_DIR / "hslb_weekly.last_run")

_STOP_WATCH_INTERVAL_SECONDS = 30  # Scheduler 循环轮询粒度为 30s
_WEEKLY_WATCH_INTERVAL_SECONDS = 300  # 周任务窗口检查间隔（5 分钟，足够覆盖周六窗口）


# --------------------------------------------------------------------------- #
# 时间解析
# --------------------------------------------------------------------------- #
def resolve_push_time(explicit: Optional[str] = None) -> str:
    """解析推送时间：显式 > HSLB_SCHEDULE_TIME > SCHEDULE_TIME > 20:00。"""
    for candidate in (
        explicit,
        os.getenv("HSLB_SCHEDULE_TIME"),
        os.getenv("SCHEDULE_TIME"),
    ):
        text = str(candidate or "").strip()
        if text:
            return text
    return DEFAULT_PUSH_TIME


def resolve_weekly_time(explicit: Optional[str] = None) -> str:
    """解析周度复盘时间：显式 > ``HSLB_WEEKLY_REVIEW_TIME`` > 周六 09:00。"""
    for candidate in (explicit, os.getenv("HSLB_WEEKLY_REVIEW_TIME")):
        text = str(candidate or "").strip()
        if text:
            return text
    return DEFAULT_WEEKLY_TIME


def resolve_weekly_weekday(explicit: Optional[Any] = None) -> int:
    """解析周度复盘星期：显式 > ``HSLB_WEEKLY_REVIEW_WEEKDAY`` > 周六（5）。

    接受 ``0~6``（周一=0）或英文/中文星期名（mon…sun / 周一…周日）。
    """
    raw = explicit if explicit not in (None, "") else os.getenv("HSLB_WEEKLY_REVIEW_WEEKDAY")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_WEEKLY_WEEKDAY
    text = str(raw).strip()
    if text.isdigit():
        value = int(text)
        return value if 0 <= value <= 6 else DEFAULT_WEEKLY_WEEKDAY
    lowered = text.lower()
    aliases = {
        "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
        "周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6,
        "星期天": 6, "星期日": 6, "周天": 6,
    }
    return aliases.get(lowered, DEFAULT_WEEKLY_WEEKDAY)


def weekly_enabled(explicit: Optional[bool] = None) -> bool:
    """周度复盘开关：显式 > ``HSLB_WEEKLY_REVIEW_ENABLED``（默认开）。"""
    if explicit is not None:
        return bool(explicit)
    raw = str(os.getenv("HSLB_WEEKLY_REVIEW_ENABLED", "") or "").strip().lower()
    if not raw:
        return True
    return raw in {"1", "true", "yes", "on", "y"}


def should_weekly_run(
    review_time: str,
    weekday: int,
    *,
    now: Optional[datetime] = None,
    ledger: str = WEEKLY_LEDGER_FILE,
) -> bool:
    """是否该跑周度复盘：今天是目标星期、已过计划时刻、且当天尚未跑过。"""
    current = now or datetime.now()
    if current.weekday() != int(weekday):
        return False
    parsed = parse_hhmm(review_time)
    if parsed is None:
        return False
    hour, minute = parsed
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if current < target:
        return False
    return _read_ledger(ledger) != current.strftime("%Y-%m-%d")


def parse_hhmm(text: str) -> Optional[Tuple[int, int]]:
    """把 ``HH:MM`` 解析为 ``(hour, minute)``；非法返回 ``None``。"""
    raw = str(text or "").strip()
    parts = raw.split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


# --------------------------------------------------------------------------- #
# PID 文件
# --------------------------------------------------------------------------- #
def pid_alive(pid: int) -> bool:
    """判断进程是否存活（跨平台）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    if os.name == "nt":  # pragma: no cover - 平台分支
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:  # pragma: no cover - 极端环境
            return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但无权限探测
    except OSError:
        return False
    return True


def read_pid(pid_file: str) -> Optional[int]:
    """读取 PID 文件；缺失或内容非法返回 ``None``。"""
    try:
        text = Path(pid_file).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None
    try:
        pid = int(text.splitlines()[0].strip())
    except (IndexError, ValueError):
        return None
    return pid if pid > 0 else None


def write_pid(pid_file: str, pid: int) -> None:
    """原子写入 PID 文件。"""
    path = Path(pid_file)
    if path.parent and str(path.parent):
        path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(f"{int(pid)}\n", encoding="utf-8")
    os.replace(tmp, path)


def remove_pid(pid_file: str) -> None:
    """删除 PID 文件（不存在则忽略）。"""
    try:
        Path(pid_file).unlink()
    except (FileNotFoundError, OSError):
        pass


# --------------------------------------------------------------------------- #
# 运行态文件（供控制面 status 读取进程的真实参数）
# --------------------------------------------------------------------------- #
def write_state(state_file: str, payload: dict) -> None:
    """原子写入运行态 JSON。"""
    import json

    path = Path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_state(state_file: str) -> Optional[dict]:
    """读取运行态 JSON；缺失/损坏返回 ``None``。"""
    import json

    try:
        payload = json.loads(Path(state_file).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def remove_state(state_file: str) -> None:
    """删除运行态文件（不存在则忽略）。"""
    try:
        Path(state_file).unlink()
    except (FileNotFoundError, OSError):
        pass


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def setup_logging(
    log_file: Optional[str] = DEFAULT_LOG_FILE,
    level: int = logging.INFO,
    console: bool = True,
) -> logging.Logger:
    """配置滚动文件日志 +（可选）控制台；重复调用不会重复挂载 handler。"""
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        target = str(path.resolve())
        has_file_handler = any(
            isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", None) == target
            for h in root.handlers
        )
        if not has_file_handler:
            file_handler = RotatingFileHandler(
                target, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)

    if console and not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in root.handlers
    ):
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    return root


# --------------------------------------------------------------------------- #
# 漏跑补偿 ledger
# --------------------------------------------------------------------------- #
def _read_ledger(ledger: str) -> str:
    try:
        return Path(ledger).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return ""


def mark_ran(ledger: str = LEDGER_FILE, now: Optional[datetime] = None) -> str:
    """记录「今天已执行」，返回写入的日期串。"""
    stamp = (now or datetime.now()).strftime("%Y-%m-%d")
    path = Path(ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{stamp}\n", encoding="utf-8")
    return stamp


def should_catch_up(
    push_time: str,
    *,
    now: Optional[datetime] = None,
    ledger: str = LEDGER_FILE,
) -> bool:
    """是否需要漏跑补偿：已过计划时刻，且 ledger 记录的不是今天。"""
    current = now or datetime.now()
    parsed = parse_hhmm(push_time)
    if parsed is None:
        return False
    hour, minute = parsed
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if current < target:
        return False
    return _read_ledger(ledger) != current.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# 任务
# --------------------------------------------------------------------------- #
def build_task(
    *,
    dry_run: bool = False,
    symbols: Optional[Sequence[str]] = None,
    require_gate: bool = False,
    state_path: Optional[str] = None,
    out_dir: Optional[str] = None,
    ledger: str = LEDGER_FILE,
):
    """构造调度任务（每次执行都新建服务，保证读取最新配置）。"""

    def _task() -> None:
        from src.services.p1_push_service import _DEFAULT_OUT_DIR, _DEFAULT_STATE_PATH, P1PushService

        service = P1PushService(
            state_path=state_path or _DEFAULT_STATE_PATH,
            out_dir=out_dir or _DEFAULT_OUT_DIR,
        )
        result = service.run(symbols=symbols, dry_run=dry_run, require_gate=require_gate)
        mark_ran(ledger)
        logger.info(
            "P1 推送完成：剩余=%s 文本=%s 附图=%s 跳过=%s",
            result.counts.get("total"),
            result.sent_text,
            result.sent_images,
            result.skipped_reason or "-",
        )
        if result.errors:
            logger.warning("P1 推送有 %d 条错误：%s", len(result.errors), "；".join(result.errors))
        if result.gate_passed is False:
            logger.warning(
                "规则体检未通过，已按质量门策略中止推送：%s",
                result.gate_detail and result.gate_detail.get("violated"),
            )

    return _task


def build_weekly_task(
    *,
    dry_run: bool = False,
    symbols: Optional[Sequence[str]] = None,
    refresh_outcomes: bool = True,
    ledger: str = WEEKLY_LEDGER_FILE,
):
    """构造周度复盘任务（P3 / FR-11；每次执行都新建服务，读取最新配置）。"""

    def _task() -> None:
        from src.services.weekly_review_service import WeeklyReviewService

        service = WeeklyReviewService(refresh_outcomes=refresh_outcomes)
        result = service.run(symbols=symbols, dry_run=dry_run)
        mark_ran(ledger)
        review = result.review
        logger.info(
            "周度复盘完成：周期=%s 信号=%s 持仓=%s 文本=%s 跳过=%s",
            review.week_key if review else "-",
            review.signal_summary.total if review else "-",
            len(review.positions) if review else "-",
            result.sent_text,
            result.skipped_reason or "-",
        )
        if result.errors:
            logger.warning("周度复盘有 %d 条错误：%s", len(result.errors), "；".join(result.errors))

    return _task


# --------------------------------------------------------------------------- #
# 服务主循环
# --------------------------------------------------------------------------- #
def run_service(
    *,
    push_time: Optional[str] = None,
    run_now: bool = False,
    dry_run: bool = False,
    symbols: Optional[Sequence[str]] = None,
    require_gate: bool = True,
    state_path: Optional[str] = None,
    out_dir: Optional[str] = None,
    log_file: Optional[str] = DEFAULT_LOG_FILE,
    pid_file: Optional[str] = DEFAULT_PID_FILE,
    state_file: Optional[str] = DEFAULT_STATE_FILE,
    sentinel: str = STOP_SENTINEL,
    ledger: str = LEDGER_FILE,
    catch_up: bool = True,
    console: bool = True,
    weekly: bool = True,
    weekly_time: Optional[str] = None,
    weekly_weekday: Optional[Any] = None,
    weekly_ledger: str = WEEKLY_LEDGER_FILE,
) -> int:
    """以常驻进程方式运行每日推送 + 周度复盘服务，返回退出码（0 正常，3 已有实例）。"""
    resolved_time = resolve_push_time(push_time)
    if parse_hhmm(resolved_time) is None:
        logging.basicConfig(level=logging.INFO)
        logger.error("推送时间格式非法：%r（应为 HH:MM）", resolved_time)
        return 2

    resolved_weekly_time = resolve_weekly_time(weekly_time)
    resolved_weekly_weekday = resolve_weekly_weekday(weekly_weekday)
    weekly_on = bool(weekly) and weekly_enabled() and parse_hhmm(resolved_weekly_time) is not None

    setup_logging(log_file=log_file, console=console)

    # 单实例守卫
    if pid_file:
        existing = read_pid(pid_file)
        if existing and pid_alive(existing):
            logger.error("已有实例在运行（PID=%s），拒绝重复启动。停止请执行：python -m src.services.p1_service stop", existing)
            return 3
        if existing:
            logger.info("清理陈旧 PID 文件（PID=%s 已不存在）", existing)
            remove_pid(pid_file)

    if pid_file:
        write_pid(pid_file, os.getpid())
    if state_file:
        write_state(
            state_file,
            {
                "pid": os.getpid(),
                "push_time": resolved_time,
                "require_gate": bool(require_gate),
                "dry_run": bool(dry_run),
                "catch_up": bool(catch_up),
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "pid_file": pid_file,
                "log_file": log_file,
                "weekly_enabled": weekly_on,
                "weekly_time": resolved_weekly_time if weekly_on else None,
                "weekly_weekday": resolved_weekly_weekday if weekly_on else None,
            },
        )
    # 启动前清理历史停止哨兵，避免"上次 stop 残留导致立即退出"
    try:
        Path(sentinel).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("无法清理停止哨兵：%s", sentinel)

    catch_up_needed = catch_up and should_catch_up(resolved_time, ledger=ledger)
    run_immediately = bool(run_now or catch_up_needed)

    logger.info("=" * 68)
    logger.info("高抛低吸助手 · P1 定时推送服务启动")
    logger.info("PID=%s | 每日执行时间=%s | 质量门=%s | dry_run=%s",
                os.getpid(), resolved_time, "开启" if require_gate else "关闭", dry_run)
    if weekly_on:
        logger.info(
            "周度复盘=%s（FR-11）| 每%s %s",
            "开启",
            _WEEKDAY_NAMES[resolved_weekly_weekday],
            resolved_weekly_time,
        )
    else:
        logger.info("周度复盘=关闭")
    logger.info("日志文件=%s", log_file or "-")
    logger.info("PID 文件=%s", pid_file or "-（未启用）")
    logger.info("停止方式：python -m src.services.p1_service stop（或创建哨兵文件 %s）", sentinel)
    if catch_up_needed:
        logger.info("检测到当日尚未执行且已过计划时刻（%s），立即补跑一次", resolved_time)
    logger.info("=" * 68)

    from src.scheduler import Scheduler

    scheduler = Scheduler(schedule_time=resolved_time)

    def _stop_watch() -> None:
        if Path(sentinel).exists():
            logger.info("检测到停止哨兵文件（%s），准备优雅退出…", sentinel)
            try:
                Path(sentinel).unlink()
            except OSError:
                pass
            scheduler.stop()

    scheduler.add_background_task(
        task=_stop_watch,
        interval_seconds=_STOP_WATCH_INTERVAL_SECONDS,
        run_immediately=False,
        name="hslb-stop-watch",
    )

    # ---- 周度复盘（FR-11）：非侵入式周任务，5 分钟粒度窗口检查 ----
    weekly_task = build_weekly_task(
        dry_run=dry_run,
        symbols=symbols,
        ledger=weekly_ledger,
    )

    def _weekly_watch() -> None:
        if not weekly_on:
            return
        try:
            if should_weekly_run(
                resolved_weekly_time, resolved_weekly_weekday, ledger=weekly_ledger
            ):
                logger.info(
                    "到达周度复盘窗口（每%s %s），开始生成周报（FR-11）",
                    _WEEKDAY_NAMES[resolved_weekly_weekday],
                    resolved_weekly_time,
                )
                weekly_task()
        except Exception as exc:  # 周任务失败不得影响每日推送
            logger.exception("周度复盘执行异常：%s", exc)

    if weekly_on:
        scheduler.add_background_task(
            task=_weekly_watch,
            interval_seconds=_WEEKLY_WATCH_INTERVAL_SECONDS,
            run_immediately=False,
            name="hslb-weekly-review",
        )

    # Windows 控制台 Ctrl+Break 亦触发优雅退出
    if hasattr(signal, "SIGBREAK"):  # pragma: no cover - 平台分支
        try:
            signal.signal(signal.SIGBREAK, lambda s, f: scheduler.stop())
        except (ValueError, OSError, AttributeError):
            pass

    task = build_task(
        dry_run=dry_run,
        symbols=symbols,
        require_gate=require_gate,
        state_path=state_path,
        out_dir=out_dir,
        ledger=ledger,
    )
    scheduler.set_daily_task(task, run_immediately=run_immediately)

    exit_code = 0
    try:
        scheduler.run()
    except KeyboardInterrupt:  # pragma: no cover - 交互式
        logger.info("收到 KeyboardInterrupt，退出调度")
        scheduler.stop()
    except Exception as exc:  # pragma: no cover - 兜底
        logger.exception("调度服务异常退出：%s", exc)
        exit_code = 1
    finally:
        if pid_file:
            remove_pid(pid_file)
        if state_file:
            remove_state(state_file)
        try:
            Path(sentinel).unlink()
        except (FileNotFoundError, OSError):
            pass
        logger.info("P1 定时推送服务已退出（code=%s）", exit_code)

    return exit_code


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="高抛低吸助手 · P1 定时推送服务（默认 20:00，纯代码调度）")
    parser.add_argument("--time", default=None, help="每日推送时间 HH:MM（默认 20:00）")
    parser.add_argument("--run-now", action="store_true", help="启动时立即执行一次")
    parser.add_argument("--dry-run", action="store_true", help="只生成不发送")
    parser.add_argument("--symbols", nargs="*", default=None, help="指定标的；缺省为全部启用自选股")
    parser.add_argument("--require-gate", dest="require_gate", action="store_true", default=True,
                        help="先跑回测质量门，不达标不发（默认开启）")
    parser.add_argument("--no-gate", dest="require_gate", action="store_false",
                        help="跳过质量门（调试用；生产不建议）")
    parser.add_argument("--state-path", default=None)
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE, help="日志文件（默认 logs/hslb_scheduler.log）")
    parser.add_argument("--no-log-file", dest="log_file", action="store_const", const=None,
                        help="不写文件，仅输出到控制台")
    parser.add_argument("--pid-file", default=DEFAULT_PID_FILE, help="PID 文件（默认 logs/hslb_scheduler.pid）")
    parser.add_argument("--no-pidfile", dest="pid_file", action="store_const", const=None,
                        help="不写 PID 文件（不启用单实例守卫）")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE,
                        help="运行态文件（默认 logs/hslb_scheduler.state.json）")
    parser.add_argument("--catch-up", dest="catch_up", action="store_true", default=True,
                        help="漏跑补偿：当日未跑且已过点则立即补跑（默认开启）")
    parser.add_argument("--no-catch-up", dest="catch_up", action="store_false",
                        help="关闭漏跑补偿")
    parser.add_argument("--no-console", dest="console", action="store_false", default=True,
                        help="不输出到控制台（适合后台常驻）")
    parser.add_argument("--weekly", dest="weekly", action="store_true", default=True,
                        help="开启周度复盘（FR-11，默认开启）")
    parser.add_argument("--no-weekly", dest="weekly", action="store_false",
                        help="关闭周度复盘")
    parser.add_argument("--weekly-time", default=None, help="周度复盘时间 HH:MM（默认 周六 09:00）")
    parser.add_argument("--weekly-weekday", default=None,
                        help="周度复盘星期（0~6 或 sat/周六；默认 周六）")
    args = parser.parse_args(argv)

    return run_service(
        push_time=args.time,
        run_now=args.run_now,
        dry_run=args.dry_run,
        symbols=args.symbols,
        require_gate=args.require_gate,
        state_path=args.state_path,
        out_dir=args.out_dir,
        log_file=args.log_file,
        pid_file=args.pid_file,
        state_file=args.state_file,
        catch_up=args.catch_up,
        console=args.console,
        weekly=args.weekly,
        weekly_time=args.weekly_time,
        weekly_weekday=args.weekly_weekday,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
