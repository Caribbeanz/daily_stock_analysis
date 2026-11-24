# -*- coding: utf-8 -*-
"""T 日复盘：拿 T-1 的条件计划对照 T 日真实行情（需求 §3.5.3.1 ① 子行）。

**只读回看，不构成新建议**：这里只回答「昨天的计划今天有没有被碰到」，不产生
`advice`、不改任何快照、不判定「该不该执行」——是否真的成交由用户自己核对。

判定口径（只用价格触发，因为条件计划本身只给价格/涨跌幅触发）：

- ``price_gte``：T 日最高价 ≥ 触发价 → 触发
- ``price_lte``：T 日最低价 ≤ 触发价 → 触发
- 其余（涨跌幅类、缺 `trigger_price`）→ **无法判定**，计入 ``undecidable``，不猜

跳空：触发价在 T 日开盘时已被越过（`price_gte` 且 open ≥ 触发价 / `price_lte` 且
open ≤ 触发价）单独标出来——这类分支实际很难按计划价成交，只写「触发」会误导。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from src.assistant.domain import Bar

__all__ = ["PlanReview", "build_plan_review"]


@dataclass(frozen=True)
class PlanReview:
    """T-1 计划的回看结果。"""

    available: bool
    prev_snapshot_id: str = ""
    prev_data_date: str = ""
    triggered: tuple[str, ...] = ()
    gap_triggered: tuple[str, ...] = ()
    untriggered: int = 0
    undecidable: int = 0
    missing_codes: tuple[str, ...] = ()

    def line(self, *, max_items: int = 2) -> str:
        """① 段的复盘子行（单行，硬约束 3：不超过三层）。"""
        if not self.available:
            return "ℹ️ 当日复盘：无可用 T-1 快照，未生成复盘（仅回看，不构成新建议）"

        parts: list[str] = []
        if self.triggered:
            shown = "、".join(self.triggered[:max_items])
            more = f" 等 {len(self.triggered)} 条" if len(self.triggered) > max_items else ""
            parts.append(f"触发 {len(self.triggered)} 条（{shown}{more}）")
        else:
            parts.append("无计划被触发")
        if self.gap_triggered:
            parts.append("跳空触发 " + "、".join(self.gap_triggered[:max_items]))
        if self.untriggered:
            parts.append(f"未触发 {self.untriggered} 条")
        if self.undecidable:
            parts.append(f"无法判定 {self.undecidable} 条")
        if self.missing_codes:
            parts.append("缺 T 日行情：" + "、".join(self.missing_codes))

        detail = " · ".join(parts)
        return (
            f"ℹ️ 当日复盘：{detail}（对照快照 {self.prev_snapshot_id}，"
            "仅回看，不构成新建议）"
        )


def build_plan_review(
    prev_content: Mapping[str, Any] | None,
    bars_by_code: Mapping[str, Sequence[Bar]],
) -> PlanReview:
    """用 T 日 K 线判定 T-1 快照里各条件计划是否被触发。"""
    if not prev_content:
        return PlanReview(available=False)

    plans = prev_content.get("plans") or []
    if not plans:
        return PlanReview(
            available=True,
            prev_snapshot_id=str(prev_content.get("snapshot_id", "")),
            prev_data_date=str(prev_content.get("data_date", "")),
        )

    triggered: list[str] = []
    gap: list[str] = []
    untriggered = 0
    undecidable = 0
    missing: list[str] = []

    for plan in plans:
        code = str(plan.get("code") or "")
        bars = list(bars_by_code.get(code) or [])
        bar = bars[-1] if bars else None
        if bar is None:
            if code and code not in missing:
                missing.append(code)
            undecidable += 1
            continue

        price = plan.get("trigger_price")
        trigger_type = str(plan.get("trigger_type") or "")
        if price is None:
            undecidable += 1
            continue

        hit = False
        gapped = False
        if trigger_type == "price_gte":
            hit = bar.high >= float(price)
            gapped = hit and bar.open >= float(price)
        elif trigger_type == "price_lte":
            hit = bar.low <= float(price)
            gapped = hit and bar.open <= float(price)
        else:
            undecidable += 1
            continue

        if not hit:
            untriggered += 1
            continue
        label = f"{code} {plan.get('scenario') or plan.get('branch_id')}→{plan.get('action')}"
        if gapped:
            gap.append(label)
        else:
            triggered.append(label)

    return PlanReview(
        available=True,
        prev_snapshot_id=str(prev_content.get("snapshot_id", "")),
        prev_data_date=str(prev_content.get("data_date", "")),
        triggered=tuple(triggered),
        gap_triggered=tuple(gap),
        untriggered=untriggered,
        undecidable=undecidable,
        missing_codes=tuple(missing),
    )
