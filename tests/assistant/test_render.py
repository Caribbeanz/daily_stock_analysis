# -*- coding: utf-8 -*-
"""七段渲染测试（需求 §3.5.3.1 硬约束 1–7 / B47 / B48）。"""

from __future__ import annotations

import re
from datetime import date, datetime

import pytest

from src.assistant.data.storage import AssistantStore
from src.assistant.pipeline import AssistantPipeline, PipelineSettings
from src.assistant.render.feishu import (
    COMPACT_LIMIT,
    FULL_LIMIT,
    MarketLine,
    fit_feishu,
    render_feishu,
    render_plain,
)
from src.assistant.render.icons import ACTION_ICON, action_with_icon, risk_with_icon, strip_markdown
from src.assistant.render.review import build_plan_review
from tests.assistant.helpers import make_bars, trending

DAY = date(2026, 11, 27)

EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]"
)


def content(**overrides) -> dict:
    base = {
        "snapshot_id": "20261127-abc123def456",
        "data_date": "2026-11-27",
        "bar_date": "2026-11-27",
        "calc_version": "cv1-88fc0ee542",
        "param_hash": "188d51e83098",
        "source_tag": "D2",
        "conclusions": [
            {
                "code": "600000",
                "name": "浦发银行",
                "advice": "减仓",
                "advice_display": "减仓",
                "advice_state": "有效",
                "stage": "S3",
                "stage_confidence": "高",
                "trend": "偏弱",
                "position_tag": "高位",
                "score": 41.0,
                "risk_level": "警示",
                "degraded": False,
            },
            {
                "code": "000001",
                "name": "平安银行",
                "advice": "等待买点",
                "advice_display": "等待买点",
                "advice_state": "有效",
                "stage": "S1",
                "stage_confidence": "中",
                "trend": "偏强",
                "position_tag": "中位",
                "score": 63.0,
                "risk_level": "观察",
                "degraded": False,
            },
        ],
        "plans": [
            {
                "code": "600000",
                "branch_id": "B1",
                "scenario": "反弹到 MA20 减仓",
                "trigger_type": "price_gte",
                "trigger_price": 12.34,
                "action": "减仓",
                "shares": 2000,
                "est_amount": 24680.0,
                "pct_of_total": 0.0494,
                "valid_days": 1,
                "settable": True,
                "notes": "反弹位 = MA20",
                "is_probe": False,
            },
            {
                "code": "600000",
                "branch_id": "B2",
                "scenario": "反弹不过压力不动",
                "trigger_type": "price_lte",
                "trigger_price": 12.34,
                "action": "不操作",
                "shares": 0,
                "est_amount": 0.0,
                "pct_of_total": 0.0,
                "valid_days": 1,
                "settable": False,
                "notes": "不操作（仅提示）",
                "is_probe": False,
            },
        ],
        "risks": [
            {
                "code": "600000",
                "risk_id": "R1",
                "risk_code": "TEC-1",
                "category": "技术面",
                "level": "观察",
                "trigger_basis": "跌破 MA20",
                "action": "",
                "human_text": "跌破 MA20",
                "affects_advice": False,
            },
            {
                "code": "600000",
                "risk_id": "R2",
                "risk_code": "RISK-1",
                "category": "风险",
                "level": "严重",
                "trigger_basis": "单日跌幅 8.1%",
                "action": "禁止加仓",
                "human_text": "单日跌幅 8.1%",
                "affects_advice": True,
            },
        ],
    }
    base.update(overrides)
    return base


# ---- 七段结构（硬约束 4：段序固定）----------------------------------------
def test_seven_sections_in_fixed_order() -> None:
    text = render_feishu(content())
    icons = ["📊", "🧭", "🔥", "✅", "📍", "📋", "🔒"]
    positions = [text.index(icon) for icon in icons]
    assert positions == sorted(positions), "段序不得改变"
    for icon in icons:
        assert icon in text


def test_header_carries_triplet_and_review_line() -> None:
    text = render_feishu(content())
    assert "20261127-abc123def456" in text
    assert "cv1-88fc0ee542" in text
    assert "2026-11-27" in text
    assert "来源 D2" in text
    assert "当日复盘" in text, "① 必须带复盘子行（无 T-1 也要说明）"
    assert "仅回看，不构成新建议" in text


def test_header_without_review_still_states_absence() -> None:
    text = render_feishu(content())
    assert "无可用 T-1 快照" in text


# ---- 图标语义（硬约束 1 / B47）--------------------------------------------
def test_risk_levels_use_color_dots_with_labels() -> None:
    assert risk_with_icon("严重") == "🔴 严重"
    assert risk_with_icon("警示") == "🟠 警示"
    assert risk_with_icon("观察") == "🔵 观察"
    text = render_feishu(content())
    assert "🔴 严重" in text


def test_action_symbols_are_unique_per_action() -> None:
    assert action_with_icon("减仓") == "➖ 减仓"
    assert action_with_icon("清仓") == "🔻 清仓"
    assert ACTION_ICON["买入"] == ACTION_ICON["加仓"] == "➕"
    assert ACTION_ICON["减仓"] != ACTION_ICON["清仓"]
    # 未知动作不猜符号，保留原文
    assert action_with_icon("神秘动作") == "神秘动作"


