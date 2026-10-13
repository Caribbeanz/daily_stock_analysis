# -*- coding: utf-8 -*-
"""P1 · 每日概览组装单测（分级 / 降噪 / 免责声明 / 事件板块）。"""

from __future__ import annotations

from datetime import datetime

from src.services.daily_digest import (
    ACTION_LABELS,
    DISCLAIMER,
    build_digest,
    classify_severity,
    normalize_signal,
)


def _signal(symbol, action, **kw):
    base = {"stock_code": symbol, "stock_name": kw.pop("name", symbol), "action": action}
    base.update(kw)
    return base


# --------------------------- 分级 --------------------------- #
def test_classify_severity_levels():
    assert classify_severity("sell", False) == "urgent"
    assert classify_severity("buy", False) == "highlight"
    assert classify_severity("add", False) == "highlight"
    assert classify_severity("reduce", False) == "highlight"
    assert classify_severity("watch", False) == "watch"
    assert classify_severity("hold", False) == "watch"
    # is_urgent 强制升级（即使 action 本身不紧急）
    assert classify_severity("watch", True) == "urgent"


# --------------------------- 归一化 --------------------------- #
def test_normalize_signal_from_serialized_record():
    """模拟 decision_signals 的序列化结构（含 metadata 内嵌字段）。"""
    raw = {
        "stock_code": "601919",
        "stock_name": "中远海控",
        "action": "buy",
        "confidence": 0.85,
        "score": 72,
        "entry_low": 8.3,
        "entry_high": 8.55,
        "stop_loss": 7.8,
        "target_price": 9.4,
        "reason": "[buy] 周线向上，回踩 MA20 不破",
        "watch_conditions": "回踩不破可加",
        "risk_summary": "仓位建议 30%",
        "metadata": {
            "asset_type": "stock",
            "scenario": "up",
            "is_urgent": False,
            "position_advice": {"action": "build", "ratio": 0.3, "reason": "首次建仓不超过 3 成"},
        },
    }
    item = normalize_signal(raw)
    assert item.symbol == "601919"
    assert item.name == "中远海控"
    assert item.action == "buy"
    assert item.severity == "highlight"
    assert item.scenario_label == "上涨趋势"
    assert item.position_ratio == 0.3
    assert item.entry_zone() == "8.30~8.55"
    assert item.stop_loss == 7.8
    assert "建仓" in item.action_label


def test_normalize_signal_error_gives_failed():
    item = normalize_signal({"stock_code": "513100", "action": "watch", "error": "no data"})
    assert item.severity == "failed"


def test_normalize_signal_accepts_symbol_alias():
    """SymbolResult 风格（symbol/name）也要能归一化。"""
    item = normalize_signal({"symbol": "601169", "name": "北京银行", "action": "watch", "scenario": "sideways"})
    assert item.symbol == "601169"
    assert item.severity == "watch"


# --------------------------- 组装 --------------------------- #
def test_build_digest_sections_and_counts():
    signals = [
        _signal("601919", "sell", scenario="down", is_urgent=True, score=90, reason="跌破止损"),
        _signal("601169", "buy", scenario="up", score=70, reason="突破回踩确认"),
        _signal("601818", "watch", scenario="sideways"),
        _signal("513100", "watch", scenario="unknown", error="no data"),
    ]
    digest = build_digest(signals, as_of="2026-10-13", now=datetime(2026, 10, 13, 20, 0))
    assert digest.counts["urgent"] == 1
    assert digest.counts["highlight"] == 1
    assert digest.counts["watch"] == 1
    assert digest.counts["failed"] == 1
    assert digest.has_actions is True
    assert "紧急" in digest.headline()


def test_build_digest_markdown_contains_required_parts():
    signals = [_signal("601919", "buy", name="中远海控", scenario="up", reason="周线向上")]
    digest = build_digest(signals, as_of="2026-10-13", events=["601919 中报预约 10-25 披露"])
    md = digest.to_markdown()
    assert "每日概览" in md
    assert "中远海控(601919)" in md
    assert "事件与风险" in md
    assert "中报预约" in md
    assert DISCLAIMER in md


def test_build_digest_collapses_repeat_watch():
    signals = [_signal("601818", "watch"), _signal("601169", "buy")]
    previous = {"as_of": "2026-10-12", "symbols": {"601818": "watch"}}
    digest = build_digest(signals, as_of="2026-10-13", previous_state=previous)
    # 与昨日一致的观望被折叠，但重点信号保留
    assert digest.counts["watch"] == 0
    assert digest.counts["collapsed"] == 1
    assert digest.counts["highlight"] == 1


def test_build_digest_never_collapses_actions():
    """紧急/重点信号即使与昨日一致也不折叠。"""
    signals = [_signal("601919", "buy"), _signal("601169", "sell")]
    previous = {"symbols": {"601919": "buy", "601169": "sell"}}
    digest = build_digest(signals, as_of="2026-10-13", previous_state=previous)
    assert digest.counts["collapsed"] == 0
    assert digest.counts["highlight"] == 1
    assert digest.counts["urgent"] == 1


def test_build_digest_empty_signals_no_action():
    digest = build_digest([], as_of="2026-10-13")
    assert digest.has_actions is False
    assert "无操作信号" in digest.headline()
    assert DISCLAIMER in digest.to_markdown()


def test_digest_state_roundtrip_shape():
    digest = build_digest([_signal("601919", "watch")], as_of="2026-10-13")
    assert digest.state["as_of"] == "2026-10-13"
    assert digest.state["symbols"]["601919"] == "watch"


def test_action_labels_cover_p0_actions():
    for action in ("build", "buy", "dip_buy", "add", "hold", "reduce", "sell", "watch"):
        assert action in ACTION_LABELS
