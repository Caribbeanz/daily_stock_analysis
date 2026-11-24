# -*- coding: utf-8 -*-
"""风险信号测试（需求 §5.10：7 类、三级联动、不叠加升级、口径边界）。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from src.assistant.domain import (
    DataQuality,
    FundamentalView,
    KeyLevel,
    MarketView,
    PortfolioView,
    SectorView,
    ValuationView,
)
from src.assistant.engine.risk import RiskContext, assess_risks
from src.assistant.enums import RiskCategory, RiskCode, RiskLevel, Stage
from tests.assistant.helpers import make_view, std_params, trending

DATA_DATE = date(2026, 11, 24)


def _quality(sufficient: bool = True, gap: int = 0, flags: tuple[str, ...] = ()) -> DataQuality:
    return DataQuality(sufficient=sufficient, bars_used=150, gap_days=gap, flags=flags)


def _ctx(**kwargs) -> RiskContext:
    view = kwargs.pop("view", None) or make_view(trending(150, 0.005))
    base = RiskContext(
        view=view,
        data_date=DATA_DATE,
        params=std_params(),
        data_quality=kwargs.pop("quality", _quality()),
        stage=kwargs.pop("stage", Stage.S1),
        trend=kwargs.pop("trend", "偏强"),
    )
    return replace(base, **kwargs)


def _codes(summary) -> set[RiskCode]:
    return {sig.code for sig in summary.signals}


def test_break_below_invalid_is_severe_tec2() -> None:
    view = make_view(trending(150, -0.005))
    close = view.last_close
    invalid = KeyLevel(price=close * 1.02, kind="invalid", source="箱体下沿-容差")
    summary = assess_risks(_ctx(view=view, stage=Stage.S3, trend="偏弱", key_levels=[invalid]))
    assert RiskCode.TEC_2 in _codes(summary)
    tec2 = next(s for s in summary.signals if s.code is RiskCode.TEC_2)
    assert tec2.level is RiskLevel.SEVERE
    assert tec2.affects_advice is True
    assert "跌破" in tec2.trigger_basis


def test_market_weak_consecutive_becomes_warning() -> None:
    market = MarketView(regime="偏弱", history_regime=("偏弱", "偏弱", "偏弱"), advance_ratio=0.25)
    summary = assess_risks(_ctx(market=market))
    assert RiskCode.MKT_1 in _codes(summary)
    assert summary.highest.rank <= RiskLevel.WARNING.rank, "大盘风险最高只到警示级（R5）"


def test_market_severe_never_exceeds_warning() -> None:
    """即使大盘极弱 + 宽度恶化，等级也不得升到严重（不得导致全场清仓）。"""
    market = MarketView(regime="极弱", history_regime=tuple(["极弱"] * 10), advance_ratio=0.1)
    summary = assess_risks(_ctx(market=market))
    for sig in summary.signals:
        if sig.category is RiskCategory.MARKET:
            assert sig.level.rank <= RiskLevel.WARNING.rank


def test_market_degraded_is_info_only() -> None:
    market = MarketView(regime="中性", regime_degraded=True, history_regime=("中性",))
    summary = assess_risks(_ctx(market=market))
    mkt3 = next(s for s in summary.signals if s.code is RiskCode.MKT_3)
    assert mkt3.level is RiskLevel.INFO
    assert mkt3.affects_advice is False


def test_fundamental_missing_is_not_bad_news() -> None:
    """财报取数失败只能观察级，不得伪装成「基本面变坏」（需求 §5.10.2）。"""
    summary = assess_risks(_ctx())
    fun3 = next(s for s in summary.signals if s.code is RiskCode.FUN_3)
    assert fun3.level is RiskLevel.INFO
    assert "不等于公司变差" in fun3.human_text


def test_fundamental_audit_abnormal_is_severe() -> None:
    view = make_view(trending(150, 0.005))
    view = replace(view, fundamental=FundamentalView(available=True, audit_opinion_abnormal=True, report_period="2026Q3"))
    summary = assess_risks(_ctx(view=view))
    fun1 = next(s for s in summary.signals if s.code is RiskCode.FUN_1)
    assert fun1.level is RiskLevel.SEVERE
    assert fun1.affects_advice is True


def test_value_trap_is_severe() -> None:
    """估值低 + 趋势下行 → SEC-2 价值陷阱（不得输出「低估可买」）。"""
    view = make_view(trending(150, -0.005))
    view = replace(view, valuation=ValuationView(pe_percentile=0.1, pb_percentile=0.12))
    summary = assess_risks(_ctx(view=view, stage=Stage.S3, trend="偏弱"))
    assert RiskCode.SEC_2 in _codes(summary)
    sec2 = next(s for s in summary.signals if s.code is RiskCode.SEC_2)
    assert sec2.level is RiskLevel.SEVERE
    assert "估值低不等于安全" in sec2.human_text


def test_three_warnings_not_upgraded_to_severe() -> None:
    """3 条警示 ≠ 1 条严重（禁止按数量叠加升级）。"""
    market = MarketView(regime="偏弱", history_regime=("偏弱", "偏弱", "偏弱"), advance_ratio=0.2)
    sector = SectorView(sector_name="银行", strength="偏弱", rs_t=-0.01, history_rs_t=(-0.01, -0.02, -0.03))
    view = make_view(trending(150, 0.005))
    view = replace(view, valuation=ValuationView(pe_percentile=0.9, pb_percentile=0.9), pnl_pct=-0.1, market_value=90_000)
    pf = PortfolioView(total_capital=500_000, cash_available=100_000, holding_market_value=400_000,
                       sector_market_values={"BK0475": 150_000})
    view = replace(view, sector_code="BK0475")
    summary = assess_risks(_ctx(view=view, market=market, sector=sector, portfolio=pf, position_tag="高位"))
    assert summary.highest.rank <= RiskLevel.WARNING.rank


def test_data_gap_is_dq1_warning_with_dq2_merged() -> None:
    """DQ-1（警示）与 DQ-2（观察）同类合并为一条，保留明细。"""
    summary = assess_risks(_ctx(quality=_quality(sufficient=False, gap=6, flags=("gap_gt_3",)), stage=Stage.S0))
    codes = _codes(summary)
    assert RiskCode.DQ_1 in codes
    dq1 = next(s for s in summary.signals if s.code is RiskCode.DQ_1)
    assert dq1.level is RiskLevel.WARNING
    assert "DQ-2" in dq1.human_text


def test_same_category_merged_into_one() -> None:
    """同类合并为一条（保留最高级），不同类全列出。"""
    market = MarketView(regime="偏弱", history_regime=("偏弱", "偏弱", "偏弱"), advance_ratio=0.1)
    summary = assess_risks(_ctx(market=market))
    market_sigs = [s for s in summary.signals if s.category is RiskCategory.MARKET]
    assert len(market_sigs) == 1
    assert "同类合并" in market_sigs[0].human_text


def test_position_over_cap_warns() -> None:
    view = make_view(trending(150, 0.005), holding=1000)
    view = replace(view, market_value=150_000)
    pf = PortfolioView(total_capital=500_000, cash_available=300_000, holding_market_value=150_000)
    summary = assess_risks(_ctx(view=view, portfolio=pf))
    pos1 = next((s for s in summary.signals if s.code is RiskCode.POS_1), None)
    assert pos1 is not None, "单只 30% 应触发 POS-1"
    assert pos1.affects_advice is True


def test_forbidden_speculation_word_rejected() -> None:
    """触发依据不得含推测词（需求 §5.10.3 硬约束 3）。"""
    from src.assistant.engine.risk import _sig

    with pytest.raises(ValueError):
        _sig(
            _ctx(),
            RiskCode.TEC_1,
            RiskCategory.TECHNICAL,
            RiskLevel.INFO,
            "预计下跌 5%",
            "观察",
            "可能还要跌",
            affects=False,
        )


def test_risk_signal_requires_all_fields() -> None:
    from src.assistant.domain import RiskSignal

    with pytest.raises(ValueError):
        RiskSignal(
            risk_id="",
            code=RiskCode.TEC_1,
            category=RiskCategory.TECHNICAL,
            level=RiskLevel.INFO,
            trigger_basis="x",
            data_date=DATA_DATE,
            affects_advice=False,
            action="观察",
            human_text="x",
        )


def test_valuation_history_missing_not_faked() -> None:
    """历史不足时不得用当日值冒充连续状态（标「数据不足」）。"""
    sector = SectorView(sector_name="银行", strength="偏弱", rs_t=-0.01, history_rs_t=(-0.01,))
    summary = assess_risks(_ctx(sector=sector))
    sec1 = next(s for s in summary.signals if s.code is RiskCode.SEC_1)
    assert sec1.level is RiskLevel.INFO
    assert "数据不足" in sec1.action
