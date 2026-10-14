# -*- coding: utf-8 -*-
"""P2.2 写操作执行门面单测（白名单 / 二次确认 / 逐条回执 / 审计）。

全部离线：WatchlistService / PositionService / RuleOverrideService 均注入假实现。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from src.agent.intent.resolver import WriteOp
from src.services.action_service import ActionService, describe_ops
from src.services.rule_override_service import InvalidRuleOverride


# --------------------------------------------------------------------------- #
# 假依赖
# --------------------------------------------------------------------------- #
class FakeWatchlist:
    def __init__(self, rows: Optional[List[str]] = None) -> None:
        self.rows = [{"symbol": s} for s in (rows or [])]

    def list_watchlist(self, *, enabled_only: bool = True) -> List[Dict[str, Any]]:
        return list(self.rows)

    def add(self, symbol: str, **_: Any) -> Dict[str, Any]:
        self.rows.append({"symbol": symbol.upper()})
        return {"symbol": symbol}

    def remove(self, symbol: str) -> int:
        before = len(self.rows)
        self.rows = [r for r in self.rows if r["symbol"] != symbol.upper()]
        return before - len(self.rows)


class FakePositionView:
    def __init__(self, symbol: str, avg_cost: float, quantity: float) -> None:
        self.symbol = symbol
        self.avg_cost = avg_cost
        self.quantity = quantity

    def has_position(self) -> bool:
        return self.quantity > 0


class FakePosition:
    def __init__(self, positions: Optional[Dict[str, Any]] = None) -> None:
        self.positions = dict(positions or {})
        self.set_calls: List[Dict[str, Any]] = []

    def list_positions(self) -> Dict[str, Any]:
        return dict(self.positions)

    def set_position(self, symbol: str, *, avg_cost: float, quantity: float, **_: Any) -> Dict[str, Any]:
        self.set_calls.append({"symbol": symbol, "avg_cost": avg_cost, "quantity": quantity})
        self.positions[symbol] = FakePositionView(symbol, avg_cost, quantity)
        return {"symbol": symbol}

    def remove_position(self, symbol: str, **_: Any) -> Dict[str, Any]:
        self.positions.pop(symbol, None)
        return {"symbol": symbol, "cleared_trades": 2}


class FakeRuleOverride:
    def __init__(self) -> None:
        self.weights = {"technical": 0.45, "market": 0.25, "news": 0.15, "fundamental": 0.15}
        self.stops: Dict[str, float] = {}

    def get_factor_weights(self) -> Dict[str, float]:
        return dict(self.weights)

    def resolve_factor_weights(self, weights: Dict[str, Any]) -> Dict[str, float]:
        merged = dict(self.weights)
        for key, value in weights.items():
            merged[str(key).lower()] = float(value)
        total = sum(merged.values())
        if abs(total - 1.0) > 0.001:
            raise InvalidRuleOverride("合计必须为 100%")
        return merged

    def adjust_factor_weights(
        self, changes: Dict[str, Any], *, base: Optional[Dict[str, float]] = None
    ) -> Dict[str, float]:
        current = dict(base) if base else dict(self.weights)
        target = dict(current)
        specified = set()
        for key, value in changes.items():
            key = str(key).lower()
            target[key] = float(value)
            specified.add(key)
        if not specified:
            return target
        specified_total = sum(target[k] for k in specified)
        if specified_total > 1.0 + 0.001:
            raise InvalidRuleOverride("指定维度合计超过 100%")
        others = [k for k in ("technical", "market", "news", "fundamental") if k not in specified]
        others_total = sum(current[k] for k in others)
        remainder = 1.0 - specified_total
        if others_total > 0:
            for key in others:
                target[key] = remainder * (current[key] / others_total)
        return target

    def set_factor_weights(self, weights: Dict[str, Any]) -> Dict[str, Any]:
        before = dict(self.weights)
        self.weights = self.resolve_factor_weights(weights)
        return {"before": before, "after": dict(self.weights)}

    def get_stop_loss(self, symbol: str) -> Optional[float]:
        return self.stops.get(str(symbol).upper())

    def set_stop_loss(self, symbol: str, price: Any) -> Dict[str, Any]:
        before = self.stops.get(str(symbol).upper())
        self.stops[str(symbol).upper()] = float(price)
        return {"symbol": symbol, "before": before, "after": float(price)}

    def remove_stop_loss(self, symbol: str) -> Dict[str, Any]:
        before = self.stops.pop(str(symbol).upper(), None)
        return {"symbol": symbol, "before": before, "after": None}


def _service(**overrides: Any) -> ActionService:
    kwargs = {
        "watchlist_service": FakeWatchlist(),
        "position_service": FakePosition(),
        "rule_override_service": FakeRuleOverride(),
    }
    kwargs.update(overrides)
    return ActionService(**kwargs)


# --------------------------------------------------------------------------- #
# 预览（plan）
# --------------------------------------------------------------------------- #
def test_plan_watchlist_add_marks_state():
    service = _service(watchlist_service=FakeWatchlist(["600519"]))
    plan = service.plan([WriteOp("watchlist_add", {"symbol": "600519"})])
    assert plan.valid
    assert plan.items[0].before == {"in_watchlist": True}
    assert "保持不变" in plan.items[0].description


def test_plan_set_weight_redistributes_remainder():
    service = _service()
    plan = service.plan([WriteOp("set_weight", {"factor": "technical", "value": 0.6})])
    item = plan.items[0]
    assert item.valid is True
    assert item.after["technical"] == 0.6
    assert abs(sum(item.after.values()) - 1.0) < 1e-9
    assert "调整后" in item.description


def test_plan_set_weight_compound_accumulates():
    service = _service()
    plan = service.plan(
        [
            WriteOp("set_weight", {"factor": "technical", "value": 0.6}),
            WriteOp("set_weight", {"factor": "market", "value": 0.2}),
        ]
    )
    assert [item.valid for item in plan.items] == [True, True]
    # 第二条基于第一条的结果继续调整
    assert plan.items[1].before["technical"] == 0.6
    assert abs(sum(plan.items[1].after.values()) - 1.0) < 1e-9


def test_plan_rejects_non_whitelisted_op():
    service = _service()
    plan = service.plan([WriteOp("set_position_ratio", {"value": 0.3})])
    assert plan.valid is False
    assert "白名单" in plan.items[0].error


def test_plan_rejects_clear_all():
    service = _service()
    plan = service.plan([WriteOp("clear_all", {})])
    assert plan.valid is False
    assert "清空" in plan.items[0].error


def test_plan_compound_mixed_validity():
    service = _service()
    plan = service.plan(
        [
            WriteOp("watchlist_add", {"symbol": "600519"}),
            WriteOp("clear_all", {}),
        ]
    )
    assert plan.valid is False  # 含不可执行项 → 整体不可直接确认
    assert [item.valid for item in plan.items] == [True, False]


def test_plan_set_position_requires_cost_and_quantity():
    service = _service()
    plan = service.plan([WriteOp("set_position", {"symbol": "601919"})])
    assert plan.items[0].valid is False
    assert "成本价与数量" in plan.items[0].error


# --------------------------------------------------------------------------- #
# 执行（execute）
# --------------------------------------------------------------------------- #
def test_execute_requires_confirmation():
    service = _service()
    with pytest.raises(Exception):
        service.execute([WriteOp("watchlist_add", {"symbol": "600519"})], confirmed=False)


def test_execute_watchlist_add_writes_and_receipts():
    fake = FakeWatchlist()
    service = _service(watchlist_service=fake)
    plan = service.execute([WriteOp("watchlist_add", {"symbol": "600519"})], confirmed=True)
    assert plan.items[0].status == "ok"
    assert "✅" in plan.items[0].receipt
    assert any(row["symbol"] == "600519" for row in fake.rows)


def test_execute_set_weight_receipt_contains_transition():
    service = _service()
    plan = service.execute(
        [WriteOp("set_weight", {"factor": "technical", "value": 0.6})],
        confirmed=True,
    )
    item = plan.items[0]
    assert item.status == "ok"
    assert "45%→60%" in item.receipt


def test_execute_set_weight_valid_receipt():
    service = _service()
    plan = service.execute(
        [WriteOp("set_weight", {"factor": "technical", "value": 0.55})],
        confirmed=True,
    )
    receipt = plan.items[0].receipt
    assert plan.items[0].status == "ok"
    assert "45%→55%" in receipt


def test_execute_compound_receipts_each_item():
    service = _service()
    plan = service.execute(
        [
            WriteOp("watchlist_add", {"symbol": "600519"}),
            WriteOp("set_stop_loss", {"symbol": "600519", "value": 15.0}),
        ],
        confirmed=True,
    )
    assert [item.status for item in plan.items] == ["ok", "ok"]
    text = plan.receipt()
    assert "600519" in text and "止损" in text


def test_execute_rejected_item_does_not_block_others():
    service = _service()
    plan = service.execute(
        [
            WriteOp("clear_all", {}),
            WriteOp("watchlist_add", {"symbol": "600519"}),
        ],
        confirmed=True,
    )
    assert plan.items[0].status == "rejected"
    assert plan.items[1].status == "ok"


def test_execute_set_position_then_remove():
    fake = FakePosition()
    service = _service(position_service=fake)
    service.execute([WriteOp("set_position", {"symbol": "601919", "cost": 12.9, "quantity": 200})], confirmed=True)
    assert fake.set_calls[0]["avg_cost"] == 12.9
    plan = service.execute([WriteOp("remove_position", {"symbol": "601919"})], confirmed=True)
    assert plan.items[0].status == "ok"
    assert "清空" in plan.items[0].receipt


def test_execute_remove_position_without_position_is_rejected():
    service = _service()
    plan = service.execute([WriteOp("remove_position", {"symbol": "601919"})], confirmed=True)
    assert plan.items[0].status == "rejected"
    assert "无持仓" in plan.items[0].receipt


# --------------------------------------------------------------------------- #
# 审计
# --------------------------------------------------------------------------- #
def test_audit_sink_receives_records():
    records: List[Dict[str, Any]] = []
    service = _service(audit_sink=records.append)
    service.execute([WriteOp("watchlist_add", {"symbol": "600519"})], confirmed=True, actor="ou_owner", source="feishu")
    assert len(records) == 1
    assert records[0]["action"] == "watchlist_add"
    assert records[0]["status"] == "ok"
    assert records[0]["actor"] == "ou_owner"
    assert records[0]["confirmed"] is True


# --------------------------------------------------------------------------- #
# 人话描述
# --------------------------------------------------------------------------- #
def test_describe_ops_renders_human_text():
    texts = describe_ops(
        [
            WriteOp("watchlist_add", {"symbol": "600519"}),
            WriteOp("set_stop_loss", {"symbol": "600519", "value": 15}),
        ]
    )
    assert texts[0] == "把 600519 加入自选股"
    assert "15.00" in texts[1]
