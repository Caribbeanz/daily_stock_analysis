# -*- coding: utf-8 -*-
"""P3 · 完整回测报告单测（场景分档 FR-12.3 / 局限性 FR-12.4 / JSON 安全）。"""

from __future__ import annotations

import json
import math

import pandas as pd
import pytest

from src.services.rule_backtest import (
    LIMITATIONS,
    SCENARIO_LABELS,
    BacktestReport,
    RuleBacktester,
    SymbolBacktest,
    Trade,
    _aggregate,
    _scenario_breakdown,
)


def _df(closes):
    n = len(closes)
    return pd.DataFrame(
        {
            "date": [f"2026-01-{i + 1:02d}" for i in range(n)],
            "open": closes,
            "high": [c + 0.2 for c in closes],
            "low": [c - 0.2 for c in closes],
            "close": closes,
            "volume": [1_000_000] * n,
        }
    )


def _trade(index, ret, scenario, symbol="X"):
    return Trade(
        symbol=symbol,
        entry_index=index,
        entry_date=f"2026-01-{index + 1:02d}",
        entry_price=10.0,
        stop_loss=None,
        target_price=None,
        scenario=scenario,
        exit_index=index + 1,
        return_pct=ret,
    )


# --------------------------- 场景分档 --------------------------- #
def test_trade_to_dict_carries_scenario():
    assert _trade(0, 5.0, "up").to_dict()["scenario"] == "up"


def test_scenario_breakdown_groups_and_orders():
    trades = [
        _trade(0, 5.0, "sideways"),
        _trade(1, -2.0, "up"),
        _trade(2, 3.0, "up"),
        _trade(3, -1.0, "down"),
        _trade(4, 1.0, ""),  # 空场景 → unknown
    ]
    breakdown = _scenario_breakdown(trades)
    assert list(breakdown.keys()) == ["up", "down", "sideways", "unknown"]
    assert breakdown["up"]["samples"] == 2
    assert breakdown["up"]["win_rate"] == pytest.approx(0.5)
    assert breakdown["down"]["avg_return"] == pytest.approx(-1.0)
    assert set(SCENARIO_LABELS) >= {"up", "down", "sideways"}


def test_scenario_breakdown_excludes_trades_key():
    breakdown = _scenario_breakdown([_trade(0, 5.0, "up")])
    assert "trades" not in breakdown["up"]


# --------------------------- JSON 安全 --------------------------- #
def test_profit_factor_infinite_is_json_safe():
    stat = SymbolBacktest(symbol="X")
    _aggregate(stat, [_trade(0, 5.0, "up"), _trade(1, 3.0, "up")])
    assert math.isinf(stat.profit_factor)  # 数据类仍保留 +∞
    payload = stat.to_dict()
    assert payload["profit_factor"] is None  # 序列化层转为 None
    json.dumps(payload)  # 不应抛异常


def test_report_to_dict_is_json_serializable():
    stat = SymbolBacktest(symbol="X")
    _aggregate(stat, [_trade(0, 5.0, "up")])
    report = BacktestReport(overall=stat, scenarios=_scenario_breakdown(stat.trades))
    json.dumps(report.to_dict(), ensure_ascii=False)


# --------------------------- 渲染 --------------------------- #
def test_markdown_has_scenario_and_limitations_sections():
    stat = SymbolBacktest(symbol="X")
    trades = [_trade(0, 5.0, "up"), _trade(1, -2.0, "down")]
    _aggregate(stat, trades)
    report = BacktestReport(overall=stat, scenarios=_scenario_breakdown(trades))
    md = report.to_markdown()
    assert "按场景分档表现（FR-12.3）" in md
    assert "上涨" in md and "下跌" in md
    assert "局限性与免责说明（FR-12.4）" in md
    for item in LIMITATIONS:
        assert item in md
    # 保留 P1 既有标题，避免破坏既有体检报告消费者
    assert "规则体检报告" in md


# --------------------------- 端到端（注入信号） --------------------------- #
def test_run_populates_scenarios_with_injected_signals(monkeypatch):
    df = _df([10, 10, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20])
    bt = RuleBacktester(horizon_days=3, warmup=2)
    scenarios = ["up", "down", "sideways"]
    counter = {"i": 0}

    def fake_signal(window):
        scenario = scenarios[counter["i"] % len(scenarios)]
        counter["i"] += 1
        return {
            "entry_price": float(window["close"].iloc[-1]),
            "stop_loss": None,
            "target_price": None,
            "scenario": scenario,
            "sub_action": "build",
        }

    monkeypatch.setattr(bt, "_signal_at", fake_signal)
    report = bt.run(["TEST"], fetch=lambda symbol, days: df)
    assert report.overall is not None and report.overall.samples >= 1
    assert set(report.scenarios.keys()) <= set(scenarios)
    assert sum(v["samples"] for v in report.scenarios.values()) == report.overall.samples
    assert report.to_dict()["scenarios"]
