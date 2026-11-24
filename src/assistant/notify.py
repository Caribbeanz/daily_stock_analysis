# -*- coding: utf-8 -*-
"""发送接线：把一份 :class:`DeliveryPayload` 交给**既有**通知链（方案 §6.3）。

护栏（方案 §1.2 表）说得很死：**不新增平行发送器、不改既有渠道行为**。所以这里
只做「翻译参数」这一件事，真正的发送仍由 ``NotificationService``（内含
``feishu_sender``）完成，路由、降噪、能力画像、分片、卡片降级全都不重写。

四个接线口径：

1. **新增 1 个 ``route_type``**：``assistant_daily``。对应 ``NOTIFICATION_ASSISTANT_CHANNELS``，
   留空则沿用全部已配置渠道（与既有三条路由的语义一致）。其余三条路由行为不变。
2. **去重键＝``snapshot_id`` + ``allow_repeat``**：多时点同源投递是**有意**重复，
   不能被 ``notification_noise`` 的内容去重静默吞掉；但静默时段与最低级别照旧生效
   （那是「什么时候发、多大声音」的问题，不是「要不要重复」的问题）。
3. **纯文本降级**：飞书主路径发 ``interactive`` 卡片（``lark_md`` 支持加粗），卡片被
   拒后降级 ``msg_type=text``；此时改发 :func:`strip_markdown` 处理过的版本，避免把
   ``**加仓**`` 原样露出来（硬约束 5）。
4. **失败即失败**：``send`` 返回 ``False`` 就返回 ``False``，由调度台账释放重试；
   这里不吞错、不重试、不伪造成功。

``src/notification.py`` 全部**延迟 import**：助手包不装通知链的依赖（dotenv 等）也能
单独跑测试与离线渲染。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Callable, Optional, Protocol

from src.assistant.render.icons import strip_markdown
from src.assistant.scheduling.delivery import DeliveryPayload
from src.assistant.scheduling.gate import GateDecision

__all__ = [
    "ASSISTANT_ROUTE_TYPE",
    "NON_TRADING_ICON",
    "make_sender",
    "non_trading_text",
    "notify_non_trading",
    "send_payload",
]

logger = logging.getLogger(__name__)

#: 助手专用路由类型（``notification_routing.NOTIFICATION_ROUTE_CONFIGS`` 已注册）
ASSISTANT_ROUTE_TYPE = "assistant_daily"

#: 非交易日短提醒的图标（与七段图标表分开：它不是七段之一）
NON_TRADING_ICON = "📴"


class SupportsSend(Protocol):
    """``NotificationService`` 的最小子集（测试注入假实现用）。"""

    def send(self, content: str, **kwargs: Any) -> bool:  # pragma: no cover - 协议声明
        ...


def _resolve_service(service: Optional[SupportsSend]) -> SupportsSend:
    if service is not None:
        return service
    from src.notification import get_notification_service

    return get_notification_service()


def send_payload(
    payload: DeliveryPayload,
    *,
    service: Optional[SupportsSend] = None,
    route_type: str = ASSISTANT_ROUTE_TYPE,
) -> bool:
    """投递一份当日快照文本；返回是否至少有一个渠道发送成功。

    幂等不由这里负责——同一时点重复投递由调度台账（``assistant_delivery_log``）
    拦住；这里只保证「内容不被降噪层静默吞掉」。
    """
    text = (payload.text or "").strip()
    if not text:
        logger.warning(
            "股票分析助手快照 %s 正文为空，已跳过投递（不发送空消息）",
            payload.snapshot_id,
        )
        return False

    notifier = _resolve_service(service)
    ok = bool(
        notifier.send(
            text,
            route_type=route_type,
            dedup_key=payload.snapshot_id,
            allow_repeat=True,  # 多时点同源：同一份文本要能再发一次
            fallback_content=strip_markdown(text),
        )
    )
    if ok:
        logger.info(
            "股票分析助手已投递 snapshot=%s date=%s bar=%s 字数=%d stale=%s",
            payload.snapshot_id,
            payload.data_date.isoformat(),
            payload.effective_bar_date.isoformat(),
            len(text),
            payload.stale,
        )
    else:
        logger.error(
            "股票分析助手投递失败 snapshot=%s date=%s（台账将释放以便重试）",
            payload.snapshot_id,
            payload.data_date.isoformat(),
        )
    return ok


def make_sender(
    service: Optional[SupportsSend] = None,
    *,
    route_type: str = ASSISTANT_ROUTE_TYPE,
) -> Callable[[DeliveryPayload], bool]:
    """给 :class:`MultiSlotDispatcher` 用的 ``sender``（``payload -> bool``）。"""

    def sender(payload: DeliveryPayload) -> bool:
        return send_payload(payload, service=service, route_type=route_type)

    return sender


def non_trading_text(day: date) -> str:
    """``notify_skip`` 下的短提醒正文（不生成结论，只交代一句）。"""
    return (
        f"{NON_TRADING_ICON} {day.isoformat()} 非交易日\n"
        "今天不生成结论，也不推送条件计划；下一交易日 20:00 恢复。"
    )


def notify_non_trading(
    day: date,
    decision: Optional[GateDecision] = None,
    *,
    service: Optional[SupportsSend] = None,
    route_type: str = ASSISTANT_ROUTE_TYPE,
) -> bool:
    """非交易日提醒（``non_trading_action=notify_skip`` 时由调度器调用）。

    ``decision`` 只用于日志（含门控原因），不参与正文——正文里写「为什么放假」
    对用户没有价值，写错反而误导。
    """
    text = non_trading_text(day)
    notifier = _resolve_service(service)
    ok = bool(
        notifier.send(
            text,
            route_type=route_type,
            dedup_key=f"assistant-skip-{day.isoformat()}",
            allow_repeat=True,
            fallback_content=strip_markdown(text),
        )
    )
    reason = getattr(getattr(decision, "reason", None), "value", "unknown")
    if ok:
        logger.info("股票分析助手非交易日提醒已发送 date=%s gate=%s", day.isoformat(), reason)
    else:
        logger.warning("股票分析助手非交易日提醒发送失败 date=%s gate=%s", day.isoformat(), reason)
    return ok
