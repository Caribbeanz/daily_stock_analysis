# -*- coding: utf-8 -*-
"""配置服务测试：env 初始值 → L4 生效、下一次快照生效、append-only 留痕。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from src.assistant.config.service import AssistantConfigService, ConfigValidationError
from src.assistant.data.storage import AssistantStore
from src.assistant.enums import Preset

NOW = datetime(2026, 11, 25, 18, 0)


@pytest.fixture()
def store() -> AssistantStore:
    s = AssistantStore(":memory:")
    s.create_all()
    return s


def make_service(store: AssistantStore, **kwargs) -> AssistantConfigService:
    return AssistantConfigService(
        store,
        env={"ASSISTANT_TOTAL_CAPITAL": "100000", "ASSISTANT_PARAM_PRESET": "standard"},
        clock=lambda: NOW,
        trading_probe=lambda day: day,
        **kwargs,
    )


class TestFundsAndPreset:
    def test_env_is_initial_value_only(self, store) -> None:
        service = make_service(store)
        assert service.current()["config_source"] == "env"
        assert service.current()["total_capital"] == 100000.0

        service.set_funds(250000, 50000)
        current = service.current()
        assert current["config_source"] == "l4"
        assert current["total_capital"] == 250000.0
        assert current["cash_available"] == 50000.0

    def test_l4_takes_precedence_over_env(self, store) -> None:
        service = make_service(store)
        service.set_funds(300000)
        assert service.current()["total_capital"] == 300000.0

    def test_preset_switch_returns_effective_date(self, store) -> None:
        service = make_service(store)
        result = service.set_preset("strict")
        assert result["baseline_preset"] == "strict"
        assert result["effective_snapshot_date"] == "2026-11-25"
        assert service.current()["baseline_preset"] == "strict"

    def test_invalid_preset_rejected(self, store) -> None:
        with pytest.raises(ConfigValidationError):
            make_service(store).set_preset("ultra")

    def test_missing_capital_rejected(self, store) -> None:
        service = AssistantConfigService(store, env={}, clock=lambda: NOW)
        with pytest.raises(ConfigValidationError, match="总资金未配置"):
            service.set_preset("strict")

    def test_invalid_money_rejected(self, store) -> None:
        with pytest.raises(ConfigValidationError):
            make_service(store).set_funds(-1)
        with pytest.raises(ConfigValidationError):
            make_service(store).set_funds(1000, -5)


class TestOverrides:
    def test_override_changes_param_hash(self, store) -> None:
        service = make_service(store)
        before = service.current()["param_hash"]
        result = service.set_override("box_touch_tol", 0.03, user_id="tester")

        assert result["value"] == 0.03
        assert result["param_hash"] != before
        assert service.current()["overrides"][0]["value"] == 0.03
        assert service.current()["param_hash"] == result["param_hash"]

    def test_rollback_restores_baseline(self, store) -> None:
        service = make_service(store)
        baseline = service.current()["param_hash"]
        service.set_override("box_touch_tol", 0.03)
        rolled = service.rollback_override("box_touch_tol")

        assert rolled["param_hash"] == baseline
        assert service.current()["overrides"] == []
        history = store.param_history("box_touch_tol")
        assert [row["op_type"] for row in history] == ["override", "rollback"]

    def test_out_of_range_and_unknown_key_rejected(self, store) -> None:
        service = make_service(store)
        with pytest.raises(ConfigValidationError):
            service.set_override("box_touch_tol", 999)
        with pytest.raises(ConfigValidationError):
            service.set_override("not_a_param", 1)

    def test_non_overridable_key_rejected(self, store) -> None:
        service = make_service(store)
        with pytest.raises(ConfigValidationError):
            service.set_override("guard_order", "S0,S1")

    def test_rollback_without_override_rejected(self, store) -> None:
        with pytest.raises(ConfigValidationError, match="没有单项覆盖"):
            make_service(store).rollback_override("box_touch_tol")

    def test_preset_change_keeps_overrides(self, store) -> None:
        service = make_service(store)
        service.set_override("box_touch_tol", 0.03)
        service.set_preset("loose")
        current = service.current()
        assert current["baseline_preset"] == "loose"
        assert current["overrides"][0]["value"] == 0.03


class TestEffectiveDate:
    def test_same_day_snapshot_pushes_to_next_day(self, store) -> None:
        store.save_portfolio_settings(
            baseline_preset="standard", total_capital=100000, now=NOW
        )
        # 当日已有快照 → 生效日必须顺延
        from src.assistant.data.models import Snapshot

        from sqlalchemy.orm import Session

        with Session(store._engine) as sess:  # noqa: SLF001 - 单测直接构造快照行
            sess.add(
                Snapshot(
                    snapshot_id="20261125-x",
                    data_date=date(2026, 11, 25),
                    bar_date=date(2026, 11, 25),
                    calc_version="cv1",
                    param_hash="p1",
                    param_preset="standard",
                    generated_at=NOW,
                    source_tag="D1",
                    bars_used=100,
                    gap_days=0,
                )
            )
            sess.commit()
        service = make_service(store)
        assert service.set_override("box_touch_tol", 0.03)["effective_date"] == "2026-11-26"

    def test_no_snapshot_effective_today(self, store) -> None:
        service = make_service(store)
        assert service.set_override("box_touch_tol", 0.03)["effective_date"] == "2026-11-25"

    def test_preset_enum_stable(self) -> None:
        assert Preset.STRICT.value == "strict"
