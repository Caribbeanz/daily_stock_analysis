# -*- coding: utf-8 -*-
"""P2.2 对话·写编排 + 二次确认令牌单测（离线，注入假依赖）。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.services.action_service import ActionService
from src.services.chat_write_service import ChatWriteService
from src.services.confirmation_store import ConfirmationStore, is_cancel_text, is_confirm_text


# --------------------------------------------------------------------------- #
# 假依赖
# --------------------------------------------------------------------------- #
class FakeWatchlist:
    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    def list_watchlist(self, *, enabled_only: bool = True) -> List[Dict[str, Any]]:
        return list(self.rows)

    def add(self, symbol: str, **_: Any) -> Dict[str, Any]:
        self.rows.append({"symbol": symbol.upper()})
        return {"symbol": symbol}

    def remove(self, symbol: str) -> int:
        before = len(self.rows)
        self.rows = [r for r in self.rows if r["symbol"] != symbol.upper()]
        return before - len(self.rows)


class FakeRuleOverride:
    def __init__(self) -> None:
        self.weights = {"technical": 0.45, "market": 0.25, "news": 0.15, "fundamental": 0.15}
        self.stops: Dict[str, float] = {}

    def get_factor_weights(self) -> Dict[str, float]:
        return dict(self.weights)

    def adjust_factor_weights(self, changes: Dict[str, Any], *, base: Optional[Dict[str, float]] = None) -> Dict[str, float]:
        current = dict(base) if base else dict(self.weights)
        target = dict(current)
        specified = set()
        for key, value in changes.items():
            key = str(key).lower()
            target[key] = float(value)
            specified.add(key)
        specified_total = sum(target[k] for k in specified)
        others = [k for k in ("technical", "market", "news", "fundamental") if k not in specified]
        others_total = sum(current[k] for k in others)
        remainder = 1.0 - specified_total
        if others_total > 0:
            for key in others:
                target[key] = remainder * (current[key] / others_total)
        return target

    def resolve_factor_weights(self, weights: Dict[str, Any]) -> Dict[str, float]:
        merged = dict(self.weights)
        merged.update({str(k).lower(): float(v) for k, v in weights.items()})
        return merged

    def set_factor_weights(self, weights: Dict[str, Any]) -> Dict[str, Any]:
        before = dict(self.weights)
        self.weights = {str(k).lower(): float(v) for k, v in weights.items()}
        return {"before": before, "after": dict(self.weights)}

    def get_stop_loss(self, symbol: str) -> Optional[float]:
        return self.stops.get(str(symbol).upper())

    def set_stop_loss(self, symbol: str, price: Any) -> Dict[str, Any]:
        self.stops[str(symbol).upper()] = float(price)
        return {"symbol": symbol, "before": None, "after": float(price)}

    def remove_stop_loss(self, symbol: str) -> Dict[str, Any]:
        return {"symbol": symbol, "before": self.stops.pop(str(symbol).upper(), None), "after": None}


def _service(*, clock=None) -> tuple:
    watchlist = FakeWatchlist()
    store = ConfirmationStore(clock=clock) if clock else ConfirmationStore()
    service = ChatWriteService(
        action_service=ActionService(
            watchlist_service=watchlist,
            rule_override_service=FakeRuleOverride(),
        ),
        store=store,
    )
    return service, store, watchlist


# --------------------------------------------------------------------------- #
# 预览
# --------------------------------------------------------------------------- #
def test_preview_write_message_returns_token():
    service, store, _ = _service()
    result = service.preview_message("添加 600519 到自选股", actor="ou_owner")
    assert result["status"] == "pending_confirmation"
    assert result["token"]
    assert "加入自选股" in result["summary"]
    assert "确认" in result["message"]
    assert store.size() == 1


def test_preview_non_write_message():
    service, _, _ = _service()
    result = service.preview_message("KDJ 是什么")
    assert result["status"] == "not_write"


def test_preview_out_of_scope_rejected():
    service, _, _ = _service()
    result = service.preview_message("帮我买 100 股 600519 下单")
    assert result["status"] == "rejected"
    assert "下单" in result["message"]


def test_preview_whitelist_out_rejected():
    service, store, _ = _service()
    result = service.preview_message("把仓位比例调到 30%")
    assert result["status"] == "rejected"
    assert store.size() == 0  # 未通过校验不产生令牌


def test_preview_compound_summary_lists_all():
    service, _, _ = _service()
    result = service.preview_message("把技术面权重调到 60%，并把 600519 止损设为 15 元")
    assert result["status"] == "pending_confirmation"
    assert "技术面" in result["summary"]
    assert "止损" in result["summary"]


# --------------------------------------------------------------------------- #
# 确认执行
# --------------------------------------------------------------------------- #
def test_confirm_executes_and_returns_receipt():
    service, _, watchlist = _service()
    preview = service.preview_message("添加 600519 到自选股", actor="ou_owner")
    result = service.confirm(token=preview["token"], actor="ou_owner")
    assert result["status"] == "executed" and result["ok"] is True
    assert "✅" in result["receipt"]
    assert any(row["symbol"] == "600519" for row in watchlist.rows)


def test_confirm_is_single_use():
    service, _, _ = _service()
    preview = service.preview_message("添加 600519 到自选股")
    service.confirm(token=preview["token"])
    second = service.confirm(token=preview["token"])
    assert second["status"] == "nothing_pending"


def test_confirm_without_token_uses_latest_for_actor():
    service, _, watchlist = _service()
    service.preview_message("添加 600519 到自选股", actor="ou_owner")
    result = service.confirm(actor="ou_owner")
    assert result["status"] == "executed"
    assert len(watchlist.rows) == 1


def test_confirm_position_change_appends_risk_note():
    service, _, _ = _service()
    preview = service.preview_message("把 601919 持仓成本设为 12.9 数量 200 股")
    assert preview["status"] == "pending_confirmation"
    result = service.confirm(token=preview["token"])
    assert "风险" in result["receipt"]


def test_cancel_discards_pending():
    service, store, watchlist = _service()
    preview = service.preview_message("添加 600519 到自选股")
    result = service.cancel(token=preview["token"])
    assert result["status"] == "cancelled"
    assert store.size() == 0
    assert watchlist.rows == []


def test_confirm_after_expiry_is_nothing_pending():
    now = {"t": 1000.0}
    service, _, _ = _service(clock=lambda: now["t"])
    preview = service.preview_message("添加 600519 到自选股")
    now["t"] += 10_000  # 超过 TTL
    result = service.confirm(token=preview["token"])
    assert result["status"] == "nothing_pending"


# --------------------------------------------------------------------------- #
# 文本指令判定
# --------------------------------------------------------------------------- #
def test_confirm_cancel_text_detection():
    assert is_confirm_text("确认") and is_confirm_text("好")
    assert is_cancel_text("取消") and is_cancel_text("算了")
    assert not is_confirm_text("KDJ 是什么")
    assert ChatWriteService.handles_text("确认") == "confirm"
    assert ChatWriteService.handles_text("取消") == "cancel"
    assert ChatWriteService.handles_text("分析一下茅台") is None
