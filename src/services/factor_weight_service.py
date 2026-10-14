# -*- coding: utf-8 -*-
"""高抛低吸助手 · 四因子权重合成（FR-7）

把「四维度独立结论」按**可调权重**合成为综合倾向，用于：

1. H5 详情页展示（FR-7.3 展开明细 / FR-7.4 可解释）；
2. ``/actions/preview`` 预览"改权重后结论如何变化"（FR-14.2.4）。

设计原则：

- **纯函数**：不读库、不触网，输入输出皆 dict，便于单测与预览复用；
- **诚实降级**：某维度无数据时标记为"无数据"并**从权重分母中剔除**（其余维度
  按比例重新归一），绝不把无依据的维度算成"中性"来凑权重（FR-7.4 可解释）；
- **不臆造结论**：若全部维度都无数据，只回"权重已更新"，不产出综合倾向。
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from src.core.rule_config import FACTOR_KEYS, FACTOR_LABELS

__all__ = [
    "DIMENSION_KEYS",
    "derive_dimension_scores",
    "synthesize",
    "render_synthesis",
]

DIMENSION_KEYS: Sequence[str] = FACTOR_KEYS  # ("technical", "market", "news", "fundamental")

_POSITIVE = "偏多"
_NEGATIVE = "偏空"
_NEUTRAL = "中性"
_NO_DATA = "无数据"

# 事件里的风险关键词（命中即视为消息面偏空；仅做方向判断，不做强度叠加）
_RISK_KEYWORDS = ("风险", "预警", "下修", "亏损", "监管", "处罚", "退市", "违约", "减持", "立案")


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _label_from_score(score: float) -> str:
    if score >= 0.25:
        return _POSITIVE
    if score <= -0.25:
        return _NEGATIVE
    return _NEUTRAL


def derive_dimension_scores(
    signal: Optional[Mapping[str, Any]],
    events: Optional[Iterable[str]] = None,
) -> Dict[str, Optional[float]]:
    """从**已落库的信号 + 事件**推导四维度方向分（-1～+1）；无依据返回 ``None``。

    当前可得依据（不新增数据源、不重算指标）：

    - ``technical``：日线趋势（up/down/sideways）+ 综合评分辅助修正；
    - ``news``：事件与风险文案中是否出现风险关键词（方向性判断）；
    - ``market`` / ``fundamental``：**当前信号未承载可用依据 → None（无数据）**。

    Args:
        signal: ``decision_signals`` 记录（dict）。
        events: 事件与风险文案列表。
    """
    result: Dict[str, Optional[float]] = {key: None for key in DIMENSION_KEYS}
    if not isinstance(signal, Mapping):
        signal = {}

    metadata = signal.get("metadata") if isinstance(signal.get("metadata"), Mapping) else {}
    trend = str(metadata.get("trend") or signal.get("trend") or "").strip().lower()

    technical: Optional[float] = None
    if trend in {"up", "down", "sideways"}:
        technical = {"up": 0.6, "down": -0.6}.get(trend, 0.0)
        score = _finite(signal.get("score"))
        if score is not None:
            # 综合评分 0~100 → -0.3~+0.3 的修正量，方向与趋势一致时加强
            technical += (score - 50.0) / 50.0 * 0.3
            technical = max(-1.0, min(1.0, technical))
    result["technical"] = technical

    event_list = [str(e) for e in (events or []) if str(e).strip()]
    if event_list:
        hit = sum(1 for text in event_list if any(kw in text for kw in _RISK_KEYWORDS))
        # 有事件但无风险关键词 → 轻微偏多；有风险关键词 → 偏空（按命中占比）
        result["news"] = round(1.0 - 2.0 * (hit / len(event_list)), 4)

    return result


def synthesize(
    scores: Mapping[str, Optional[float]],
    weights: Mapping[str, float],
) -> Dict[str, Any]:
    """按权重合成综合倾向。

    Args:
        scores: 各维度方向分（``None`` = 无数据）。
        weights: 各维度权重（合计 1.0）。

    Returns:
        ``{"score": float|None, "label": str, "available": [...],
           "contributions": [{key,label,weight,score,label_tone,contribution,available}...],
           "note": str}``
    """
    contributions: List[Dict[str, Any]] = []
    available_weight = 0.0
    weighted_sum = 0.0

    for key in DIMENSION_KEYS:
        weight = float(weights.get(key, 0.0) or 0.0)
        score = _finite(scores.get(key))
        available = score is not None
        if available:
            available_weight += weight
            weighted_sum += weight * float(score)
        contributions.append(
            {
                "key": key,
                "label": FACTOR_LABELS.get(key, key),
                "weight": round(weight, 4),
                "score": None if score is None else round(float(score), 4),
                "tone": _label_from_score(float(score)) if available else _NO_DATA,
                "contribution": None if score is None else round(weight * float(score), 4),
                "available": available,
            }
        )

    if available_weight <= 0:
        return {
            "score": None,
            "label": _NO_DATA,
            "available": [],
            "contributions": contributions,
            "note": "各维度均缺少可用打分依据，仅更新权重，暂不产出综合倾向。",
        }

    final = max(-1.0, min(1.0, weighted_sum / available_weight))
    return {
        "score": round(final, 4),
        "label": _label_from_score(final),
        "available": [item["key"] for item in contributions if item["available"]],
        "contributions": contributions,
        "note": "综合倾向仅由**有数据维度**按权重归一合成；无数据维度不计权（FR-7.4）。",
    }


def render_synthesis(synthesis: Mapping[str, Any]) -> str:
    """把合成结果渲染成一句人话（供推送 / 对话回执）。"""
    label = str(synthesis.get("label") or _NO_DATA)
    if synthesis.get("score") is None:
        return f"综合倾向：{_NO_DATA}（{synthesis.get('note', '')}）"
    parts = []
    for item in synthesis.get("contributions") or []:
        if item.get("available"):
            parts.append(f"{item['label']} {item['tone']}（权重 {float(item['weight']) * 100:.0f}%）")
    detail = "、".join(parts)
    return f"综合倾向：{label}｜{detail}" if detail else f"综合倾向：{label}"
