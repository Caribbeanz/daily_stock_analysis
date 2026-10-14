# -*- coding: utf-8 -*-
"""高抛低吸助手 · 域事件总线（P2.4 / FR-14.0.5）

跨端实时刷新的发布 / 订阅底座：任一端（H5 / 飞书 / API）发生**写操作**后，
发布一条 ``invalidate`` 域事件；订阅者（SSE 端点）即把事件推给各端，
各端据 ``scopes`` 做**增量刷新**，无需重开页面（方案 §3.4）。

设计要点：
- **线程安全**：写操作发生在线程池（``run_in_executor``），而 SSE 消费在
  asyncio 事件循环；``publish`` 用 ``loop.call_soon_threadsafe`` 跨线程投递，
  与既有 ``AnalysisTaskQueue`` 同一套思路；
- **无订阅者不报错**：无人订阅时仅记入最近事件环形缓冲（供新连接补发 / 单测）；
- **不阻塞写链路**：发布失败只记日志，绝不影响已完成的写操作。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = ["DomainEvent", "DomainEventBus", "get_domain_event_bus"]

# 最近事件环形缓冲上限（供新订阅者补发最近一次失效通知）
_RECENT_LIMIT = 50


class DomainEvent:
    """一条域事件（``type`` + ``data``）。"""

    __slots__ = ("type", "data")

    def __init__(self, event_type: str, data: Dict[str, Any]):
        self.type = str(event_type)
        self.data = dict(data or {})

    def as_payload(self) -> Dict[str, Any]:
        return {"type": self.type, "data": self.data}

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<DomainEvent {self.type} {self.data}>"


class DomainEventBus:
    """进程内域事件总线（线程安全，可注入事件循环以便单测）。"""

    def __init__(self, *, recent_limit: int = _RECENT_LIMIT) -> None:
        self._subscribers: List["asyncio.Queue[DomainEvent]"] = []
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._recent: Deque[DomainEvent] = deque(maxlen=max(1, int(recent_limit)))

    # ---------------- 订阅 ---------------- #
    def bind_loop(self, loop: Optional[asyncio.AbstractEventLoop]) -> None:
        """绑定事件循环（应在 asyncio 上下文中调用）。"""
        with self._lock:
            self._loop = loop

    def subscribe(self, queue: "asyncio.Queue[DomainEvent]") -> None:
        """订阅域事件；同时尝试绑定当前事件循环。"""
        if self._loop is None:
            try:
                self.bind_loop(asyncio.get_running_loop())
            except RuntimeError:
                pass
        with self._lock:
            if queue not in self._subscribers:
                self._subscribers.append(queue)

    def unsubscribe(self, queue: "asyncio.Queue[DomainEvent]") -> None:
        with self._lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    # ---------------- 发布 ---------------- #
    def publish(self, event_type: str, data: Dict[str, Any]) -> DomainEvent:
        """发布一条域事件（线程安全；无订阅者时仅入环形缓冲）。"""
        event = DomainEvent(event_type, data)
        with self._lock:
            self._recent.append(event)
            subscribers = list(self._subscribers)
            loop = self._loop

        if not subscribers:
            return event

        if loop is None or loop.is_closed():
            logger.debug("[DomainEventBus] 无可用事件循环，事件仅入缓冲：%s", event.type)
            return event

        for queue in subscribers:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError as exc:  # 循环已关闭
                logger.debug("[DomainEventBus] 投递跳过（循环关闭）：%s", exc)
            except Exception as exc:  # 不影响写链路
                logger.warning("[DomainEventBus] 投递失败：%s", exc)
        return event

    def recent(self, limit: int = 10) -> List[Dict[str, Any]]:
        """最近事件快照（供新连接补发 / 单测）。"""
        with self._lock:
            events = list(self._recent)
        return [event.as_payload() for event in events[-max(1, int(limit)):]]

    def clear(self) -> None:
        """清空订阅者与缓冲（仅供单测）。"""
        with self._lock:
            self._subscribers.clear()
            self._recent.clear()


# --------------------------------------------------------------------------- #
# 进程级单例
# --------------------------------------------------------------------------- #
_BUS: Optional[DomainEventBus] = None
_BUS_LOCK = threading.Lock()


def get_domain_event_bus() -> DomainEventBus:
    """获取进程级域事件总线单例。"""
    global _BUS
    if _BUS is None:
        with _BUS_LOCK:
            if _BUS is None:
                _BUS = DomainEventBus()
    return _BUS
