# -*- coding: utf-8 -*-
"""评分与决策表（需求 §5.1 / §5.4 S5，技术方案 §4.2）。

**技术面主导**：以 0–100 的技术面基准分为主轴，其余维度只做小幅度加减分
（四项之和 ≤ 40 分，由 :func:`src.assistant.config.presets.validate_weights` 守护）。

**不感知阶段**：评分与决策表完全不读 ``stage``（DP-6）；阶段只由阶段门 S5.5 事后收紧。
**不重复计价**：同一事实若已生成风险信号，只在风险侧产生决策效果，评分侧仅注记。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from src.assistant.config.overrides import ParamSet
from src.assistant.domain import (
    CandidateAdvice,
    FundamentalView,
    MarketView,
    SectorView,
    StockView,
    ValuationView,
)
from src.assistant.engine.indicators import Box, average
from src.assistant.enums import Action, CandidateReason


#: 决策表档位边界（初值，纸面回看后回写；需求 §5.4 只锁结构不锁数值）
SCORE_BANDS: tuple[tuple[float, str], ...] = (
    (72.0, "strong"),
    (58.0, "mild"),
    (42.0, "neutral"),
    (28.0, "weak"),
    (0.0, "bad"),
)

#: 分数档 → 动作（持仓 / 未持仓）
_DECISION_TABLE: Mapping[str, Mapping[bool, Action]] = {
    "strong": {True: Action.ADD, False: Action.WAIT_BUY},
    "mild": {True: Action.HOLD, False: Action.WAIT_BUY},
    "neutral": {True: Action.HOLD, False: Action.WATCH},
    "weak": {True: Action.TRIM, False: Action.WATCH},
    "bad": {True: Action.CLEAR, False: Action.WATCH},
}


@dataclass(frozen=True)
class ScoreBreakdown:
    """可逐项审计的评分结果（对应 §6.6 八字段的 ``metric=score``）。"""

    tech: float
    market_adj: float = 0.0
    fundamental_adj: float = 0.0
    dividend_adj: float = 0.0
    valuation_adj: float = 0.0
    notes: tuple[str, ...] = ()
    parts: Mapping[str, float] = field(default_factory=dict)

    @property
    def total(self) -> float:
        return round(
            max(0.0, min(100.0, self.tech + self.market_adj + self.fundamental_adj + self.dividend_adj + self.valuation_adj)),
            2,
        )

    def as_dict(self) -> dict[str, float | list[str]]:
        return {
            "tech": round(self.tech, 2),
            "market_adj": round(self.market_adj, 2),
            "fundamental_adj": round(self.fundamental_adj, 2),
            "dividend_adj": round(self.dividend_adj, 2),
            "valuation_adj": round(self.valuation_adj, 2),
            "total": self.total,
            "notes": list(self.notes),
            "parts": {key: round(val, 2) for key, val in self.parts.items()},
        }


def tech_score(
    view: StockView,
    *,
    ma: float,
    slope: float,
    theta: float,
    epsilon: float,
    pos20: float,
    alignment: int,
    box: Box | None,
) -> tuple[float, dict[str, float], tuple[str, ...]]:
    """技术面基准分（0–100），返回 ``(分数, 分项, 注记)``。"""
    bars = list(view.bars)
    close = bars[-1].close
    parts: dict[str, float] = {"base": 50.0}
    notes: list[str] = []

    # ① 价格与 MA 关系（±12）
    if close > ma * (1 + epsilon):
        parts["ma"] = 12.0
        notes.append("收盘高于 MA20×(1+ε)")
    elif close < ma * (1 - epsilon):
        parts["ma"] = -12.0
        notes.append("收盘低于 MA20×(1-ε)")
    else:
        parts["ma"] = 0.0
        notes.append("收盘贴近 MA20（缓冲内）")

    # ② 斜率（±12，按 θ 归一化后截断）
    if theta > 0:
        norm = max(-1.0, min(1.0, slope / theta))
    else:
        norm = 0.0
    parts["slope"] = 12.0 * norm

    # ③ 均线排列（−6 ~ +6）
    parts["alignment"] = (alignment - 1.5) * 4.0

    # ④ 位置分位（±6）：只作微调，避免把「低位」当扣分项主导结论
    parts["position"] = (pos20 - 0.5) * 12.0

    # ⑤ 量能（±4）：涨放量加分，跌放量扣分
    vols = [bar.volume for bar in bars]
    vol_ma = average(vols[-20:]) if len(vols) >= 20 else average(vols)
    vol_ratio = (vols[-1] / vol_ma) if vol_ma > 0 else 1.0
    prev_close = bars[-2].close if len(bars) >= 2 else close
    if vol_ratio >= 1.2:
        parts["volume"] = 4.0 if close >= prev_close else -4.0
    else:
        parts["volume"] = 0.0

    # ⑥ 关键价位距离（±4）：贴近支撑加分，贴近压力扣分
    parts["levels"] = 0.0
    if box is not None and box.valid:
        if close <= box.low * 1.03:
            parts["levels"] = 4.0
        elif close >= box.high * 0.97:
            parts["levels"] = -4.0

    tech = max(0.0, min(100.0, sum(parts.values())))
    return tech, parts, tuple(notes)


def market_adjustment(market: MarketView | None, sector: SectorView | None, weight: float) -> tuple[float, tuple[str, ...]]:
    """大盘/板块强弱加减分（**行情结构数据，非消息面情绪**，R4/R5）。"""
    if market is None and sector is None:
        return 0.0, ("大盘/板块数据缺失，按中性代入",)
    notes: list[str] = []
    raw = 0.0
    if market is not None:
        raw += {
            "强势": 1.0,
            "偏强": 0.6,
            "中性": 0.0,
            "偏弱": -0.8,
            "极弱": -1.0,
        }.get(market.regime, 0.0)
        if market.regime not in ("强势", "偏强", "中性", "偏弱", "极弱"):
            notes.append(f"未知大盘环境 {market.regime!r}，按中性代入")
        if market.regime_degraded:
            notes.append("大盘数据降级，仅注记不升级风险（MKT-3）")
    if sector is not None:
        raw += {"偏强": 0.4, "中性": 0.0, "偏弱": -0.4}.get(sector.strength, 0.0)
    adj = max(-weight, min(weight, raw * weight))
    return adj, tuple(notes)


def fundamental_adjustment(fund: FundamentalView | None, weight: float) -> tuple[float, tuple[str, ...]]:
    """财报事实加减分：**不做情绪打分**，只按事实性恶化条数计。"""
    if fund is None or not fund.available:
        return 0.0, ("财报数据缺失，标注缺失并降置信度（不改档位）",)
    notes: list[str] = []
    hits = 0
    if fund.loss_making:
        hits += 1
        notes.append("当期亏损")
    if fund.revenue_declining_periods >= 2:
        hits += 1
        notes.append(f"营收连续 {fund.revenue_declining_periods} 期下滑")
    if fund.profit_declining_periods >= 2:
        hits += 1
        notes.append(f"利润连续 {fund.profit_declining_periods} 期下滑")
    if fund.roe_declining_periods >= 2:
        hits += 1
        notes.append(f"ROE 连续 {fund.roe_declining_periods} 期下滑")
    if fund.cashflow_worsening:
        hits += 1
        notes.append("经营现金流恶化")
    if fund.audit_opinion_abnormal:
        hits += 2
        notes.append("审计意见异常")
    adj = -weight * min(1.0, hits / 3)
    return adj, tuple(notes)


def dividend_adjustment(val: ValuationView | None, weight: float) -> tuple[float, tuple[str, ...]]:
    """分红加减分：连续性加分、中断扣分（严重级风险另由风险层处理）。"""
    if val is None:
        return 0.0, ("分红数据缺失，标注缺失",)
    notes: list[str] = []
    adj = 0.0
    if val.dividend_interrupted:
        adj -= weight
        notes.append("分红中断（DIV-1 另计严重级风险）")
    elif val.dividend_continuity_years is not None:
        if val.dividend_continuity_years >= 5:
            adj += weight
            notes.append(f"连续分红 {val.dividend_continuity_years} 年")
        elif val.dividend_continuity_years <= 1:
            adj -= weight / 2
            notes.append("分红连续性不足")
    if val.dividend_yield is not None and val.dividend_yield_percentile is not None:
        if val.dividend_yield_percentile >= 0.8:
            adj += weight / 2
            notes.append("股息率处历史高分位")
    return max(-weight, min(weight, adj)), tuple(notes)


def valuation_adjustment(val: ValuationView | None, weight: float, params: ParamSet) -> tuple[float, tuple[str, ...]]:
    """估值分位加减分。**估值位置 ≠ 价格位置**（需求 §5.8），此处只看估值。"""
    if val is None or val.is_missing:
        return 0.0, ("估值分位缺失，标注缺失（不改档位）",)
    pctiles = [p for p in (val.pe_percentile, val.pb_percentile) if p is not None]
    if not pctiles:
        return 0.0, ("估值分位缺失",)
    avg_pct = sum(pctiles) / len(pctiles)
    low = float(params.get("risk_pctile_low"))
    high = float(params.get("risk_pctile_high"))
    notes = [f"估值分位均值 {avg_pct:.2f}（低界 {low} / 高界 {high}）"]
    if avg_pct <= low:
        # 注意：低估**不单独构成买入理由**（SEC-2 价值陷阱），这里只是小幅加分
        return weight / 2, tuple(notes + ["估值处低位（不单独构成买入理由）"])
    if avg_pct >= high:
        return -weight / 2, tuple(notes + ["估值处高位"])
    return 0.0, tuple(notes)


def score_stock(
    view: StockView,
    params: ParamSet,
    *,
    ma: float,
    slope: float,
    pos20: float,
    alignment: int,
    box: Box | None,
    market: MarketView | None = None,
    sector: SectorView | None = None,
) -> ScoreBreakdown:
    """完整评分：技术面 + 四个低权重维度。"""
    tech, parts, tech_notes = tech_score(
        view,
        ma=ma,
        slope=slope,
        theta=float(params.get("stage_theta")),
        epsilon=float(params.get("stage_epsilon")),
        pos20=pos20,
        alignment=alignment,
        box=box,
    )
    m_adj, m_notes = market_adjustment(market, sector, float(params.get("w_market")))
    f_adj, f_notes = fundamental_adjustment(view.fundamental, float(params.get("w_fundamental")))
    d_adj, d_notes = dividend_adjustment(view.valuation, float(params.get("w_dividend")))
    v_adj, v_notes = valuation_adjustment(view.valuation, float(params.get("w_valuation")), params)
    return ScoreBreakdown(
        tech=tech,
        market_adj=m_adj,
        fundamental_adj=f_adj,
        dividend_adj=d_adj,
        valuation_adj=v_adj,
        notes=tech_notes + m_notes + f_notes + d_notes + v_notes,
        parts=parts,
    )


def band_of(score: float) -> str:
    for threshold, name in SCORE_BANDS:
        if score >= threshold:
            return name
    return "bad"


def decide(
    score: float,
    *,
    is_holding: bool,
    params: ParamSet,
    previous_advice: Action | None = None,
) -> CandidateAdvice:
    """决策表映射 + 档位切换缓冲（``advice_margin``）。

    缓冲口径：若分数距**最近档位边界**不足 ``advice_margin × 100`` 分，则维持上一次意见，
    避免在边界附近来回翻档（需求 §5.4「档位切换分数差未超过则维持原档」）。
    """
    band = band_of(score)
    proposed = _DECISION_TABLE[band][is_holding]
    margin = float(params.get("advice_margin")) * 100.0

    if previous_advice is not None and previous_advice is not proposed:
        distance = _distance_to_nearest_band(score)
        if distance < margin and _same_direction(previous_advice, proposed):
            return CandidateAdvice(
                action=previous_advice,
                score=score,
                reason=CandidateReason.SCORE_TABLE,
                summary=f"分数 {score:.1f}（档位 {band}）距边界 {distance:.1f} < 缓冲 {margin:.1f}，维持原档",
            )

    return CandidateAdvice(
        action=proposed,
        score=score,
        reason=CandidateReason.SCORE_TABLE,
        summary=f"分数 {score:.1f} → 档位 {band} → {proposed.value}",
    )


def _distance_to_nearest_band(score: float) -> float:
    return min(abs(score - threshold) for threshold, _ in SCORE_BANDS)


def _same_direction(a: Action, b: Action) -> bool:
    """缓冲只在「同向档位」间生效，避免把方向反转也一起缓冲掉。"""
    buys = {Action.ADD, Action.BUY, Action.WAIT_BUY}
    sells = {Action.TRIM, Action.CLEAR, Action.WAIT_SELL}
    neutrals = {Action.HOLD, Action.WATCH, Action.NO_OP}
    for group in (buys, sells, neutrals):
        if a in group and b in group:
            return True
    return False
