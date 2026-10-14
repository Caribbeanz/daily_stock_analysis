# -*- coding: utf-8 -*-
"""高抛低吸助手 · 结构化对话记忆（P2.3 / FR-15 / 决议 D5）

把对话历史**压缩成结构化长期记忆**，与 ``conversation_summaries`` 的滚动摘要、
``memory.py`` 的分析结论记忆**明确分层**：

- 本模块只产出/管理**结构化记忆条目**，不覆盖原始对话；
- 触发沿用既有节奏：token 阈值（默认 12000）+ 每周兜底 + 会话结束增量（``force``）；
- 保留策略（决议 D5）：原文长期保留、压缩**只增不删**；删除由用户显式触发，
  支持软删（保留可审计）/ 硬删 / 一键清空（强确认）。

记忆类型（FR-15.3）：

==============  ==========================
memory_type     含义
==============  ==========================
position_change 持仓 / 成本变更
preference      用户偏好与约束
confirmed_action 用户确认执行的操作（来自 ``action_service`` 审计）
analysis_conclusion 重要分析结论与决策
==============  ==========================

抽取策略：**规则优先**（纯函数、零成本、可单测），LLM 兜底**可注入**（默认不启用）。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from src.repositories.chat_memory_repo import (
    ChatMemoryRepository,
    MEMORY_STATUS_ACTIVE,
    MEMORY_STATUS_DELETED,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MEMORY_TYPES",
    "MEMORY_TYPE_LABELS",
    "MemoryCandidate",
    "ChatMemoryService",
    "classify_message",
    "extract_candidates",
]

MEMORY_TYPES = ("position_change", "preference", "confirmed_action", "analysis_conclusion")

MEMORY_TYPE_LABELS: Dict[str, str] = {
    "position_change": "持仓变更",
    "preference": "偏好与约束",
    "confirmed_action": "已执行操作",
    "analysis_conclusion": "分析结论",
}

# 单条记忆内容上限（防止把整段长文塞进记忆）
_MAX_CONTENT_CHARS = 400

# ---- 规则模式（用户消息） ---- #
_PREFERENCE_PATTERNS = (
    "我偏好", "我更喜欢", "我倾向", "我喜欢", "我个人偏好",
    "我的风险偏好", "我的操作风格", "我的投资风格", "我的偏好",
    "记住", "以后都", "以后请", "以后要", "以后别", "以后不要",
    "今后都", "今后请", "下次别", "下次不要",
    "不要再", "别提醒", "不要提醒", "不要推送", "不要通知",
)
_POSITION_PATTERNS = (
    "持仓设为", "持仓设成", "仓位设为", "仓位设成", "成本设为",
    "已经清仓", "已清仓", "清仓了", "清空了", "我的持仓", "我的成本",
    "加仓了", "减仓了", "买入了", "卖出了", "建仓了",
)
# ---- 分析结论标记（助手消息） ---- #
_CONCLUSION_MARKERS = (
    "结论：", "人话结论", "综上", "目标价", "止损位", "操作建议", "最终结论",
)


@dataclass(frozen=True)
class MemoryCandidate:
    """一条待写入的结构化记忆。"""

    memory_type: str
    content: str


def _trim(text: str, limit: int = _MAX_CONTENT_CHARS) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[:limit].rstrip() + "…"


def classify_message(role: str, content: str) -> List[MemoryCandidate]:
    """对单条消息做**规则**抽取（纯函数）。

    - 用户消息 → ``preference`` / ``position_change``；
    - 助手消息 → ``analysis_conclusion``（命中结论标记时）。
    """
    text = str(content or "").strip()
    if not text:
        return []
    role = str(role or "").strip().lower()
    candidates: List[MemoryCandidate] = []

    if role == "user":
        if any(kw in text for kw in _PREFERENCE_PATTERNS):
            candidates.append(MemoryCandidate("preference", _trim(text)))
        if any(kw in text for kw in _POSITION_PATTERNS):
            candidates.append(MemoryCandidate("position_change", _trim(text)))
    elif role in {"assistant", "system"}:
        if any(marker in text for marker in _CONCLUSION_MARKERS):
            candidates.append(MemoryCandidate("analysis_conclusion", _trim(text)))

    return candidates


def extract_candidates(messages: Sequence[Mapping[str, Any]]) -> List[MemoryCandidate]:
    """从一批消息中抽取候选记忆（去重，保持出现顺序）。"""
    seen = set()
    result: List[MemoryCandidate] = []
    for message in messages:
        role = str(message.get("role") or "")
        content = str(message.get("content") or "")
        for candidate in classify_message(role, content):
            key = (candidate.memory_type, candidate.content)
            if key in seen:
                continue
            seen.add(key)
            result.append(candidate)
    return result


def _estimate_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    """确定性 token 估计（字符数 / 3），避免单测依赖外部 tokenizer。"""
    total = sum(len(str(m.get("content") or "")) for m in messages)
    return int(math.ceil(total / 3)) if total else 0


class ChatMemoryService:
    """结构化记忆编排：抽取 / 压缩 / 管理（FR-15.2~.5）。"""

    def __init__(
        self,
        *,
        repo: Optional[ChatMemoryRepository] = None,
        db: Any = None,
        llm: Optional[Callable[[Sequence[Mapping[str, Any]]], Optional[List[Dict[str, Any]]]]] = None,
        config: Any = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._repo = repo
        self._db = db
        self._llm = llm
        self._config = config
        self._clock = clock or datetime.now

    # ---------------- 依赖 ---------------- #
    @property
    def repo(self) -> ChatMemoryRepository:
        if self._repo is None:
            self._repo = ChatMemoryRepository()
        return self._repo

    @property
    def db(self):
        if self._db is None:
            from src.storage import get_db

            self._db = get_db()
        return self._db

    @property
    def config(self):
        if self._config is None:
            from src.config import get_config

            self._config = get_config()
        return self._config

    def _cfg(self, name: str, default: Any) -> Any:
        try:
            return getattr(self.config, name, default)
        except Exception:
            return default

    @property
    def enabled(self) -> bool:
        return bool(self._cfg("hslb_memory_enabled", True))

    @property
    def token_threshold(self) -> int:
        try:
            return int(self._cfg("hslb_memory_compression_tokens", 12000) or 12000)
        except (TypeError, ValueError):
            return 12000

    @property
    def weekly_seconds(self) -> int:
        try:
            return int(self._cfg("hslb_memory_weekly_seconds", 7 * 24 * 3600) or 7 * 24 * 3600)
        except (TypeError, ValueError):
            return 7 * 24 * 3600

    # ---------------- 写入 ---------------- #
    def remember(
        self,
        memory_type: str,
        content: str,
        *,
        source_message_ids: Optional[Sequence[int]] = None,
        session_id: Optional[str] = None,
    ) -> Optional[int]:
        """显式写入一条结构化记忆（类型非法 / 内容为空则忽略）。"""
        if memory_type not in MEMORY_TYPES:
            logger.warning("忽略非法记忆类型：%s", memory_type)
            return None
        text = _trim(content)
        if not text:
            return None
        return self.repo.add(
            memory_type,
            text,
            source_message_ids=list(source_message_ids or []),
            session_id=session_id,
        )

    def record_action(self, event: Mapping[str, Any]) -> Optional[int]:
        """把 ``action_service`` 审计事件（仅成功项）记为 ``confirmed_action``。

        供 P2.2 的 ``audit_sink`` 接线（FR-15.3「用户确认执行的操作」）。
        """
        if str(event.get("status") or "") != "ok":
            return None
        op = str(event.get("action") or "")
        target = str(event.get("target") or "")
        description = str(event.get("description") or op)
        content = f"已执行：{description}"
        if target and target not in description:
            content = f"{content}（{target}）"
        return self.remember(
            "confirmed_action",
            content,
            source_message_ids=event.get("source_message_ids") or [],
            session_id=event.get("session_id"),
        )

    # ---------------- 压缩 ---------------- #
    def should_compress(self, session_id: str, messages: Optional[Sequence[Mapping[str, Any]]] = None) -> bool:
        """是否需要压缩（token 阈值 或 每周兜底）。"""
        if not self.enabled:
            return False
        state = self.repo.get_cursor_state(session_id)
        if messages is None:
            messages = self.db.get_conversation_messages_after(session_id, state["last_message_id"])
        if not messages:
            return False
        if _estimate_tokens(messages) >= self.token_threshold:
            return True
        updated = state.get("updated_at")
        if updated is None:
            # 首次压缩等待阈值触发（避免刚开会话就产生记忆条目）
            return False
        if isinstance(updated, str):
            try:
                updated = datetime.fromisoformat(updated)
            except ValueError:
                return False
        try:
            return (self._clock() - updated) >= timedelta(seconds=self.weekly_seconds)
        except TypeError:
            return False

    def compress_session(self, session_id: str, *, force: bool = False) -> Dict[str, Any]:
        """把某会话的**未压缩消息**抽取为结构化记忆。

        Args:
            session_id: 会话 id。
            force: 忽略触发条件（供"会话结束增量压缩"）。
        """
        if not self.enabled and not force:
            return {"compressed": 0, "skipped": "disabled"}
        if not session_id:
            return {"compressed": 0, "skipped": "no_session"}

        state = self.repo.get_cursor_state(session_id)
        messages = self.db.get_conversation_messages_after(session_id, state["last_message_id"])
        if not messages:
            return {"compressed": 0, "skipped": "no_new_messages"}
        if not force and not self.should_compress(session_id, messages):
            return {"compressed": 0, "skipped": "below_threshold"}

        candidates = extract_candidates(messages)
        if not candidates and self._llm is not None:
            candidates = self._extract_with_llm(messages)

        ids = [int(m.get("id") or 0) for m in messages]
        created = 0
        for candidate in candidates:
            if self.remember(
                candidate.memory_type,
                candidate.content,
                source_message_ids=ids,
                session_id=session_id,
            ) is not None:
                created += 1
        last_id = max([i for i in ids if i > 0] or [state["last_message_id"]])
        self.repo.set_cursor(session_id, last_id)
        return {
            "compressed": created,
            "scanned": len(messages),
            "last_message_id": last_id,
            "types": {c.memory_type: sum(1 for x in candidates if x.memory_type == c.memory_type) for c in candidates},
        }

    def _extract_with_llm(self, messages: Sequence[Mapping[str, Any]]) -> List[MemoryCandidate]:
        try:
            raw = self._llm(messages)  # type: ignore[misc]
        except Exception as exc:  # LLM 失败不影响压缩主流程
            logger.warning("记忆 LLM 兜底失败：%s", exc)
            return []
        result: List[MemoryCandidate] = []
        for item in raw or []:
            if not isinstance(item, Mapping):
                continue
            memory_type = str(item.get("type") or item.get("memory_type") or "")
            content = str(item.get("content") or "")
            if memory_type in MEMORY_TYPES and content:
                result.append(MemoryCandidate(memory_type, _trim(content)))
        return result

    # ---------------- 管理（FR-15.5） ---------------- #
    def list_memories(
        self,
        *,
        memory_type: Optional[str] = None,
        session_id: Optional[str] = None,
        include_deleted: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        status = None if include_deleted else MEMORY_STATUS_ACTIVE
        items = self.repo.list_memories(
            memory_type=memory_type,
            session_id=session_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        total = self.repo.count(memory_type=memory_type, session_id=session_id, status=status)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def get_memory(self, memory_id: int, *, with_sources: bool = True) -> Optional[Dict[str, Any]]:
        record = self.repo.get(memory_id)
        if record is None:
            return None
        if with_sources:
            record["sources"] = self.db.get_conversation_messages_by_ids(record.get("source_message_ids") or [])
        return record

    def delete(self, memory_id: int, *, hard: bool = False) -> Dict[str, Any]:
        """删除一条记忆：默认软删（保留可审计），``hard=True`` 物理删除。"""
        if hard:
            affected = self.repo.hard_delete(memory_id)
            mode = "hard"
        else:
            affected = self.repo.soft_delete(memory_id)
            mode = "soft"
        return {"deleted": affected > 0, "mode": mode, "id": int(memory_id)}

    def restore(self, memory_id: int) -> Dict[str, Any]:
        affected = self.repo.restore(memory_id)
        return {"restored": affected > 0, "id": int(memory_id)}

    def clear(self, *, session_id: Optional[str] = None, hard: bool = True) -> Dict[str, Any]:
        """一键清空（**强确认由调用方把关**，FR-15.5）。"""
        affected = self.repo.clear(session_id=session_id, hard=hard)
        return {"cleared": affected, "mode": "hard" if hard else "soft", "session_id": session_id}

    def stats(self) -> Dict[str, Any]:
        active = self.repo.count(status=MEMORY_STATUS_ACTIVE)
        deleted = self.repo.count(status=MEMORY_STATUS_DELETED)
        by_type = self.repo.count_by_type(status=MEMORY_STATUS_ACTIVE)
        return {
            "active": active,
            "deleted": deleted,
            "total": active + deleted,
            "by_type": {t: by_type.get(t, 0) for t in MEMORY_TYPES},
        }
