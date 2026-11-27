# -*- coding: utf-8 -*-
"""历史计划回放驱动（D-24）：把「逐日重跑守卫链 → 当日计划 → 后续行情模拟」连成一条批处理。

## 为什么单独一个模块

:mod:`src.assistant.backtest` 只做**回合模拟**（计划 + 日线 → 回合），它是纯函数、不知道
计划从哪来。要在 250 个交易日上跑 walk-forward，还需要一层驱动：

- **逐日切片**：第 T 天只能用 ≤ T 的 K 线跑守卫链（防前视偏差，§9.4.3），模拟阶段才允许看 T 之后；
- **跨日状态**：阶段防抖（连续 N 日才切换）与「上一日趋势/建议」必须**顺着日子累积**，
  否则每天都是「第一天」，防抖规则形同虚设；
- **可中断**：几百个交易日 × 十几只标的是分钟级任务，必须能断点续跑、能看进度；
- **规模护栏**：参数网格容易爆炸，一次运行只允许一套参数，并可用 ``max_days`` 截断。

## 口径（每条都能在需求里找到出处）

- **不用未来数据**：守卫链输入截到 T 日收盘；T 日 K 线尚未发布（`bars[-1].trade_date != T`）
  的日子**跳过该标的**并在报告里计数，绝不拿 T-1 的结论冒充 T 日（§8.3 不推测）。
- **回合口径不重写**：成交价、跳空、T+1、同日先止损、未闭合不计分母、样本 ≥30 才给胜率，
  全部沿用 :func:`src.assistant.backtest.simulate_rounds` / `summarize_rounds`。
- **出场价来自计划结构**：计划分支本身不带止损/止盈（§5.11 的失效条件是文字），因此回放
  按**当日关键价位**取「入场价下方最近的支撑」作止损、「上方最近的阻力」作止盈（§5.3 结构
  破坏口径）；取不到就只剩 `max_hold_days` 强制平仓，并在 `exit_rules_missing` 里计数。
- **不建模组合联动**：持仓视图是固定的（不随回放中的成交变化），资金/股数按配置的
  一次算定；这会让「资金耗尽后不再买」这类约束缺失，已写进 ``not_modelled``。
- **大盘缺失照实说**：没注入指数序列时 `market=None`，M1 环境档与「大盘 ≤ 警示」不会触发，
  报告里用 `market_missing_days` 明写，避免把偏乐观的结果当成真实表现。

## 不做的

参数网格扫描、多参数并行、组合层资金曲线、分红、做 T、分级止损多档——这些是后续扩展；
报告里的 ``not_modelled`` 会逐条列出，不假装覆盖。
"""

from __future__ import annotations

import json
import logging
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from src.assistant.backtest import PlanRound, RoundConfig, simulate_rounds, summarize_rounds
from src.assistant.config.overrides import ParamSet, default_param_set
from src.assistant.data.dividend import DividendEvent, apply_total_return
from src.assistant.data.valuation import ValuationRow, valuation_view
from src.assistant.domain import Bar, MarketView, PortfolioView, PositionView, SectorView, StockView
from src.assistant.engine.guard_chain import GuardChainInput, run_guard_chain
from src.assistant.engine.market import market_line_from_bars
from src.assistant.engine.sector import (
    SectorStrengthParams,
    STRENGTH_STRONG,
    STRENGTH_WEAK,
    sector_strength_from_bars,
)
from src.assistant.engine.stage import StageDebounceState
from src.assistant.enums import Action, SourceTag
from src.assistant.versioning import RuleFingerprint

__all__ = [
    "ReplayConfig",
    "ReplayResult",
    "ReplayState",
    "SectorSeries",
    "load_state",
    "market_views_from_index",
    "replay",
    "save_state",
    "sector_view_for_day",
]

logger = logging.getLogger(__name__)

#: 买入方向动作；与 `backtest._BUY_ACTIONS` 同口径（卖出分支不作为回合起点）
_BUY_ACTIONS = frozenset({Action.BUY, Action.ADD, Action.PROBE})

#: 快照生成时刻固定为收盘后（守卫链只把它写进结果，不参与判定）
_GENERATED_AT = time(20, 0)


