# -*- coding: utf-8 -*-
"""阶段展示文案的唯一出处（飞书 ⑤ 段与 WebUI 详情页第 3 区块共用）。

为什么单独一个模块：同一只标的的阶段码、打法、允许/禁止动作、切换信号，飞书和
网页都要显示。若各写一份，改一处忘另一处就会出现「推送说只能观望、页面说可以加仓」
——这类口径分叉比起排版问题严重得多（DP-3 单一真值）。

本模块只做**查表**，不做判定：阶段码由引擎产出，这里只把它翻译成中文短句。
"""

from __future__ import annotations

from src.assistant.enums import ACTION_ORDER, STAGE_ALLOWED, Stage

__all__ = [
    "STAGE_PLAYBOOK",
    "STAGE_SWITCH",
    "action_lists",
    "stage_detail",
]

#: 各阶段打法（一句话）。需求只给「打法」这个词，未给原文，此处为初值（见 D-15）。
STAGE_PLAYBOOK: dict[str, str] = {
    Stage.S0.value: "数据不足，先补数据，不出手",
    Stage.S1.value: "回踩不破再买，突破前高追买需确认",
    Stage.S2.value: "上沿减 T 仓、下沿买回，不追高",
    Stage.S3.value: "只减不加，反弹减仓，破位清仓",
    Stage.S4.value: "箱体下沿小仓试探，确认后再建仓",
    Stage.S5.value: "无结构不交易，只处理风险",
}

#: 各阶段的切换信号（初值，同 D-15）
STAGE_SWITCH: dict[str, str] = {
    Stage.S0.value: "数据补齐后重新判定",
    Stage.S1.value: "跌破 MA20 且连续 2 日不收回 → 转 S3",
    Stage.S2.value: "有效突破箱体上沿或下沿 → 转 S1 / S3",
    Stage.S3.value: "站回 MA20 且连续 2 日不破 → 转 S4",
    Stage.S4.value: "有效跌破箱体下沿 → 转 S3",
    Stage.S5.value: "出现 20 日新高或新低且量能配合 → 重判",
}


def action_lists(stage: str) -> tuple[list[str], list[str]]:
    """返回 ``(允许, 禁止)`` 两个动作名列表；未知阶段返回空列表而非猜一个。"""
    try:
        parsed = Stage(str(stage))
    except ValueError:
        return [], []
    allowed_set = STAGE_ALLOWED.get(parsed, frozenset())
    allowed = [action.value for action in ACTION_ORDER if action in allowed_set]
    forbidden = [action.value for action in ACTION_ORDER if action not in allowed_set]
    return allowed, forbidden


def stage_detail(stage: str, confidence: object = None) -> dict:
    """详情页第 3 区块的结构化字段（API 原样返回，前端不再拼文案）。"""
    allowed, forbidden = action_lists(stage)
    label = ""
    try:
        label = Stage(str(stage)).label
    except ValueError:
        label = ""
    return {
        "code": str(stage or ""),
        "name": label,
        "confidence": confidence,
        "playbook": STAGE_PLAYBOOK.get(str(stage), ""),
        "switch": STAGE_SWITCH.get(str(stage), ""),
        "allowed": allowed,
        "forbidden": forbidden,
    }
