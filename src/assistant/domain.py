# -*- coding: utf-8 -*-
"""领域数据模型（纯数据，不含 IO 与 ORM）。

这些 dataclass 是决策引擎的输入/输出契约，也是 L3 快照落库前的中间表示：
- 输入：:class:`Bar`、:class:`StockView`、:class:`PortfolioView`
- 输出：:class:`StageResult`、:class:`RiskSignal`、:class:`PlanBranch`、:class:`HoldingConclusion`
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from src.assistant.enums import (
    Action,
    AdviceState,
    CandidateReason,
    GuardStep,
    LotRule,
    RiskCategory,
    RiskLevel,
    RiskCode,
    RuleId,
    SourceTag,
    Stage,
    StageConfidence,
    TriggerType,
)


# ---------------------------------------------------------------------------
# 输入
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Bar:
    """一根 K 线。**价格为不复权原始价**（需求 §4.6.4），复权在 L2 内存派生。"""

    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float = 0.0
    adj_factor: float = 1.0
    source_tag: SourceTag = SourceTag.D1

    def __post_init__(self) -> None:
        if self.high < self.low:
            raise ValueError(f"{self.trade_date} high({self.high}) < low({self.low})")
        if not (self.low <= self.close <= self.high):
            raise ValueError(f"{self.trade_date} close({self.close}) 不在 [{self.low}, {self.high}] 内")
        if self.volume < 0:
            raise ValueError(f"{self.trade_date} volume 为负")

    @property
    def post_adj_close(self) -> float:
        """后复权收盘（L2 派生，仅内存用，不落库）。"""
        return self.close * self.adj_factor


@dataclass(frozen=True)
class KeyLevel:
    """关键价位（需求 §5.3）：**每个价位必须带来源说明**，否则卡片视为不完整。"""

    price: float
    kind: str  # support | resistance | invalid
    source: str  # 前高前低 / 箱体边界 / 重要均线 / 缺口 …
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("关键价位必须带来源说明（需求 §5.3）")
        if self.price <= 0:
            raise ValueError(f"价位必须为正：{self.price}")


@dataclass(frozen=True)
class PositionView:
    """持仓视图（L4 用户输入）。未持仓时 ``shares=0``。"""

    shares: int = 0
    cost_basis: float = 0.0
    sellable_shares: int | None = None  # T+1 可卖数量；None 表示等于 shares
    t_shares: int = 0  # 已持有 T 仓

    @property
    def is_holding(self) -> bool:
        return self.shares > 0

    @property
    def available_to_sell(self) -> int:
        return self.shares if self.sellable_shares is None else max(0, min(self.sellable_shares, self.shares))


@dataclass(frozen=True)
class PortfolioView:
    """组合视图：总资金为**用户填写的固定金额**（需求 §5.6），不倒推。"""

    total_capital: float
    cash_available: float
    holding_market_value: float = 0.0
    sector_market_values: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.total_capital <= 0:
            raise ValueError("总资金必须为正（需求 §5.6 用户填写固定金额）")


@dataclass(frozen=True)
class ValuationView:
    """估值与分红事实（低权重维度，可缺失）。"""

    pe_percentile: float | None = None
    pb_percentile: float | None = None
    dividend_yield: float | None = None
    dividend_yield_percentile: float | None = None
    dividend_continuity_years: int | None = None
    dividend_interrupted: bool = False
    ex_dividend_date: date | None = None
    long_cycle_percentile: float | None = None

    @property
    def is_missing(self) -> bool:
        return self.pe_percentile is None and self.pb_percentile is None


@dataclass(frozen=True)
class FundamentalView:
    """财报事实（不做情绪打分，需求 §5.1 / NG-4）。"""

    available: bool = False
    loss_making: bool = False
    revenue_declining_periods: int = 0
    profit_declining_periods: int = 0
    roe_declining_periods: int = 0
    cashflow_worsening: bool = False
    audit_opinion_abnormal: bool = False
    report_period: str = ""


@dataclass(frozen=True)
class MarketView:
    """大盘环境（M1 2×2 评级 + 宽度）。"""

    index_code: str = "sh000001"
    index_name: str = "上证指数"
    pct_change: float = 0.0
    regime: str = "中性"
    regime_degraded: bool = False
    advance_ratio: float | None = None
    width_degraded: bool = False
    history_regime: Sequence[str] = ()


@dataclass(frozen=True)
class SectorView:
    """板块环境（板块强弱与相对强度序列）。"""

    sector_code: str = ""
    sector_name: str = ""
    pct_change: float = 0.0
    strength: str = "中性"
    rs_t: float | None = None
    history_rs_t: Sequence[float] = ()
    up_count: int | None = None
    down_count: int | None = None
    lead_stock: str = ""
    degraded: bool = False


@dataclass(frozen=True)
class StockView:
    """单只标的的完整输入视图。"""

    code: str
    name: str
    bars: Sequence[Bar]
    position: PositionView = field(default_factory=PositionView)
    sector_code: str = ""
    lot_rule: LotRule = LotRule.A_SHARE
    suspended: bool = False
    is_etf: bool = False
    valuation: ValuationView = field(default_factory=ValuationView)
    fundamental: FundamentalView = field(default_factory=FundamentalView)
    market_value: float = 0.0
    pnl_pct: float | None = None
    asset_class: str = ""
    source_tag: SourceTag = SourceTag.D1

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("标的代码不能为空")
        dates = [bar.trade_date for bar in self.bars]
        if dates != sorted(dates):
            raise ValueError(f"{self.code} K 线未按日期升序")
        if len(set(dates)) != len(dates):
            raise ValueError(f"{self.code} K 线存在重复交易日")

    @property
    def last_bar(self) -> Bar | None:
        return self.bars[-1] if self.bars else None

    @property
    def last_close(self) -> float:
        if not self.bars:
            raise ValueError(f"{self.code} 无 K 线数据")
        return self.bars[-1].close


# ---------------------------------------------------------------------------
# 阶段
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StageResult:
    """阶段判定结果（需求 §5.14.2）。``stage`` 是**阶段码**，不是守卫序。"""

    stage: Stage
    confidence: StageConfidence
    reasons: tuple[str, ...] = ()
    metrics: Mapping[str, Any] = field(default_factory=dict)
    previous_stage: Stage | None = None
    switched: bool = False
    pending_stage: Stage | None = None
    pending_days: int = 0

    @property
    def blocked(self) -> bool:
        """S0 时不出阶段、不出策略、不出计划（需求 §5.14.5）。"""
        return self.stage is Stage.S0

    def rationale(self) -> str:
        return "；".join(self.reasons) if self.reasons else "无"


@dataclass(frozen=True)
class StageGateHit:
    """阶段门改写留痕（需求 §5.14.5）：随快照留痕，供复盘与回测归因。"""

    stage: Stage
    candidate: Action
    final: Action
    rule_id: RuleId
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "candidate": self.candidate.value,
            "final": self.final.value,
            "rule_id": self.rule_id.value,
        }


# ---------------------------------------------------------------------------
# 风险
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RiskSignal:
    """风险信号（需求 §5.10.1 必带字段）。

    缺任一必带字段视为卡片不完整，构造时即校验。
    """

    risk_id: str
    code: RiskCode
    category: RiskCategory
    level: RiskLevel
    trigger_basis: str
    data_date: date
    affects_advice: bool
    action: str
    human_text: str
    source_tag: SourceTag = SourceTag.D1
    degraded: bool = False
    stock_code: str = ""

    def __post_init__(self) -> None:
        for name in ("risk_id", "trigger_basis", "action", "human_text"):
            if not getattr(self, name):
                raise ValueError(f"风险信号缺必带字段 {name}（需求 §5.10.1）")
        # 数据质量/缺失类只能观察级，不得伪装成基本面或技术面风险（需求 §5.10.2 口径边界）
        if self.code in (RiskCode.MKT_3, RiskCode.FUN_3, RiskCode.SEC_3, RiskCode.DQ_2):
            if self.level.rank > RiskLevel.INFO.rank:
                raise ValueError(f"{self.code.value} 属数据质量，只能观察级，不得伪装成基本面/技术面风险")

    def as_dict(self) -> dict[str, Any]:
        return {
            "risk_id": self.risk_id,
            "code": self.code.value,
            "category": self.category.value,
            "level": self.level.value,
            "trigger_basis": self.trigger_basis,
            "data_date": self.data_date.isoformat(),
            "affects_advice": self.affects_advice,
            "action": self.action,
            "human_text": self.human_text,
            "source_tag": self.source_tag.value,
            "degraded": self.degraded,
            "stock_code": self.stock_code,
        }


# ---------------------------------------------------------------------------
# 条件计划
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PlanBranch:
    """条件计划的一个分支（需求 §5.11.4「全给」粒度）。

    每条分支**单独**通过硬上限；越界分支不出（``position_ok=False`` 由生成器过滤）。
    """

    branch_id: str
    scenario: str
    trigger_type: TriggerType
    trigger_price: float | None
    action: Action
    shares: int
    est_amount: float
    pct_of_total: float
    valid_days: int = 1
    settable: bool = True
    notes: str = ""
    invalidate_if: tuple[str, ...] = ("收盘未触发", "次日快照重新生成")
    position_check: Mapping[str, Any] = field(default_factory=dict)
    technical_basis: str = ""
    pct_change: float | None = None
    is_probe: bool = False

    @property
    def has_quantified_trigger(self) -> bool:
        """必须有可挂单的触发（价格或涨跌幅），否则只能标注「需人工判断」。"""
        return self.trigger_type.writable and (
            (self.trigger_price is not None and self.trigger_price > 0)
            or (self.pct_change is not None and self.pct_change != 0)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "branch_id": self.branch_id,
            "scenario": self.scenario,
            "trigger_type": self.trigger_type.value,
            "trigger_price": self.trigger_price,
            "pct_change": self.pct_change,
            "action": self.action.value,
            "shares": self.shares,
            "est_amount": round(self.est_amount, 2),
            "pct_of_total": round(self.pct_of_total, 6),
            "valid_days": self.valid_days,
            "settable": self.settable,
            "notes": self.notes,
            "invalidate_if": list(self.invalidate_if),
            "position_check": dict(self.position_check),
            "technical_basis": self.technical_basis,
            "is_probe": self.is_probe,
        }


@dataclass(frozen=True)
class PlanAggregate:
    """全买入分支同时触发的合计校验（需求 §5.11.5，只提示不裁决）。"""

    total_amount: float
    total_pct: float
    over_single_cap: bool
    over_total_cap: bool
    message: str = ""


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PositionAdvice:
    """仓位建议（S6）：软建议 + 硬上限裁剪结果。"""

    tier: str = "轻"  # 轻 / 中 / 重
    target_pct: float = 0.0
    target_amount: float = 0.0
    capped_pct: float = 0.0
    cap_reasons: tuple[str, ...] = ()
    t_cap_amount: float = 0.0


@dataclass(frozen=True)
class HoldingConclusion:
    """个股结论（对应 L3 ``holding_conclusion`` 一行）。"""

    code: str
    name: str
    advice: Action | None
    advice_state: AdviceState
    stage: Stage
    stage_confidence: StageConfidence
    stage_gate_hit: StageGateHit | None = None
    trend: str = "中性"
    position_tag: str = "不明"
    score: float = 0.0
    is_holding: bool = False
    est_level: str = ""
    div_yield: float | None = None
    pnl_pct: float | None = None
    risk_level: RiskLevel = RiskLevel.INFO
    human_text: str = ""
    degraded: bool = False

    @property
    def advice_display(self) -> str:
        """展示层唯一输出：有效意见给动作名，硬约束态给状态位文字（禁止留白）。"""
        if self.advice_state is not AdviceState.VALID:
            return self.advice_state.value
        return self.advice.value if self.advice else AdviceState.NO_DATA.value


@dataclass(frozen=True)
class DataQuality:
    """S0 数据校验产出。"""

    sufficient: bool
    bars_used: int
    gap_days: int
    flags: tuple[str, ...] = ()
    reason: str = ""

    @property
    def degraded(self) -> bool:
        return not self.sufficient


@dataclass(frozen=True)
class GuardTrace:
    """守卫链一步的可审计中间结果。"""

    step: GuardStep
    summary: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GuardChainResult:
    """守卫链 S0→S6 的完整产出，可直接装配为 L3 快照。"""

    code: str
    name: str
    data_date: date
    calc_version: str
    param_hash: str
    generated_at: datetime
    data_quality: DataQuality
    stage_result: StageResult
    conclusion: HoldingConclusion
    plans: tuple[PlanBranch, ...] = ()
    plan_aggregate: PlanAggregate | None = None
    risks: tuple[RiskSignal, ...] = ()
    position_advice: PositionAdvice | None = None
    key_levels: tuple[KeyLevel, ...] = ()
    trace: tuple[GuardTrace, ...] = ()
    probe: Mapping[str, Any] = field(default_factory=dict)

    @property
    def risk_level(self) -> RiskLevel:
        return self.conclusion.risk_level

    @property
    def triple(self) -> tuple[str, str, str]:
        """三元组 [data_date, snapshot_id, calc_version]——snapshot_id 由快照层补。"""
        return (self.data_date.isoformat(), "", self.calc_version)


@dataclass(frozen=True)
class CandidateAdvice:
    """S5 决策表产出（阶段门的输入）。"""

    action: Action
    score: float
    reason: CandidateReason
    summary: str = ""


# ---------------------------------------------------------------------------
# 引擎产出、渲染消费的值对象
# ---------------------------------------------------------------------------
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
