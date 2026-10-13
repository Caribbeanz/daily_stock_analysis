# -*- coding: utf-8 -*-
"""持仓录入服务单测：stub PortfolioService，不触真实库 / 不联网。"""

from __future__ import annotations

import pytest

from src.services.position_service import PositionService, _parse_set_arg


# --------------------------- 测试替身 --------------------------- #
class _StubPortfolio:
    def __init__(self):
        self.accounts = []
        self.trades = []
        self.snapshots = []
        self._acc_seq = 1
        self._trade_seq = 1

    def list_accounts(self, include_inactive=False):
        return [dict(a) for a in self.accounts]

    def create_account(self, *, name, broker, market, base_currency):
        acc = {"id": self._acc_seq, "name": name, "market": market, "base_currency": base_currency}
        self._acc_seq += 1
        self.accounts.append(acc)
        return dict(acc)

    def record_trade(self, **kwargs):
        row = {"id": self._trade_seq, **kwargs}
        self._trade_seq += 1
        self.trades.append(row)
        return {"id": row["id"]}

    def list_trade_events(self, *, account_id, symbol, page, page_size):
        items = [t for t in self.trades if t["account_id"] == account_id and t["symbol"] == symbol]
        return {"items": items, "total": len(items), "page": page, "page_size": page_size}

    def delete_trade_event(self, trade_id):
        before = len(self.trades)
        self.trades = [t for t in self.trades if t["id"] != trade_id]
        return len(self.trades) < before

    def get_portfolio_snapshot(self, *, account_id, include_realtime):
        self.snapshots.append({"account_id": account_id, "include_realtime": include_realtime})
        return {"account_id": account_id}


def _svc():
    return PositionService(portfolio_service=_StubPortfolio())


# --------------------------- 账户 --------------------------- #
def test_ensure_account_is_idempotent():
    svc = _svc()
    a1 = svc.ensure_account()
    a2 = svc.ensure_account()
    assert a1 == a2
    assert len(svc.portfolio.accounts) == 1


def test_ensure_account_named():
    svc = _svc()
    a1 = svc.ensure_account("另一个账户")
    a2 = svc.ensure_account()
    assert a1 != a2
    assert len(svc.portfolio.accounts) == 2


# --------------------------- 设置持仓 --------------------------- #
def test_set_position_creates_trade_and_snapshot():
    svc = _svc()
    res = svc.set_position("601919", avg_cost=12.34, quantity=1000)
    assert res["symbol"] == "601919"
    assert res["cleared_trades"] == 0
    assert len(svc.portfolio.trades) == 1
    assert svc.portfolio.trades[0]["side"] == "buy"
    assert svc.portfolio.trades[0]["price"] == pytest.approx(12.34)
    assert svc.portfolio.trades[0]["quantity"] == pytest.approx(1000)
    assert len(svc.portfolio.snapshots) == 1


def test_set_position_is_overwrite_not_accumulate():
    svc = _svc()
    svc.set_position("601919", avg_cost=12.34, quantity=1000)
    svc.set_position("601919", avg_cost=13.00, quantity=500)
    # 只保留最后一次设置的一条流水
    assert len(svc.portfolio.trades) == 1
    assert svc.portfolio.trades[0]["price"] == pytest.approx(13.00)
    assert svc.portfolio.trades[0]["quantity"] == pytest.approx(500)


def test_set_position_multiple_symbols():
    svc = _svc()
    svc.set_position("601919", avg_cost=12.34, quantity=1000)
    svc.set_position("513100", avg_cost=1.234, quantity=10000)
    assert len(svc.portfolio.trades) == 2
    assert len(svc.portfolio.accounts) == 1


@pytest.mark.parametrize("cost,qty", [(0, 100), (-1, 100), (10, 0), (10, -5)])
def test_set_position_rejects_non_positive(cost, qty):
    svc = _svc()
    with pytest.raises(ValueError):
        svc.set_position("601919", avg_cost=cost, quantity=qty)


def test_set_position_requires_symbol():
    svc = _svc()
    with pytest.raises(ValueError):
        svc.set_position("  ", avg_cost=10, quantity=100)


# --------------------------- 移除持仓 --------------------------- #
def test_remove_position_clears_trades():
    svc = _svc()
    svc.set_position("601919", avg_cost=12.34, quantity=1000)
    res = svc.remove_position("601919")
    assert res["cleared_trades"] == 1
    assert svc.portfolio.trades == []


def test_remove_missing_position_is_noop():
    svc = _svc()
    res = svc.remove_position("601919")
    assert res["cleared_trades"] == 0


# --------------------------- 读取持仓 --------------------------- #
def test_list_positions_delegates_to_p0(monkeypatch):
    from src.services.p0_signal_builder import P0SignalBuilder

    monkeypatch.setattr(P0SignalBuilder, "load_positions", staticmethod(lambda: {"601919": "VIEW"}))
    svc = _svc()
    assert svc.list_positions() == {"601919": "VIEW"}


# --------------------------- CLI 解析 --------------------------- #
def test_parse_set_arg_ok():
    assert _parse_set_arg("601919:12.34:1000") == ("601919", 12.34, 1000.0)


def test_parse_set_arg_lowercases_symbol():
    assert _parse_set_arg("513100:1.5:200")[0] == "513100"


@pytest.mark.parametrize("raw", ["601919:12.34", "601919:abc:100", "601919:12:34:56"])
def test_parse_set_arg_invalid(raw):
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        _parse_set_arg(raw)
