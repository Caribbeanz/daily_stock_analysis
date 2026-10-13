# -*- coding: utf-8 -*-
"""P2.0 个股详情聚合（H5 主入口）单测。

覆盖：
- 纯函数组装：多因子明细、四要素、结论、降级说明；
- 编排服务：依赖注入 + 各子块失败隔离（NFR-8）。

全部离线：不触网、不读库（依赖一律注入假实现）。
API 层契约见 ``tests/test_p2_stock_overview_api.py``（需 fastapi）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import pytest

from src.services.stock_overview_service import (
    StockOverviewService,
    build_factors,
    build_overview,
    format_price,
    format_ratio,
)


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _signal(**overrides: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "id": 42,
        "stock_code": "601919",
        "stock_name": "中远海控",
        "action": "reduce",
        "score": 62,
        "confidence": 0.8,
        "entry_low": 12.1,
        "entry_high": 12.8,
        "stop_loss": 11.5,
        "target_price": 14.2,
        "reason": "[reduce] 接近箱体上沿，建议减仓",
        "risk_summary": "已持有 500 股，触及分批止盈第一档",
        "watch_conditions": "跌破 11.5 离场",
        "created_at": "2026-10-13T20:00:00",
        "metadata": {
            "scenario": "sideways",
            "trend": "up",
            "weekly_trend": "up",
            "multi_period_conflict": False,
            "is_urgent": False,
            "box_high": 13.05,
            "position_advice": {"action": "reduce", "ratio": 0.3, "reason": "减半仓落袋"},
        },
        "evidence": {
            "reasons": ["日线放量滞涨", "接近前高"],
            "risk_reasons": ["已浮盈 8%"],
        },
    }
    payload.update(overrides)
    return payload


class _FakeSignalService:
    def __init__(self, payload: Optional[Dict[str, Any]] = None, boom: bool = False):
        self._payload = payload
        self._boom = boom

    def get_latest_active(self, *, stock_code: str, limit: int = 1) -> Dict[str, Any]:
        if self._boom:
            raise RuntimeError("db down")
        if not self._payload:
            return {"items": [], "total": 0, "page": 1, "page_size": 1}
        return {"items": [self._payload], "total": 1, "page": 1, "page_size": 1}


# --------------------------------------------------------------------------- #
# 格式化
# --------------------------------------------------------------------------- #
def test_format_price_two_decimals_above_one():
    assert format_price(12.345) == "12.35"


def test_format_price_three_decimals_below_one():
    assert format_price(0.1234) == "0.123"


def test_format_price_handles_none_and_nan():
    assert format_price(None) == "-"
    assert format_price(float("nan")) == "-"


def test_format_ratio_percent():
    assert format_ratio(0.3) == "30%"
    assert format_ratio(None) == "-"


# --------------------------------------------------------------------------- #
# 多因子明细
# --------------------------------------------------------------------------- #
def test_build_factors_without_signal_is_empty():
    assert build_factors(None) == []
    assert build_factors({}) == []


def test_build_factors_derives_rows_from_metadata_and_evidence():
    rows = {row.key: row for row in build_factors(_signal())}
    assert rows["scenario"].value == "震荡趋势"
    assert rows["trend"].value == "向上"
    assert rows["trend"].tone == "positive"
    assert rows["weekly_trend"].value == "向上"
    assert rows["multi_period_conflict"].value.startswith("共振")
    assert rows["multi_period_conflict"].tone == "positive"
    assert rows["score"].value == "62/100"
    assert rows["confidence"].value == "0.80"
    assert rows["box_high"].value == "13.05"
    assert rows["risk_reasons"].tone == "warning"
    assert rows["risk_reasons"].value == "已浮盈 8%"
    assert "日线放量滞涨" in rows["scenario_reasons"].value


def test_build_factors_flags_multi_period_conflict():
    rows = {row.key: row for row in build_factors(_signal(metadata={"multi_period_conflict": True}))}
    assert rows["multi_period_conflict"].tone == "warning"
    assert "冲突" in rows["multi_period_conflict"].value


def test_build_factors_marks_down_trend_negative():
    rows = {row.key: row for row in build_factors(_signal(metadata={"trend": "down"}))}
    assert rows["trend"].tone == "negative"


# --------------------------------------------------------------------------- #
# 一屏组装
# --------------------------------------------------------------------------- #
def test_build_overview_maps_four_elements():
    overview = build_overview(
        symbol="601919",
        signal=_signal(),
        events=["601919 发布三季报预告"],
        quote={"current_price": 12.6, "change_percent": 1.2, "update_time": "2026-10-13T15:00:00"},
        now=datetime(2026, 10, 13, 20, 0, 0),
    )
    assert overview.symbol == "601919"
    assert overview.name == "中远海控"
    assert overview.action_label == "减仓"
    assert overview.scenario_label == "震荡趋势"
    assert overview.conclusion.startswith("减仓（震荡趋势）：")
    # 四要素
    assert overview.entry_zone == "12.10~12.80"
    assert overview.stop_loss == 11.5
    assert overview.target_price == 14.2
    assert overview.position_ratio_label == "30%"
    assert "仓位建议 30%" in overview.discipline
    assert "止损 11.50" in overview.discipline
    # 事件与溯源
    assert overview.events == ["601919 发布三季报预告"]
    assert overview.signal_id == 42
    assert overview.signal_created_at == "2026-10-13T20:00:00"
    # 距止损 / 距目标
    assert overview.distance_to_stop_pct == pytest.approx((12.6 - 11.5) / 11.5 * 100)
    assert overview.distance_to_target_pct == pytest.approx((12.6 - 14.2) / 14.2 * 100)
    assert overview.degraded == []


def test_build_overview_marks_urgent_for_sell():
    overview = build_overview(symbol="601919", signal=_signal(action="sell"))
    assert overview.is_urgent is True


def test_build_overview_without_signal_is_degraded_but_safe():
    overview = build_overview(symbol="601919", signal=None, events=[])
    assert overview.conclusion == "暂无决策信号"
    assert overview.action_label == "观望"
    assert overview.entry_zone == "-"
    assert overview.factors == []
    assert any("暂无" in note for note in overview.degraded)


def test_build_overview_without_quote_hides_distance_and_explains():
    overview = build_overview(symbol="601919", signal=_signal(), quote=None)
    assert overview.quote is None
    assert overview.distance_to_stop_pct is None
    assert any("行情" in note for note in overview.degraded)


def test_build_overview_serializes_to_dict_payload():
    payload = build_overview(symbol="601919", signal=_signal()).to_dict()
    assert set(payload) >= {
        "symbol",
        "conclusion",
        "entry_zone",
        "stop_loss",
        "target_price",
        "position_ratio_label",
        "factors",
        "events",
        "degraded",
        "disclaimer",
    }
    assert payload["disclaimer"]


def test_build_overview_single_sided_entry_zone():
    overview = build_overview(symbol="601919", signal=_signal(entry_low=None, entry_high=12.8))
    assert overview.entry_zone == "12.80"


def test_build_overview_reads_scenario_from_metadata_fallback():
    overview = build_overview(symbol="601919", signal=_signal(metadata={"scenario": "breakout"}))
    assert overview.scenario == "breakout"
    assert overview.scenario_label == "突破待确认"


# --------------------------------------------------------------------------- #
# 编排服务：注入 + 失败隔离
# --------------------------------------------------------------------------- #
def test_service_load_with_injected_dependencies():
    service = StockOverviewService(
        signal_service=_FakeSignalService(_signal()),
        event_collector=lambda symbol: [f"{symbol} 事件"],
        quote_loader=lambda symbol: {"current_price": 12.6, "change_percent": 1.2},
    )
    payload = service.load("601919")
    assert payload["action_label"] == "减仓"
    assert payload["events"] == ["601919 事件"]
    assert payload["quote"]["current_price"] == 12.6
    assert payload["degraded"] == []


def test_service_signal_failure_degrades_without_raising():
    service = StockOverviewService(
        signal_service=_FakeSignalService(_signal(), boom=True),
        event_collector=lambda symbol: [],
        quote_loader=lambda symbol: None,
    )
    payload = service.load("601919")
    assert payload["conclusion"] == "暂无决策信号"
    assert any("读取决策信号失败" in note for note in payload["degraded"])


def test_service_event_and_quote_failure_are_isolated():
    def boom_collector(symbol: str) -> List[str]:
        raise RuntimeError("intel down")

    def boom_quote(symbol: str) -> Optional[Dict[str, Any]]:
        raise RuntimeError("quote down")

    service = StockOverviewService(
        signal_service=_FakeSignalService(_signal()),
        event_collector=boom_collector,
        quote_loader=boom_quote,
    )
    payload = service.load("601919")
    # 结论仍在（不被子块失败阻断）
    assert payload["action_label"] == "减仓"
    assert payload["events"] == []
    assert payload["quote"] is None
    assert any("事件源不可用" in note for note in payload["degraded"])
    assert any("行情获取失败" in note for note in payload["degraded"])


def test_service_with_quote_false_skips_quote_loader():
    called: List[str] = []

    def loader(symbol: str) -> Dict[str, Any]:
        called.append(symbol)
        return {"current_price": 1.0}

    service = StockOverviewService(
        signal_service=_FakeSignalService(_signal()),
        event_collector=lambda symbol: [],
        quote_loader=loader,
    )
    payload = service.load("601919", with_quote=False)
    assert called == []
    assert payload["quote"] is None


def test_service_normalizes_symbol_case():
    seen: List[str] = []

    def loader(symbol: str) -> Dict[str, Any]:
        seen.append(symbol)
        return {"current_price": 1.0}

    service = StockOverviewService(
        signal_service=_FakeSignalService(_signal()),
        event_collector=lambda symbol: [],
        quote_loader=loader,
    )
    payload = service.load(" sh601919 ")
    assert payload["symbol"] == "SH601919"
    assert seen == ["SH601919"]

# --------------------------------------------------------------------------- #
# CLI 冒烟
# --------------------------------------------------------------------------- #
def test_cli_dry_run_renders(monkeypatch, capsys):
    from src.services import stock_overview_service as module

    monkeypatch.setattr(
        module.StockOverviewService,
        "load",
        lambda self, symbol, with_quote=True: build_overview(
            symbol=symbol, signal=_signal(), events=["测试事件"]
        ).to_dict(),
    )
    exit_code = module._main(["601919", "--no-quote"])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "减仓（震荡趋势）" in output
    assert "测试事件" in output