def market_views_from_index(
    index_bars: Sequence[Bar],
    days: Sequence[date],
    *,
    symbol: str = "sh000001",
    index_name: str = "上证指数",
    min_bars: int = 30,
) -> dict[date, MarketView]:
    """按交易日算 M1 环境档，构造 ``{日期: MarketView}`` 供回放注入。

    - **不碰未来**：每个 T 只用 ``trade_date <= T`` 的指数日线，不补点、不平滑。
    - **不拿旧档冒充当日**：T 当天没有指数 K 线就不给（不能用上一交易日的档位顶上），
      与个股「T 日无 K 线即跳过」同一条纪律。
    - **算不出就不放**：数据不足 ``min_bars`` 根、或两因子都取不到有效档（落缓冲带且往前
      也找不到有效档）时**不往字典里放**，由回放层如实记成「大盘缺失」，不猜档位。
    - **宽度留空**：涨跌家数的历史序列回放拿不到，``advance_ratio=None``、
      ``width_degraded=True``，对应的「市场宽度恶化」风险项因此不会触发（写进 `not_modelled`）。
    """
    out: dict[date, MarketView] = {}
    ordered = sorted(index_bars, key=lambda bar: bar.trade_date)
    for day in days:
        upto = [bar for bar in ordered if bar.trade_date <= day]
        if len(upto) < min_bars or upto[-1].trade_date != day:
            continue
        line = market_line_from_bars(upto, index_name=index_name)
        if not line.available:
            continue
        out[day] = MarketView(
            index_code=symbol,
            index_name=line.index_name or index_name,
            pct_change=line.pct_change or 0.0,
            regime=line.regime,
            regime_degraded=False,
            advance_ratio=None,
            width_degraded=True,
        )
    return out


@dataclass(frozen=True)
class SectorSeries:
    """一个板块的指数日线 + 逐日涨跌，供回放按日切片（一次建索引，避免每天重扫）。"""

    name: str
    code: str
    bars: tuple[Bar, ...]
    pct: Mapping[date, float]
    dates: tuple[date, ...]

    @classmethod
    def build(cls, name: str, code: str, bars: Sequence[Bar]) -> "SectorSeries":
        ordered = tuple(sorted(bars, key=lambda bar: bar.trade_date))
        return cls(
            name=name,
            code=code,
            bars=ordered,
            pct=_pct_by_day(ordered),
            dates=tuple(bar.trade_date for bar in ordered),
        )

    def upto(self, day: date) -> tuple[Bar, ...]:
        """``trade_date <= day`` 的前缀（回放不允许看未来，§9.4.3）。"""
        return self.bars[: bisect_right(self.dates, day)]


def _pct_by_day(bars: Sequence[Bar]) -> dict[date, float]:
    """逐日涨跌幅（百分点）；前一根收盘非正的日子直接不给值，不插值。"""
    out: dict[date, float] = {}
    for prev, cur in zip(bars, bars[1:]):
        if prev.close > 0:
            out[cur.trade_date] = (cur.close / prev.close - 1) * 100
    return out


def sector_view_for_day(
    series: SectorSeries,
    day: date,
    index_pct: Mapping[date, float],
    *,
    depth: int = 10,
    params: SectorStrengthParams | None = None,
) -> SectorView | None:
    """板块序列 → 当日 ``SectorView``（三档 + 当日 rs_t + 近 N 日相对强度）。

    与实盘 :meth:`AssistantPipeline._sector_context` 同口径，两处差异如实写在这里：

    1. **当日涨跌幅由序列自算**：回放没有板块一览的历史，``pct_change`` 只能取板块指数
       「今收 / 昨收 − 1」；实盘用的是板块一览的当日涨跌幅。两者通常一致，尾差来自
       指数与一览的取整/口径，报告里单独注明。
    2. **当日序列缺失就不给 rs_t**：板块指数若还停在 T−1（实盘见过晚一天发布），
       ``pct_change`` 与 ``rs_t`` 都留空、``degraded=True``，**不拿昨天的差冒充今天**；
       三档仍按 T−1 的序列判（§5.10：降级只注记 + 降置信度，不改档）。
    """
    upto = series.upto(day)
    if not upto:
        return None
    pct_today = series.pct.get(day)
    strength = sector_strength_from_bars(
        upto,
        pct_change=pct_today,
        market_pct=index_pct.get(day),
        as_of_day=day,
        params=params,
    )
    history: list[float] = []
    for bar_day in series.dates[: len(upto)][-depth:]:
        base = index_pct.get(bar_day)
        if base is None or bar_day not in series.pct:
            continue
        history.append(round(series.pct[bar_day] - base, 4))
    if history and strength.rs_t is not None:
        # 末位用当日 rs_t 覆盖：它已按「当日板块涨跌 − 当日大盘涨跌」算过，口径同实盘
        history[-1] = round(float(strength.rs_t), 4)
    return SectorView(
        sector_code=series.code,
        sector_name=series.name,
        # 当日板块 K 线未出时这里只能是 0：真正判档与判风险都不用 pct_change，不会误判成「平盘」
        pct_change=float(pct_today or 0.0),
        strength=strength.strength,
        rs_t=strength.rs_t,
        history_rs_t=tuple(history),
        degraded=strength.degraded,
    )


