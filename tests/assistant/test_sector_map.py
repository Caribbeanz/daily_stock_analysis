# -*- coding: utf-8 -*-
"""个股 → 板块映射（D-22）与 focus/market 两种板块区口径。"""

from __future__ import annotations

import json
from datetime import date

import pytest

from src.assistant.data.fetcher import SectorOverview, SectorRow
from src.assistant.data.sector_map import (
    IndustryResolver,
    normalize_industry_name,
    to_secucode,
)
from src.assistant.data.storage import AssistantStore
from src.assistant.enums import SourceTag
from src.assistant.pipeline import AssistantPipeline, PipelineSettings
from tests.assistant.helpers import make_bars

DAY = date(2026, 11, 26)


class TestNameNormalize:
    def test_strips_roman_suffix(self) -> None:
        # 东财带三级后缀，同花顺不带：不剥掉就一个都匹配不上
        assert normalize_industry_name("银行Ⅱ") == "银行"
        assert normalize_industry_name("股份制银行Ⅲ") == "股份制银行"
        assert normalize_industry_name("白酒Ⅱ") == "白酒"
        assert normalize_industry_name("保险III") == "保险"

    def test_keeps_plain_names(self) -> None:
        assert normalize_industry_name("银行") == "银行"
        assert normalize_industry_name("  电池 ") == "电池"

    def test_secucode(self) -> None:
        assert to_secucode("600000") == "600000.SH"
        assert to_secucode("000001") == "000001.SZ"
        assert to_secucode("300750") == "300750.SZ"
        assert to_secucode("830799") == "830799.BJ"
        assert to_secucode("600000.SH") == "600000.SH"


def _resolver(table: dict[str, list[tuple[str, str]]], calls: list[str], **kwargs) -> IndustryResolver:
    def fetch(code: str):  # noqa: ANN202
        calls.append(code)
        if code not in table:
            raise RuntimeError("boom")
        return table[code]

    kwargs.setdefault("cache_path", None)
    return IndustryResolver(fetch_industries=fetch, **kwargs)


class TestResolve:
    def test_prefers_level_two(self) -> None:
        calls: list[str] = []
        resolver = _resolver(
            {"600000": [("1", "银行"), ("2", "银行Ⅱ"), ("3", "股份制银行Ⅲ")]}, calls
        )

        ref = resolver.resolve(["600000"], {"银行": "881155", "股份制银行": "881999"})["600000"]

        assert ref.sector_name == "银行"
        assert ref.sector_code == "881155"
        assert ref.matched_level == "2"

    def test_falls_back_to_level_one(self) -> None:
        calls: list[str] = []
        resolver = _resolver({"600519": [("1", "食品饮料"), ("2", "白酒Ⅱ"), ("3", "白酒Ⅲ")]}, calls)

        ref = resolver.resolve(["600519"], {"食品饮料": "881273"})["600519"]

        assert ref.matched_level == "1"
        assert ref.sector_name == "食品饮料"

    def test_unmatched_is_absent_not_guessed(self) -> None:
        calls: list[str] = []
        resolver = _resolver({"002594": [("1", "汽车"), ("2", "乘用车")]}, calls)

        assert resolver.resolve(["002594"], {"银行": "881155"}) == {}

    def test_fetch_failure_is_empty(self) -> None:
        calls: list[str] = []
        resolver = _resolver({}, calls)

        assert resolver.resolve(["600000"], {"银行": "881155"}) == {}

    def test_second_call_uses_memory_cache(self) -> None:
        calls: list[str] = []
        resolver = _resolver({"600000": [("1", "银行")]}, calls)

        resolver.resolve(["600000"], {"银行": "881155"})
        resolver.resolve(["600000"], {"银行": "881155"})

        assert calls == ["600000"]

    def test_disk_cache_roundtrip(self, tmp_path) -> None:
        path = tmp_path / "map.json"
        calls: list[str] = []
        first = _resolver({"600000": [("1", "银行")]}, calls, cache_path=path)
        first.resolve(["600000"], {"银行": "881155"})

        assert json.loads(path.read_text(encoding="utf-8"))["600000"]["rows"] == [["1", "银行"]]

        calls.clear()
        second = _resolver({"600000": [("1", "别的")]}, calls, cache_path=path)
        ref = second.resolve(["600000"], {"银行": "881155"})["600000"]

        assert calls == []  # 命中磁盘缓存，没有再请求
        assert ref.sector_name == "银行"

    def test_expired_cache_refetches(self, tmp_path) -> None:
        path = tmp_path / "map.json"
        calls: list[str] = []
        now = [1_000_000.0]
        first = _resolver({"600000": [("1", "银行")]}, calls,
                          cache_path=path, ttl_hours=1, clock=lambda: now[0])
        first.resolve(["600000"], {"银行": "881155"})

        calls.clear()
        now[0] += 7200  # 过两小时，TTL 已过
        second = _resolver({"600000": [("1", "银行")]}, calls,
                           cache_path=path, ttl_hours=1, clock=lambda: now[0])
        second.resolve(["600000"], {"银行": "881155"})

        assert calls == ["600000"]


