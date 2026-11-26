# -*- coding: utf-8 -*-
"""飞书入站四项能力（需求 §3.6）。

定位：**入站只写配置与笔记，不碰决策**。四条硬边界：

1. **身份**：写指令（关注增删、笔记）必须命中 `user_id` 白名单；未授权只回提示，
   **不执行、不记录内容**（连笔记原文都不落库）。
2. **二次确认**：关注增删先回显再等「确认」，带超时；未确认不写入。确认态只存内存，
   进程重启即作废——宁可让用户重发，也不要在重启后凭空执行一条旧指令。
3. **生效时点**：写的是**输入配置**，下一次快照生成时才生效；当日已生成的结论、
   条件计划、风险信号一律不重算、不回溯修改。
4. **只读**：查询与复盘只读已固化快照与笔记，不重算、不产生新建议。

本模块不依赖 `bot/`，命令层只是薄壳，便于离线单测。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Mapping, Optional

from src.assistant.data.storage import AssistantStore

__all__ = [
    "AssistantInbound",
    "InboundSettings",
    "PendingWatch",
]

logger = logging.getLogger(__name__)

WATCH_ADD = "add"
WATCH_REMOVE = "remove"
_ACTION_LABEL = {WATCH_ADD: "加入关注", WATCH_REMOVE: "移除关注"}


def _env_ids(raw: Any) -> frozenset[str]:
    text = str(raw or "").strip()
    if not text:
        return frozenset()
    return frozenset(part.strip() for part in text.replace(";", ",").split(",") if part.strip())


@dataclass(frozen=True)
class InboundSettings:
    """入站配置：白名单 + 确认超时。"""

    allowed_user_ids: frozenset[str] = frozenset()
    confirm_ttl_seconds: int = 180

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> InboundSettings:
        import os

        source = env if env is not None else os.environ
        raw_ttl = str(source.get("ASSISTANT_INBOUND_CONFIRM_TTL") or "").strip()
        ttl = 180
        if raw_ttl:
            try:
                ttl = max(30, min(900, int(float(raw_ttl))))
            except ValueError:
                logger.warning("ASSISTANT_INBOUND_CONFIRM_TTL=%r 非法，用默认 180 秒", raw_ttl)
        return cls(
            allowed_user_ids=_env_ids(source.get("ASSISTANT_INBOUND_ALLOWED_USERS")),
            confirm_ttl_seconds=ttl,
        )

    def is_allowed(self, user_id: str) -> bool:
        return bool(user_id) and user_id in self.allowed_user_ids


@dataclass(frozen=True)
class PendingWatch:
    """待确认的关注变更（内存态，超时作废）。"""

    user_id: str
    action: str
    code: str
    name: str
    created_at: datetime
    raw_text: str = ""


@dataclass
class AssistantInbound:
    """入站业务处理：返回**给用户看的文本**，不直接发消息（发送由平台适配器负责）。"""

    store: AssistantStore
    settings: InboundSettings = field(default_factory=InboundSettings.from_env)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now())
    #: 交易日推进（确认后的生效日）；None 时保守取「次日」
    next_trading_day: Optional[Callable[[date], date]] = None
    _pending: dict[str, PendingWatch] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------
    # 1. 关注增删（写配置，二次确认）
    # ------------------------------------------------------------------
    def request_watch(self, user_id: str, action: str, code: str, *, raw_text: str = "") -> str:
        """收到「关注/移除」指令：校验 → 回显 → 等确认；**此时还不写入**。"""
        if not self.settings.is_allowed(user_id):
            return self._unauthorized()
        if action not in (WATCH_ADD, WATCH_REMOVE):
            return "只支持「关注」与「移除关注」两种操作。"
        code = (code or "").strip()
        if not (code.isdigit() and len(code) == 6):
            return f"代码 {code or '（空）'} 不是 6 位 A 股代码，未做任何改动。"

        current = self.store.effective_watchlist()
        if action == WATCH_ADD and code in current:
            return f"{code} 已在关注列表，无需重复添加。"
        if action == WATCH_REMOVE and code not in current:
            return f"{code} 不在关注列表，未做任何改动。"

        name = self._name_of(code)
        self._pending[user_id] = PendingWatch(
            user_id=user_id,
            action=action,
            code=code,
            name=name,
            created_at=self.clock(),
            raw_text=raw_text or f"{_ACTION_LABEL[action]} {code}",
        )
        label = f"{code} {name}".strip() if name else f"{code}（名称待确认）"
        return (
            f"将{_ACTION_LABEL[action]}：{label}\n"
            f"确认请回复「确认」，{self.settings.confirm_ttl_seconds // 60 or 1} 分钟内有效；"
            "回复「取消」放弃。\n"
            "确认后从**下一次快照**开始纳入分析，当日已生成的结论不会变。"
        )

    def confirm(self, user_id: str) -> str:
        """确认一次待办；超时/无待办都如实说明，不静默。"""
        if not self.settings.is_allowed(user_id):
            return self._unauthorized()
        pending = self._pending.get(user_id)
        if pending is None:
            return "没有待确认的操作。"
        if self._expired(pending):
            self._pending.pop(user_id, None)
            return "这条确认已超时作废，请重新发送指令。"
        self._pending.pop(user_id, None)

        before = sorted(self.store.effective_watchlist())
        effective = self._effective_date()
        audit_id = self.store.append_watchlist(
            pending.code, pending.action, effective_date=effective, user_id=user_id
        )
        after = sorted(self.store.effective_watchlist())
        try:
            self.store.log_inbound(
                user_id=user_id,
                command="watch",
                action=pending.action,
                code=pending.code,
                confirmed=True,
                effective_date=effective,
                before_state=before,
                after_state=after,
                raw_text=pending.raw_text,
            )
        except Exception as exc:  # noqa: BLE001 - 留痕失败不该吞掉已生效的写入
            logger.warning("入站审计写入失败（关注已生效）：%s: %s", type(exc).__name__, exc)

        label = f"{pending.code} {pending.name}".strip()
        if pending.action == WATCH_REMOVE:
            return (
                f"已移除关注：{label}（生效日 {effective:%Y-%m-%d}）\n"
                "移除只影响后续分析，不会改动已有持仓记录。\n"
                f"留痕号 {audit_id}。"
            )
        return (
            f"已于 {self.clock():%Y-%m-%d} 记录：{label}\n"
            f"**{effective:%Y-%m-%d} 20:00 快照开始纳入分析**（当日已生成的结论不重算）。\n"
            f"留痕号 {audit_id}。"
        )

    def cancel(self, user_id: str) -> str:
        if not self.settings.is_allowed(user_id):
            return self._unauthorized()
        pending = self._pending.pop(user_id, None)
        if pending is None:
            return "没有待确认的操作。"
        try:
            self.store.log_inbound(
                user_id=user_id,
                command="watch",
                action=pending.action,
                code=pending.code,
                confirmed=False,
                raw_text=pending.raw_text,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("入站审计写入失败（取消）：%s: %s", type(exc).__name__, exc)
        return f"已取消：{_ACTION_LABEL[pending.action]} {pending.code}。"

    # ------------------------------------------------------------------
    # 2. 笔记
    # ------------------------------------------------------------------
    def add_note(self, user_id: str, text: str, *, code: str | None = None) -> str:
        if not self.settings.is_allowed(user_id):
            return self._unauthorized()
        content = (text or "").strip()
        if not content:
            return "笔记内容为空，未记录。"
        if code and not (code.isdigit() and len(code) == 6):
            return f"代码 {code} 不是 6 位 A 股代码，未记录。"
        trade_date = self._latest_data_date()
        note_id = self.store.save_note(
            content, trade_date=trade_date, user_id=user_id, code=code or None
        )
        scope = f"{trade_date:%Y-%m-%d}" + (f" · {code}" if code else " · 组合")
        return (
            f"已记录 {scope}\n"
            f"笔记编号 {note_id}；用 `/笔记` 回看。笔记不进评分、不改任何结论。"
        )

    def list_notes(self, *, code: str | None = None, limit: int = 5) -> str:
        rows = self.store.list_notes(code=code or None, limit=limit)
        if not rows:
            return "还没有笔记。" + (f"（标的 {code}）" if code else "")
        lines = [f"最近 {len(rows)} 条笔记：" + (f"（{code}）" if code else "")]
        for row in rows:
            scope = row["trade_date"] + (f" · {row['code']}" if row["code"] else " · 组合")
            lines.append(f"· {scope}｜{row['content'][:120]}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 3. 当日结论查询（只读）
    # ------------------------------------------------------------------
    def brief(self, code: str | None = None, *, include_position: bool = False) -> str:
        """只读当前快照；``include_position=False``（群聊）时隐藏股数与成本。"""
        snapshot = self.store.latest_snapshot()
        if snapshot is None:
            return "还没有生成过快照，暂无可查结论（交易日 20:00 后可用）。"
        content = self.store.snapshot_content(snapshot["snapshot_id"])
        if content is None:  # pragma: no cover
            return "快照读不出来，请稍后再试。"
        rows = list(content.get("conclusions") or [])
        head = (
            f"数据截止 {snapshot['data_date']}（快照 {snapshot['snapshot_id'][:13]}，"
            f"{content.get('calc_version', '')}）\n"
            "开盘后不更新：以下是当日固化结论，不是实时建议。"
        )
        if code:
            hit = next((row for row in rows if row.get("code") == code), None)
            if hit is None:
                return head + f"\n\n{code} 不在当日快照里（可能不在关注清单，或当日数据不足）。"
            return head + "\n\n" + self._render_stock(hit, content, include_position=include_position)

        counts: dict[str, int] = {}
        for row in rows:
            key = str(row.get("advice") or "未给")
            counts[key] = counts.get(key, 0) + 1
        summary = " · ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
        lines = [head, "", f"自选 {len(rows)} 只：{summary or '无'}"]
        for row in rows[:10]:
            lines.append(
                f"· {row.get('code')} {row.get('name') or ''}｜{row.get('advice_display') or row.get('advice')}"
                f"｜{row.get('stage')}｜风险 {row.get('risk_level')}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 4. 盘后复盘（只读回看）
    # ------------------------------------------------------------------
    def review(self, trade_date: date | None = None) -> str:
        target = trade_date or self._latest_data_date()
        snapshot = self.store.latest_snapshot(target)
        if snapshot is None:
            return f"{target:%Y-%m-%d} 没有快照，无法复盘（非交易日或当日未生成）。"
        content = self.store.snapshot_content(snapshot["snapshot_id"]) or {}
        rows = list(content.get("conclusions") or [])
        plans = list(content.get("plans") or [])
        risks = list(content.get("risks") or [])
        notes = self.store.list_notes(trade_date=target, limit=5)
        stage_counts: dict[str, int] = {}
        for row in rows:
            stage = str(row.get("stage") or "—")
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
        stage_text = " · ".join(f"{k} {v}" for k, v in sorted(stage_counts.items()))
        lines = [
            f"{target:%Y-%m-%d} 复盘（只读回看）",
            f"自选 {len(rows)} 只；阶段分布 {stage_text or '—'}",
            f"条件计划 {len(plans)} 条；风险信号 {len(risks)} 项",
        ]
        if notes:
            lines.append("当日笔记：")
            for note in notes:
                scope = note["code"] or "组合"
                lines.append(f"· {scope}｜{note['content'][:80]}")
        else:
            lines.append("当日没有笔记。")
        lines.append("以上为当日已固化内容，仅回看，不构成新建议；自由问答请用 /ask。")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _unauthorized(self) -> str:
        return "未授权：当前账号不在 ASSISTANT_INBOUND_ALLOWED_USERS 白名单内，指令未执行。"

    def _expired(self, pending: PendingWatch) -> bool:
        ttl = timedelta(seconds=max(30, self.settings.confirm_ttl_seconds))
        return self.clock() - pending.created_at > ttl

    def _latest_data_date(self) -> date:
        snapshot = self.store.latest_snapshot()
        if snapshot is not None:
            return date.fromisoformat(snapshot["data_date"])
        return self.clock().date()

    def _effective_date(self) -> date:
        """写入生效日：当日未生成快照则当日，否则顺延到下一交易日。"""
        today = self.clock().date()
        if self.store.latest_snapshot(today) is None:
            return today
        if self.next_trading_day is not None:
            try:
                return self.next_trading_day(today)
            except Exception as exc:  # noqa: BLE001 - 日历不可用按次日兜底
                logger.warning("下一交易日推算失败，按次日处理：%s: %s", type(exc).__name__, exc)
        return today + timedelta(days=1)

    def _name_of(self, code: str) -> str:
        snapshot = self.store.latest_snapshot()
        if snapshot is None:
            return ""
        content = self.store.snapshot_content(snapshot["snapshot_id"]) or {}
        for row in content.get("conclusions") or []:
            if row.get("code") == code:
                return str(row.get("name") or "")
        return ""

    def _render_stock(self, row: Mapping[str, Any], content: Mapping[str, Any], *, include_position: bool) -> str:
        lines = [
            f"{row.get('code')} {row.get('name') or ''}",
            f"操作意见：{row.get('advice_display') or row.get('advice')}（{row.get('advice_state') or '—'}）",
            f"阶段 {row.get('stage')}｜趋势 {row.get('trend')}｜位置 {row.get('position_tag')}｜评分 {row.get('score')}",
            f"风险等级：{row.get('risk_level')}",
        ]
        if row.get("human_text"):
            lines.append(str(row["human_text"]))
        plans = [item for item in (content.get("plans") or []) if item.get("code") == row.get("code")]
        if plans:
            lines.append(f"条件计划 {len(plans)} 条：")
            for plan in plans[:4]:
                lines.append(
                    f"· {plan.get('scenario') or '分支'}：{plan.get('trigger') or '—'} → {plan.get('action') or '—'}"
                )
        risks = [item for item in (content.get("risks") or []) if item.get("code") == row.get("code")]
        if risks:
            lines.append("风险：" + "；".join(str(item.get("human_text") or item.get("category")) for item in risks[:3]))
        if include_position:
            position = row.get("position") or {}
            if position:
                lines.append(f"仓位建议：{position.get('tier') or '—'}（{position.get('pct') or '—'}）")
            if row.get("last_close") is not None:
                lines.append(f"最近收盘 {row['last_close']}｜涨跌 {row.get('pct_change')}%")
        else:
            # 群聊默认不回显持仓成本/股数/金额（需求 §3.5 群聊口径）
            lines.append("（群聊不回显持仓与金额字段）")
        return "\n".join(lines)
