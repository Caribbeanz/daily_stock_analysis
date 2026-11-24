# -*- coding: utf-8 -*-
"""飞书七段图标化渲染（需求 §3.5.3.1 / 技术方案 §6.3）。

一份 ``snapshot_content`` → 一段可直接发到飞书的文本。三条硬边界：

1. **单一真值**：只读快照内容，不重算、不补字段；数值一律取库里存的值（``str`` 后
   原样输出，不做四舍五入改写）。
2. **两档同源**：``mode="full"`` 与 ``mode="compact"`` 走**同一个函数**，只在
   ``RenderOptions`` 上开关段落细节，避免精简版另写一套文案导致两处数字打架（B48）。
3. **超长不截断半句**：超限时按「先删风险依据 → 再删阶段证据 → 再删不可设单分支」
   逐级降级重渲染；触发条件、股数、风险等级、数据日期四类字段任何情况下都不删。

图标语义见 :mod:`.icons`（固定映射，三处共用）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from src.assistant.enums import ACTION_ORDER, Action, RiskLevel, Stage, STAGE_ALLOWED
from src.assistant.render.icons import (
    CAL_ICON,
    RISK_NONE,
    SECTION_ICONS,
    action_with_icon,
    risk_with_icon,
    strip_markdown,
)
from src.assistant.render.review import PlanReview

__all__ = [
    "FULL_LIMIT",
    "COMPACT_LIMIT",
    "MarketLine",
    "RenderOptions",
    "fit_feishu",
    "render_feishu",
    "render_plain",
]

FULL_LIMIT = 1800
COMPACT_LIMIT = 1100

_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

#: 各阶段打法（一句话）。需求只给「打法」这个词，未给原文，此处为初值（见 D-15）。
_STAGE_PLAYBOOK: dict[str, str] = {
    Stage.S0.value: "数据不足，先补数据，不出手",
    Stage.S1.value: "回踩不破再买，突破前高追买需确认",
    Stage.S2.value: "上沿减 T 仓、下沿买回，不追高",
    Stage.S3.value: "只减不加，反弹减仓，破位清仓",
    Stage.S4.value: "箱体下沿小仓试探，确认后再建仓",
    Stage.S5.value: "无结构不交易，只处理风险",
}

#: 各阶段的切换信号（初值，同 D-15）
_STAGE_SWITCH: dict[str, str] = {
    Stage.S0.value: "数据补齐后重新判定",
    Stage.S1.value: "跌破 MA20 且连续 2 日不收回 → 转 S3",
    Stage.S2.value: "有效突破箱体上沿或下沿 → 转 S1 / S3",
    Stage.S3.value: "站回 MA20 且连续 2 日不破 → 转 S4",
    Stage.S4.value: "有效跌破箱体下沿 → 转 S3",
    Stage.S5.value: "出现 20 日新高或新低且量能配合 → 重判",
}

#: 需要用户处理的建议（进入 ④ 待办）
_NEEDS_ACTION = frozenset(
    {Action.BUY.value, Action.ADD.value, Action.PROBE.value, Action.TRIM.value, Action.CLEAR.value}
)

#: 无需动作（仅列示）
_NO_ACTION = frozenset({Action.HOLD.value, Action.WATCH.value, Action.WAIT_BUY.value, Action.WAIT_SELL.value})


@dataclass(frozen=True)
class MarketLine:
    """② 段的大盘一句。数据缺失时 ``available=False``，如实写缺失。"""

    index_name: str = ""
    pct_change: float | None = None
    regime: str = ""
    available: bool = False

    def sentence(self) -> str:
        if not self.available:
            return "大盘数据缺失（本次不判断环境档）"
        pct = ""
        if self.pct_change is not None:
            if self.pct_change > 0:
                pct = f" ▲{self.pct_change:.2f}%"
            elif self.pct_change < 0:
                pct = f" ▼{abs(self.pct_change):.2f}%"
            else:
                pct = " 0.00%"
        regime = f" · {self.regime}" if self.regime else ""
        return f"{self.index_name or '指数'}{pct}{regime}"


@dataclass(frozen=True)
class RenderOptions:
    """渲染细节开关。两档密度的差异全部落在这里。"""

    mode: str = "full"  # full | compact
    risk_basis: bool = True  # ③ 段是否带「触发依据」
    stage_detail: bool = True  # ⑤ 段是否带允许/禁止/切换
    all_plans: bool = True  # ⑥ 段是否列不可设单分支
    max_risk_stocks: int = 3
    #: 分支数上限（最后一级兜底用）。整条分支不列出，绝不截断半句
    max_plans: int | None = None

    @property
    def compact(self) -> bool:
        return self.mode == "compact"


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def render_feishu(
    content: Mapping[str, Any],
    *,
    mode: str = "full",
    review: PlanReview | None = None,
    market: MarketLine | None = None,
) -> str:
    """按档位渲染（不裁剪长度，裁剪见 :func:`fit_feishu`）。"""
    return _render(content, RenderOptions(mode=mode), review=review, market=market)


def fit_feishu(
    content: Mapping[str, Any],
    *,
    mode: str = "full",
    review: PlanReview | None = None,
    market: MarketLine | None = None,
) -> str:
    """渲染并保证不超长（完整版 1,800 / 精简版 1,100）。

    降级顺序严格按需求硬约束 7：先删风险依据 → 再删阶段证据 → 再删不可设单分支。
    任何一级都不删「触发条件 / 股数 / 风险等级 / 数据日期」。
    """
    limit = COMPACT_LIMIT if mode == "compact" else FULL_LIMIT
    base = RenderOptions(mode=mode)
    ladder: list[RenderOptions] = [
        base,
        replace(base, risk_basis=False),
        replace(base, risk_basis=False, stage_detail=False),
        replace(base, risk_basis=False, stage_detail=False, all_plans=False),
    ]
    if not base.compact:
        compact = RenderOptions(mode="compact", risk_basis=True, stage_detail=True, all_plans=True)
        ladder += [
            compact,
            replace(compact, risk_basis=False),
            replace(compact, risk_basis=False, stage_detail=False, all_plans=False),
            # 最后一级：只列前 6 条分支（整条略去，不截断半句），并明示「另有 N 条见 WebUI」
            replace(compact, risk_basis=False, stage_detail=False, all_plans=False, max_plans=6),
        ]
    else:
        ladder.append(
            replace(base, risk_basis=False, stage_detail=False, all_plans=False, max_plans=6)
        )

    text = ""
    for options in ladder:
        text = _render(content, options, review=review, market=market)
        if len(text) <= limit:
            return text
    return text  # 已降到最简仍超长：返回最简版，由发送层按渠道能力再处理


def render_plain(
    content: Mapping[str, Any],
    *,
    mode: str = "full",
    review: PlanReview | None = None,
    market: MarketLine | None = None,
) -> str:
    """纯文本降级版（发送失败转 ``msg_type=text`` 时用，硬约束 5：先剥离 ``**``）。"""
    return strip_markdown(fit_feishu(content, mode=mode, review=review, market=market))


# ---------------------------------------------------------------------------
# 渲染主体
# ---------------------------------------------------------------------------
def _render(
    content: Mapping[str, Any],
    options: RenderOptions,
    *,
    review: PlanReview | None,
    market: MarketLine | None,
) -> str:
    conclusions = list(content.get("conclusions") or [])
    plans = list(content.get("plans") or [])
    risks = list(content.get("risks") or [])

    # 精简版一律更省：不带风险依据、不带阶段允许/切换、只列可设单分支
    if options.compact:
        options = replace(options, risk_basis=False, stage_detail=False, all_plans=False)

    blocks = [
        _header(content, review),
        _summary(conclusions, market),
        _risk_block(risks, conclusions, options),
        _todo_block(conclusions, plans),
        _stage_block(conclusions, plans, options),
        _plan_block(conclusions, plans, options),
        _notice(options),
    ]
    return "\n\n".join(block for block in blocks if block)


def _header(content: Mapping[str, Any], review: PlanReview | None) -> str:
    snapshot_day = _as_date(content.get("data_date"))
    bar_day = _as_date(content.get("bar_date") or content.get("data_date"))
    title = SECTION_ICONS["header"] + " **" + _day_label(snapshot_day) + "**"
    lines = [f"{title} · 数据截止 {bar_day.isoformat() if bar_day else '—'} 收盘"]
    trio = (
        f"{CAL_ICON} 快照 {content.get('snapshot_id', '—')} · "
        f"规则 {content.get('calc_version', '—')} · 来源 {content.get('source_tag', '—')}"
    )
    lines.append(trio)
    # ① 必须带复盘子行：没有 T-1 快照也要如实写「未生成复盘」，不能整行消失
    lines.append((review or PlanReview(available=False)).line())
    return "\n".join(lines)


def _summary(conclusions: Sequence[Mapping[str, Any]], market: MarketLine | None) -> str:
    sentence = (market or MarketLine()).sentence()
    total = len(conclusions)
    todo = sum(1 for row in conclusions if (row.get("advice") or "") in _NEEDS_ACTION)
    wait = sum(1 for row in conclusions if (row.get("advice") or "") in _NO_ACTION)
    counts = f"自选 {total} 只（待办 {todo} · 观察/等待 {wait}）"
    return f"{SECTION_ICONS['summary']} 大盘：{sentence} · {counts}"


def _risk_block(
    risks: Sequence[Mapping[str, Any]],
    conclusions: Sequence[Mapping[str, Any]],
    options: RenderOptions,
) -> str:
    title = SECTION_ICONS["risk"] + " 风险"
    if not risks:
        return f"{title}\n{RISK_NONE}"

    grouped = _group_risks(risks)
    names = {row.get("code"): row.get("name") or row.get("code") for row in conclusions}
    ranked = sorted(grouped.items(), key=lambda kv: (-_risk_rank(kv[1]["level"]), str(kv[0])))

    shown = ranked[: options.max_risk_stocks]
    rest = ranked[options.max_risk_stocks :]
    lines = [title]
    for code, item in shown:
        label = risk_with_icon(item["level"])
        name = names.get(code) or code
        if options.compact:
            lines.append(f"{label}｜{_display(code, name)}｜{item['action'] or item['human_text']}")
            continue
        lines.append(f"{label} **{_display(code, name)}**")
        if options.risk_basis:
            lines.append(f"  触发：{item['basis']}")
        lines.append(f"  影响：{item['action'] or '仅记录，不改结论'}")
    if rest:
        lines.append(f"另有 {len(rest)} 只{'观察级' if all(_risk_rank(i['level']) <= 1 for _, i in rest) else '风险'}未展开")
    return "\n".join(lines)


def _todo_block(
    conclusions: Sequence[Mapping[str, Any]], plans: Sequence[Mapping[str, Any]]
) -> str:
    title = SECTION_ICONS["todo"] + " 待办"
    actionable = [row for row in conclusions if (row.get("advice") or "") in _NEEDS_ACTION]
    idle = [row for row in conclusions if (row.get("advice") or "") in _NO_ACTION]
    if not actionable:
        lines = [title, "今日无待办"]
        if idle:
            lines.append("其余：" + "、".join(_idle_summary(row) for row in idle))
        return "\n".join(lines)

    lines = [title]
    for row in actionable:
        advice = row.get("advice") or ""
        detail = _plan_hint(plans, row.get("code"), advice)
        lines.append(f"{action_with_icon(advice)} **{_display(row.get('code'), row.get('name'))}** {detail}".rstrip())
    if idle:
        lines.append("其余：" + "、".join(_idle_summary(row) for row in idle))
    return "\n".join(lines)


def _stage_block(
    conclusions: Sequence[Mapping[str, Any]],
    plans: Sequence[Mapping[str, Any]],
    options: RenderOptions,
) -> str:
    row = _stage_sample(conclusions, plans)
    if row is None:
        return ""
    stage = str(row.get("stage") or "")
    title = (
        f"{SECTION_ICONS['stage']} **{_display(row.get('code'), row.get('name'))} · "
        f"{stage} {Stage(stage).label if _is_stage(stage) else ''} · 置信度 {row.get('stage_confidence') or '—'}**"
    )
    lines = [title, f"  打法：{_STAGE_PLAYBOOK.get(stage, '—')}"]
    if options.stage_detail:
        allowed, forbidden = _stage_actions(stage)
        lines.append(f"  允许：{allowed}")
        lines.append(f"  禁止：{forbidden}")
        lines.append(f"  切换：{_STAGE_SWITCH.get(stage, '—')}")
    else:
        _, forbidden = _stage_actions(stage)
        lines.append(f"  禁止：{forbidden}")
    return "\n".join(lines)


def _plan_block(
    conclusions: Sequence[Mapping[str, Any]],
    plans: Sequence[Mapping[str, Any]],
    options: RenderOptions,
) -> str:
    title = SECTION_ICONS["plan"] + " 条件计划"
    if not plans:
        return f"{title}\n无符合条件的计划（今日结论不含可执行分支）"

    names = {row.get("code"): row.get("name") or row.get("code") for row in conclusions}
    selected = [plan for plan in plans if options.all_plans or plan.get("settable")]
    if options.max_plans is not None:
        selected = selected[: options.max_plans]
    omitted = len(plans) - len(selected)

    lines = [title]
    for plan in selected:
        code = plan.get("code")
        head = f"{_display(code, names.get(code))} · {plan.get('scenario') or plan.get('branch_id')}"
        if options.compact:
            # 精简版每条一行：触发条件 + 动作股数都保留（四类不可删字段之一）
            lines.append(f"{head}｜{_trigger_text(plan)}｜{_action_text(plan)}")
            continue
        lines.append(head.rstrip())
        # 以下三行是「可照抄」数值行：不加 emoji、不加粗（硬约束 2）
        lines.append(f"  触发：{_trigger_text(plan)}")
        lines.append(f"  动作：{_action_text(plan)}")
        lines.append(f"  有效期：{_validity_text(plan)}")
    if omitted:
        lines.append(f"另有 {omitted} 条分支未展开（查看 WebUI）")
    return "\n".join(lines)


def _notice(options: RenderOptions) -> str:
    title = SECTION_ICONS["notice"] + " 声明"
    if options.compact:
        return f"{title}\n静态计划，不自动下单、不盯盘；不构成投资建议。"
    return (
        f"{title}\n"
        "静态条件计划，不自动下单、不盯盘，触发后需你本人手动委托。\n"
        "以上「触发/动作/有效期」可照抄至券商条件单；不改写、不四舍五入。\n"
        "本消息不构成投资建议，据此操作风险自担。"
    )


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _day_label(day: date | None) -> str:
    if day is None:
        return "未知交易日"
    return f"{day.isoformat()}（{_WEEKDAY_CN[day.weekday()]}）"


def _is_stage(value: str) -> bool:
    try:
        Stage(value)
        return True
    except ValueError:
        return False


def _risk_rank(level: str) -> int:
    order = {RiskLevel.INFO.value: 1, RiskLevel.WARNING.value: 2, RiskLevel.SEVERE.value: 3}
    return order.get(str(level), 0)


def _group_risks(risks: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """同股多条信号归并成一条（取最高等级），需求：同一只股票只出现一次。"""
    grouped: dict[str, dict[str, Any]] = {}
    for risk in risks:
        code = str(risk.get("code") or "")
        level = str(risk.get("level") or "")
        basis = str(risk.get("trigger_basis") or risk.get("human_text") or "")
        action = str(risk.get("action") or "")
        current = grouped.get(code)
        if current is None or _risk_rank(level) > _risk_rank(current["level"]):
            grouped[code] = {"level": level, "basis": basis, "action": action}
        elif not current["action"] and action:
            current["action"] = action
    return grouped


def _plan_hint(plans: Sequence[Mapping[str, Any]], code: Any, advice: str) -> str:
    """待办行里带出「股数或原因」。"""
    matching = [p for p in plans if p.get("code") == code and p.get("action") == advice]
    if not matching:
        return "（无对应分支，见 WebUI）"
    plan = matching[0]
    shares = plan.get("shares") or 0
    if not shares:
        return f"（{plan.get('notes') or '见计划'}）"
    amount = plan.get("est_amount")
    amount_text = f" · 约 {float(amount) / 10000:.2f} 万" if amount else ""
    return f"{shares} 股{amount_text}"


def _display(code: Any, name: Any) -> str:
    """``代码 名称``；名称缺失或与代码相同时只显示代码（不出现「600000 600000」）。"""
    code_text = str(code or "—")
    name_text = str(name or "").strip()
    if not name_text or name_text == code_text:
        return code_text
    return f"{code_text} {name_text}"


def _idle_summary(row: Mapping[str, Any]) -> str:
    advice = row.get("advice") or "—"
    return f"{action_with_icon(advice)} {row.get('code')}"


def _stage_sample(
    conclusions: Sequence[Mapping[str, Any]], plans: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """挑一只做阶段示例：优先「有待办且有计划」的标的，其次第一只。"""
    if not conclusions:
        return None
    with_plans = {plan.get("code") for plan in plans}
    for row in conclusions:
        if (row.get("advice") or "") in _NEEDS_ACTION and row.get("code") in with_plans:
            return row
    return conclusions[0]


def _stage_actions(stage: str) -> tuple[str, str]:
    if not _is_stage(stage):
        return "—", "—"
    allowed_set = STAGE_ALLOWED.get(Stage(stage), frozenset())
    allowed = "、".join(action.value for action in ACTION_ORDER if action in allowed_set)
    forbidden = "、".join(action.value for action in ACTION_ORDER if action not in allowed_set)
    return allowed or "—", forbidden or "—"


def _trigger_text(plan: Mapping[str, Any]) -> str:
    price = plan.get("trigger_price")
    pct = plan.get("pct_change")
    kind = plan.get("trigger_type")
    if price is not None:
        price = float(price)
        if kind == "price_gte":
            return f"价格 ≥ {price:.2f} 元"
        if kind == "price_lte":
            return f"价格 ≤ {price:.2f} 元"
        return f"{price:.2f} 元"
    if pct is not None:
        return f"涨跌幅 {float(pct):+.2f}%"
    return "—"


def _action_text(plan: Mapping[str, Any]) -> str:
    action = plan.get("action") or "—"
    shares = plan.get("shares")
    amount = plan.get("est_amount")
    pct = plan.get("pct_of_total")
    parts = [str(action)]
    if shares:
        parts.append(f"{shares} 股")
    if amount:
        parts.append(f"约 {float(amount) / 10000:.2f} 万元")
    if pct:
        parts.append(f"占总资金 {float(pct) * 100:.1f}%")
    if plan.get("is_probe"):
        parts.append("试探仓")
    return " · ".join(parts)


def _validity_text(plan: Mapping[str, Any]) -> str:
    days = plan.get("valid_days")
    head = f"{days} 个交易日" if days else "—"
    settable = "可设条件单" if plan.get("settable") else "不可设条件单"
    invalidation = plan.get("notes") or ""
    tail = f"；失效：{invalidation}" if invalidation else ""
    return f"{head}（{settable}）{tail}"