@dataclass(frozen=True)
class ReplayConfig:
    """一次回放的配置。

    :param lookback_bars: 每天喂给守卫链的最大 K 线根数（够算 MA60 + 分位即可，别把十年数据塞进去）
    :param min_bars: 少于这个根数不判（指标不足时的结论没有意义）
    :param params: 参数集；**一次运行只允许一套**，参数网格请多次调用（避免组合爆炸）
    :param portfolio: 固定的持仓视图；回放不追踪成交后的资金变化
    :param round_config: 回合模拟参数（成本、最长持有、样本门槛）
    :param max_days: 规模护栏，超过就截断并在报告里标 ``truncated``
    :param progress_every: 每处理多少个交易日打一条进度日志
    :param checkpoint_every: 每多少个交易日写一次检查点
    """

    lookback_bars: int = 260
    min_bars: int = 60
    params: ParamSet = field(default_factory=default_param_set)
    portfolio: PortfolioView = field(
        default_factory=lambda: PortfolioView(
            total_capital=500_000.0, cash_available=400_000.0, holding_market_value=0.0
        )
    )
    round_config: RoundConfig = field(default_factory=RoundConfig)
    #: 回放起点就持有的标的与股数（不填即全部按「未持仓」处理，建议会偏「等待买点」）
    holdings: Mapping[str, int] = field(default_factory=dict)
    #: 持仓成本（缺省用当日收盘，仅影响盈亏展示，不影响守卫链判定）
    cost_basis: Mapping[str, float] = field(default_factory=dict)
    #: 回放用的数据来源标签：库里看不出原始来源，默认 D1；只有 D3 会影响判定，
    #: 因此 D1/D2 在此等价。若明确知道历史数据曾降级，传 D3 让风险项如实触发。
    source_tag: SourceTag = SourceTag.D1
    #: 价格口径：``raw`` = 不复权（与 L1 一致）；``total_return`` = 含分红再投
    #: （前复权口径，除权日不再凭空下跌，股息按除权日价格再投）
    adjust: str = "raw"
    #: 红利税率：0 = 税前全额；持股 1 个月–1 年按 10%。只影响 ``total_return`` 口径
    dividend_tax_rate: float = 0.0
    max_days: int | None = None
    progress_every: int = 20
    checkpoint_every: int = 20

    def __post_init__(self) -> None:
        if self.lookback_bars < self.min_bars:
            raise ValueError("lookback_bars 不能小于 min_bars")
        if self.min_bars < 2:
            raise ValueError("min_bars 至少为 2")
        if self.adjust not in ("raw", "total_return"):
            raise ValueError("adjust 只能是 raw 或 total_return")
        if not 0 <= self.dividend_tax_rate < 1:
            raise ValueError("dividend_tax_rate 必须落在 [0, 1)")

    def position(self, code: str, last_close: float = 0.0) -> PositionView:
        """起点持仓视图；没登记的标的一律按未持仓（不猜测）。"""
        shares = int(self.holdings.get(code) or 0)
        if shares <= 0:
            return PositionView()
        basis = float(self.cost_basis.get(code) or 0.0)
        return PositionView(shares=shares, cost_basis=basis if basis > 0 else last_close)


@dataclass
class ReplayState:
    """可续跑的回放状态（检查点就是它的 JSON 形态）。"""

    last_day: date | None = None
    days_done: int = 0
    rounds: list[PlanRound] = field(default_factory=list)
    stages: dict[str, StageDebounceState] = field(default_factory=dict)
    previous: dict[str, tuple[str, str]] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)

    def bump(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n


@dataclass(frozen=True)
class ReplayResult:
    """回放结果：回合 + 统计 + 明确的降级计数。"""

    days: int
    days_skipped: int
    plans: int
    rounds: tuple[PlanRound, ...]
    stats: Mapping[str, Any]
    counters: Mapping[str, int]
    truncated: bool = False
    checkpoint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "days_skipped": self.days_skipped,
            "plans": self.plans,
            "truncated": self.truncated,
            "checkpoint": self.checkpoint,
            "counters": dict(self.counters),
            "rounds": [_round_to_dict(row) for row in self.rounds],
            "stats": dict(self.stats),
        }


