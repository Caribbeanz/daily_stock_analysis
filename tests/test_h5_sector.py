# -*- coding: utf-8 -*-
"""板块归属与行业排行（M2-B）单测：名称归一化、结论模板、排名/相对强弱、缓存与降级。"""

from __future__ import annotations

from datetime import date

import pytest

from src.services.sector_service import (
    SectorService,
    build_sector_verdict,
    normalize_industry_name,
)


# --------------------------------------------------------------------------- #
# 名称归一化
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("银行", "银行"),
        ("银行板块", "银行"),
        (" 银行 板块 ", "银行"),
        ("银行（申万）", "银行(申万)"),
        ("半导体行业", "半导体"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_industry_name(raw, expected):
    assert normalize_industry_name(raw) == expected


# --------------------------------------------------------------------------- #
# 结论模板
# --------------------------------------------------------------------------- #
def test_verdict_strong_sector_and_lagging_stock():
    verdict = build_sector_verdict(
        {
            "industry": "银行",
            "change_pct": 2.0,
            "rank": 3,
            "total": 86,
            "relative_pct": -1.8,
            "ranking_available": True,
        }
    )
    assert "银行板块 +2.00%（3/86）" in verdict
    assert "弱于板块（跑输行业）" in verdict
    assert "板块强势" in verdict
    assert "不构成换股建议" in verdict


def test_verdict_weak_sector_synced_stock():
    verdict = build_sector_verdict(
        {
            "industry": "煤炭",
            "change_pct": -2.0,
            "rank": 80,
            "total": 86,
            "relative_pct": 0.3,
            "ranking_available": True,
        }
    )
    assert "板块走弱" in verdict
    assert "与板块基本同步" in verdict


def test_verdict_without_ranking():
    verdict = build_sector_verdict(
        {
            "industry": "银行",
            "change_pct": 0.5,
            "rank": None,
            "total": None,
            "relative_pct": 0.1,
            "ranking_available": False,
        }
    )
    assert "板块排名暂不可用" in verdict
    assert "0.5" in verdict


def test_verdict_without_industry():
    assert build_sector_verdict({}) == "行业归属暂不可用"


# --------------------------------------------------------------------------- #
# 服务：匹配 / 未匹配 / 无行业 / 缓存
# --------------------------------------------------------------------------- #
SNAPSHOT = [
    {"name": "银行板块", "change_pct": 0.83, "advancers": 32, "decliners": 12},
    {"name": "煤炭", "change_pct": -1.2, "advancers": 5, "decliners": 30},
    {"name": "半导体", "change_pct": 0.2, "advancers": 40, "decliners": 20},
]


class FakeClock:
    def __init__(self, value: date):
        self.value = value

    def __call__(self):
        return self.value


def test_analyze_matched_sector():
    calls = {"snapshot": 0, "industry": 0}

    def snapshot_provider():
        calls["snapshot"] += 1
        return list(SNAPSHOT)

    def industry_loader(symbol):
        calls["industry"] += 1
        return "银行"

    service = SectorService(
        snapshot_provider=snapshot_provider,
        industry_loader=industry_loader,
        now=FakeClock(date(2026, 10, 20)),
    )
    result = service.analyze("601169", 0.63)

    detail = result["detail"]
    assert result["available"] is True
    assert detail["industry"] == "银行"
    assert detail["change_pct"] == 0.83
    assert detail["rank"] == 1
    assert detail["total"] == 3
    assert detail["relative_pct"] == pytest.approx(-0.2)
    assert detail["advancers"] == 32
    assert detail["decliners"] == 12
    assert detail["ranking_available"] is True
    assert "银行板块 +0.83%（1/3）" in result["verdict"]

    # 同一交易日内重复调用命中缓存
    service.analyze("601169", 0.63)
    assert calls["snapshot"] == 1
    assert calls["industry"] == 1


def test_analyze_unmatched_industry_is_honest():
    service = SectorService(
        snapshot_provider=lambda: list(SNAPSHOT),
        industry_loader=lambda symbol: "证券",
        now=FakeClock(date(2026, 10, 20)),
    )
    result = service.analyze("600030", 1.0)

    assert result["available"] is False
    assert result["detail"]["industry"] == "证券"
    assert result["detail"]["ranking_available"] is False
    assert "暂未匹配" in result["verdict"]


def test_analyze_without_industry_degrades():
    service = SectorService(
        snapshot_provider=lambda: list(SNAPSHOT),
        industry_loader=lambda symbol: None,
        now=FakeClock(date(2026, 10, 20)),
    )
    result = service.analyze("601169", 0.63)

    assert result["available"] is False
    assert result["verdict"] == "行业归属暂不可用"


def test_snapshot_provider_failure_degrades():
    def boom():
        raise RuntimeError("network down")

    service = SectorService(
        snapshot_provider=boom,
        industry_loader=lambda symbol: "银行",
        now=FakeClock(date(2026, 10, 20)),
    )
    result = service.analyze("601169", 0.63)

    assert result["available"] is False
    assert "暂未匹配" in result["verdict"]


def test_snapshot_cache_invalidates_on_new_trade_date():
    calls = {"snapshot": 0}

    def snapshot_provider():
        calls["snapshot"] += 1
        return list(SNAPSHOT)

    clock = FakeClock(date(2026, 10, 20))
    service = SectorService(
        snapshot_provider=snapshot_provider,
        industry_loader=lambda symbol: "银行",
        now=clock,
    )
    service.analyze("601169", 0.63)
    assert calls["snapshot"] == 1

    # 跨交易日必须重新取数，避免次日仍用昨日排行
    clock.value = date(2026, 10, 21)
    service.analyze("601169", 0.63)
    assert calls["snapshot"] == 2
