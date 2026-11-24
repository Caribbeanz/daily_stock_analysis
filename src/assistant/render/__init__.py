# -*- coding: utf-8 -*-
"""渲染层（需求 §3.5.3.1 / §6）。

三块：

- :mod:`.icons`  图标语义表（**固定映射**，飞书/卡片/WebUI 共用一套）
- :mod:`.review` T 日复盘（T-1 计划对照 T 日行情的只读回看）
- :mod:`.feishu` 七段图标化文本渲染（完整版 / 精简版同源产出、超长按序降级）

渲染层只读快照内容，**不重算、不改字段**；因此它天然不会与 WebUI 出现口径分叉。
"""

from src.assistant.render.feishu import (
    COMPACT_LIMIT,
    FULL_LIMIT,
    MarketLine,
    RenderOptions,
    fit_feishu,
    render_feishu,
    render_plain,
)
from src.assistant.render.icons import (
    ACTION_ICON,
    RISK_ICON,
    SECTION_ICONS,
    action_with_icon,
    risk_with_icon,
    strip_markdown,
    trend_arrow,
)
from src.assistant.render.review import PlanReview, build_plan_review

__all__ = [
    "ACTION_ICON",
    "COMPACT_LIMIT",
    "FULL_LIMIT",
    "MarketLine",
    "PlanReview",
    "RISK_ICON",
    "RenderOptions",
    "SECTION_ICONS",
    "action_with_icon",
    "build_plan_review",
    "fit_feishu",
    "render_feishu",
    "render_plain",
    "risk_with_icon",
    "strip_markdown",
    "trend_arrow",
]
