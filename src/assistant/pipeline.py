# -*- coding: utf-8 -*-
"""装配层：取数 → 守卫链 → 快照落库 → 渲染（技术方案 §2.1 / §2.3）。

一条 ``ensure_snapshot(day)`` 走完全链路，并保证三件事：

1. **幂等**：同日 + 同 ``param_hash`` + 同 ``calc_version`` 已有快照就**直接复用**，
   不重算。这既是 B50 ③「当日快照只生成一次」的实现，也是进程重启后不重算的依据。
2. **可复现**：投递正文**一律从库里回读后再渲染**（不是拿内存里的结果直接拼），
   所以「首次生成」与「后来复用」得到的文本逐字相同——同源不靠自觉，靠结构保证。
3. **不造数据**：某只标的取数失败且无缓存时**跳过该标的并记录原因**，不填默认值；
   全部标的都拿不到数据则抛错，由上层决定「这次不产出」。

阶段防抖状态在每次运行后写回 ``assistant_stage_state``：只在内存里的话，进程每天
重启都算「首次判定 → 直接确认」，连续 N 日防抖等于不存在。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Mapping, Optional, Sequence

from src.assistant.config.overrides import ParamSet
from src.assistant.data.storage import AssistantStore
from src.assistant.domain import (
    Bar,
    PortfolioView,
    PositionView,
    StockView,
)
from src.assistant.engine.guard_chain import GuardChainInput, run_guard_chain
from src.assistant.engine.stage import StageDebounceState
from src.assistant.enums import Preset, SourceTag, Stage
from src.assistant.render.feishu import MarketLine, fit_feishu
from src.assistant.render.review import PlanReview, build_plan_review
from src.assistant.scheduling.delivery import DeliveryPayload
from src.assistant.versioning import RuleFingerprint

__all__ = [
    "AssistantPipeline",
    "PipelineError",
    "PipelineSettings",
    "SnapshotOutcome",
    "render_summary",
]

DEFAULT_HISTORY_YEARS = 3
DEFAULT_INDEX_CODE = "sh000001"


class PipelineError(RuntimeError):
    """装配链无法产出（无标的 / 全部取数失败 / 全部数据不足）。"""


@dataclass(frozen=True)
class PipelineSettings:
    """装配层运行参数（**资金类不进三档预设**，属用户填写的固定金额）。"""

    total_capital: float
    cash_available: float | None = None
    preset: Preset = Preset.STANDARD
    index_code: str = DEFAULT_INDEX_CODE
    history_years: int = DEFAULT_HISTORY_YEARS

    def __post_init__(self) -> None:
        if self.total_capital <= 0:
            raise ValueError("total_capital 必须为正数（需求 §5.6：总资金为用户填写的固定金额）")
        if self.cash_available is not None and self.cash_available < 0:
            raise ValueError("cash_available 不能为负")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> PipelineSettings:
        """从环境变量读资金与预设；**缺总资金直接报错，不猜默认值**。"""
        raw_total = (env.get("ASSISTANT_TOTAL_CAPITAL") or "").strip()
        if not raw_total:
            raise PipelineError(
                "缺少 ASSISTANT_TOTAL_CAPITAL：仓位计算依赖用户填写的总资金，"
                "没有它无法给出股数与占比（不提供默认值，避免算出看似合理的错数）"
            )
        try:
            total = float(raw_total)
        except ValueError as exc:
            raise PipelineError(f"ASSISTANT_TOTAL_CAPITAL={raw_total!r} 不是数字") from exc

        raw_cash = (env.get("ASSISTANT_CASH_AVAILABLE") or "").strip()
        cash: float | None = None
        if raw_cash:
            try:
                cash = float(raw_cash)
            except ValueError as exc:
                raise PipelineError(f"ASSISTANT_CASH_AVAILABLE={raw_cash!r} 不是数字") from exc

        raw_preset = (env.get("ASSISTANT_PARAM_PRESET") or "").strip().lower()
        preset = Preset.STANDARD
        if raw_preset:
            try:
                preset = Preset(raw_preset)
            except ValueError as exc:
                raise PipelineError(
                    f"ASSISTANT_PARAM_PRESET={raw_preset!r} 非法（需 loose | standard | strict）"
                ) from exc

        return cls(
            total_capital=total,
            cash_available=cash,
            preset=preset,
            index_code=(env.get("ASSISTANT_INDEX_CODE") or DEFAULT_INDEX_CODE).strip(),
        )


@dataclass(frozen=True)
class SnapshotOutcome:
    """一次 ``ensure_snapshot`` 的结果。"""

    snapshot_id: str
    content: dict
    created: bool
    warnings: tuple[str, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()

    @property
    def bar_date(self) -> date:
        """数据截止日（可能是 T-1，收盘数据未出时）。"""
        return date.fromisoformat(self.content.get("bar_date") or self.content["data_date"])

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(row["code"] for row in self.content["conclusions"])


def render_summary(content: Mapping[str, Any]) -> str:
    """调试用紧凑摘要（**不是投递文本**，投递走 :func:`fit_feishu` 的七段渲染）。

    保留它的原因：排障时一眼看完三元组与每只标的的关键字段，比读七段快。
    """

    rows = content.get("conclusions") or []
    plans = content.get("plans") or []
    plan_count: dict[str, int] = {}
    for plan in plans:
        plan_count[plan["code"]] = plan_count.get(plan["code"], 0) + 1

    lines = [
        f"数据截止 {content.get('bar_date', content['data_date'])} 收盘（快照日 {content['data_date']}）",
        f"快照 {content['snapshot_id']} · 规则 {content['calc_version']} · 参数 {content['param_hash'][:8]}",
        f"共 {len(rows)} 只标的",
    ]
    for row in rows:
        lines.append(
            f"[{row['code']} {row['name']}] {row['stage']}/{row['stage_confidence']} "
            f"评分{row['score']:.0f} 建议：{row['advice_display'] or '—'} "
            f"风险：{row['risk_level']} 计划{plan_count.get(row['code'], 0)}条"
        )
    return "\n".join(lines)


@dataclass
class AssistantPipeline:
    """端到端装配：一只标的从「库里有没有 K 线」到「一份可投递快照」。"""

    store: AssistantStore
    settings: PipelineSettings
    fetcher: Any = None  # DailyFetcher；为 None 时只用 L1 已有数据（离线/测试）
    params: ParamSet | None = None
    names: Mapping[str, str] = field(default_factory=dict)
    #: 渲染函数，签名须为 ``render(content, *, review=None, market=None) -> str``
    #: （默认七段图标化；测试可注入简化实现，但必须接受这两个关键字）
    render: Callable[..., str] = fit_feishu
    trading_days: Optional[Callable[[date, date], Sequence[date] | None]] = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now())
    _trading_cache: dict[date, Optional[tuple[date, ...]]] = field(default_factory=dict, repr=False)
    _known_tags: dict[str, SourceTag] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def ensure_snapshot(self, day: date, *, now: datetime | None = None) -> SnapshotOutcome:
        """保证 ``day`` 有一份当日快照；已存在则复用（不重算）。"""
        params = self._params()
        calc_version = self._calc_version(params)
        existing = self.store.find_snapshot(
            day, param_hash=params.param_hash, calc_version=calc_version
        )
        if existing is not None:
            content = self.store.snapshot_content(existing["snapshot_id"])
            if content is None:  # pragma: no cover - find_snapshot 命中必有子行
                raise PipelineError(f"快照 {existing['snapshot_id']} 主子表不一致")
            return SnapshotOutcome(
                snapshot_id=existing["snapshot_id"], content=content, created=False
            )

        now = now or self.clock()
        warnings: list[str] = []
        skipped: list[tuple[str, str]] = []
        views = self._build_views(day, warnings, skipped)
        if not views:
            detail = "；".join(f"{code}: {reason}" for code, reason in skipped) or "关注清单与持仓均为空"
            raise PipelineError(f"{day} 无可用标的，本次不产出（{detail}）")

        portfolio = self._portfolio(views)
        results = []
        for view in views:
            outcome = run_guard_chain(
                GuardChainInput(
                    view=view,
                    portfolio=portfolio,
                    params=params,
                    calc_version=calc_version,
                    generated_at=now,
                    trading_days=self._trading_days(day),
                    expected_data_date=day,
                )
            )
            self._save_stage_state(view.code, outcome.stage_state)
            results.append(outcome.result)

        snapshot_id = self.store.write_day_snapshot(
            results,
            preset=self.settings.preset.value,
            generated_at=now,
            source_tag=self._worst_tag(views),
            # data_date 必须是**请求日 T**：快照幂等键与投递台账都按它分组。
            # 若沿用 GuardChainResult.data_date（= 最后一根 K 线），收盘数据未出那天
            # 快照会被记到前一交易日，同日第二次调用就会新建一份（真实数据上踩过）
            data_date=day,
            notes="" if not skipped else "跳过：" + "；".join(f"{c}({r})" for c, r in skipped),
        )
        content = self.store.snapshot_content(snapshot_id)
        if content is None:  # pragma: no cover
            raise PipelineError(f"快照 {snapshot_id} 写入后读不回")
        return SnapshotOutcome(
            snapshot_id=snapshot_id,
            content=content,
            created=True,
            warnings=tuple(warnings),
            skipped=tuple(skipped),
        )

    def payload(self, day: date, *, now: datetime | None = None) -> DeliveryPayload:
        """生成可投递载荷（调度层 ``generate`` 钩子）。"""
        outcome = self.ensure_snapshot(day, now=now)
        content = outcome.content
        return DeliveryPayload(
            data_date=day,
            bar_date=outcome.bar_date,
            snapshot_id=outcome.snapshot_id,
            calc_version=content["calc_version"],
            param_hash=content["param_hash"],
            text=self.render(content, review=self.review(day), market=self.market_line(day)),
            stale=content["source_tag"] == "D3" or content["gap_days"] > 3,
        )

    def review(self, day: date) -> PlanReview:
        """T 日复盘：拿 T-1 快照的条件计划对照 T 日行情（只读回看）。"""
        previous = self.store.previous_snapshot(day)
        if previous is None:
            return build_plan_review(None, {})
        content = self.store.snapshot_content(previous["snapshot_id"])
        if content is None:  # pragma: no cover - previous_snapshot 命中必有子行
            return build_plan_review(None, {})
        codes = {row["code"] for row in content.get("plans") or []}
        bars = {code: self.store.load_bars(code) for code in codes}
        return build_plan_review(content, bars)

    def market_line(self, day: date) -> MarketLine:
        """② 段的大盘一句。**暂未接指数取数**，如实返回「缺失」而不编造（见 D-16）。"""
        return MarketLine(available=False)

    #: 调度层直接把它当 ``generate`` 传进去
    generate = payload

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _params(self) -> ParamSet:
        if self.params is not None:
            return self.params
        # 单一真值：预设 + L4 已生效的单项覆盖
        return ParamSet.build(self.settings.preset, overrides=self.store.current_overrides())

    def _calc_version(self, params: ParamSet) -> str:
        # 战法启停清单在 §5.12 接入后填进 strategy_ids；现在为空即「全部启用的默认集」
        return RuleFingerprint(param_hash=params.param_hash).calc_version()

    def codes(self) -> tuple[str, ...]:
        """待分析标的：关注清单 ∪ 持仓（去重排序，保证快照内顺序稳定）。"""
        watch = set(self.store.effective_watchlist())
        held = set(self.store.load_holdings())
        return tuple(sorted(watch | held))

    def _build_views(
        self, day: date, warnings: list[str], skipped: list[tuple[str, str]]
    ) -> list[StockView]:
        codes = self.codes()
        if not codes:
            return []
        holdings = self.store.load_holdings()
        views: list[StockView] = []
        for code in codes:
            bars, tag = self._bars(code, day, warnings, skipped)
            if not bars:
                continue
            holding = holdings.get(code)
            position = (
                PositionView(shares=int(holding["shares"]), cost_basis=float(holding["cost_basis"]))
                if holding
                else PositionView()
            )
            views.append(
                StockView(
                    code=code,
                    name=self.names.get(code, code),
                    bars=bars,
                    position=position,
                    source_tag=tag,
                )
            )
        return views

    def _bars(
        self, code: str, day: date, warnings: list[str], skipped: list[tuple[str, str]]
    ) -> tuple[list[Bar], SourceTag]:
        """优先用 L1；缺口才取数；取数失败但有缓存则用缓存并标注降级。

        返回 ``(bars, source_tag)``——**来源必须如实记录**：主源失败降到新浪取到的数据
        不是 D1，快照与风险信号都要按实际来源标注（需求 §4.6.2）。
        """
        cached = list(self.store.load_bars(code))
        cached_tag = self._known_tags.get(code, SourceTag.D1)
        if self._fresh_enough(cached, day):
            return cached, cached_tag
        if self.fetcher is None:
            if cached:
                warnings.append(f"{code} 未配置取数器，使用 L1 缓存（可能滞后）")
                return cached, cached_tag
            skipped.append((code, "无 K 线且未配置取数器"))
            return [], cached_tag

        # 增量取数：从本地最后一条往前留 5 天余量（覆盖除权/补充修正），不必每次拉 3 年
        floor = day - timedelta(days=365 * self.settings.history_years + 30)
        start = max(floor, cached[-1].trade_date - timedelta(days=5)) if cached else floor
        try:
            result = self.fetcher.fetch_daily(code, start, day)
        except Exception as exc:  # noqa: BLE001 - 取数失败降级为「用缓存或跳过」
            if cached:
                warnings.append(f"{code} 取数失败（{type(exc).__name__}），使用 L1 缓存")
                return cached, cached_tag
            skipped.append((code, f"取数失败：{type(exc).__name__}"))
            return [], cached_tag

        bars = list(result.bars)
        if not bars:
            if cached:
                warnings.append(f"{code} 取数返回空，使用 L1 缓存")
                return cached, cached_tag
            skipped.append((code, "取数返回空"))
            return [], cached_tag
        self.store.save_bars(code, bars)
        self.store.rebuild_weekly_monthly(code)
        # 重新从 L1 回读：增量取数只拉了「最后一条往前 5 天」，直接拿它当完整历史会
        # 把 700+ 根 K 线缩成 7 根，阶段判定会掉到 S0（真实数据上踩过）
        bars = list(self.store.load_bars(code))
        tag = result.source_tag
        self._known_tags[code] = tag
        if tag is not SourceTag.D1 or result.degraded:
            warnings.append(f"{code} 实际来源 {tag.value}（主源未命中，已按实际来源标注）")
        return bars, tag

    def _fresh_enough(self, bars: Sequence[Bar], day: date) -> bool:
        if not bars:
            return False
        last = bars[-1].trade_date
        if last >= day:
            return True
        # 允许「当天数据还没出」：最后一条是上一个交易日就够，缺口交给 S0 判
        days = self._trading_days(day)
        if days:
            expected = [d for d in days if d <= day]
            if not expected:
                return True
            return last >= expected[max(0, len(expected) - 2)]
        return (day - last).days <= 3

    @staticmethod
    def _worst_tag(views: Sequence[StockView]) -> str:
        """快照级来源取**最差**：任一标的降级，快照就不能标成一手完整数据。"""
        rank = {SourceTag.D1: 1, SourceTag.D2: 2, SourceTag.D3: 3}
        worst = max(views, key=lambda view: rank.get(view.source_tag, 3), default=None)
        return (worst.source_tag if worst is not None else SourceTag.D1).value

    def _portfolio(self, views: Sequence[StockView]) -> PortfolioView:
        holding_value = sum(
            view.position.shares * view.last_close for view in views if view.position.is_holding
        )
        cash = self.settings.cash_available
        if cash is None:
            cash = max(self.settings.total_capital - holding_value, 0.0)
        return PortfolioView(
            total_capital=self.settings.total_capital,
            cash_available=cash,
            holding_market_value=holding_value,
        )

    def _stage_state(self, code: str) -> StageDebounceState:
        raw = self.store.load_stage_state(code)
        if not raw:
            return StageDebounceState()
        return StageDebounceState(
            stage=Stage(raw["stage"]) if raw.get("stage") else None,
            pending_stage=Stage(raw["pending_stage"]) if raw.get("pending_stage") else None,
            pending_days=int(raw.get("pending_days") or 0),
            as_of=date.fromisoformat(raw["as_of"]) if raw.get("as_of") else None,
        )

    def _save_stage_state(self, code: str, state: StageDebounceState) -> None:
        self.store.save_stage_state(
            code,
            stage=state.stage.value if state.stage else None,
            pending_stage=state.pending_stage.value if state.pending_stage else None,
            pending_days=int(state.pending_days),
            as_of=state.as_of,
        )

    def _trading_days(self, day: date) -> tuple[date, ...] | None:
        if day in self._trading_cache:
            return self._trading_cache[day]
        window_start = day - timedelta(days=400)
        if self.trading_days is not None:
            try:
                provided = self.trading_days(window_start, day)
            except Exception:  # noqa: BLE001
                provided = None
            result = None if provided is None else tuple(provided)
            self._trading_cache[day] = result
            return result
        try:
            from src.core.trading_calendar import get_trading_dates

            index = get_trading_dates("cn", window_start, day)
            result = None if index is None else tuple(item.date() for item in index)
        except Exception:  # noqa: BLE001 - 日历不可用时交给 S0 用自然日兜底
            result = None
        self._trading_cache[day] = result
        return result