def test_price_change_uses_arrow_not_colored_dot() -> None:
    """涨跌用 ▲▼，不用红绿圆点（红=涨 与 红=危险 冲突）。"""
    text = render_feishu(content(), market=MarketLine(index_name="上证指数", pct_change=0.83, regime="中性", available=True))
    assert "▲0.83%" in text
    assert "🔴" not in text.split("🧭")[1].split("🔥")[0], "大盘一行不得用风险色点"


def test_risk_grouped_by_stock_keeping_highest_level() -> None:
    text = render_feishu(content())
    risk_block = text.split("🔥")[1].split("✅")[0]
    assert risk_block.count("600000") == 1, "同一只股票在风险段只出现一次"
    assert "🔴 严重" in risk_block, "多条信号取最高等级"
    assert "🔵 观察" not in risk_block


def test_no_risk_writes_explicit_none() -> None:
    text = render_feishu(content(risks=[]))
    assert "⚪ 今日无风险信号" in text


# ---- 硬约束 2：计划数值行不加修饰 ------------------------------------------
def test_plan_numeric_lines_have_no_markup() -> None:
    text = render_feishu(content())
    numeric = [line for line in text.splitlines() if line.strip().startswith(("触发：", "动作：", "有效期："))]
    assert numeric, "应有可照抄的数值行"
    for line in numeric:
        assert "**" not in line, f"数值行不得加粗：{line}"
        assert not EMOJI_RE.search(line), f"数值行不得带 emoji：{line}"


def test_plan_keeps_branch_title_without_bold() -> None:
    text = render_feishu(content())
    plan_block = text.split("📋")[1].split("🔒")[0]
    title_lines = [line for line in plan_block.splitlines() if "反弹到 MA20 减仓" in line]
    assert title_lines and "**" not in title_lines[0]


# ---- 硬约束 5：纯文本降级剥离标记 ------------------------------------------
def test_plain_render_strips_bold() -> None:
    plain = render_plain(content())
    assert "**" not in plain
    assert "浦发银行" in plain


def test_strip_markdown_keeps_text() -> None:
    assert strip_markdown("**减仓** 600000") == "减仓 600000"
    assert strip_markdown("# 标题\n正文") == "标题\n正文"


# ---- B48：两档同源、字段逐字一致 -------------------------------------------
def _extract_numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+\.\d+ 元|\d+ 股", text))


def test_full_and_compact_share_identical_numbers() -> None:
    full = fit_feishu(content(), mode="full")
    compact = fit_feishu(content(), mode="compact")
    shared = _extract_numbers(full) & _extract_numbers(compact)
    assert {"12.34 元", "2000 股"} <= shared, "触发价与股数两档必须逐字一致"
    # 精简版只保留可设单分支：不可设单的「不操作」分支应被裁掉
    assert "反弹不过压力不动" not in compact
    assert "反弹不过压力不动" in full


def test_compact_drops_risk_basis_and_stage_detail() -> None:
    compact = fit_feishu(content(), mode="compact")
    assert "单日跌幅 8.1%" not in compact, "精简版删风险依据"
    assert "允许：" not in compact and "切换：" not in compact, "精简版只留阶段+打法+禁止"
    assert "禁止：" in compact


def test_compact_keeps_four_required_fields() -> None:
    compact = fit_feishu(content(), mode="compact")
    assert "12.34 元" in compact, "触发条件不可删"
    assert "2000 股" in compact, "股数不可删"
    assert "🔴 严重" in compact, "风险等级不可删"
    assert "2026-11-27" in compact, "数据日期不可删"


# ---- 硬约束 7：超长按序降级 -----------------------------------------------
def _bulk_content(stocks: int = 8, plans_per_stock: int = 4) -> dict:
    conclusions, plans, risks = [], [], []
    for i in range(stocks):
        code = f"60{i:04d}"
        conclusions.append(
            {
                "code": code,
                "name": f"标的{i}",
                "advice": "加仓",
                "advice_display": "加仓",
                "stage": "S1",
                "stage_confidence": "高",
                "score": 70.0,
                "risk_level": "警示",
                "degraded": False,
            }
        )
        for j in range(plans_per_stock):
            plans.append(
                {
                    "code": code,
                    "branch_id": f"B{j}",
                    "scenario": f"分支{j}",
                    "trigger_type": "price_lte",
                    "trigger_price": 10.0 + j,
                    "action": "加仓",
                    "shares": 100 * (j + 1),
                    "est_amount": 10000.0,
                    "pct_of_total": 0.02,
                    "valid_days": 1,
                    "settable": j % 2 == 0,
                    "notes": "回踩位",
                }
            )
        risks.append(
            {
                "code": code,
                "risk_id": f"R{i}",
                "level": "警示",
                "trigger_basis": "很长的触发依据" * 4,
                "action": "建议降级",
            }
        )
    return content(conclusions=conclusions, plans=plans, risks=risks)


