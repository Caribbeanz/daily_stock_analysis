# -*- coding: utf-8 -*-
"""P1 · 规则体检回测单测（统计 / 质量门 / 出场模拟）。"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from src.services.rule_backtest import (
    DEFAULT_GATE,
    RuleBacktester,
    SymbolBacktest,
    Trade,
    _aggregate,
    _apply_gate,
    _benchmark_return,
    BacktestReport,
    load_gate_thresholds,
)


def _df(closes, highs=None, lows=None, start="2026-01-01"):
    n = len(closes)
    highs = highs or [c + 0.2 for c in closes]
    lows = lows or [c - 0.2 for c in closes]
    return pd.DataFrame(
        {
            "date": [f"2026-01-{i + 1:02d}" for i in range(n)],
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1_000_000] * n,
        }
    )


# --------------------------- 聚合统计 --------------------------- #
def _trade(index, ret, symbol="X"):
    return Trade(
        symbol=symbol,
        entry_index=index,
        entry_date=f"2026-01-{index + 1:02d}",
        entry_price=10.0,
        stop_loss=None,
        target_price=None,
        exit_index=index + 1,
        return_pct=ret,
    )


def test_aggregate_win_rate_and_profit_factor():
    stat = SymbolBacktest(symbol="X")
    _aggregate(stat, [_trade(0, 5.0), _trade(1, -2.0), _trade(2, 3.0), _trade(3, -1.0)])
    assert stat.samples == 4
    assert stat.wins == 2
    assert stat.win_rate == pytest.approx(0.5)
    assert stat.avg_win == pytest.approx(4.0)
    assert stat.avg_loss == pytest.approx(-1.5)
    assert stat.profit_factor == pytest.approx(8.0 / 3.0, rel=1e-6)
    assert 0.0 < stat.max_drawdown < 0.05


def test_aggregate_no_losses_profit_factor_infinite():
    stat = SymbolBacktest(symbol="X")
    _aggregate(stat, [_trade(0, 5.0), _trade(1, 2.0)])
    assert stat.win_rate == pytest.approx(1.0)
    assert math.isinf(stat.profit_factor)


def test_aggregateempty_is_safe():
    stat = SymbolBacktest(symbol="X")
    _aggregate(stat, [])
    assert stat.samples == 0
    assert stat.win_rate == 0.0


# --------------------------- 质量门 --------------------------- #
def _report(**overrides):
    stat = SymbolBacktest(symbol="")
    stat.samples = overrides.get("samples", 50)
    stat.win_rate = overrides.get("win_rate", 0.6)
    stat.profit_factor = overrides.get("profit_factor", 2.0)
    stat.max_drawdown = overrides.get("max_drawdown", 0.1)
    stat.avg_return = overrides.get("avg_return", 1.5)
    report = BacktestReport(overall=stat, gate=overrides.get("gate") or dict(DEFAULT_GATE))
    _apply_gate(report)
    return report


def test_gate_passes_on_good_metrics():
    report = _report()
    assert report.passed is True
    assert report.violated == []


def test_gate_ignores_low_win_rate_by_default():
    """新口径（期望收益制）：胜率默认仅作参考，不再拦截。"""
    report = _report(win_rate=0.26)
    assert report.passed is True
    assert not any("胜率" in v for v in report.violated)


def test_gate_enforces_win_rate_when_configured():
    """把 min_win_rate 设回 0.5，即可恢复"胜率≥50%"硬门槛。"""
    gate = {**DEFAULT_GATE, "min_win_rate": 0.5}
    report = _report(win_rate=0.4, gate=gate)
    assert report.passed is False
    assert any("胜率" in v for v in report.violated)


def test_gate_fails_on_negative_expectation():
    report = _report(avg_return=-0.8)
    assert report.passed is False
    assert any("期望收益" in v for v in report.violated)


def test_gate_fails_on_few_samples():
    report = _report(samples=10)
    assert report.passed is False
    assert any("样本数" in v for v in report.violated)


def test_gate_fails_on_deep_drawdown():
    report = _report(max_drawdown=0.4)
    assert report.passed is False
    assert any("回撤" in v for v in report.violated)


# --------------------------- 出场模拟 --------------------------- #
def test_simulate_horizon_exit():
    df = _df([10, 10, 10, 10, 10, 10, 10, 10, 10, 12])
    bt = RuleBacktester(horizon_days=15, warmup=2)
    trade = bt._simulate_trade("X", df, 0, {"entry_price": 10.0, "stop_loss": None, "target_price": None})
    assert trade.exit_reason == "horizon"
    assert trade.exit_price == pytest.approx(12.0)
    assert trade.return_pct == pytest.approx(20.0)


def test_simulate_stop_exit_takes_priority():
    df = _df([10, 10, 10, 10, 10], highs=[10.5] * 5, lows=[9.5, 9.0, 9.0, 9.0, 9.0])
    bt = RuleBacktester(horizon_days=15, warmup=1)
    trade = bt._simulate_trade("X", df, 0, {"entry_price": 10.0, "stop_loss": 9.8, "target_price": None})
    assert trade.exit_reason == "stop"
    assert trade.exit_price == pytest.approx(9.8)
    assert trade.return_pct == pytest.approx(-2.0)


def test_simulate_target_exit():
    df = _df([10, 10, 10, 10, 10], highs=[10.5, 11.0, 11.0, 11.0, 11.0], lows=[9.9] * 5)
    bt = RuleBacktester(horizon_days=15, warmup=1)
    trade = bt._simulate_trade("X", df, 0, {"entry_price": 10.0, "stop_loss": None, "target_price": 10.4})
    assert trade.exit_reason == "target"
    assert trade.exit_price == pytest.approx(10.4)
    assert trade.return_pct == pytest.approx(4.0)


# --------------------------- 端到端（注入取数） --------------------------- #
def _uptrend(n=120):
    closes = []
    price = 10.0
    for i in range(n):
        price *= 1.004 if i % 5 < 3 else 0.998
        closes.append(round(price, 3))
    return _df(closes)


def test_backtest_symbol_runs_on_synthetic_uptrend():
    bt = RuleBacktester(horizon_days=10, warmup=30)
    stat = bt.backtest_symbol("TEST", _uptrend(140))
    assert isinstance(stat, SymbolBacktest)
    assert stat.bars == 140
    # 统计恒等式：wins 不超过样本数
    assert stat.wins <= stat.samples


def test_run_with_injected_fetch_and_gate():
    bt = RuleBacktester(horizon_days=10, warmup=30)

    def fetch(symbol, days):
        return _uptrend(150)

    report = bt.run(["600000", "600001"], fetch=fetch, gate={"min_samples": 1})
    assert isinstance(report, BacktestReport)
    assert report.overall is not None
    assert len(report.symbols) == 2
    # 至少产出了统计结构
    assert report.overall.samples >= 0


def test_run_handles_fetch_failure_gracefully():
    bt = RuleBacktester(horizon_days=10, warmup=30)

    def fetch(symbol, days):
        raise RuntimeError("network down")

    report = bt.run(["600000"], fetch=fetch)
    assert len(report.symbols) == 1
    assert report.symbols[0].samples == 0
    assert any("取数失败" in n for n in report.notes)


def test_benchmark_return_uses_close_range():
    df = _df([10.0] * 10 + [12.0] * 10)
    value = _benchmark_return(df, warmup=0)
    assert value == pytest.approx(20.0)


def test_report_markdown_contains_verdict():
    bt = RuleBacktester(horizon_days=10, warmup=30)
    report = bt.run(["600000"], fetch=lambda s, d: _uptrend(150))
    md = report.to_markdown()
    assert "规则体检报告" in md
    assert ("通过" in md) or ("未通过" in md)


# --------------------------- 质量门配置 --------------------------- #
def test_load_gate_thresholds_falls_back_on_missing_file(tmp_path):
    thresholds = load_gate_thresholds(str(tmp_path / "does_not_exist.yaml"))
    assert thresholds == DEFAULT_GATE


def test_load_gate_thresholds_reads_repo_config():
    thresholds = load_gate_thresholds()
    assert set(thresholds) == set(DEFAULT_GATE)
    assert all(isinstance(v, float) for v in thresholds.values())


def test_repo_gate_config_is_expectation_based():
    """仓库默认口径为期望收益制：期望收益>0，胜率不强制。"""
    thresholds = load_gate_thresholds()
    assert thresholds["min_expectation"] == pytest.approx(0.0)
    assert thresholds["min_win_rate"] == pytest.approx(0.0)


def test_report_markdown_contains_expectation_threshold():
    bt = RuleBacktester(horizon_days=10, warmup=30)
    report = bt.run(["600000"], fetch=lambda s, d: _uptrend(150))
    md = report.to_markdown()
    assert "期望收益" in md