def _round_to_dict(row: PlanRound) -> dict[str, Any]:
    return {
        "code": row.code,
        "branch_id": row.branch_id,
        "scenario": row.scenario,
        "stage": row.stage,
        "action": row.action,
        "shares": row.shares,
        "entry_date": row.entry_date.isoformat() if row.entry_date else None,
        "entry_price": row.entry_price,
        "exit_date": row.exit_date.isoformat() if row.exit_date else None,
        "exit_price": row.exit_price,
        "exit_reason": row.exit_reason,
        "days_held": row.days_held,
        "gross_pnl": row.gross_pnl,
        "cost": row.cost,
        "net_pnl": row.net_pnl,
        "closed": row.closed,
        "note": row.note,
    }


def _round_from_dict(raw: Mapping[str, Any]) -> PlanRound:
    def _day(value: Any) -> date | None:
        return date.fromisoformat(value) if isinstance(value, str) and value else None

    return PlanRound(
        code=str(raw.get("code") or ""),
        branch_id=str(raw.get("branch_id") or ""),
        scenario=str(raw.get("scenario") or ""),
        stage=str(raw.get("stage") or ""),
        action=str(raw.get("action") or ""),
        shares=int(raw.get("shares") or 0),
        entry_date=_day(raw.get("entry_date")),
        entry_price=raw.get("entry_price"),
        exit_date=_day(raw.get("exit_date")),
        exit_price=raw.get("exit_price"),
        exit_reason=str(raw.get("exit_reason") or ""),
        days_held=int(raw.get("days_held") or 0),
        gross_pnl=float(raw.get("gross_pnl") or 0.0),
        cost=float(raw.get("cost") or 0.0),
        net_pnl=float(raw.get("net_pnl") or 0.0),
        closed=bool(raw.get("closed")),
        note=str(raw.get("note") or ""),
    )


def _stage_to_dict(state: StageDebounceState) -> dict[str, Any]:
    return {
        "stage": state.stage.value if state.stage else None,
        "pending_stage": state.pending_stage.value if state.pending_stage else None,
        "pending_days": state.pending_days,
        "as_of": state.as_of.isoformat() if state.as_of else None,
    }


def _stage_from_dict(raw: Mapping[str, Any]) -> StageDebounceState:
    from src.assistant.enums import Stage  # noqa: PLC0415 - 仅反序列化时需要

    def _stage(value: Any) -> Stage | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            return Stage(value)
        except ValueError:
            return None

    as_of = raw.get("as_of")
    return StageDebounceState(
        stage=_stage(raw.get("stage")),
        pending_stage=_stage(raw.get("pending_stage")),
        pending_days=int(raw.get("pending_days") or 0),
        as_of=date.fromisoformat(as_of) if isinstance(as_of, str) and as_of else None,
    )


