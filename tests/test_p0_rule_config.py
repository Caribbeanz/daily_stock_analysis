# -*- coding: utf-8 -*-
"""P0 规则配置加载器单元测试。"""

from __future__ import annotations

from pathlib import Path

from src.core import rule_config as rc


def test_load_risk_rules_from_repo_config():
    rules = rc.load_risk_rules()
    assert rules.stop_loss.ma == (5, 10, 20)
    assert rules.take_profit.targets[0] == 0.10
    assert rules.position.build_max == 0.30
    assert rules.position.cap == 0.65
    assert rules.position.dip_buy_max == 0.25
    assert rules.urgency.ma20_breach is True


def test_load_scenario_rules_from_repo_config():
    rules = rc.load_scenario_rules()
    assert rules.thresholds.rsi_dip_buy == 20.0
    assert rules.thresholds.bias_oversold == -10.0
    assert rules.trade_cost.min_t_range_pct == 0.015
    assert "pullback_after_breakout" in rules.build.daily_buy_points
    assert rules.up_market.reduce_on_ma5 == 0.20


def test_missing_file_falls_back_to_defaults(tmp_path: Path):
    rules = rc.load_risk_rules(tmp_path / "not_exist.yaml")
    assert rules.stop_loss.mode == "technical"
    assert rules.position.build_max == 0.30


def test_partial_file_fills_defaults(tmp_path: Path):
    path = tmp_path / "risk_rules.yaml"
    path.write_text("stop_loss:\n  mode: percent\n  percent: 0.05\n", encoding="utf-8")
    rules = rc.load_risk_rules(path)
    assert rules.stop_loss.mode == "percent"
    assert rules.stop_loss.percent == 0.05
    assert rules.position.cap == 0.65  # 未提供的字段回退默认
