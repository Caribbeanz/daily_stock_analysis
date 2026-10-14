# -*- coding: utf-8 -*-
"""高抛低吸助手 · 写操作二次确认令牌（P2.2 / FR-14.2.6、决议 D2）

"全部写操作一律二次确认"的落地方式：**预览产生令牌 → 用户确认 → 凭令牌执行**。

设计要点：

- **一次性消费**：``pop`` 取走即失效，避免同一确认被重放执行两次；
- **带 TTL**：默认 5 分钟过期，超时需重新预览（防止"隔很久的旧确认"误落地）；
- **进程内存储**：本助手按 D6 为**单机内网**部署，单进程内存表足够；
  同时保留 ``store_path`` 可选持久化位（后续 P2.4 需要跨进程/重启可换实现）；
- **按 actor 取最近一条**：供飞书"回复「确认」"这种无令牌交互定位待确认动作。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = ["PendingAction", "ConfirmationStore"]

DEFAULT_TTL_SECONDS = 300.0
CONFIRM_WORDS = ("确认", "确定", "是", "好", "执行", "可以", "ok", "yes", "y", "confirm")
CANCEL_WORDS = ("取消", "算了", "不用了", "放弃", "撤销", "no", "cancel", "n")


def _norm(text: str) -> str:
    return str(text or "").strip().strip("。.！!～~ ").lower()


def is_confirm_text(text: str) -> bool:
    """判断一段文本是否是"确认"。"""
    return _norm(text) in CONFIRM_WORDS


def is_cancel_text(text: str) -> bool:
    """判断一段文本是否是"取消"。"""
    return _norm(text) in CANCEL_WORDS


@dataclass
class PendingAction:
    """一条待确认的写操作计划。"""

    token: str
    payload: Any
    actor: str = ""
    session_id: str = ""
    source: str = ""
    summary: str = ""
    created_at: float = 0.0
    expires_at: float = 0.0

    def is_expired(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "token": self.token,
            "actor": self.actor,
            "session_id": self.session_id,
            "source": self.source,
            "summary": self.summary,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


class ConfirmationStore:
    """进程内确认令牌表（线程安全，可注入 clock 便于单测）。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        max_items: int = 500,
    ) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._max_items = int(max_items)
        self._items: Dict[str, PendingAction] = {}
        self._lock = threading.Lock()

    # ---------------- 写入 ---------------- #
    def put(
        self,
        payload: Any,
        *,
        actor: str = "",
        session_id: str = "",
        source: str = "",
        summary: str = "",
        ttl_seconds: Optional[float] = None,
    ) -> PendingAction:
        now = self._clock()
        ttl = self.ttl_seconds if ttl_seconds is None else float(ttl_seconds)
        pending = PendingAction(
            token=uuid.uuid4().hex[:12],
            payload=payload,
            actor=str(actor or ""),
            session_id=str(session_id or ""),
            source=str(source or ""),
            summary=str(summary or ""),
            created_at=now,
            expires_at=now + ttl,
        )
        with self._lock:
            self._purge_locked(now)
            self._items[pending.token] = pending
            if len(self._items) > self._max_items:  # 淘汰最旧，避免无限增长
                oldest = min(self._items.values(), key=lambda item: item.created_at)
                self._items.pop(oldest.token, None)
        return pending

    # ---------------- 读取 / 消费 ---------------- #
    def peek(self, token: str) -> Optional[PendingAction]:
        with self._lock:
            self._purge_locked(self._clock())
            return self._items.get(str(token or ""))

    def pop(self, token: str) -> Optional[PendingAction]:
        """取走并作废（一次性消费）。"""
        with self._lock:
            self._purge_locked(self._clock())
            return self._items.pop(str(token or ""), None)

    def latest_for_actor(self, actor: str) -> Optional[PendingAction]:
        key = str(actor or "")
        if not key:
            return None
        with self._lock:
            self._purge_locked(self._clock())
            candidates = [item for item in self._items.values() if item.actor == key]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.created_at)

    def pop_latest_for_actor(self, actor: str) -> Optional[PendingAction]:
        pending = self.latest_for_actor(actor)
        if pending is None:
            return None
        return self.pop(pending.token)

    def cancel(self, token: str) -> bool:
        with self._lock:
            return self._items.pop(str(token or ""), None) is not None

    def size(self) -> int:
        with self._lock:
            self._purge_locked(self._clock())
            return len(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    # ---------------- 内部 ---------------- #
    def _purge_locked(self, now: float) -> None:
        expired = [token for token, item in self._items.items() if item.expires_at <= now]
        for token in expired:
            self._items.pop(token, None)
