# -*- coding: utf-8 -*-
"""助手运行时装配与调度循环（`main.py` 显式 opt-in 接线用）。

把已经分散实现的四层拼起来，不重写任何一层：

    config(env) → store / fetcher / pipeline → notify + ledger → scheduler

默认**不启动**：`main.py` 只有显式 `--assistant` 或 `ASSISTANT_ENABLED=true` 才进入。
时点仍由助手自己的 `ScheduleConfig` 决定（默认 20:00），非交易日 fail-closed，
同一时点由持久化台账防重投——与主流程的 `--schedule` 是两条独立路径，互不接管。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from src.assistant.data.fetcher import DailyFetcher, ReferenceFetcher
from src.assistant.data.sector_map import IndustryResolver
from src.assistant.data.storage import AssistantStore
from src.assistant.llm import LlmTextSettings
from src.assistant.mode import AssistantModeDecision, assistant_mode_decision
from src.assistant.notify import make_sender, notify_non_trading
from src.assistant.pipeline import AssistantPipeline, PipelineSettings
from src.assistant.scheduling.config import ScheduleConfig, parse_env_config
from src.assistant.scheduling.delivery import MultiSlotDispatcher
from src.assistant.scheduling.gate import TradingDayGate
from src.assistant.scheduling.runner import AssistantScheduler, TickResult

__all__ = [
    "AssistantModeDecision",
    "AssistantRuntime",
    "assistant_mode_decision",
    "build_runtime",
    "run_loop",
    "run_once",
]

logger = logging.getLogger(__name__)


@dataclass
class AssistantRuntime:
    """一次装配好的助手运行时（可注入测试替身，不持有全局状态）。"""

    store: AssistantStore
    pipeline: AssistantPipeline
    scheduler: AssistantScheduler
    schedule: ScheduleConfig

    def tick(self, now: Optional[datetime] = None) -> TickResult:
        return self.scheduler.tick(now)


def build_runtime(
    *,
    env: Optional[Mapping[str, str]] = None,
    db_path: str | Path | None = None,
    service: Any = None,
    fetcher: Any = None,
    market_fetcher: Any = None,
    llm: Any = None,
    llm_settings: LlmTextSettings | None = None,
) -> AssistantRuntime:
    """按环境变量装配运行时；缺资金等必填项直接抛错，不猜默认值。"""
    environ: Mapping[str, str] = os.environ if env is None else env
    settings = PipelineSettings.from_env(environ)
    schedule = parse_env_config(environ)
    for warning in schedule.warnings:
        logger.warning("助手调度配置：%s", warning)

    store = AssistantStore(db_path)
    store.create_all()  # additively creates assistant_* tables; never touches stock_daily
    # 行业映射缓存与库同目录（db_path 可能是 str 或 None；None 时不落盘，只走内存）
    data_dir = Path(db_path).parent if db_path is not None else Path("data")
    daily_fetcher = fetcher if fetcher is not None else DailyFetcher(store=store)
    index_fetcher = market_fetcher if market_fetcher is not None else ReferenceFetcher()

    pipeline = AssistantPipeline(
        store=store,
        settings=settings,
        fetcher=daily_fetcher,
        market_fetcher=index_fetcher,
        llm=llm,
        llm_settings=llm_settings or LlmTextSettings.from_env(environ),
        # 个股 → 板块映射（D-22）；缓存与库同放在 data/ 下，跨进程重启也省 17 次请求
        industry_resolver=IndustryResolver(
            cache_path=data_dir / "cache" / "assistant_industry_map.json"
        ),
    )
    gate = TradingDayGate(
        action=schedule.non_trading_action,
        timezone=schedule.timezone,
    )
    dispatcher = MultiSlotDispatcher(
        ledger=store,  # 持久化台账：进程重启后仍防重投
        sender=make_sender(service),
    )
    scheduler = AssistantScheduler(
        config=schedule,
        gate=gate,
        dispatcher=dispatcher,
        generate=pipeline.payload,
        notify_non_trading=partial(notify_non_trading, service=service),
    )
    return AssistantRuntime(
        store=store,
        pipeline=pipeline,
        scheduler=scheduler,
        schedule=schedule,
    )


def run_once(runtime: AssistantRuntime, *, now: Optional[datetime] = None) -> TickResult:
    """跑一次节拍（供手工验证 / 定时钩子用），不吞异常。"""
    result = runtime.tick(now)
    for warning in result.warnings:
        logger.warning("助手节拍告警：%s", warning)
    return result


def run_loop(
    runtime: AssistantRuntime,
    *,
    poll_seconds: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = datetime.now,
) -> None:
    """每分钟一次节拍，直到 ``KeyboardInterrupt``。

    拍与拍之间同源幂等：非触发时点不生成快照，已投时点由台账判 `duplicate`，
    所以重复 tick 不会重复取数或重复推送。这里只负责把 `tick` 按固定节奏叫醒，
    不做任何"到点才跑"的判断——那已经属于调度层。
    """
    logger.info(
        "助手调度已启动：时区=%s 时点=%s 取数=%s 非交易日=%s",
        runtime.schedule.timezone,
        [entry.raw for entry in runtime.schedule.entries],
        runtime.schedule.fetch_time,
        runtime.schedule.non_trading_action.value,
    )
    try:
        while True:
            result = run_once(runtime, now=clock())
            if result.generated:
                logger.info("助手快照已生成：%s", result.payload.snapshot_id if result.payload else "—")
            if result.sent:
                logger.info("助手本拍投递 %d 个时点", result.sent)
            sleep(poll_seconds)
    except KeyboardInterrupt:  # pragma: no cover - 手动退出路径
        logger.info("助手调度已停止")
