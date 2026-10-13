# -*- coding: utf-8 -*-
"""高抛低吸助手 · 持仓录入服务（解锁风控真实计算）

背景：P0 详设中"持仓初始录入"曾留空待 P2，导致风控引擎始终按**无持仓**运行——
真实浮盈亏、保本止损（移动止损）、分批止盈、金字塔加仓等都无法生效。

本模块提供**覆盖语义**的持仓录入入口（纯代码、可脚本化、可重复执行）：

- ``set_position``  ：设置某标的的持仓（成本价 / 数量）；重复调用是**覆盖**而非累加；
- ``remove_position``：清空某标的持仓；
- ``list_positions`` ：读取当前持仓（复用 P0 的 ``P0SignalBuilder.load_positions``）。

底层复用项目既有 ``PortfolioService``（账户 + 交易流水 → 重放 → 落库
``portfolio_positions``），因此**与 H5 / API / 报告共用同一数据源**（NFR-9 四端同源）。

用法::

    python -m src.services.position_service --set 601919:12.34:1000 --set 513100:1.234:10000
    python -m src.services.position_service --remove 601919
    python -m src.services.position_service --list
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["PositionService", "DEFAULT_ACCOUNT_NAME"]

DEFAULT_ACCOUNT_NAME = "高抛低吸默认账户"
DEFAULT_MARKET = "cn"
DEFAULT_BASE_CURRENCY = "CNY"


class PositionService:
    """持仓录入 / 查询服务（覆盖语义）。"""

    def __init__(
        self,
        portfolio_service: Any = None,
        *,
        account_name: str = DEFAULT_ACCOUNT_NAME,
        market: str = DEFAULT_MARKET,
        base_currency: str = DEFAULT_BASE_CURRENCY,
    ):
        self._portfolio = portfolio_service
        self.account_name = account_name
        self.market = market
        self.base_currency = base_currency

    # ---------------- 依赖 ---------------- #
    @property
    def portfolio(self):
        if self._portfolio is None:
            from src.services.portfolio_service import PortfolioService

            self._portfolio = PortfolioService()
        return self._portfolio

    # ---------------- 账户 ---------------- #
    def ensure_account(self, name: Optional[str] = None) -> int:
        """幂等获取（或创建）默认账户，返回 account_id。"""
        target = (name or self.account_name).strip() or DEFAULT_ACCOUNT_NAME
        for account in self.portfolio.list_accounts():
            if str(account.get("name") or "").strip() == target:
                return int(account["id"])
        created = self.portfolio.create_account(
            name=target,
            broker=None,
            market=self.market,
            base_currency=self.base_currency,
        )
        return int(created["id"])

    # ---------------- 录入 / 移除 ---------------- #
    def set_position(
        self,
        symbol: str,
        *,
        avg_cost: float,
        quantity: float,
        market: Optional[str] = None,
        trade_date: Optional[date] = None,
        account_name: Optional[str] = None,
        note: Optional[str] = None,
        refresh_realtime: bool = False,
    ) -> Dict[str, Any]:
        """设置某标的持仓（覆盖语义）。"""
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        if float(quantity) <= 0 or float(avg_cost) <= 0:
            raise ValueError("avg_cost 与 quantity 必须 > 0")

        account_id = self.ensure_account(account_name)
        removed = self._clear_symbol(account_id, symbol)
        trade = self.portfolio.record_trade(
            account_id=account_id,
            symbol=symbol,
            trade_date=trade_date or date.today(),
            side="buy",
            quantity=float(quantity),
            price=float(avg_cost),
            market=market or self.market,
            note=(note or "高抛低吸助手持仓录入").strip(),
        )
        self.portfolio.get_portfolio_snapshot(account_id=account_id, include_realtime=refresh_realtime)
        return {
            "symbol": symbol,
            "account_id": account_id,
            "avg_cost": float(avg_cost),
            "quantity": float(quantity),
            "cleared_trades": removed,
            "trade_id": trade.get("id"),
        }

    def remove_position(
        self,
        symbol: str,
        *,
        account_name: Optional[str] = None,
        refresh_realtime: bool = False,
    ) -> Dict[str, Any]:
        """清空某标的持仓（删除其全部交易流水后重算）。"""
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        account_id = self.ensure_account(account_name)
        removed = self._clear_symbol(account_id, symbol)
        self.portfolio.get_portfolio_snapshot(account_id=account_id, include_realtime=refresh_realtime)
        return {"symbol": symbol, "account_id": account_id, "cleared_trades": removed}

    def list_positions(self) -> Dict[str, Any]:
        """读取当前持仓（``symbol -> PositionView``）；复用 P0 读取逻辑，四端同源。"""
        from src.services.p0_signal_builder import P0SignalBuilder

        return P0SignalBuilder.load_positions()

    # ---------------- 内部 ---------------- #
    def _clear_symbol(self, account_id: int, symbol: str) -> int:
        """删除某账户下该标的的全部交易流水，返回删除条数。"""
        removed = 0
        # list_trade_events 的 page_size 上限为 100；每轮删完再从第 1 页取。
        for _ in range(100):  # 防御性上限，避免异常情况下死循环
            page = self.portfolio.list_trade_events(account_id=account_id, symbol=symbol, page=1, page_size=100)
            items = page.get("items") or []
            if not items:
                break
            deleted_this_round = 0
            for item in items:
                try:
                    if self.portfolio.delete_trade_event(int(item["id"])):
                        removed += 1
                        deleted_this_round += 1
                except Exception as exc:  # 单条删除失败不影响其余
                    logger.warning("删除交易 %s 失败：%s", item.get("id"), exc)
            if deleted_this_round == 0:  # 无法推进则退出，避免死循环
                break
        return removed


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_set_arg(raw: str) -> Tuple[str, float, float]:
    """解析 ``SYMBOL:COST:QTY``。"""
    parts = [p.strip() for p in str(raw).split(":")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(f"--set 需要 SYMBOL:COST:QTY 三段，收到：{raw}")
    symbol, cost, qty = parts
    try:
        return symbol.upper(), float(cost), float(qty)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--set 的 COST/QTY 必须是数字：{raw}") from exc


def _parse_date_arg(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    return datetime.strptime(raw.strip(), "%Y-%m-%d").date()


def _print_positions(positions: Dict[str, Any]) -> None:
    if not positions:
        print("当前持仓：空")
        return
    print(f"当前持仓（{len(positions)} 只）：")
    for symbol, view in sorted(positions.items()):
        avg_cost = getattr(view, "avg_cost", 0.0)
        quantity = getattr(view, "quantity", 0.0)
        print(f"  · {symbol}  成本 {avg_cost:.4f}  数量 {quantity:.0f}  市值约 {avg_cost * quantity:.2f}")


def _main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(description="高抛低吸助手 · 持仓录入（解锁风控真实计算）")
    parser.add_argument("--set", dest="sets", action="append", type=_parse_set_arg, default=None,
                        metavar="SYMBOL:COST:QTY", help="设置持仓（覆盖语义）；可重复")
    parser.add_argument("--remove", action="append", default=None, metavar="SYMBOL", help="清空某标的持仓；可重复")
    parser.add_argument("--list", action="store_true", help="打印当前持仓")
    parser.add_argument("--account", default=DEFAULT_ACCOUNT_NAME, help="账户名（缺省为默认账户）")
    parser.add_argument("--date", default=None, help="交易日 YYYY-MM-DD（缺省今天）")
    parser.add_argument("--realtime", action="store_true", help="刷新时拉取实时价（需要网络）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    service = PositionService(account_name=args.account)
    trade_date = _parse_date_arg(args.date)
    results: List[Dict[str, Any]] = []

    for symbol, cost, qty in (args.sets or []):
        results.append(service.set_position(
            symbol, avg_cost=cost, quantity=qty,
            trade_date=trade_date, refresh_realtime=args.realtime,
        ))
    for symbol in (args.remove or []):
        results.append(service.remove_position(symbol, refresh_realtime=args.realtime))

    positions = service.list_positions()
    if args.json:
        print(json.dumps(
            {"ops": results, "positions": {k: {"avg_cost": getattr(v, "avg_cost", 0.0),
                                               "quantity": getattr(v, "quantity", 0.0)}
                                           for k, v in positions.items()}},
            ensure_ascii=False, indent=2,
        ))
    else:
        for row in results:
            if "avg_cost" in row:
                print(f"[设置] {row['symbol']}  成本 {row['avg_cost']:.4f}  数量 {row['quantity']:.0f}"
                      f"（清理旧流水 {row['cleared_trades']} 条）")
            else:
                print(f"[移除] {row['symbol']}（清理旧流水 {row['cleared_trades']} 条）")
        if results:
            print("")
        _print_positions(positions)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