def save_state(state: ReplayState, path: str | Path) -> Path:
    """把检查点写成 JSON（原子替换，避免中断后拿到半个文件）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "last_day": state.last_day.isoformat() if state.last_day else None,
        "days_done": state.days_done,
        "counters": state.counters,
        "rounds": [_round_to_dict(row) for row in state.rounds],
        "stages": {code: _stage_to_dict(value) for code, value in state.stages.items()},
        "previous": {code: list(pair) for code, pair in state.previous.items()},
    }
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    tmp.replace(target)
    return target


def load_state(path: str | Path) -> ReplayState | None:
    """读检查点；坏文件按「没有检查点」处理（留日志，不静默丢数据）。"""
    source = Path(path)
    if not source.is_file():
        return None
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("回放检查点读不出来，按从头开始处理：%s", exc)
        return None
    if not isinstance(raw, dict):
        return None
    last = raw.get("last_day")
    return ReplayState(
        last_day=date.fromisoformat(last) if isinstance(last, str) and last else None,
        days_done=int(raw.get("days_done") or 0),
        rounds=[_round_from_dict(item) for item in raw.get("rounds") or [] if isinstance(item, dict)],
        stages={
            str(code): _stage_from_dict(value)
            for code, value in (raw.get("stages") or {}).items()
            if isinstance(value, dict)
        },
        previous={
            str(code): (str(pair[0]), str(pair[1]))
            for code, pair in (raw.get("previous") or {}).items()
            if isinstance(pair, (list, tuple)) and len(pair) == 2
        },
        counters={str(k): int(v) for k, v in (raw.get("counters") or {}).items()},
    )


# ---------------------------------------------------------------------------
# 计划 → 回合入参
# ---------------------------------------------------------------------------
def _exit_levels(result: Any, entry_price: float) -> tuple[float | None, float | None]:
    """按当日关键价位给出场价：最近支撑作止损、最近阻力作止盈（§5.3 结构破坏口径）。"""
    supports = [lvl.price for lvl in result.key_levels if lvl.kind == "support" and lvl.price < entry_price]
    resistances = [
        lvl.price for lvl in result.key_levels if lvl.kind == "resistance" and lvl.price > entry_price
    ]
    return (max(supports) if supports else None, min(resistances) if resistances else None)


def _plan_rows(result: Any, day: date) -> list[dict[str, Any]]:
    """把一个标的当日结论里的买入分支摊平成 ``simulate_rounds`` 的入参。"""
    rows: list[dict[str, Any]] = []
    for branch in result.plans:
        if branch.action not in _BUY_ACTIONS:
            continue
        entry = branch.trigger_price
        stop_loss, take_profit = (None, None)
        if isinstance(entry, (int, float)) and entry > 0:
            stop_loss, take_profit = _exit_levels(result, float(entry))
        rows.append(
            {
                "code": result.code,
                "branch_id": branch.branch_id,
                "date": day,
                "trigger_type": branch.trigger_type.value,
                "trigger_price": branch.trigger_price,
                "pct_change": branch.pct_change,
                "action": branch.action.value,
                "shares": branch.shares,
                "valid_days": branch.valid_days,
                "scenario": branch.scenario,
                "stage": result.stage_result.stage.value,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            }
        )
    return rows


def _drop_overlaps(rounds: Iterable[PlanRound], taken: list[PlanRound]) -> tuple[list[PlanRound], int]:
    """**同一标的**已有未平仓回合时，后面的触发不再建仓（组合层约束的保守近似）。

    返回 ``(可接受的新回合, 被跳过的条数)``。按入场日排序，逐条与**该标的**已接受的回合
    比较时间区间，重叠即判「当时已有持仓」，不重复计一次暴露。

    注意必须按 ``code`` 分组：不同标的各占各的仓位，早年实现漏了这个过滤，
    结果一只标的的持仓会把整个组合的后续入场全部挡掉（10 只 × 460 天只成交 10 笔）。
    """
    accepted: list[PlanRound] = []
    dropped = 0
    for row in sorted(rounds, key=lambda r: (r.entry_date or date.max)):
        if row.entry_date is None:  # 未触发/卖出分支：不影响仓位，原样保留
            accepted.append(row)
            continue
        same_code = [other for other in list(taken) + accepted if other.code == row.code]
        busy = any(
            other.entry_date
            and other.closed
            and other.exit_date
            and other.entry_date <= row.entry_date <= other.exit_date
            for other in same_code
        ) or any(
            other.entry_date and not other.closed and other.entry_date <= row.entry_date
            for other in same_code
        )
        if busy:
            dropped += 1
            continue
        accepted.append(row)
    return accepted, dropped


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def replay(
    *,
    days: Sequence[date],
    bars_by_code: Mapping[str, Sequence[Bar]],
    config: ReplayConfig | None = None,
    market_by_day: Mapping[date, MarketView] | None = None,
    dividend_events: Mapping[str, Sequence[DividendEvent]] | None = None,
    sector_by_code: Mapping[str, str] | None = None,
    sector_bars_by_name: Mapping[str, Sequence[Bar]] | None = None,
    sector_code_by_name: Mapping[str, str] | None = None,
    index_bars: Sequence[Bar] | None = None,
    valuations_by_code: Mapping[str, Sequence[ValuationRow]] | None = None,
    state: ReplayState | None = None,
    checkpoint_path: str | Path | None = None,
) -> ReplayResult:
    """在给定交易日上逐日回放守卫链并模拟回合。

    ``days`` 由调用方给（交易日历或库内 K 线日期都行），模块不自己猜；``bars_by_code``
    是**完整历史**（守卫链每天自己截到 T，模拟阶段要用 T 之后的行情）。
    传入 ``state`` 可续跑；给 ``checkpoint_path`` 会按 ``checkpoint_every`` 落盘。

    ``dividend_events`` 只在 ``config.adjust == "total_return"`` 时生效：把不复权序列
    折算成含分红序列（前复权），这样「除权日掉价」不再被当成下跌，股息也不会漏算。

    ``sector_by_code`` / ``sector_bars_by_name`` 把板块层接进回放：前者是「代码 → 板块名」，
    后者是「板块名 → 板块指数日线」。给了之后每天按 ≤T 的板块序列复算三档与 rs_t，
    评分里的 ``sector_adj_up/down`` 与风险里的 SEC-1 才真正会动——在此之前板块分恒为 0。
    ``index_bars`` 只用于算「大盘当日涨跌」（rs_t 的减项），缺省时退回 ``market_by_day``
    的涨跌幅（M1 档算不出来、没进字典的日子就没有减项）。

    ``valuations_by_code`` 是 L1 估值行（PE/PB + 当日分位），``dividend_events`` 同时兼作
    分红事实的输入（连续年数 / 是否中断 / 股息率）；两者合起来填 ``ValuationView``，
    ``w_valuation``/``w_dividend`` 与 DIV-1/2/3 才不再是空转。分位在落库时已按
    「只用 ≤该日的样本」算好，回放按日期取最近一条，**不做前视**。
    """
    cfg = config or ReplayConfig()
    work = state or ReplayState()
    ordered = sorted({day for day in days})
    codes = sorted({code for code, bars in bars_by_code.items() if bars})
    if not codes:
        raise ValueError("没有可用 K 线，无法回放")

    history: dict[str, list[Bar]] = {
        code: sorted(bars_by_code[code], key=lambda bar: bar.trade_date) for code in codes
    }
    dividend_applied = dividend_skipped = dividend_missing = 0
    dividend_cash = 0.0
    if cfg.adjust == "total_return":
        events_map = dividend_events or {}
        for code in codes:
            events = list(events_map.get(code) or ())
            if not events:
                dividend_missing += 1
                continue
            outcome = apply_total_return(history[code], events, tax_rate=cfg.dividend_tax_rate)
            history[code] = list(outcome.bars)
            dividend_applied += outcome.applied
            dividend_skipped += outcome.skipped
            dividend_cash += outcome.cash_per_share
    calc_version = RuleFingerprint(param_hash=cfg.params.param_hash).calc_version()
    if cfg.adjust == "total_return":
        work.bump("dividend_events_applied", dividend_applied)
        work.bump("dividend_events_skipped", dividend_skipped)
        work.bump("dividend_codes_without_events", dividend_missing)

    # ---- 板块层：一次建索引，避免每天重扫整段板块历史 -------------------
    sector_map = {
        str(code): str(name) for code, name in (sector_by_code or {}).items() if name
    }
    sector_codes = {str(name): str(code) for name, code in (sector_code_by_name or {}).items()}
    series_by_name: dict[str, SectorSeries] = {}
    for name, bars in (sector_bars_by_name or {}).items():
        if bars:
            series_by_name[str(name)] = SectorSeries.build(
                str(name), sector_codes.get(str(name), ""), bars
            )
    series_of_code = {
        code: series_by_name[name] for code, name in sector_map.items() if name in series_by_name
    }
    sector_layer = bool(sector_map)
    if index_bars:
        # rs_t 的减项用指数**原始日线**算：不依赖 M1 档位算得出来（档位落在缓冲带时
        # market_by_day 里那天是缺的，但「大盘涨跌」仍然有值，SEC-1 不该因此断档）
        index_pct = _pct_by_day(sorted(index_bars, key=lambda bar: bar.trade_date))
    else:
        index_pct = {day: view.pct_change for day, view in (market_by_day or {}).items()}
    sector_view_cache: dict[tuple[str, date], SectorView | None] = {}

    # ---- 估值与分红事实：按日期建索引，每天只做一次 bisect ----------------
    valuation_series: dict[str, tuple[tuple[date, ...], tuple[ValuationRow, ...]]] = {}
    for code, rows in (valuations_by_code or {}).items():
        # 变量名别用 ordered：外层 ordered 是回放交易日列表，覆盖了会让循环拿到估值行
        series_rows = tuple(sorted(rows, key=lambda row: row.trade_date))
        if series_rows:
            valuation_series[code] = (
                tuple(row.trade_date for row in series_rows),
                series_rows,
            )
    events_by_code = {code: tuple(items) for code, items in (dividend_events or {}).items()}

    processed = 0
    plans_total = 0
    truncated = False
    for position, day in enumerate(ordered):
        if work.last_day is not None and day <= work.last_day:
            continue
        if cfg.max_days is not None and processed >= cfg.max_days:
            truncated = True
            break

        day_plans: list[dict[str, Any]] = []
        ran = 0
        for code in codes:
            bars = history[code]
            upto = [bar for bar in bars if bar.trade_date <= day]
            if len(upto) < cfg.min_bars:
                work.bump("skipped_insufficient_bars")
                continue
            if upto[-1].trade_date != day:
                # T 日 K 线还没出（停牌/数据滞后）：不拿旧结论冒充当日结论
                work.bump("skipped_stale_bars")
                continue
            window = upto[-cfg.lookback_bars :]
            trend, advice = work.previous.get(code, ("", ""))
            valuation_row: ValuationRow | None = None
            series_meta = valuation_series.get(code)
            if series_meta is not None:
                idx = bisect_right(series_meta[0], day) - 1
                if idx >= 0:
                    valuation_row = series_meta[1][idx]
            view = StockView(
                code=code,
                name=code,
                bars=window,
                position=cfg.position(code, window[-1].close),
                source_tag=cfg.source_tag,
                valuation=valuation_view(
                    [valuation_row] if valuation_row is not None else [],
                    events_by_code.get(code, ()),
                    day,
                    window[-1].close,
                ),
            )
            sector: SectorView | None = None
            series = series_of_code.get(code)
            if series is not None:
                key = (series.name, day)
                if key not in sector_view_cache:
                    sector_view_cache[key] = sector_view_for_day(series, day, index_pct)
                sector = sector_view_cache[key]
            if sector_layer or series_by_name:
                if sector is None:
                    work.bump("sector_missing_code_days")
                else:
                    work.bump("sector_view_days")
                    if sector.degraded:
                        work.bump("sector_degraded_days")
                    if sector.rs_t is None:
                        work.bump("sector_rs_missing_days")
                    if sector.strength == STRENGTH_STRONG:
                        work.bump("sector_strong_days")
                    elif sector.strength == STRENGTH_WEAK:
                        work.bump("sector_weak_days")
            outcome = run_guard_chain(
                GuardChainInput(
                    view=view,
                    portfolio=cfg.portfolio,
                    params=cfg.params,
                    calc_version=calc_version,
                    generated_at=datetime.combine(day, _GENERATED_AT),
                    market=(market_by_day or {}).get(day),
                    # 板块区没接入时保持「只注记」的老行为；接入后按真实映射标，
                    # 没映射上才出 SEC-3「映射失败」（与实盘 pipeline 同一条口径）
                    sector=sector,
                    sector_mapped=True if not sector_layer else code in sector_map,
                    expected_data_date=day,
                    trading_days=ordered[: position + 1],
                    stage_state=work.stages.get(code, StageDebounceState()),
                    previous_trend=trend or None,
                    previous_advice=Action(advice) if advice else None,
                )
            )
            work.stages[code] = outcome.stage_state
            work.previous[code] = (
                outcome.result.conclusion.trend,
                outcome.result.conclusion.advice.value if outcome.result.conclusion.advice else "",
            )
            work.bump("guard_runs")
            ran += 1
            day_plans.extend(_plan_rows(outcome.result, day))

        if ran and not (market_by_day or {}).get(day):
            # 只统计「真正跑过守卫链」的日子：没跑成的日子不算大盘缺失
            work.bump("market_missing_days")
        if not ran:
            work.bump("skipped_days")

        if day_plans:
            simulated = simulate_rounds(day_plans, history, config=cfg.round_config)
            accepted, dropped = _drop_overlaps(simulated, work.rounds)
            if dropped:
                work.bump("overlap_skipped", dropped)
            work.rounds.extend(accepted)
            plans_total += len(day_plans)
            missing = sum(1 for plan in day_plans if plan.get("stop_loss") is None)
            if missing:
                work.bump("exit_rules_missing", missing)

        if ran:
            processed += 1  # 一个标的都没跑成的日子不算「已回放」，但仍要推进 last_day 以便续跑
        work.days_done += 1
        work.last_day = day
        if checkpoint_path and processed % max(1, cfg.checkpoint_every) == 0:
            save_state(work, checkpoint_path)
        if cfg.progress_every and processed % cfg.progress_every == 0:
            logger.info(
                "回放进度：%s（第 %d 个交易日），累计计划 %d 条、回合 %d 个",
                day.isoformat(),
                processed,
                plans_total,
                len(work.rounds),
            )

    checkpoint = str(save_state(work, checkpoint_path)) if checkpoint_path else ""
    stats = summarize_rounds(work.rounds, config=cfg.round_config)
    # 把「未触发」从「未平仓」里拆出来：前者根本没建仓，混在一起会让 open 数字失真
    untriggered = sum(1 for row in work.rounds if row.entry_date is None)
    stats["triggered"] = len(work.rounds) - untriggered
    stats["untriggered"] = untriggered
    stats["open_positions"] = len(work.rounds) - int(stats["closed"]) - untriggered
    modeled = list(stats.get("not_modelled") or [])
    if cfg.adjust == "total_return":
        # 分红一旦进模型，就必须把「已建模」的和「仍未建模」的分开说清楚：
        # 不做这一步，报告会继续挂着「分红再投未建模」，与结果自相矛盾。
        modeled = [item for item in modeled if not item.startswith("分红再投")]
        if cfg.dividend_tax_rate > 0:
            modeled.append(
                f"分红按除权日再投、按 {cfg.dividend_tax_rate:.0%} 红利税一次性扣除"
                "（实际按持有期差别化：≤1 月 20%、1 月–1 年 10%、>1 年免征）"
            )
        else:
            modeled.append("分红按除权日再投、税前全额（未计红利税，高估股息收益）")
        if dividend_skipped:
            modeled.append(f"{dividend_skipped} 次除权事件因缺前一交易日 K 线未能折算")
        if dividend_missing:
            modeled.append(f"{dividend_missing} 只标的没有分红事件记录（按无分红处理，可能低估）")
    if sector_layer:
        unmapped = sorted(set(codes) - set(sector_map))
        modeled.append(
            "板块当日涨跌幅由板块指数序列自算（实盘用板块一览的当日涨跌幅），两者可能有尾差"
        )
        modeled.append(
            "板块 2×2 阈值（20 日线 / 5 日斜率 / 0.5% 缓冲带）用默认值，尚未进参数注册表"
        )
        if unmapped:
            modeled.append(
                f"{len(unmapped)} 只未映射到板块，板块分恒 0：{', '.join(unmapped)}"
            )
    if valuation_series or events_by_code:
        modeled.append(
            "估值分位按 ≤T 的样本滚动计算（样本 <500 根不给分位）；PE≤0/PB≤0 不参与分位"
        )
        modeled.append("股息率为税前 TTM（近 365 天每股现金分红 ÷ 收盘价），未扣红利税")
    stats["sector"] = {
        "enabled": sector_layer,
        "mapped_codes": len(set(codes) & set(sector_map)),
        "unmapped_codes": sorted(set(codes) - set(sector_map)) if sector_layer else [],
        "sectors": sorted(series_by_name) if sector_layer else [],
        # 序列覆盖随数据补齐会变，而 calc_version 不会：不记下来，同一版本号的两次回放
        # 结果对不上时无从判断是规则改了还是数据补了
        "series": {
            name: [len(series.bars), series.bars[0].trade_date.isoformat(),
                   series.bars[-1].trade_date.isoformat()]
            for name, series in sorted(series_by_name.items())
        } if sector_layer else {},
        "view_code_days": work.counters.get("sector_view_days", 0),
        "missing_code_days": work.counters.get("sector_missing_code_days", 0),
        "degraded_code_days": work.counters.get("sector_degraded_days", 0),
        "strong_code_days": work.counters.get("sector_strong_days", 0),
        "weak_code_days": work.counters.get("sector_weak_days", 0),
    }
    stats["valuation"] = {
        "codes_with_rows": sum(1 for code in codes if code in valuation_series),
        "codes_with_events": sum(1 for code in codes if events_by_code.get(code)),
    }
    stats["not_modelled"] = modeled + [
        "组合层资金曲线与「资金耗尽不再买」约束",
        "大盘缺失日：M1 环境档与「大盘 ≤ 警示」风险不触发（见 market_missing_days）",
    ]
    stats["adjust"] = cfg.adjust
    stats["dividend_tax_rate"] = cfg.dividend_tax_rate
    if cfg.adjust == "total_return":
        stats["dividend_events_applied"] = dividend_applied
        stats["dividend_cash_per_share_total"] = round(dividend_cash, 6)
    stats["calc_version"] = calc_version
    stats["trading_days_replayed"] = processed
    stats["plans"] = plans_total
    return ReplayResult(
        days=processed,
        days_skipped=work.counters.get("skipped_days", 0),
        plans=plans_total,
        rounds=tuple(work.rounds),
        stats=stats,
        counters=dict(work.counters),
        truncated=truncated,
        checkpoint=checkpoint,
    )
