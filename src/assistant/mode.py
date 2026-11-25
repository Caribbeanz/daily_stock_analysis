# -*- coding: utf-8 -*-
"""`main.py` 的助手入口判定（零依赖，避免未启用助手时也加载整套取数栈）。"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["AssistantModeDecision", "assistant_mode_decision"]


@dataclass(frozen=True)
class AssistantModeDecision:
    """是否进入助手独立模式；``message``/``level`` 给主流程写日志。"""

    enter: bool
    message: str = ""
    level: str = ""  # "" / "warning" / "error"


def assistant_mode_decision(
    *,
    assistant_flag: bool,
    assistant_once: bool,
    env_enabled: bool,
    start_serve: bool,
) -> AssistantModeDecision:
    """显式参数或环境变量请求助手模式；与 Web 服务并发时不允许两个调度器接管时点。"""
    requested = assistant_flag or assistant_once or env_enabled
    if not requested:
        return AssistantModeDecision(enter=False)
    if start_serve:
        if assistant_flag:
            return AssistantModeDecision(
                enter=False,
                message="--assistant/--assistant-once 不能与 --serve/--webui 同时使用",
                level="error",
            )
        return AssistantModeDecision(
            enter=False,
            message=(
                "ASSISTANT_ENABLED=true 与 Web 服务同时启用："
                "本次让 Web/API scheduler 接管，助手独立调度不启动"
            ),
            level="warning",
        )
    return AssistantModeDecision(enter=True)
