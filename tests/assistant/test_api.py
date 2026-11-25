# -*- coding: utf-8 -*-
"""助手 API 测试：只读快照 + 参数配置（用最小 FastAPI app，避免加载整个主服务）。"""

from __future__ import annotations

from datetime import date, datetime

import importlib.util
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="API test needs fastapi")
pytest.importorskip("httpx", reason="API test needs httpx")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.deps import get_assistant_config_service, get_assistant_store  # noqa: E402


def _load_assistant_router():
    """只加载被测端点文件，不执行 endpoints/__init__ 的无关渠道模块导入。"""
    parent = "api.v1.endpoints"
    path = Path(__file__).resolve().parents[2] / "api" / "v1" / "endpoints" / "assistant.py"
    original_parent = sys.modules.get(parent)
    original_self = sys.modules.get(f"{parent}.assistant")
    stub = types.ModuleType(parent)
    stub.__path__ = [str(path.parent)]
    sys.modules[parent] = stub
    try:
        spec = importlib.util.spec_from_file_location(f"{parent}.assistant", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{parent}.assistant"] = module
        spec.loader.exec_module(module)
        return module.router
    finally:
        if original_parent is None:
            sys.modules.pop(parent, None)
        else:
            sys.modules[parent] = original_parent
        if original_self is None:
            sys.modules.pop(f"{parent}.assistant", None)
        else:
            sys.modules[f"{parent}.assistant"] = original_self


router = _load_assistant_router()
from src.assistant.config.service import AssistantConfigService  # noqa: E402
from src.assistant.data.storage import AssistantStore  # noqa: E402
from tests.assistant.helpers import make_bars, trending  # noqa: E402

DAY = date(2026, 11, 25)


@pytest.fixture()
def client(tmp_path) -> TestClient:
    # 用文件库：TestClient 在另一线程跑应用，SQLite :memory: 每连接一份会丢表
    store = AssistantStore(tmp_path / "assistant_api.db")
    store.create_all()
    store.save_bars("600000", make_bars(trending(150)))
    store.append_watchlist("600000", "add", effective_date=date(2026, 5, 1))

    from src.assistant.pipeline import AssistantPipeline, PipelineSettings

    pipeline = AssistantPipeline(
        store=store,
        settings=PipelineSettings(total_capital=100_000.0),
        clock=lambda: datetime(2026, 11, 25, 18, 0),
    )
    pipeline.ensure_snapshot(DAY)
    sid = store.latest_snapshot(DAY)["snapshot_id"]

    service = AssistantConfigService(store, env={"ASSISTANT_TOTAL_CAPITAL": "100000"},
                                     trading_probe=lambda day: day)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/assistant")
    app.dependency_overrides[get_assistant_store] = lambda: store
    app.dependency_overrides[get_assistant_config_service] = lambda: service
    with TestClient(app) as test_client:
        yield test_client, store, sid


def triple(body: dict, sid: str) -> None:
    assert body["snapshot_id"] == sid
    assert body["data_date"] == DAY.isoformat()
    assert body["calc_version"]


class TestReadApi:
    def test_snapshot_and_holdings(self, client) -> None:
        http, _, sid = client
        snap = http.get("/api/v1/assistant/snapshot", params={"data_date": DAY.isoformat()})
        assert snap.status_code == 200
        assert snap.json()["snapshot_id"] == sid

        holdings = http.get("/api/v1/assistant/holdings", params={"snapshot_id": sid})
        assert holdings.status_code == 200
        body = holdings.json()
        triple(body, sid)
        assert body["holding_conclusion"][0]["code"] == "600000"

    def test_detail_plans_and_risks(self, client) -> None:
        http, _, sid = client
        detail = http.get(
            "/api/v1/assistant/holding/600000", params={"snapshot_id": sid}
        )
        assert detail.status_code == 200
        row = detail.json()["holding_conclusion"]
        assert row["code"] == "600000"
        assert isinstance(row["conditional_plan"], list)
        assert isinstance(row["risk_signals"], list)

        for path in ("plans/600000", "risks", "sectors"):
            response = http.get(f"/api/v1/assistant/{path}", params={"snapshot_id": sid})
            assert response.status_code == 200
            triple(response.json(), sid)

    def test_market_missing_is_honest(self, client) -> None:
        http, _, sid = client
        response = http.get("/api/v1/assistant/market", params={"snapshot_id": sid})
        assert response.status_code == 200
        assert response.json()["market_snapshot"]["available"] is False

    def test_market_row_is_served_when_written(self, client) -> None:
        http, store, sid = client
        store.save_market_snapshot(
            sid,
            index_code="sh000001",
            index_name="上证指数",
            pct_change=0.27,
            market_regime="偏弱",
            regime_cells="below+not_up",
            degraded=False,
        )
        body = http.get("/api/v1/assistant/market", params={"snapshot_id": sid}).json()

        triple(body, sid)
        assert body["market_snapshot"]["available"] is True
        assert body["market_snapshot"]["market_regime"] == "偏弱"
        assert body["market_snapshot"]["regime_cells"] == "below+not_up"

    def test_detail_carries_stage_block_and_stock_text(self, client) -> None:
        """第 3 区块的打法/允许/禁止由后端给，前端不拼文案；个股文案只读缓存。"""
        http, store, sid = client
        detail = http.get("/api/v1/assistant/holding/600000", params={"snapshot_id": sid}).json()
        row = detail["holding_conclusion"]

        stage = row["stage_detail"]
        assert stage["code"] == row["stage"]
        assert stage["name"]
        assert stage["playbook"]
        assert stage["switch"]
        assert stage["allowed"] and stage["forbidden"]
        # 阶段门允许/禁止不得同时包含同一动作
        assert not set(stage["allowed"]) & set(stage["forbidden"])

        assert row["llm_text"]["available"] is False
        store.save_llm_text(sid, "stock", "浦发银行：加仓；条件计划 1 条。", code="600000", model="m1")
        again = http.get("/api/v1/assistant/holding/600000", params={"snapshot_id": sid}).json()
        assert again["holding_conclusion"]["llm_text"]["available"] is True
        assert "加仓" in again["holding_conclusion"]["llm_text"]["text"]

    def test_llm_text_miss_then_hit(self, client) -> None:
        http, store, sid = client
        miss = http.get(
            "/api/v1/assistant/llm_text", params={"snapshot_id": sid, "position": "summary"}
        )
        assert miss.json()["llm_text"]["available"] is False

        store.save_llm_text(sid, "summary", "共 1 只，按结构化字段执行。", model="m1")
        hit = http.get(
            "/api/v1/assistant/llm_text", params={"snapshot_id": sid, "position": "summary"}
        )
        assert hit.json()["llm_text"]["text"].startswith("共 1 只")
        assert hit.json()["llm_text"]["model"] == "m1"

    def test_unknown_snapshot_returns_404(self, client) -> None:
        http, _, _ = client
        response = http.get("/api/v1/assistant/holdings", params={"snapshot_id": "missing"})
        assert response.status_code == 404

    def test_broken_triple_returns_waiting(self, client) -> None:
        http, store, sid = client
        original = store.snapshot_content

        def broken(snapshot_id):
            content = original(snapshot_id)
            content.pop("calc_version", None)
            return content

        store.snapshot_content = broken
        response = http.get("/api/v1/assistant/risks", params={"snapshot_id": sid})
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "out_of_sync"


class TestConfigApi:
    def test_get_params_reflects_env_fallback(self, client) -> None:
        http, _, _ = client
        body = http.get("/api/v1/assistant/params").json()
        assert body["baseline_preset"] == "standard"
        assert body["total_capital"] == 100000.0
        assert body["config_source"] == "env"

    def test_params_expose_registry_for_form(self, client) -> None:
        """配置页渲染表单与区间校验都靠注册表，不写死键名。"""
        http, _, _ = client
        body = http.get("/api/v1/assistant/params").json()

        registry = body["registry"]
        assert len(registry) == len(body["values"])
        assert {row["key"] for row in registry} == set(body["values"])
        groups = [item["key"] for item in body["groups"]]
        assert [row["group"] for row in registry][0] == groups[0]
        sample = next(row for row in registry if row["key"] == "box_touch_tol")
        assert sample["kind"] == "ratio"
        assert sample["minimum"] is not None and sample["maximum"] is not None
        assert sample["label"]

    def test_preset_override_and_rollback(self, client) -> None:
        http, _, _ = client
        preset = http.put("/api/v1/assistant/params/preset", json={"preset": "strict"})
        assert preset.status_code == 200
        assert http.get("/api/v1/assistant/params").json()["baseline_preset"] == "strict"

        changed = http.put(
            "/api/v1/assistant/params/override",
            json={"key": "box_touch_tol", "value": 0.03},
        )
        assert changed.status_code == 200
        assert changed.json()["param_hash"]
        assert any(
            item["key"] == "box_touch_tol"
            for item in http.get("/api/v1/assistant/params").json()["overrides"]
        )

        rolled = http.delete(
            "/api/v1/assistant/params/override", params={"key": "box_touch_tol"}
        )
        assert rolled.status_code == 200
        assert http.get("/api/v1/assistant/params").json()["overrides"] == []

    def test_invalid_override_rejected(self, client) -> None:
        http, _, _ = client
        unknown = http.put(
            "/api/v1/assistant/params/override", json={"key": "not_a_param", "value": 1}
        )
        assert unknown.status_code == 400
        out_of_range = http.put(
            "/api/v1/assistant/params/override", json={"key": "box_touch_tol", "value": 99}
        )
        assert out_of_range.status_code == 400

    def test_funds_move_to_l4(self, client) -> None:
        http, _, _ = client
        saved = http.put(
            "/api/v1/assistant/params/funds",
            json={"total_capital": 250000, "cash_available": 50000},
        )
        assert saved.status_code == 200
        body = http.get("/api/v1/assistant/params").json()
        assert body["config_source"] == "l4"
        assert body["total_capital"] == 250000.0
        assert body["cash_available"] == 50000.0
