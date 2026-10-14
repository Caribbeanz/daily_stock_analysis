# -*- coding: utf-8 -*-
"""P2.2 规则覆盖层单测（四因子权重 / 单标的止损覆盖 / 加权合成）。

全部离线：不触网、不读真实库；规则文件写入临时目录。
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from src.core.rule_config import (
    FACTOR_KEYS,
    FactorWeightRules,
    load_factor_weights,
    load_stop_loss_overrides,
)
from src.services import factor_weight_service
from src.services.rule_override_service import InvalidRuleOverride, RuleOverrideService


# --------------------------------------------------------------------------- #
# 加载器默认值 / 归一化
# --------------------------------------------------------------------------- #
def test_factor_weights_defaults_match_fr72():
    defaults = FactorWeightRules()
    assert defaults.technical == 0.45
    assert defaults.market == 0.25
    assert defaults.news == 0.15
    assert defaults.fundamental == 0.15
    assert math.isclose(defaults.total, 1.0, rel_tol=1e-9)


def test_load_factor_weights_missing_file_returns_defaults(tmp_path):
    rules = load_factor_weights(tmp_path / "not-exist.yaml")
    assert rules.as_dict() == FactorWeightRules().as_dict()


def test_load_factor_weights_normalizes_bad_sum(tmp_path):
    path = tmp_path / "factor_weights.yaml"
    path.write_text(
        "version: 1\nweights:\n  technical: 6\n  market: 2\n  news: 1\n  fundamental: 1\n",
        encoding="utf-8",
    )
    rules = load_factor_weights(path)
    assert math.isclose(rules.total, 1.0, rel_tol=1e-9)
    assert math.isclose(rules.technical, 0.6, rel_tol=1e-9)


def test_load_stop_loss_overrides_skips_invalid(tmp_path):
    path = tmp_path / "stop_loss_overrides.yaml"
    path.write_text(
        "version: 1\noverrides:\n  '601919': 15.0\n  'bad': abc\n  '513180': 0.48\n  'zero': 0\n",
        encoding="utf-8",
    )
    result = load_stop_loss_overrides(path)
    assert result == {"601919": 15.0, "513180": 0.48}


# --------------------------------------------------------------------------- #
# 写服务：四因子权重
# --------------------------------------------------------------------------- #
def test_set_factor_weights_roundtrip(tmp_path):
    service = RuleOverrideService(rules_dir=tmp_path)
    result = service.set_factor_weights({"technical": 0.6, "market": 0.15, "news": 0.1, "fundamental": 0.15})
    assert result["after"]["technical"] == 0.6
    # 重新读取（新实例）确认已落盘
    again = RuleOverrideService(rules_dir=tmp_path).get_factor_weights()
    assert math.isclose(again["technical"], 0.6, rel_tol=1e-9)
    assert math.isclose(sum(again.values()), 1.0, rel_tol=1e-9)


def test_set_factor_weights_accepts_percent_string(tmp_path):
    service = RuleOverrideService(rules_dir=tmp_path)
    result = service.set_factor_weights({"technical": "60%", "market": "20%", "news": "10%", "fundamental": "10%"})
    assert math.isclose(result["after"]["technical"], 0.6, rel_tol=1e-9)


def test_set_factor_weights_rejects_bad_sum(tmp_path):
    service = RuleOverrideService(rules_dir=tmp_path)
    with pytest.raises(InvalidRuleOverride):
        service.set_factor_weights({"technical": 0.9})


def test_set_factor_weights_rejects_unknown_dimension(tmp_path):
    service = RuleOverrideService(rules_dir=tmp_path)
    with pytest.raises(InvalidRuleOverride):
        service.set_factor_weights({"volume": 0.5})


def test_partial_update_keeps_other_dimensions(tmp_path):
    service = RuleOverrideService(rules_dir=tmp_path)
    before = service.get_factor_weights()
    # 只给出要改的维度，未给出的沿用当前值；总和仍须为 1.0
    result = service.set_factor_weights({"technical": 0.55, "market": 0.15})
    assert result["before"] == before
    assert math.isclose(result["after"]["news"], before["news"], rel_tol=1e-9)
    assert math.isclose(result["after"]["fundamental"], before["fundamental"], rel_tol=1e-9)
    assert math.isclose(sum(result["after"].values()), 1.0, rel_tol=1e-9)


def test_partial_update_breaking_sum_is_rejected(tmp_path):
    service = RuleOverrideService(rules_dir=tmp_path)
    with pytest.raises(InvalidRuleOverride):
        service.set_factor_weights({"technical": 0.55})  # 未同步下调其他维度 → 合计 110%


# --------------------------------------------------------------------------- #
# 写服务：单标的止损覆盖
# --------------------------------------------------------------------------- #
def test_set_and_remove_stop_loss_roundtrip(tmp_path):
    service = RuleOverrideService(rules_dir=tmp_path)
    set_result = service.set_stop_loss("601919", 15.0)
    assert set_result["before"] is None and set_result["after"] == 15.0
    assert RuleOverrideService(rules_dir=tmp_path).get_stop_loss("601919") == 15.0

    remove_result = service.remove_stop_loss("601919")
    assert remove_result["before"] == 15.0
    assert RuleOverrideService(rules_dir=tmp_path).get_stop_loss("601919") is None


def test_set_stop_loss_rejects_non_positive(tmp_path):
    service = RuleOverrideService(rules_dir=tmp_path)
    with pytest.raises(InvalidRuleOverride):
        service.set_stop_loss("601919", -1)
    with pytest.raises(InvalidRuleOverride):
        service.set_stop_loss("", 15)


# --------------------------------------------------------------------------- #
# 止损覆盖在风控引擎中生效
# --------------------------------------------------------------------------- #
def _snapshot(close=16.0, ma=None, low_20=14.0):
    from src.core.indicators import IndicatorSnapshot

    return IndicatorSnapshot(
        close=close,
        prev_close=close,
        ma=ma or {5: 15.5, 10: 15.0, 20: 14.5},
        prev_ma={20: 14.5},
        low_20=low_20,
        atr=0.3,
    )


def test_risk_engine_uses_explicit_override():
    from src.core.risk_engine import RiskEngine

    engine = RiskEngine()
    auto = engine.compute_stop_loss(_snapshot())
    explicit = engine.compute_stop_loss(_snapshot(), stop_loss_override=15.2)
    assert not math.isclose(auto, explicit)
    assert math.isclose(explicit, 15.2, rel_tol=1e-9)


def test_risk_engine_override_capped_at_close():
    from src.core.risk_engine import RiskEngine

    engine = RiskEngine()
    # 用户把止损设到现价之上 → 收敛到现价（避免"止损高于市价"的悖论）
    result = engine.compute_stop_loss(_snapshot(close=14.0), stop_loss_override=15.0)
    assert math.isclose(result, 14.0, rel_tol=1e-9)


def test_risk_engine_default_unchanged_without_override():
    from src.core.risk_engine import RiskEngine

    engine = RiskEngine()
    assert math.isclose(
        engine.compute_stop_loss(_snapshot()),
        engine.compute_stop_loss(_snapshot(), stop_loss_override=None),
        rel_tol=1e-9,
    )


# --------------------------------------------------------------------------- #
# 四因子加权合成
# --------------------------------------------------------------------------- #
def test_synthesize_renormalizes_over_available_dimensions():
    scores = {"technical": 1.0, "market": None, "news": None, "fundamental": None}
    weights = {key: 0.25 for key in FACTOR_KEYS}
    result = factor_weight_service.synthesize(scores, weights)
    assert result["available"] == ["technical"]
    assert math.isclose(result["score"], 1.0, rel_tol=1e-9)
    assert result["label"] == "偏多"


def test_synthesize_no_data_returns_none():
    scores = {key: None for key in FACTOR_KEYS}
    weights = {key: 0.25 for key in FACTOR_KEYS}
    result = factor_weight_service.synthesize(scores, weights)
    assert result["score"] is None
    assert result["label"] == "无数据"
    assert "仅更新权重" in result["note"]


def test_derive_dimension_scores_from_signal_and_events():
    signal = {"metadata": {"trend": "up"}, "score": 70}
    scores = factor_weight_service.derive_dimension_scores(
        signal, ["公司发布公告", "监管处罚落地", "立案调查"]
    )
    assert scores["technical"] is not None and scores["technical"] > 0
    assert scores["news"] is not None and scores["news"] < 0
    assert scores["market"] is None  # 当前信号未承载大盘依据 → 诚实标注无数据
    assert scores["fundamental"] is None


def test_render_synthesis_is_human_readable():
    scores = {"technical": 0.8, "market": None, "news": None, "fundamental": None}
    weights = {"technical": 0.45, "market": 0.25, "news": 0.15, "fundamental": 0.15}
    text = factor_weight_service.render_synthesis(factor_weight_service.synthesize(scores, weights))
    assert "综合倾向" in text and "技术面" in text
