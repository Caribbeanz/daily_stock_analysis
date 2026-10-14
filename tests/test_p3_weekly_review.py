# -*- coding: utf-8 -*-
"""P3 周度复盘：服务 / 执行回填仓储 单测（离线注入 + 真实 SQLite 临时库）。

运行方式（规避 tests/conftest.py 的重型前端依赖）：
    PYTHONPATH=. pytest --noconftest tests/test_p3_weekly_review.py
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from src.repositories.execution_feedback_repo import (
    EXECUTION_STATUSES,
    ExecutionFeedbackRepository,
)
from src.services.weekly_review_service import (
    DISCLAIMER,
    WeeklyReviewService,
    week_key_of,
    week_window,
)
from src.storage import DatabaseManager


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
class FakeSignalService:
    def __init__(self, *, signals=None, latest=None, raise_on_list=False):
        self._signals = signals if signals is not None else [
            {
                "id": 1,
                "stock_code": "601919",
                "stock_name": "中远海控",
                "action": "build",
                "created_at": "2026-10-13 20:00:00",
                "entry_low": 12.0,
                "entry_high": 12.5,
                "stop_loss": 11.2,
                "target_price": 14.0,
                "metadata": {"scenario": "up"},
            },
            {
                "id": 2,
                "stock_code": "513180",
                "stock_name": "恒生科技ETF",
                "action": "watch",
                "created_at": "2026-10-14 20:00:00",
            },
        ]
        self._latest = latest
        self._raise_on_list = raise_on_list

    def list_signals(self, **kwargs):
        if self._raise_on_list:
            raise RuntimeError("boom")
        return {"items": list(self._signals)}

    def get_latest_active(self, *, stock_code, limit=1):
        if self._latest is not None:
            return {"items": list(self._latest)}
        return {
            "items": [
                {
                    "stock_code": stock_code,
                    "stock_name": "示例",
                    "action": "build",
                    "entry_low": 1.0,
                    "entry_high": 1.1,
                    "stop_loss": 0.9,
                    "target_price": 1.3,
                }
            ]
        }


class FakeOutcomeService:
    def __init__(self, *, items=None, raise_on_read=False):
        self._items = items if items is not None else [
            {"eval_status": "completed", "outcome": "hit", "direction_correct": True, "stock_return_pct": 3.2}
        ]
        self._raise_on_read = raise_on_read
        self.refreshed = 0

    def run_outcomes(self, **kwargs):
        self.refreshed += 1
        return {}

    def list_signal_outcomes(self, signal_id):
        if self._raise_on_read:
            raise RuntimeError("boom")
        return {"items": list(self._items)}


class FakePositionService:
    def __init__(self, positions=None, *, raise_on_list=False):
        self._positions = positions if positions is not None else {
            "601919": SimpleNamespace(symbol="601919", avg_cost=12.927, quantity=200, peak_price=12.9, market="cn")
        }
        self._raise = raise_on_list

    def list_positions(self):
        if self._raise:
            raise RuntimeError("boom")
        return dict(self._positions)


class FakeWatchlistService:
    def __init__(self, symbols=None):
        self._symbols = symbols if symbols is not None else ["601919", "513180"]

    def list_watchlist(self, *, enabled_only=True):
        return [{"symbol": s} for s in self._symbols]


class FakeFeedbackRepo:
    def __init__(self, mapping=None):
        self._mapping = mapping or {}

    def get(self, symbol, week_key):
        return self._mapping.get(symbol)


def _make_bars(closes, dates):
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1] * len(closes),
        }
    )


def fake_fetch(symbol, days):
    return _make_bars([10.0, 10.5, 11.0], ["2026-10-06", "2026-10-13", "2026-10-14"])


def build_service(**overrides) -> WeeklyReviewService:
    kwargs = dict(
        signal_service=FakeSignalService(),
        outcome_service=FakeOutcomeService(),
        position_service=FakePositionService(),
        watchlist_service=FakeWatchlistService(),
        feedback_repo=FakeFeedbackRepo(),
        fetch=fake_fetch,
        market_provider=lambda week_start: [
            {"kind": "index", "name": "上证指数", "week_return_pct": 1.2, "value": 3800.0}
        ],
        hotspot_provider=lambda: [
            {"kind": "sector", "name": "半导体", "week_return_pct": 5.5, "note": "领涨"}
        ],
    )
    kwargs.update(overrides)
    return WeeklyReviewService(**kwargs)


NOW = datetime(2026, 10, 14, 10, 0)  # 周三


# --------------------------------------------------------------------------- #
# 时间窗口
# --------------------------------------------------------------------------- #
def test_week_window_monday_to_now():
    start, end = week_window(NOW)
    assert start.strftime("%Y-%m-%d") == "2026-10-12"
    assert start.hour == 0 and start.minute == 0
    assert end == NOW


def test_week_key_is_monday():
    assert week_key_of(NOW) == "2026-10-12"
    assert week_key_of(datetime(2026, 10, 18, 23, 59)) == "2026-10-12"  # 周日仍属本周


# --------------------------------------------------------------------------- #
# FR-11.1 信号回顾与胜率
# --------------------------------------------------------------------------- #
def test_signal_summary_win_rate_and_avg_return():
    review = build_service().build(now=NOW)
    summary = review.signal_summary
    assert summary.total == 2
    assert summary.evaluated == 2
    assert summary.correct == 2
    assert summary.win_rate == pytest.approx(1.0)
    assert summary.avg_return_pct == pytest.approx(3.2)
    assert summary.by_action == {"build": 1, "watch": 1}


def test_signal_scenario_prefers_metadata():
    review = build_service().build(now=NOW)
    first = next(s for s in review.signals if s.symbol == "601919")
    assert first.scenario == "up"
    assert first.entry_price == pytest.approx(12.25)  # (12.0+12.5)/2


def test_signal_unevaluated_counts_when_no_outcome():
    service = build_service(outcome_service=FakeOutcomeService(items=[]))
    review = service.build(now=NOW)
    assert review.signal_summary.evaluated == 0
    assert review.signal_summary.unevaluated == 2
    assert review.signal_summary.win_rate is None
    assert "暂无已评估样本" in review.to_markdown()


def test_signal_block_degrades_on_error():
    service = build_service(signal_service=FakeSignalService(raise_on_list=True))
    review = service.build(now=NOW)
    assert review.signals == []
    assert any("信号板块数据暂缺" in n for n in review.notes)


def test_outcome_read_error_does_not_break():
    service = build_service(outcome_service=FakeOutcomeService(raise_on_read=True))
    review = service.build(now=NOW)
    assert review.signal_summary.total == 2
    assert review.signal_summary.evaluated == 0


# --------------------------------------------------------------------------- #
# FR-11.2 持仓与执行回填
# --------------------------------------------------------------------------- #
def test_position_week_return_and_pnl():
    review = build_service().build(now=NOW)
    pos = review.positions[0]
    assert pos.symbol == "601919"
    assert pos.week_open_price == pytest.approx(10.0)  # 周一(10-12)或之前最后一根 = 10-06 的 10.0
    assert pos.week_return_pct == pytest.approx((11.0 - pos.week_open_price) / pos.week_open_price * 100, rel=1e-6)
    assert pos.week_pnl_amount == pytest.approx((11.0 - pos.week_open_price) * 200, rel=1e-6)
    assert pos.total_return_pct == pytest.approx((11.0 - 12.927) / 12.927 * 100, abs=1e-3)


def test_execution_label_maps_status():
    service = build_service(feedback_repo=FakeFeedbackRepo({"601919": {"execution_status": "ignored"}}))
    review = service.build(now=NOW)
    assert review.positions[0].execution_label == "忽略"
    assert review.execution_counts() == {"ignored": 1}


def test_position_block_degrades_on_error():
    service = build_service(position_service=FakePositionService(raise_on_list=True))
    review = service.build(now=NOW)
    assert review.positions == []
    assert any("持仓板块数据暂缺" in n for n in review.notes)


# --------------------------------------------------------------------------- #
# FR-11.3 / FR-11.4
# --------------------------------------------------------------------------- #
def test_market_and_next_week_sections():
    review = build_service().build(now=NOW)
    assert [m.name for m in review.market] == ["上证指数", "半导体"]
    assert len(review.next_week) == 2
    assert review.next_week[0].entry_zone() == "1.00 ~ 1.10"


def test_next_week_accepts_explicit_symbols():
    review = build_service().build(now=NOW, symbols=["600519"])
    assert [f.symbol for f in review.next_week] == ["600519"]


def test_market_provider_error_degrades():
    def boom(_week_start):
        raise RuntimeError("boom")

    service = build_service(market_provider=boom, hotspot_provider=lambda: [])
    review = service.build(now=NOW)
    assert review.market == []
    assert any("大盘板块数据暂缺" in n for n in review.notes)


# --------------------------------------------------------------------------- #
# 渲染 / 运行
# --------------------------------------------------------------------------- #
def test_markdown_contains_all_sections_and_disclaimer():
    md = build_service().build(now=NOW).to_markdown()
    for title in ("① 本周信号回顾与胜率", "② 持仓盈亏与执行情况", "③ 大盘 / 板块周度总结", "④ 下周关注与关键价位"):
        assert title in md
    assert DISCLAIMER in md


def test_run_dry_run_skips_push():
    result = build_service().run(now=NOW, dry_run=True)
    assert result.skipped_reason == "dry_run"
    assert result.sent_text is False
    assert result.review is not None


def test_run_pushes_via_notifier():
    class Notifier:
        def __init__(self):
            self.sent = []

        def is_available(self):
            return True

        def send_text(self, text):
            self.sent.append(text)
            return True

    notifier = Notifier()
    result = build_service(notifier=notifier).run(now=NOW)
    assert result.sent_text is True
    assert len(notifier.sent) == 1


def test_run_skips_when_no_channel():
    class Notifier:
        def is_available(self):
            return False

    result = build_service(notifier=Notifier()).run(now=NOW)
    assert result.skipped_reason == "no_channel_configured"
    assert result.errors


# --------------------------------------------------------------------------- #
# 执行回填仓储（真实临时库）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def repo(tmp_path):
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{(tmp_path / 'feedback.db').as_posix()}")
    yield ExecutionFeedbackRepository(db)
    DatabaseManager.reset_instance()


def test_repo_upsert_is_overwriting(repo):
    first = repo.upsert("601919", "2026-10-12", "executed", note="已减仓")
    assert first["execution_status"] == "executed"
    second = repo.upsert("601919", "2026-10-12", "later")
    assert second["id"] == first["id"]  # 覆盖语义，不新增
    assert second["execution_status"] == "later"
    assert repo.get("601919", "2026-10-12")["execution_status"] == "later"


def test_repo_symbol_normalized_upper(repo):
    repo.upsert("sh601919", "2026-10-12", "ignored")
    assert repo.get("SH601919", "2026-10-12") is not None


def test_repo_rejects_invalid_status(repo):
    with pytest.raises(ValueError):
        repo.upsert("601919", "2026-10-12", "nope")
    assert EXECUTION_STATUSES == {"executed", "ignored", "later"}


def test_repo_rejects_empty_symbol_or_week(repo):
    with pytest.raises(ValueError):
        repo.upsert("", "2026-10-12", "executed")
    with pytest.raises(ValueError):
        repo.upsert("601919", "", "executed")


def test_repo_list_by_week_and_delete(repo):
    repo.upsert("601919", "2026-10-12", "executed")
    repo.upsert("513180", "2026-10-12", "ignored")
    repo.upsert("600519", "2026-10-05", "later")
    assert [i["symbol"] for i in repo.list_by_week("2026-10-12")] == ["513180", "601919"]
    assert repo.delete("601919", "2026-10-12") == 1
    assert repo.get("601919", "2026-10-12") is None
    assert repo.list_by_week("") == []
