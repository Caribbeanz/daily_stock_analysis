# -*- coding: utf-8 -*-
"""板块三档 → 评分与 SEC-1（需求 §4.4 P0-4 / §5.10.2 SEC-1，D-32）。

此前 `sector_strength` 只算来展示：结论层拿到的 `sector` 恒为 ``None``，评分里的
板块加减分因此永远是 0、SEC-1 永远不触发。这一批把它接上，本文件钉住四条：

1. 偏强/偏弱按**可配置分值**加减分，中性为 0；
2. 板块降级只注记、不扣分（拿不到数据 ≠ 板块弱）；
3. 板块区未接入时**保持老行为**（不假装板块中性，也不改档位）；
4. `sector_strength=偏弱` 且 `rs_t` 连续 N 日为负 → SEC-1 警示级。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

import pytest

from src.assistant.data.sector_map import SectorRef
from src.assistant.data.storage import AssistantStore
from src.assistant.domain import MarketView, SectorView
from src.assistant.engine.risk import RiskCode
from src.assistant.engine.scoring import market_adjustment
from src.assistant.data.fetcher import SectorOverview, SectorRow
from src.assistant.pipeline import AssistantPipeline, PipelineSettings
from src.assistant.enums import SourceTag
from tests.assistant.helpers import make_bars, trending

DAY = date(2026, 11, 26)


# ---------------------------------------------------------------------------
# 纯函数：板块三档分值
# ---------------------------------------------------------------------------
class TestMarketAdjustment:
    def test_sector_points_are_configurable(self) -> None:
        strong = SectorView(sector_name="银行", strength="偏强")
        weak = SectorView(sector_name="银行", strength="偏弱")

        up, notes = market_adjustment(None, strong, 8.0, sector_up=3.0, sector_down=1.5)
        down, _ = market_adjustment(None, weak, 8.0, sector_up=3.0, sector_down=1.5)

        assert up == pytest.approx(3.0) and notes == ()
        assert down == pytest.approx(-1.5)

    def test_neutral_sector_adds_nothing(self) -> None:
        adj, notes = market_adjustment(None, SectorView(strength="中性"), 8.0)

        assert adj == 0.0
        assert notes == ()

    def test_total_is_clipped_to_weight(self) -> None:
        """低权重维度不允许吃掉技术面主导地位：大盘吃满后板块再加也不超预算。"""
        market = MarketView(regime="强势")

        adj, _ = market_adjustment(market, SectorView(strength="偏强"), 8.0, sector_up=6.0)

        assert adj == pytest.approx(8.0)

    def test_degraded_sector_only_notes(self) -> None:
        adj, notes = market_adjustment(None, SectorView(strength="偏弱", degraded=True), 8.0)

        assert adj == pytest.approx(-2.0), "降级不代表板块弱，分值仍按已有档位给"
        assert any("板块数据降级" in item for note in notes for item in [note])

    def test_unknown_strength_falls_back_to_neutral(self) -> None:
        adj, notes = market_adjustment(None, SectorView(strength="很强"), 8.0)

        assert adj == 0.0
        assert any("未知板块档位" in item for item in notes)

    def test_both_missing(self) -> None:
        adj, notes = market_adjustment(None, None, 8.0)

        assert adj == 0.0
        assert notes == ("大盘/板块数据缺失，按中性代入",)


# ---------------------------------------------------------------------------
# 装配：板块视图真的进了结论
# ---------------------------------------------------------------------------
@dataclass
class _FakeMarketFetcher:
    """板块一览 + 板块指数日线；指数（大盘）走 `market_bars` 注入。"""

    rows: tuple[SectorRow, ...]
    bars_by_name: dict[str, tuple]

    def fetch_sectors(self) -> SectorOverview:
        return SectorOverview(rows=self.rows, source_tag=SourceTag.D1, source_name="fake")

    def fetch_sector_index(self, name: str, start: date, end: date):  # noqa: ANN201
        return tuple(self.bars_by_name.get(name, ()))


@dataclass
class _Resolver:
    """把每只标的都映射到同一个板块（真实实现见 sector_map.IndustryResolver）。"""

    sector_name: str

    def resolve(self, codes, boards):  # noqa: ANN001, ANN201
        return {
            code: SectorRef(
                code=code,
                sector_name=self.sector_name,
                sector_code="881001",
                matched_level="一级",
                raw_industry=self.sector_name,
            )
            for code in codes
        }

    def name(self, code: str) -> str | None:  # noqa: ARG002 - 名称解析器契约
        return None


def _pipeline(
    tmp_path, *, strength: str, mapped: bool = True, enabled: bool = True
) -> tuple:
    store = AssistantStore(tmp_path / "s.db")
    store.create_all()
    # 夹具默认从 2026-05-04 起，160 根覆盖到 12 月；截到 DAY 后「最后一根正好是 T 日」，
    # 否则板块 2×2 会因 as_of 不是当日而标降级，测的就不是分值而是降级了
    bars = [b for b in make_bars(trending(160)) if b.trade_date <= DAY]
    store.save_bars("600000", bars)
    store.append_watchlist("600000", "add", effective_date=date(2026, 1, 5))

    # 板块指数：偏强 = 稳步上行；偏弱 = 稳步下行（都远强于/弱于大盘）
    step = 0.004 if strength == "偏强" else -0.004
    price = 1000.0
    sector_bars = []
    for bar in bars:
        price *= 1 + step
        sector_bars.append(
            bar.__class__(
                trade_date=bar.trade_date,
                open=price * 0.999,
                high=price * 1.002,
                low=price * 0.998,
                close=price,
                volume=1000.0,
            )
        )
    rows = (
        SectorRow(
            sector_code="881001",
            sector_name="测试板块",
            pct_change=4.0 if strength == "偏强" else -4.0,
            up_count=10,
            down_count=2,
        ),
    )
    fetcher = _FakeMarketFetcher(rows=rows, bars_by_name={"测试板块": tuple(sector_bars)})
    pipeline = AssistantPipeline(
        store=store,
        settings=PipelineSettings(
            total_capital=100_000.0, cash_available=80_000.0, sector_enabled=enabled
        ),
        market_fetcher=fetcher,
        market_bars=bars,
        industry_resolver=_Resolver("测试板块") if mapped else None,
    )
    return pipeline, store


def _conclusion(pipeline, store) -> dict:
    out = pipeline.ensure_snapshot(DAY, now=datetime.combine(DAY, time(20, 0)))
    content = out.content
    return content


class TestSectorIntoConclusions:
    def test_strong_sector_raises_score(self, tmp_path) -> None:
        strong, store_a = _pipeline(tmp_path / "a", strength="偏强")
        weak, store_b = _pipeline(tmp_path / "b", strength="偏弱")

        strong_row = _conclusion(strong, store_a)["conclusions"][0]
        weak_row = _conclusion(weak, store_b)["conclusions"][0]

        assert strong_row["score"] - weak_row["score"] == pytest.approx(4.0, abs=0.01), (
            "偏强 +2、偏弱 −2，差值应为 4 分（sector_adj_up/down 默认值）"
        )

    def test_weak_sector_with_consecutive_negative_rs_trips_sec1(self, tmp_path) -> None:
        pipeline, store = _pipeline(tmp_path, strength="偏弱")

        content = _conclusion(pipeline, store)

        risks = content.get("risks", [])
        sec1 = [item for item in risks if item.get("risk_code") == RiskCode.SEC_1.value]
        assert sec1, f"板块偏弱且 rs_t 连续为负应触发 SEC-1，实际风险：{risks}"
        assert sec1[0]["level"] == "警示"
        assert sec1[0]["affects_advice"] is True

    def test_unmapped_stock_gets_sec3_without_sector_points(self, tmp_path) -> None:
        """板块区有数据但这只标的没映射上：SEC-3 观察级、评分里不掺板块分。"""
        unmapped, store_a = _pipeline(tmp_path / "a", strength="偏强", mapped=False)
        mapped, store_b = _pipeline(tmp_path / "b", strength="偏强")

        row_unmapped = _conclusion(unmapped, store_a)["conclusions"][0]
        row_mapped = _conclusion(mapped, store_b)["conclusions"][0]

        assert row_mapped["score"] - row_unmapped["score"] == pytest.approx(2.0, abs=0.01)
        risks = _conclusion(unmapped, store_a).get("risks", [])
        sec3 = [item for item in risks if item.get("risk_code") == RiskCode.SEC_3.value]
        assert sec3 and sec3[0]["level"] == "观察"
        assert sec3[0]["affects_advice"] is False

    def test_sector_disabled_keeps_old_behaviour(self, tmp_path) -> None:
        """总开关关闭：板块数据不取、评分不加分，SEC-3 只写「缺失」而不是「映射失败」。"""
        pipeline, store = _pipeline(tmp_path, strength="偏强", enabled=False)

        content = _conclusion(pipeline, store)

        risks = content.get("risks", [])
        sec3 = [item for item in risks if item.get("risk_code") == RiskCode.SEC_3.value]
        assert sec3, "板块缺失应留 SEC-3 观察级信号，而不是静默"
        assert "映射失败" not in sec3[0]["trigger_basis"]
        assert sec3[0]["level"] == "观察"
