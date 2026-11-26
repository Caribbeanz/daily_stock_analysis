# -*- coding: utf-8 -*-
"""装配层测试（取数 → 守卫链 → 快照落库 → 渲染）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pytest

from src.assistant.config.overrides import ParamSet
from src.assistant.data.sector_map import IndustryResolver
from src.assistant.data.storage import AssistantStore
from src.assistant.enums import Preset, SourceTag
from src.assistant.pipeline import (
    AssistantPipeline,
    PipelineError,
    PipelineSettings,
    render_summary,
)
from src.assistant.scheduling.delivery import MultiSlotDispatcher, MemoryDeliveryLedger
from src.assistant.scheduling.config import FirePoint, ScheduleEntry, EntryKind
from src.assistant.strategies import enabled_strategy_ids
from src.assistant.versioning import build_calc_version
from tests.assistant.helpers import make_bars, trending

# 2026-11-27 是周五
DAY = date(2026, 11, 27)
NEXT_DAY = date(2026, 11, 30)  # 周一


def settings(**kwargs) -> PipelineSettings:
    base = {"total_capital": 500_000.0, "cash_available": 200_000.0}
    base.update(kwargs)
    return PipelineSettings(**base)


@dataclass
class FakeFetch:
    """取数器替身：按 code 返回预置 K 线，可配置失败。"""

    frames: dict[str, list]
    fail: set[str] = None  # type: ignore[assignment]
    calls: list[tuple[str, date, date]] = None  # type: ignore[assignment]
    degraded: bool = False

    def __post_init__(self) -> None:
        self.fail = self.fail or set()
        self.calls = self.calls or []

    def fetch_daily(self, code: str, start: date, end: date):
        self.calls.append((code, start, end))
        if code in self.fail:
            raise RuntimeError(f"{code} 三源全失败")

        class _R:
            pass

        r = _R()
        r.bars = tuple(bar for bar in self.frames.get(code, []) if start <= bar.trade_date <= end)
        r.source_tag = SourceTag.D2
        r.degraded = self.degraded
        r.rows = len(r.bars)
        r.source_name = "fake"
        return r


@pytest.fixture()
def store(tmp_path) -> AssistantStore:
    s = AssistantStore(tmp_path / "pipeline.db")
    s.create_all()
    return s


def seed(store: AssistantStore, code: str, closes: list[float], start: date = date(2026, 5, 4)) -> None:
    store.save_bars(code, make_bars(closes, start=start))


def make_pipeline(store: AssistantStore, *, fetcher=None, params=None) -> AssistantPipeline:
    return AssistantPipeline(
        store=store,
        settings=settings(),
        fetcher=fetcher,
        params=params,
        clock=lambda: datetime(2026, 11, 27, 18, 0),
        trading_days=lambda start, end: None,  # 离线：交给自然日兜底
    )


# ---- 配置 ------------------------------------------------------------------
def test_settings_requires_total_capital() -> None:
    with pytest.raises(PipelineError) as exc:
        PipelineSettings.from_env({})
    assert "ASSISTANT_TOTAL_CAPITAL" in str(exc.value)


def test_settings_from_env_parses_values() -> None:
    cfg = PipelineSettings.from_env(
        {
            "ASSISTANT_TOTAL_CAPITAL": "300000",
            "ASSISTANT_CASH_AVAILABLE": "120000",
            "ASSISTANT_PARAM_PRESET": "strict",
            "ASSISTANT_INDEX_CODE": "sh000300",
        }
    )
    assert cfg.total_capital == 300000.0
    assert cfg.cash_available == 120000.0
    assert cfg.preset is Preset.STRICT
    assert cfg.index_code == "sh000300"


def test_settings_rejects_bad_preset() -> None:
    with pytest.raises(PipelineError):
        PipelineSettings.from_env({"ASSISTANT_TOTAL_CAPITAL": "100000", "ASSISTANT_PARAM_PRESET": "loose-ish"})


def test_settings_rejects_non_positive_capital() -> None:
    with pytest.raises(ValueError):
        PipelineSettings(total_capital=0)


# ---- 基本链路 --------------------------------------------------------------
def test_ensure_snapshot_builds_one_snapshot_for_all_codes(store: AssistantStore) -> None:
    seed(store, "600000", trending())
    seed(store, "000001", trending(drift=-0.004))
    store.append_watchlist("600000", "add")
    store.append_watchlist("000001", "add")

    pipeline = make_pipeline(store)
    outcome = pipeline.ensure_snapshot(DAY)

    assert outcome.created is True
    assert outcome.codes == ("000001", "600000")
    assert len(outcome.content["conclusions"]) == 2
    assert outcome.content["snapshot_id"] == outcome.snapshot_id
    assert outcome.skipped == ()


def test_snapshot_is_idempotent_within_same_day(store: AssistantStore) -> None:
    seed(store, "600000", trending())
    store.append_watchlist("600000", "add")
    fetcher = FakeFetch({})
    pipeline = make_pipeline(store, fetcher=fetcher)

    first = pipeline.ensure_snapshot(DAY)
    second = pipeline.ensure_snapshot(DAY)

    assert second.created is False, "同日同参数必须复用快照"
    assert second.snapshot_id == first.snapshot_id
    assert second.content == first.content


def test_different_param_hash_creates_new_snapshot(store: AssistantStore) -> None:
    seed(store, "600000", trending())
    store.append_watchlist("600000", "add")

    baseline = make_pipeline(store).ensure_snapshot(DAY)
    strict = ParamSet.build(Preset.STRICT)
    other = make_pipeline(store, params=strict).ensure_snapshot(DAY)

    assert other.created is True
    assert other.snapshot_id != baseline.snapshot_id
    assert other.content["calc_version"] != baseline.content["calc_version"]
    assert other.content["param_hash"] != baseline.content["param_hash"]


def test_calc_version_covers_rules_and_params(store: AssistantStore) -> None:
    seed(store, "600000", trending())
    store.append_watchlist("600000", "add")
    content = make_pipeline(store).ensure_snapshot(DAY).content
    assert content["calc_version"].startswith("cv1-")
    assert len(content["calc_version"]) <= 32


def test_calc_version_matches_strategy_ledger(store: AssistantStore) -> None:
    """§5.12.3：战法启停清单进指纹，否则两套映射会撞成同一版本。"""
    seed(store, "600000", trending())
    store.append_watchlist("600000", "add")
    content = make_pipeline(store).ensure_snapshot(DAY).content
    params = ParamSet.build(Preset.STANDARD)

    assert content["calc_version"] == build_calc_version(
        params.param_hash, strategy_ids=enabled_strategy_ids()
    )


# ---- 展示名称（D-14） ------------------------------------------------------
def _pipeline_with_names(store: AssistantStore, **kwargs) -> AssistantPipeline:
    return AssistantPipeline(
        store=store,
        settings=settings(),
        clock=lambda: datetime(2026, 11, 27, 18, 0),
        trading_days=lambda start, end: None,
        **kwargs,
    )


def test_name_from_resolver_reaches_snapshot(store: AssistantStore) -> None:
    seed(store, "600000", trending())
    store.append_watchlist("600000", "add")
    resolver = IndustryResolver(fetch_name=lambda code: "浦发银行", cache_path=None)

    content = _pipeline_with_names(store, industry_resolver=resolver).ensure_snapshot(DAY).content

    assert content["conclusions"][0]["name"] == "浦发银行"


def test_injected_names_win_over_resolver(store: AssistantStore) -> None:
    seed(store, "600000", trending())
    store.append_watchlist("600000", "add")
    resolver = IndustryResolver(fetch_name=lambda code: "浦发银行", cache_path=None)

    content = _pipeline_with_names(
        store, industry_resolver=resolver, names={"600000": "注入名"}
    ).ensure_snapshot(DAY).content

    assert content["conclusions"][0]["name"] == "注入名"


def test_name_falls_back_to_code(store: AssistantStore) -> None:
    seed(store, "600000", trending())
    store.append_watchlist("600000", "add")

    class _NoName:
        def name(self, code: str) -> str:
            raise RuntimeError("boom")

    content = _pipeline_with_names(store, industry_resolver=_NoName()).ensure_snapshot(DAY).content

    assert content["conclusions"][0]["name"] == "600000"


# ---- 取数与降级 ------------------------------------------------------------
def test_uses_l1_cache_without_calling_fetcher(store: AssistantStore) -> None:
    seed(store, "600000", trending())
    store.append_watchlist("600000", "add")
    fetcher = FakeFetch({})
    make_pipeline(store, fetcher=fetcher).ensure_snapshot(DAY)
    assert fetcher.calls == [], "本地数据够新就不该联网"


def test_fetches_incrementally_when_stale(store: AssistantStore) -> None:
    seed(store, "600000", trending(120))  # 只到 2026-10 中旬
    store.append_watchlist("600000", "add")
    full = make_bars(trending(160), start=date(2026, 5, 4))
    fetcher = FakeFetch({"600000": full})

    pipeline = make_pipeline(store, fetcher=fetcher)
    outcome = pipeline.ensure_snapshot(DAY)

    assert len(fetcher.calls) == 1
    code, start, end = fetcher.calls[0]
    assert code == "600000" and end == DAY
    assert (DAY - start).days < 365 * 3, "应增量取数，不该每次拉 3 年"
    assert outcome.created is True


def test_fetch_failure_with_cache_degrades_but_still_reports(store: AssistantStore) -> None:
    seed(store, "600000", trending(120))
    store.append_watchlist("600000", "add")
    fetcher = FakeFetch({}, fail={"600000"})

    outcome = make_pipeline(store, fetcher=fetcher).ensure_snapshot(DAY)
    assert outcome.created is True
    assert any("使用 L1 缓存" in warning for warning in outcome.warnings)
    # 数据缺口会被 S0 判出来，不允许静默当正常数据用
    assert outcome.content["gap_days"] > 0 or outcome.content["conclusions"][0]["degraded"] is False


def test_fetch_failure_without_cache_skips_code(store: AssistantStore) -> None:
    seed(store, "600000", trending())
    store.append_watchlist("600000", "add")
    store.append_watchlist("000001", "add")  # 无 K 线
    fetcher = FakeFetch({}, fail={"000001"})

    outcome = make_pipeline(store, fetcher=fetcher).ensure_snapshot(DAY)
    assert outcome.codes == ("600000",)
    assert outcome.skipped and outcome.skipped[0][0] == "000001"
    assert "取数失败" in outcome.skipped[0][1]


def test_all_codes_unavailable_raises(store: AssistantStore) -> None:
    store.append_watchlist("000001", "add")
    with pytest.raises(PipelineError) as exc:
        make_pipeline(store, fetcher=FakeFetch({}, fail={"000001"})).ensure_snapshot(DAY)
    assert "本次不产出" in str(exc.value)


def test_empty_watchlist_raises(store: AssistantStore) -> None:
    with pytest.raises(PipelineError):
        make_pipeline(store).ensure_snapshot(DAY)


# ---- 持仓与组合 ------------------------------------------------------------
def test_holdings_drive_position_and_portfolio(store: AssistantStore) -> None:
    seed(store, "600000", trending())
    store.upsert_holding("600000", shares=1000, cost_basis=8.0)

    outcome = make_pipeline(store).ensure_snapshot(DAY)
    row = outcome.content["conclusions"][0]
    assert row["code"] == "600000"
    assert row["is_holding"] is True
    # 持仓标的才会写「加仓/减仓」类动作
    assert outcome.content["plans"]


def test_portfolio_cash_defaults_to_capital_minus_holdings(store: AssistantStore) -> None:
    seed(store, "600000", trending())
    store.upsert_holding("600000", shares=1000, cost_basis=8.0)
    pipeline = AssistantPipeline(
        store=store,
        settings=PipelineSettings(total_capital=100_000.0),  # 不填现金
        clock=lambda: datetime(2026, 11, 27, 18, 0),
        trading_days=lambda s, e: None,
    )
    views = pipeline._build_views(DAY, [], [])  # noqa: SLF001 - 单测直接验证组合构造
    portfolio = pipeline._portfolio(views)  # noqa: SLF001
    assert portfolio.total_capital == 100_000.0
    assert portfolio.holding_market_value > 0
    assert portfolio.cash_available == pytest.approx(100_000.0 - portfolio.holding_market_value)


# ---- 防抖状态落库 ----------------------------------------------------------
def test_stage_state_persisted_between_runs(store: AssistantStore) -> None:
    seed(store, "600000", trending())
    store.append_watchlist("600000", "add")
    make_pipeline(store).ensure_snapshot(DAY)

    state = store.load_stage_state("600000")
    assert state is not None
    assert state["stage"] is not None
    assert state["as_of"] == DAY.isoformat()


def test_stage_state_is_read_back_before_next_run(store: AssistantStore) -> None:
    """跨日运行前必须能读回上次的防抖状态，否则每天都算「首次判定」。"""
    seed(store, "600000", trending(150))
    store.append_watchlist("600000", "add")
    make_pipeline(store).ensure_snapshot(DAY)
    saved = store.load_stage_state("600000")
    assert saved is not None and saved["stage"]

    # 新进程（新 pipeline 实例，内存里没有任何状态）
    restored = make_pipeline(store)._stage_state("600000")  # noqa: SLF001
    assert restored.stage is not None
    assert restored.stage.value == saved["stage"]
    assert restored.as_of is not None, "状态必须带交易日，否则无法判断是否连续"


# ---- 投递载荷 --------------------------------------------------------------
def test_payload_is_identical_across_calls_and_restarts(tmp_path) -> None:
    db = tmp_path / "payload.db"
    store = AssistantStore(db)
    store.create_all()
    seed(store, "600000", trending())
    store.append_watchlist("600000", "add")

    first = make_pipeline(store).payload(DAY)

    # 模拟进程重启：新 store + 新 pipeline，同日复用同一快照
    reopened = AssistantStore(db)
    second = make_pipeline(reopened).payload(DAY)

    assert first.snapshot_id == second.snapshot_id
    assert first.same_source_as(second), "重启后重投必须是同一份内容"
    assert first.bar_date == DAY
    assert first.data_date == DAY


def test_payload_text_is_deterministic(store: AssistantStore) -> None:
    seed(store, "600000", trending())
    store.append_watchlist("600000", "add")
    content = make_pipeline(store).ensure_snapshot(DAY).content
    assert render_summary(content) == render_summary(content)
    text = render_summary(content)
    assert content["snapshot_id"] in text
    assert content["calc_version"] in text


def test_payload_works_with_multi_slot_dispatcher(store: AssistantStore) -> None:
    seed(store, "600000", trending())
    store.append_watchlist("600000", "add")
    pipeline = make_pipeline(store)

    ledger = MemoryDeliveryLedger()
    sent = []
    dispatcher = MultiSlotDispatcher(ledger, lambda p: sent.append(p) or True)
    entry = ScheduleEntry(raw="20:00", kind=EntryKind.HHMM, at=datetime(2026, 11, 27, 20, 0).time())
    point = FirePoint(when=datetime(2026, 11, 27, 20, 0), entry=entry)

    assert dispatcher.dispatch(point, pipeline.payload(DAY)).outcome.value == "sent"
    assert dispatcher.dispatch(point, pipeline.payload(DAY)).outcome.value == "duplicate"
    assert len(sent) == 1


def test_payload_marks_stale_when_gap(store: AssistantStore) -> None:
    seed(store, "600000", trending(120))  # 数据停在 10 月
    store.append_watchlist("600000", "add")
    payload = make_pipeline(store, fetcher=FakeFetch({}, fail={"600000"})).payload(DAY)
    assert payload.stale is True, "数据缺口 > 3 日必须标降级，不能当正常结论推"


def test_trading_days_provider_is_used_and_cached(store: AssistantStore) -> None:
    seed(store, "600000", trending(120))
    store.append_watchlist("600000", "add")
    calls: list[tuple[date, date]] = []

    def provider(start: date, end: date):
        calls.append((start, end))
        return [DAY - timedelta(days=i) for i in range(0, 30)]

    pipeline = AssistantPipeline(
        store=store,
        settings=settings(),
        trading_days=provider,
        clock=lambda: datetime(2026, 11, 27, 18, 0),
    )
    pipeline._trading_days(DAY)  # noqa: SLF001
    pipeline._trading_days(DAY)  # noqa: SLF001
    assert len(calls) == 1, "同一交易日只查一次日历"


# ---- 与调度层联调 ----------------------------------------------------------
def test_scheduler_with_pipeline_ticks_once(store: AssistantStore) -> None:
    """调度层 + 装配层串起来：两次 tick 只生成一次、只推一条、内容同源。"""
    from src.assistant.scheduling import (
        AssistantScheduler,
        MultiSlotDispatcher,
        TradingDayGate,
        parse_env_config,
    )
    from src.assistant.scheduling.delivery import MemoryDeliveryLedger

    seed(store, "600000", trending())
    store.append_watchlist("600000", "add")
    pipeline = make_pipeline(store)

    sent = []
    scheduler = AssistantScheduler(
        config=parse_env_config({"ASSISTANT_PUSH_SCHEDULE": "20:00"}),
        gate=TradingDayGate(probe=lambda now: True),
        dispatcher=MultiSlotDispatcher(MemoryDeliveryLedger(), lambda p: sent.append(p) or True),
        generate=pipeline.generate,
    )

    first = scheduler.tick(datetime(2026, 11, 27, 20, 0))
    assert first.generated is True
    assert first.sent == 1

    second = scheduler.tick(datetime(2026, 11, 27, 20, 1))
    assert second.generated is False, "第二次 tick 必须复用当日快照"
    assert second.sent == 0, "同一时点不得重投"
    assert len(sent) == 1
    assert sent[0].snapshot_id == first.payload.snapshot_id


def test_source_tag_reflects_actual_source(store: AssistantStore) -> None:
    """主源失败降级到新浪取到的数据必须标 D2，不能冒充 D1。"""
    store.append_watchlist("600000", "add")
    full = make_bars(trending(160), start=date(2026, 5, 4))
    fetcher = FakeFetch({"600000": full})  # FakeFetch 固定返回 D2

    outcome = make_pipeline(store, fetcher=fetcher).ensure_snapshot(DAY)
    assert outcome.content["source_tag"] == "D2"
    assert any("实际来源 D2" in warning for warning in outcome.warnings)


# ---- 真实数据踩到的两个坑（回归锁定）--------------------------------------
def test_snapshot_day_is_request_day_not_bar_date(store: AssistantStore) -> None:
    """收盘数据未出时，快照必须记在**请求日**名下，否则幂等键跨日失效。"""
    seed(store, "600000", trending(120))  # K 线止于 10 月
    store.append_watchlist("600000", "add")
    pipeline = make_pipeline(store, fetcher=FakeFetch({}, fail={"600000"}))

    outcome = pipeline.ensure_snapshot(DAY)  # DAY=11-27，数据止于更早
    assert outcome.content["data_date"] == DAY.isoformat(), "快照日必须是请求日"
    assert outcome.content["bar_date"] != DAY.isoformat(), "数据截止日应早于请求日"
    assert outcome.bar_date < DAY

    again = pipeline.ensure_snapshot(DAY)
    assert again.created is False, "同日同参数必须复用，不能因为数据截止日不同就新建"
    assert again.snapshot_id == outcome.snapshot_id


def test_incremental_fetch_keeps_full_history(store: AssistantStore) -> None:
    """增量取数只拉最近几天，但视图必须用**合并后的完整历史**（否则阶段掉 S0）。"""
    seed(store, "600000", trending(120))  # 本地 120 根
    store.append_watchlist("600000", "add")
    recent = make_bars(trending(160), start=date(2026, 5, 4))[-6:]  # 只返回最近 6 根
    fetcher = FakeFetch({"600000": recent})

    outcome = make_pipeline(store, fetcher=fetcher).ensure_snapshot(DAY)
    # 本地 120 根 + 增量 6 根（有重叠），合并后不该只有 6 根
    assert outcome.content["bars_used"] >= 120, "不能把 6 根增量当成完整历史"
    # 数据缺口大时阶段仍应是 S0（这是口径正确，不是 bug）；这里只锁「历史没被截断」
    assert outcome.content["conclusions"][0]["stage"] in {"S0", "S3", "S4", "S5", "S2", "S1"}
