# -*- coding: utf-8 -*-
"""图标语义表（需求 §3.5.3.1「图标语义表」+ R22）。

**固定映射，不得同义换符**——飞书、卡片、WebUI 三处共用这一份，同一含义出现两种
符号就是缺陷（B47）。

三条设计理由，写在这里避免以后被「优化」掉：

1. **风险用颜色圆点，涨跌用箭头**：A 股「红＝涨」，而风险「红＝危险」，同一个红色
   承担两种相反语义必然误读；纯文本又无法用字色区分，所以价格涨跌一律 ▲/▼。
2. **符号是前缀，不是替代**：每个符号后必须跟中文标签（`🔴 严重`），客户端不渲染
   emoji 或被转发到纯文本渠道时语义不能丢（硬约束 1）。
3. **一个动作只对应一个符号**：`买入` 与 `加仓` 同为 ➕ 是「同一类动作」，不是一符多义；
   但「减仓 ➖」与「清仓 🔻」必须分开，因为执行方式完全不同。
"""

from __future__ import annotations

import re

from src.assistant.enums import Action, RiskLevel

__all__ = [
    "ACTION_ICON",
    "CAL_ICON",
    "RISK_ICON",
    "SECTION_ICONS",
    "action_label",
    "action_with_icon",
    "risk_label",
    "risk_with_icon",
    "strip_markdown",
    "trend_arrow",
]

#: 风险等级 → 图标 + 中文标签（只用于风险，不得用于涨跌）
RISK_ICON: dict[str, str] = {
    RiskLevel.SEVERE.value: "🔴 严重",
    RiskLevel.WARNING.value: "🟠 警示",
    RiskLevel.INFO.value: "🔵 观察",
}

#: 无风险信号时的统一写法（需求 §3.5.3.1 ③ 原文）
RISK_NONE = "⚪ 今日无风险信号"

#: 动作 → 图标（一个动作一个符号）
ACTION_ICON: dict[str, str] = {
    Action.BUY.value: "➕",
    Action.ADD.value: "➕",
    Action.PROBE.value: "➕",
    Action.WAIT_BUY.value: "⏳",
    Action.WAIT_SELL.value: "⏳",
    Action.TRIM.value: "➖",
    Action.CLEAR.value: "🔻",
    Action.HOLD.value: "⏸",
    Action.WATCH.value: "👀",
    Action.NO_OP.value: "⏸",
    Action.UNAVAILABLE.value: "⛔",
}

#: ① 段第二行（三元组）用的日历图标
CAL_ICON = "🗓"

#: 七段标题图标（固定，不新增）
SECTION_ICONS: dict[str, str] = {
    "header": "📊",
    "summary": "🧭",
    "risk": "🔥",
    "todo": "✅",
    "stage": "📍",
    "plan": "📋",
    "notice": "🔒",
}


def risk_with_icon(level: str) -> str:
    """``"严重"`` → ``"🔴 严重"``；未知等级直接返回原文（不猜）。"""
    return RISK_ICON.get(level, level)


def action_with_icon(action: str) -> str:
    """``"减仓"`` → ``"➖ 减仓"``；未知动作直接返回原文。"""
    icon = ACTION_ICON.get(action)
    return f"{icon} {action}" if icon else action


def action_label(action: str | None) -> str:
    """动作的带图标写法；``None``/空值写作「—」。"""
    if not action:
        return "—"
    return action_with_icon(action)


def risk_label(level: str | None) -> str:
    if not level:
        return RISK_NONE
    return risk_with_icon(level)


def trend_arrow(pct: float | None) -> str:
    """涨跌箭头（**不用红绿圆点**，见模块说明）。"""
    if pct is None:
        return ""
    if pct > 0:
        return f"▲ {pct:+.2f}%"
    if pct < 0:
        return f"▼ {pct:+.2f}%"
    return "— 0.00%"


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_HEADING_RE = re.compile(r"^#{1,6}\s*", re.MULTILINE)


def strip_markdown(text: str) -> str:
    """纯文本降级前剥离标记（硬约束 5）。

    飞书主路径发 `interactive` 卡片（`lark_md` 支持加粗），失败降级 `msg_type=text`
    时若不剥离就会把 `**减仓**` 原样露出来。同时去掉标题井号——纯文本渠道不解析。
    """
    if not text:
        return ""
    stripped = _BOLD_RE.sub(r"\1", text)
    stripped = _HEADING_RE.sub("", stripped)
    return stripped
