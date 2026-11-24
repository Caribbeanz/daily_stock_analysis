# -*- coding: utf-8 -*-
"""风险信号 7 类（需求 §5.10 / 技术方案 §4.5）。

四条铁律：
1. **一条链**：信号 → 等级 → 动作 → 展示位置，任何信号都必须走完四步。
2. **事实驱动**：``trigger_basis`` 必须是可回溯事实 + 数据日期，禁止「可能/预计/大约」。
3. **不叠加升级**：3 条警示 ≠ 1 条严重；升级只能由某条信号**自身规则**触发。
4. **口径边界**：数据质量/缺失不得伪装成「基本面变坏」；大盘最高只到警示级（R5）。

历史序列不足时一律记「观察级 · 数据不足」，**不得用当日值冒充连续状态**（§7.3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Mapping, Sequence

from src.assistant.config.overrides import ParamSet
from src.assistant.domain import (
    DataQuality,
    FundamentalView,
    KeyLevel,
    MarketView,
    PortfolioView,
    SectorView,
    StockView,
    ValuationView,
)
from src.assistant.engine.indicators import average, consecutive
from src.assistant.enums import RiskCategory, RiskCode, RiskLevel, Stage


#: 禁止出现在触发依据里的推测词（需求 §5.10.7）
FORBIDDEN_WORDS: tuple[str, ...] = ("可能", "预计", "大概率", "大约", "约", "或许", "有望")


@dataclass(frozen=True)
class RiskContext:
    """风险评估的完整输入（一次传齐，风险层不再取数）。"""

    view: StockView
    data_date: date
    params: ParamSet
    data_quality: DataQuality
    stage: Stage
    trend: str = "中性"
    position_tag: str = "不明"
    market: MarketView | None = None
    sector: SectorView | None = None
    portfolio: PortfolioView | None = None
    key_levels: Sequence[KeyLevel] = ()
    previous_trend: str | None = None
    l1_triggered: bool = False
    l1_reference_price: float | None = None
    sector_mapped: bool = True


@dataclass(frozen=True)
class RiskSummary:
    """页级风险汇总（Tab A 顶部「今日风险 N 条」）。"""

    signals: tuple = ()
    highest: RiskLevel = RiskLevel.INFO
    counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.signals)

    @property
    def buy_forbidden(self) -> bool:
        """警示级及以上禁止买入类动作（需求 §5.10.3）。"""
        return self.highest.rank >= RiskLevel.WARNING.rank

    @property
    def severe(self) -> bool:
        return self.highest is RiskLevel.SEVERE


def assess_risks(ctx: RiskContext) -> RiskSummary:
    """执行 7 类风险判定，返回去重合并后的信号集合。"""
    found: list = []
    found.extend(_market_risks(ctx))
    found.extend(_sector_risks(ctx))
    found.extend(_fundamental_risks(ctx))
    found.extend(_technical_risks(ctx))
    found.extend(_dividend_valuation_risks(ctx))
    found.extend(_position_risks(ctx))
    found.extend(_data_quality_risks(ctx))

    # 同类合并为一条（保留最高级），不同类全列出（需求 §5.10.4）
    merged: dict[RiskCategory, list] = {}
    for sig in found:
        merged.setdefault(sig.category, []).append(sig)

    final: list = []
    for category, group in merged.items():
        group.sort(key=lambda s: s.level.rank, reverse=True)
        head = group[0]
        if len(group) > 1:
            detail = "；".join(g.code.value for g in group)
            head = _with_text(head, f"{head.human_text}（同类合并：{detail}）")
        final.append(head)

    final.sort(key=lambda s: (-s.level.rank, s.code.value))
    highest = RiskLevel.INFO
    for sig in final:
        if sig.level.rank > highest.rank:
            highest = sig.level
    counts: dict[str, int] = {lvl.value: 0 for lvl in RiskLevel}
    for sig in final:
        counts[sig.level.value] += 1
    return RiskSummary(signals=tuple(final), highest=highest, counts=counts)


# ---------------------------------------------------------------------------
# ① 大盘环境（最高只到警示级，R5）
# ---------------------------------------------------------------------------
def _market_risks(ctx: RiskContext) -> list:
    out: list = []
    market = ctx.market
    if market is None:
        return [
            _sig(
                ctx,
                RiskCode.MKT_3,
                RiskCategory.MARKET,
                RiskLevel.INFO,
                "未取得大盘环境数据",
                "仅注记，不作环境风险",
                "大盘数据缺失，按中性代入",
                affects=False,
            )
        ]
    if market.regime_degraded:
        out.append(
            _sig(
                ctx,
                RiskCode.MKT_3,
                RiskCategory.MARKET,
                RiskLevel.INFO,
                "大盘环境评级降级（regime_degraded=true）",
                "记数据质量，不作环境风险",
                "大盘数据降级，仅注记",
                affects=False,
            )
        )

    n = int(ctx.params.get("risk_warn_consec_days"))
    history = list(market.history_regime)
    weak_flags = [r in ("偏弱", "极弱") for r in history]
    weak_days = consecutive(weak_flags)
    if weak_days >= n:
        out.append(
            _sig(
                ctx,
                RiskCode.MKT_1,
                RiskCategory.MARKET,
                RiskLevel.WARNING,
                f"market_regime 连续 {weak_days} 日为偏弱（阈值 {n} 日）",
                "限制加仓/新建仓，不改主档",
                f"大盘连续 {weak_days} 日偏弱，今天不加仓",
                affects=True,
            )
        )
    elif len(history) < n and history and all(weak_flags):
        out.append(
            _sig(
                ctx,
                RiskCode.MKT_1,
                RiskCategory.MARKET,
                RiskLevel.INFO,
                f"大盘偏弱仅 {len(history)} 日，历史不足 {n} 日",
                "观察级 · 数据不足",
                "大盘偏弱但还没到连续天数，先观察",
                affects=False,
            )
        )

    if market.advance_ratio is not None and market.advance_ratio < 0.3:
        out.append(
            _sig(
                ctx,
                RiskCode.MKT_2,
                RiskCategory.MARKET,
                RiskLevel.WARNING,
                f"市场宽度恶化：上涨占比 {market.advance_ratio:.0%} < 30%",
                "与 MKT-1 同时成立仍只到警示级",
                f"大盘上涨家数仅 {market.advance_ratio:.0%}，环境偏弱",
                affects=True,
            )
        )
    return out


# ---------------------------------------------------------------------------
# ② 板块与低估陷阱
# ---------------------------------------------------------------------------
def _sector_risks(ctx: RiskContext) -> list:
    out: list = []
    if not ctx.sector_mapped:
        out.append(
            _sig(
                ctx,
                RiskCode.SEC_3,
                RiskCategory.SECTOR,
                RiskLevel.INFO,
                "板块映射失败（unmapped_holdings）",
                "如实记缺失，不推测板块强弱",
                "未映射到板块，板块强弱未参与判断",
                affects=False,
            )
        )
        return out + _value_trap(ctx)

    sector = ctx.sector
    if sector is None or sector.degraded:
        out.append(
            _sig(
                ctx,
                RiskCode.SEC_3,
                RiskCategory.SECTOR,
                RiskLevel.INFO,
                "板块快照缺失或降级",
                "如实记缺失，不推测板块强弱",
                "板块数据缺失，今天不看板块强弱",
                affects=False,
            )
        )
        # SEC-2 只依赖「估值分位 + 趋势方向」，不依赖板块数据，不得因板块缺失而漏判
        return out + _value_trap(ctx)

    n = int(ctx.params.get("risk_warn_consec_days"))
    rs_hist = list(sector.history_rs_t)
    neg_days = consecutive([val < 0 for val in rs_hist])
    if sector.strength == "偏弱" and neg_days >= n:
        out.append(
            _sig(
                ctx,
                RiskCode.SEC_1,
                RiskCategory.SECTOR,
                RiskLevel.WARNING,
                f"{sector.sector_name or sector.sector_code} 板块偏弱且 rs_t 连续 {neg_days} 日为负",
                "限制加仓/新建仓",
                f"{sector.sector_name or '所在板块'}连续 {neg_days} 日弱于大盘",
                affects=True,
            )
        )
    elif sector.strength == "偏弱" and len(rs_hist) < n:
        out.append(
            _sig(
                ctx,
                RiskCode.SEC_1,
                RiskCategory.SECTOR,
                RiskLevel.INFO,
                f"板块偏弱但 rs_t 历史仅 {len(rs_hist)} 日（不足 {n} 日）",
                "观察级 · 数据不足",
                "板块偏弱，但历史天数不够，先观察",
                affects=False,
            )
        )

    out.extend(_value_trap(ctx))
    return out


def _value_trap(ctx: RiskContext) -> list:
    """SEC-2 价值陷阱：估值分位低 **且** 价格/趋势持续下行。

    只依赖估值分位与趋势方向，**不依赖板块数据**——板块缺失时也要判定，
    否则「低估可买」的错误结论会漏掉唯一能拦住它的信号。
    """
    val = ctx.view.valuation
    low = float(ctx.params.get("risk_pctile_low"))
    pctiles = [p for p in (val.pe_percentile, val.pb_percentile) if p is not None]
    if not pctiles:
        return []
    avg_pct = sum(pctiles) / len(pctiles)
    downtrend = ctx.stage is Stage.S3 or ctx.trend == "偏弱"
    if avg_pct > low or not downtrend:
        return []
    return [
        _sig(
            ctx,
            RiskCode.SEC_2,
            RiskCategory.SECTOR,
            RiskLevel.SEVERE,
            f"估值分位 {avg_pct:.2f} ≤ 低界 {low}，且当前处下跌/偏弱（阶段 {ctx.stage.value}、趋势 {ctx.trend}）",
            "禁止以「低估」为由买入；建议降级",
            "估值低不等于安全：价格还在往下走，别把便宜当买点",
            affects=True,
        )
    ]


# ---------------------------------------------------------------------------
# ③ 个股基本面
# ---------------------------------------------------------------------------
def _fundamental_risks(ctx: RiskContext) -> list:
    fund: FundamentalView = ctx.view.fundamental
    if not fund.available:
        return [
            _sig(
                ctx,
                RiskCode.FUN_3,
                RiskCategory.FUNDAMENTAL,
                RiskLevel.INFO,
                "财报数据未取得或未更新（fundamental_ok=false）",
                "记数据质量，不得标成「基本面变坏」",
                "财报数据没取到，不等于公司变差",
                affects=False,
            )
        ]
    out: list = []
    if fund.loss_making or fund.audit_opinion_abnormal:
        facts = []
        if fund.loss_making:
            facts.append("当期亏损")
        if fund.audit_opinion_abnormal:
            facts.append("审计意见异常")
        out.append(
            _sig(
                ctx,
                RiskCode.FUN_1,
                RiskCategory.FUNDAMENTAL,
                RiskLevel.SEVERE,
                f"{fund.report_period or '最新报告期'}：{'、'.join(facts)}",
                "强制建议降级 + 禁止买入类",
                f"财报出问题（{'、'.join(facts)}），今天不买入",
                affects=True,
            )
        )

    n = int(ctx.params.get("risk_warn_consec_days"))
    worst = max(fund.revenue_declining_periods, fund.profit_declining_periods, fund.roe_declining_periods)
    if worst >= n or fund.cashflow_worsening:
        basis = [f"营收连降 {fund.revenue_declining_periods} 期", f"利润连降 {fund.profit_declining_periods} 期", f"ROE 连降 {fund.roe_declining_periods} 期"]
        if fund.cashflow_worsening:
            basis.append("经营现金流恶化")
        out.append(
            _sig(
                ctx,
                RiskCode.FUN_2,
                RiskCategory.FUNDAMENTAL,
                RiskLevel.WARNING,
                f"{fund.report_period or '近几期'}：" + "、".join(b for b in basis if "0 期" not in b),
                "限制加仓/新建仓",
                "基本面在渐进变差，先不加仓",
                affects=True,
            )
        )
    return out


# ---------------------------------------------------------------------------
# ④ 技术面
# ---------------------------------------------------------------------------
def _technical_risks(ctx: RiskContext) -> list:
    out: list = []
    close = ctx.view.last_close if ctx.view.bars else 0.0
    invalid = next((lv for lv in ctx.key_levels if lv.kind == "invalid"), None)

    if invalid is not None and close > 0 and close < invalid.price:
        out.append(
            _sig(
                ctx,
                RiskCode.TEC_2,
                RiskCategory.TECHNICAL,
                RiskLevel.SEVERE,
                f"收盘 {close:.2f} 跌破趋势失效位 {invalid.price:.2f}（来源：{invalid.source}）",
                "触发 L1：先减仓保留底仓",
                f"已跌破 {invalid.price:.2f}，先减一部分，底仓留着",
                affects=True,
            )
        )
    if ctx.l1_triggered and ctx.l1_reference_price:
        l2_line = float(ctx.l1_reference_price) * (1 - float(ctx.params.get("stop_l2_pct")))
        if close < l2_line:
            out.append(
                _sig(
                    ctx,
                    RiskCode.TEC_3,
                    RiskCategory.TECHNICAL,
                    RiskLevel.SEVERE,
                    f"L1 后继续下跌：收盘 {close:.2f} < L2 线 {l2_line:.2f}（L1 参考 {ctx.l1_reference_price:.2f} × (1-{ctx.params.get('stop_l2_pct')})）",
                    "触发 L2：清仓",
                    f"跌破 L2 线 {l2_line:.2f}，按纪律清仓",
                    affects=True,
                )
            )

    worsened = ctx.previous_trend in ("偏强", "中性") and ctx.trend in ("中性", "偏弱") and ctx.previous_trend != ctx.trend
    vol_ratio = _volume_ratio(ctx.view)
    heavy_drop = vol_ratio >= 1.5 and bool(ctx.view.bars) and len(ctx.view.bars) >= 2 and (
        ctx.view.bars[-1].close / ctx.view.bars[-2].close - 1 <= -0.03
    )
    if worsened or heavy_drop:
        facts = []
        if worsened:
            facts.append(f"趋势档 {ctx.previous_trend}→{ctx.trend}")
        if heavy_drop:
            facts.append(f"放量下跌（量比 {vol_ratio:.2f}）")
        out.append(
            _sig(
                ctx,
                RiskCode.TEC_1,
                RiskCategory.TECHNICAL,
                RiskLevel.WARNING if heavy_drop else RiskLevel.INFO,
                "、".join(facts),
                "预警态：禁买入类" if heavy_drop else "观察",
                "技术面在转弱" + ("，今天不买入" if heavy_drop else "，先观察"),
                affects=heavy_drop,
            )
        )
    return out


def _volume_ratio(view: StockView) -> float:
    vols = [bar.volume for bar in view.bars]
    if not vols:
        return 1.0
    base = average(vols[-20:]) if len(vols) >= 20 else average(vols)
    return (vols[-1] / base) if base > 0 else 1.0


# ---------------------------------------------------------------------------
# ⑤ 分红与估值
# ---------------------------------------------------------------------------
def _dividend_valuation_risks(ctx: RiskContext) -> list:
    val: ValuationView = ctx.view.valuation
    out: list = []
    if val.dividend_interrupted:
        out.append(
            _sig(
                ctx,
                RiskCode.DIV_1,
                RiskCategory.DIVIDEND_VALUATION,
                RiskLevel.SEVERE,
                f"分红连续性中断（连续 {val.dividend_continuity_years if val.dividend_continuity_years is not None else '未知'} 年）",
                "分红兜底逻辑被破坏，直接触发",
                "分红中断了，原有的分红兜底逻辑不成立",
                affects=True,
            )
        )
    if val.ex_dividend_date is not None and ctx.data_date <= val.ex_dividend_date:
        days = (val.ex_dividend_date - ctx.data_date).days
        if days <= 7:
            out.append(
                _sig(
                    ctx,
                    RiskCode.DIV_2,
                    RiskCategory.DIVIDEND_VALUATION,
                    RiskLevel.INFO,
                    f"除权除息日 {val.ex_dividend_date.isoformat()}（距今 {days} 天），价位须按除权重算",
                    "展示与观察，不改档",
                    f"{days} 天后除权，条件单价位要按除权重算",
                    affects=False,
                )
            )
    pctiles = [p for p in (val.pe_percentile, val.pb_percentile) if p is not None]
    if pctiles:
        avg_pct = sum(pctiles) / len(pctiles)
        high = float(ctx.params.get("risk_pctile_high"))
        if avg_pct >= high and ctx.position_tag == "高位":
            out.append(
                _sig(
                    ctx,
                    RiskCode.DIV_3,
                    RiskCategory.DIVIDEND_VALUATION,
                    RiskLevel.WARNING,
                    f"估值分位 {avg_pct:.2f} ≥ 高界 {high} 且价格位置为高位",
                    "高位风险提示，限制买入类",
                    "估值和价格都在高位，别在这个位置加仓",
                    affects=True,
                )
            )
    return out


# ---------------------------------------------------------------------------
# ⑥ 组合与仓位
# ---------------------------------------------------------------------------
def _position_risks(ctx: RiskContext) -> list:
    pf = ctx.portfolio
    if pf is None:
        return []
    out: list = []
    single_cap = float(ctx.params.get("single_cap"))
    total_cap = float(ctx.params.get("total_cap"))
    cash_floor = float(ctx.params.get("cash_floor"))
    mv = ctx.view.market_value or ctx.view.position.shares * (ctx.view.last_close if ctx.view.bars else 0.0)
    single_pct = mv / pf.total_capital
    if single_pct >= single_cap * 0.9:
        over = single_pct > single_cap
        out.append(
            _sig(
                ctx,
                RiskCode.POS_1,
                RiskCategory.POSITION,
                RiskLevel.WARNING,
                f"{ctx.view.code} 单只占比 {single_pct:.1%}，{'已越' if over else '逼近'}硬上限 {single_cap:.0%}",
                "越界由 S6 硬上限裁剪，只减不增",
                f"这只票占了 {single_pct:.1%}，{'超' if over else '接近'}单只上限",
                affects=over,
            )
        )
    total_pct = pf.holding_market_value / pf.total_capital if pf.holding_market_value else 0.0
    cash_pct = pf.cash_available / pf.total_capital
    if total_pct > total_cap or cash_pct < cash_floor:
        out.append(
            _sig(
                ctx,
                RiskCode.POS_1,
                RiskCategory.POSITION,
                RiskLevel.WARNING,
                f"总仓 {total_pct:.1%}（上限 {total_cap:.0%}）/ 现金 {cash_pct:.1%}（底线 {cash_floor:.0%}）",
                "越界由 S6 硬上限裁剪，只减不增",
                "总仓或现金已触红线，只能减不能加",
                affects=True,
            )
        )
    if pf.sector_market_values and ctx.view.sector_code:
        sector_value = pf.sector_market_values.get(ctx.view.sector_code, 0.0)
        concentration = sector_value / pf.total_capital if pf.total_capital else 0.0
        alert = float(ctx.params.get("concentration_alert"))
        if concentration >= alert:
            out.append(
                _sig(
                    ctx,
                    RiskCode.POS_2,
                    RiskCategory.POSITION,
                    RiskLevel.INFO,
                    f"行业集中度 {concentration:.1%} ≥ 提示阈值 {alert:.0%}",
                    "只提示不硬限",
                    f"这个行业占了 {concentration:.1%}，分散度偏低（仅提示）",
                    affects=False,
                )
            )
    if ctx.view.pnl_pct is not None and ctx.view.pnl_pct < 0 and single_pct >= float(ctx.params.get("tier_light_max")):
        out.append(
            _sig(
                ctx,
                RiskCode.POS_3,
                RiskCategory.POSITION,
                RiskLevel.WARNING,
                f"成本倒挂（浮亏 {ctx.view.pnl_pct:.1%}）且仓位已达 {single_pct:.1%}",
                "风险强调 + L1→L2 加速观察因子，不作触发前置",
                "亏着且仓位不轻，破位要更快执行纪律",
                affects=True,
            )
        )
    return out


# ---------------------------------------------------------------------------
# ⑦ 数据质量
# ---------------------------------------------------------------------------
def _data_quality_risks(ctx: RiskContext) -> list:
    out: list = []
    q = ctx.data_quality
    if q.gap_days > 0 or q.flags:
        out.append(
            _sig(
                ctx,
                RiskCode.DQ_2,
                RiskCategory.DATA_QUALITY,
                RiskLevel.INFO,
                f"数据缺口 {q.gap_days} 日；标记={','.join(q.flags) or '无'}；来源={ctx.view.source_tag.value}",
                "标注缓存与数据日期",
                "数据不是最新，结论请看数据日期",
                affects=False,
                degraded=True,
            )
        )
    if not q.sufficient and q.gap_days > 3:
        out.append(
            _sig(
                ctx,
                RiskCode.DQ_1,
                RiskCategory.DATA_QUALITY,
                RiskLevel.WARNING,
                f"数据缺口 {q.gap_days} 日 > 3 日，阶段降为 S0",
                "该源降级或排除，标注实际来源",
                "数据缺口超过 3 日，今天不给操作建议",
                affects=True,
                degraded=True,
            )
        )
    return out


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _sig(
    ctx: RiskContext,
    code: RiskCode,
    category: RiskCategory,
    level: RiskLevel,
    basis: str,
    action: str,
    human: str,
    *,
    affects: bool,
    degraded: bool = False,
):
    from src.assistant.domain import RiskSignal

    if any(word in basis for word in FORBIDDEN_WORDS):
        raise ValueError(f"风险触发依据含推测词：{basis}")
    return RiskSignal(
        risk_id=f"{ctx.view.code}-{code.value}",
        code=code,
        category=category,
        level=level,
        trigger_basis=basis,
        data_date=ctx.data_date,
        affects_advice=affects,
        action=action,
        human_text=human,
        source_tag=ctx.view.source_tag,
        degraded=degraded,
        stock_code=ctx.view.code,
    )


def _with_text(signal, text: str):
    from dataclasses import replace

    return replace(signal, human_text=text)