class _Overview:
    def __init__(self, rows: tuple[SectorRow, ...]) -> None:
        self._rows = rows

    def fetch_sectors(self) -> SectorOverview:
        return SectorOverview(rows=self._rows, source_tag=SourceTag.D1, source_name="fake")

    def fetch_sector_index(self, name: str, start: date, end: date):  # noqa: ANN201
        return tuple(make_bars([100.0 + 2.0 * i for i in range(40)]))


ROWS = (
    SectorRow(sector_code="881155", sector_name="银行", pct_change=-0.8, up_count=3, down_count=9,
              lead_stock="甲"),
    SectorRow(sector_code="881273", sector_name="白酒", pct_change=1.2, up_count=8, down_count=1,
              lead_stock="乙"),
    SectorRow(sector_code="881281", sector_name="电池", pct_change=3.4, up_count=12, down_count=2,
              lead_stock="丙"),
    SectorRow(sector_code="881999", sector_name="贵金属", pct_change=6.0, up_count=14, down_count=0,
              lead_stock="丁"),
)


class TestFocusMode:
    def setup_method(self) -> None:
        self.store = AssistantStore(":memory:")
        self.store.create_all()

    def _pipeline(self, resolver, **kwargs) -> AssistantPipeline:
        return AssistantPipeline(
            store=self.store,
            settings=PipelineSettings(total_capital=100_000.0, **kwargs),
            market_fetcher=_Overview(ROWS),
            industry_resolver=resolver,
        )

    def test_only_holding_sectors_are_listed(self) -> None:
        """需求 §3.5.2：板块区列「涉及板块」，不该把市场最强板块混进来。"""
        self.store.append_watchlist("600000", "add", effective_date=date(2026, 5, 1))
        calls: list[str] = []
        resolver = _resolver({"600000": [("1", "银行")]}, calls)
        pipeline = self._pipeline(resolver)

        rows = pipeline.sector_rows(DAY)

        assert [row["sector_name"] for row in rows] == ["银行"]
        assert "贵金属" not in [row["sector_name"] for row in rows]

    def test_sorted_by_daily_change(self) -> None:
        self.store.append_watchlist("600000", "add", effective_date=date(2026, 5, 1))
        self.store.append_watchlist("600519", "add", effective_date=date(2026, 5, 1))
        calls: list[str] = []
        resolver = _resolver(
            {"600000": [("1", "银行")], "600519": [("2", "白酒Ⅱ")]}, calls
        )
        pipeline = self._pipeline(resolver)

        assert [row["sector_name"] for row in pipeline.sector_rows(DAY)] == ["白酒", "银行"]

    def test_no_resolver_falls_back_to_market_ends(self) -> None:
        self.store.append_watchlist("600000", "add", effective_date=date(2026, 5, 1))
        pipeline = self._pipeline(None, sector_top_n=1, sector_weak_n=1)

        rows = pipeline.sector_rows(DAY)

        assert [row["sector_name"] for row in rows] == ["贵金属", "银行"]  # 最强 + 最弱

    def test_unmapped_holdings_fall_back(self) -> None:
        self.store.append_watchlist("002594", "add", effective_date=date(2026, 5, 1))
        calls: list[str] = []
        resolver = _resolver({"002594": [("1", "汽车")]}, calls)  # 汽车不在板块表里
        pipeline = self._pipeline(resolver, sector_top_n=1, sector_weak_n=0)

        assert [row["sector_name"] for row in pipeline.sector_rows(DAY)] == ["贵金属"]

    def test_market_mode_still_works(self) -> None:
        self.store.append_watchlist("600000", "add", effective_date=date(2026, 5, 1))
        calls: list[str] = []
        resolver = _resolver({"600000": [("1", "银行")]}, calls)
        pipeline = self._pipeline(resolver, sector_mode="market", sector_top_n=1, sector_weak_n=0)

        assert [row["sector_name"] for row in pipeline.sector_rows(DAY)] == ["贵金属"]
        assert calls == []  # market 模式不查映射

    def test_resolver_error_falls_back(self) -> None:
        self.store.append_watchlist("600000", "add", effective_date=date(2026, 5, 1))

        class _Broken:
            def resolve(self, codes, boards):  # noqa: ANN001, ANN202
                raise RuntimeError("boom")

        pipeline = self._pipeline(_Broken(), sector_top_n=1, sector_weak_n=0)

        assert [row["sector_name"] for row in pipeline.sector_rows(DAY)] == ["贵金属"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, "focus"), ("", "focus"), ("MARKET", "market"), ("乱写", "focus")],
)
def test_sector_mode_env(raw, expected) -> None:
    env = {"ASSISTANT_TOTAL_CAPITAL": "100000"}
    if raw is not None:
        env["ASSISTANT_SECTOR_MODE"] = raw

    assert PipelineSettings.from_env(env).sector_mode == expected
