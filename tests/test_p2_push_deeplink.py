# -*- coding: utf-8 -*-
"""P2.0 推送 → H5 深链单测（FR-14.0.2 / D6-I1）。

核心断言：
- 未配置 H5 基址时，推送文案**与 P1 完全一致**（不产生任何链接，向后兼容）；
- 配置后仅"紧急 / 重点"项附带个股详情深链，链接形如
  ``{base}/m/stocks/{symbol}?from=push``；
- 推送文案**自带完整四要素摘要**（关键信息不得只放在 H5 里 · D6-I1）。
"""

from __future__ import annotations


from datetime import datetime

from src.services.daily_digest import build_digest
from src.services.p1_push_service import P1PushService


def _signal(symbol: str, action: str, with_position: bool = True) -> dict:
    payload = {
        "symbol": symbol,
        "name": f"标的{symbol}",
        "action": action,
        "scenario": "sideways",
        "score": 66,
        "reason": f"[{action}] 接近压力位",
        "entry_low": 10.0,
        "entry_high": 10.6,
        "stop_loss": 9.4,
        "target_price": 12.0,
        "risk_summary": "轻仓试探",
    }
    if with_position:
        payload["metadata"] = {"position_advice": {"ratio": 0.2, "reason": "试仓两成"}}
    return payload


NOW = datetime(2026, 10, 13, 20, 0, 0)


def test_markdown_without_h5_base_url_has_no_link():
    digest = build_digest(
        [_signal("601919", "reduce"), _signal("601288", "watch")],
        events=[],
        now=NOW,
    )
    markdown = digest.to_markdown()
    assert "http://" not in markdown
    assert "详情" not in markdown


def test_markdown_adds_detail_link_for_actionable_items():
    digest = build_digest(
        [_signal("601919", "reduce")],
        events=[],
        now=NOW,
        h5_base_url="http://192.168.1.10:8000",
    )
    markdown = digest.to_markdown()
    assert "http://192.168.1.10:8000/m/stocks/601919?from=push" in markdown


def test_markdown_normalizes_trailing_slash():
    digest = build_digest(
        [_signal("601919", "reduce")],
        events=[],
        now=NOW,
        h5_base_url="http://192.168.1.10:8000/",
    )
    assert "http://192.168.1.10:8000/m/stocks/601919?from=push" in digest.to_markdown()


def test_watch_items_do_not_get_links():
    digest = build_digest([_signal("601288", "watch")], events=[], now=NOW, h5_base_url="http://10.0.0.2:8000")
    markdown = digest.to_markdown()
    assert "601288" in markdown
    assert "/m/stocks/601288" not in markdown


def test_payload_keeps_self_contained_summary_even_with_links():
    """D6-I1：手机不在内网时链接打不开，关键信息必须仍在文案里。"""
    digest = build_digest(
        [_signal("601919", "reduce")],
        events=["601919 发布回购公告"],
        now=NOW,
        h5_base_url="http://192.168.1.10:8000",
    )
    markdown = digest.to_markdown()
    assert "减仓" in markdown
    assert "10.00~10.60" in markdown  # 建仓区间
    assert "9.40" in markdown  # 止损
    assert "12.00" in markdown  # 目标
    assert "20%" in markdown  # 仓位
    assert "601919 发布回购公告" in markdown  # 事件与风险


def test_detail_url_helper_returns_empty_without_base():
    digest = build_digest([_signal("601919", "reduce")], now=NOW)
    assert digest.detail_url("601919") == ""


def test_push_service_resolves_base_url_from_env(monkeypatch):
    monkeypatch.setenv("HSLB_H5_BASE_URL", "http://192.168.1.10:8000/")
    service = P1PushService()
    assert service._resolve_h5_base_url() == "http://192.168.1.10:8000/"


def test_push_service_explicit_base_url_wins(monkeypatch):
    monkeypatch.setenv("HSLB_H5_BASE_URL", "http://env-host:8000")
    service = P1PushService(h5_base_url="http://explicit-host:8000")
    assert service._resolve_h5_base_url() == "http://explicit-host:8000"


def test_push_service_falls_back_to_config(monkeypatch):
    monkeypatch.delenv("HSLB_H5_BASE_URL", raising=False)
    import src.config as config_module

    class _Config:
        hslb_h5_base_url = "http://192.168.1.10:8000"

    monkeypatch.setattr(config_module, "get_config", lambda: _Config())
    service = P1PushService()
    assert service._resolve_h5_base_url() == "http://192.168.1.10:8000"


def test_push_service_returns_empty_when_nothing_configured(monkeypatch):
    monkeypatch.delenv("HSLB_H5_BASE_URL", raising=False)
    import src.config as config_module

    class _Config:
        hslb_h5_base_url = None

    monkeypatch.setattr(config_module, "get_config", lambda: _Config())
    service = P1PushService()
    assert service._resolve_h5_base_url() == ""