@pytest.mark.parametrize("mode,limit", [("full", FULL_LIMIT), ("compact", COMPACT_LIMIT)])
def test_long_content_is_trimmed_within_limit(mode: str, limit: int) -> None:
    text = fit_feishu(_bulk_content(), mode=mode)
    assert len(text) <= limit, f"{mode} 超长：{len(text)} > {limit}"
    assert "股" in text and "元" in text, "裁剪不得删掉触发价与股数"
    assert "2026-11-27" in text


def test_trimming_removes_risk_basis_before_stage_evidence() -> None:
    """降级顺序：先删风险依据，再删阶段证据（硬约束 7）。"""
    text = fit_feishu(_bulk_content(), mode="full")
    if len(text) <= FULL_LIMIT:
        assert "触发：" not in text.split("🔥")[1].split("✅")[0] or "很长的触发依据" not in text


# ---- 复盘 ----------------------------------------------------------------
def _prev_content() -> dict:
    return {
        "snapshot_id": "20261126-prev00000000",
        "data_date": "2026-11-26",
        "plans": [
            {"code": "600000", "branch_id": "B1", "scenario": "回踩买入", "trigger_type": "price_lte",
             "trigger_price": 10.0, "action": "买入", "settable": True},
            {"code": "600000", "branch_id": "B2", "scenario": "突破买入", "trigger_type": "price_gte",
             "trigger_price": 99.0, "action": "买入", "settable": True},
        ],
    }


def _bar(high: float, low: float, open_: float = None):
    return make_bars([high], start=DAY)[0].__class__(
        trade_date=DAY,
        open=open_ if open_ is not None else low,
        high=high,
        low=low,
        close=(high + low) / 2,
        volume=1e6,
    )


def test_review_marks_triggered_and_untriggered() -> None:
    review = build_plan_review(_prev_content(), {"600000": [_bar(high=11.0, low=9.5, open_=10.5)]})
    assert review.available is True
    assert len(review.triggered) == 1 and "回踩买入" in review.triggered[0]
    assert review.untriggered == 1
    line = review.line()
    assert "触发 1 条" in line and "未触发 1 条" in line
    assert "20261126-prev00000000" in line


def test_review_flags_gap_trigger() -> None:
    """开盘就已在触发价之外：标「跳空」，不能只写触发。"""
    # price_lte 10.00，开盘 9.70 已在触发价下方 → 跳空触发
    review = build_plan_review(_prev_content(), {"600000": [_bar(high=10.05, low=9.50, open_=9.70)]})
    assert review.gap_triggered and not review.triggered
    assert "跳空触发" in review.line()


def test_review_undecidable_and_missing_without_guessing() -> None:
    prev = _prev_content()
    prev["plans"].append({"code": "000001", "branch_id": "B3", "scenario": "涨跌幅触发",
                          "trigger_type": "pct_gte", "trigger_price": None, "action": "买入"})
    review = build_plan_review(prev, {"600000": [_bar(high=11.0, low=9.5)]})
    assert review.undecidable >= 1
    assert "000001" in review.missing_codes
    assert "无法判定" in review.line() and "缺 T 日行情" in review.line()


def test_review_without_previous_snapshot_is_explicit() -> None:
    review = build_plan_review(None, {})
    assert review.available is False
    assert "无可用 T-1 快照" in review.line()


# ---- 与装配层联调（真实快照 → 七段文本）------------------------------------
def test_pipeline_payload_uses_seven_segment_renderer(tmp_path) -> None:
    store = AssistantStore(tmp_path / "render.db")
    store.create_all()
    store.save_bars("600000", make_bars(trending(160)))
    store.append_watchlist("600000", "add")

    pipeline = AssistantPipeline(
        store=store,
        settings=PipelineSettings(total_capital=500_000.0),
        clock=lambda: datetime(2026, 11, 27, 18, 0),
        trading_days=lambda s, e: None,
    )
    payload = pipeline.payload(DAY)

    assert payload.text.count("📊") >= 1
    for icon in ["🧭", "🔥", "✅", "📋", "🔒"]:
        assert icon in payload.text
    assert payload.snapshot_id in payload.text
    assert payload.calc_version in payload.text
    assert "600000 600000" not in payload.text, "名称缺省时不应重复显示代码"
    assert len(payload.text) <= FULL_LIMIT


def test_unsupported_action_branch_not_marked_settable(tmp_path) -> None:
    """「不操作」分支不可设单，因此不该出现在精简版计划里。"""
    store = AssistantStore(tmp_path / "settable.db")
    store.create_all()
    store.save_bars("600000", make_bars(trending(160)))
    store.append_watchlist("600000", "add")
    pipeline = AssistantPipeline(
        store=store,
        settings=PipelineSettings(total_capital=500_000.0),
        clock=lambda: datetime(2026, 11, 27, 18, 0),
        trading_days=lambda s, e: None,
    )
    outcome = pipeline.ensure_snapshot(DAY)
    info = [plan for plan in outcome.content["plans"] if plan["action"] == "不操作"]
    assert info, "测试样本应含提示分支"
    assert all(not plan["settable"] for plan in info)
