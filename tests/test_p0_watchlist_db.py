# -*- coding: utf-8 -*-
"""P0 自选股建表 / 仓储 / 服务冒烟测试（真实 SQLite 临时库）。

运行方式（规避 tests/conftest.py 的重型前端依赖）：
    PYTHONPATH=. pytest --noconftest tests/test_p0_watchlist_db.py
"""

from __future__ import annotations

import pytest

from src.repositories.watchlist_repo import WatchlistRepository
from src.services.watchlist_service import (
    DEFAULT_WATCHLIST,
    WatchlistService,
    infer_asset_type,
)
from src.storage import DatabaseManager


@pytest.fixture()
def repo(tmp_path):
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{(tmp_path / 'watchlist.db').as_posix()}")
    yield WatchlistRepository(db)
    DatabaseManager.reset_instance()


def test_watchlist_table_created_and_crud(repo):
    repo.upsert("601919", name="中远海控", asset_type="stock")
    repo.upsert("513100", name="纳指ETF(国泰)", asset_type="etf", track_index="纳斯达克100")

    items = repo.list_items()
    assert len(items) == 2
    row = repo.get("601919")
    assert row is not None and row.asset_type == "stock"
    assert repo.get("513100").track_index == "纳斯达克100"


def test_upsert_is_idempotent(repo):
    repo.upsert("601919", name="中远海控")
    repo.upsert("601919", name="中远海控")
    assert repo.count() == 1


def test_seed_defaults_creates_eight(repo):
    service = WatchlistService(repo)
    created = service.seed_defaults()
    assert created == len(DEFAULT_WATCHLIST) == 8
    assert repo.count() == 8
    # 幂等：再次 seed 不新增
    assert service.seed_defaults() == 0
    assert repo.count() == 8


def test_seed_defaults_marks_etf_types(repo):
    service = WatchlistService(repo)
    service.seed_defaults()
    etfs = [i for i in repo.list_dicts() if i["asset_type"] == "etf"]
    assert len(etfs) == 4
    symbols = {i["symbol"] for i in etfs}
    assert symbols == {"513100", "513180", "513530", "520660"}


def test_set_enabled_and_remove(repo):
    repo.upsert("600000", name="浦发银行", asset_type="stock")
    assert repo.set_enabled("600000", False) == 1
    assert repo.list_items(enabled_only=True) == []
    assert repo.remove("600000") == 1
    assert repo.count() == 0


def test_infer_asset_type():
    assert infer_asset_type("513180") == "etf"
    assert infer_asset_type("520660") == "etf"
    assert infer_asset_type("159915") == "etf"
    assert infer_asset_type("160123") == "lof"
    assert infer_asset_type("601169") == "stock"
    assert infer_asset_type("000725") == "stock"
