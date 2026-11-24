# -*- coding: utf-8 -*-
"""参数框架测试（需求 §5.16 / 技术方案 §5.1–§5.3）。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from src.assistant.config.overrides import (
    ParamSet,
    compute_param_hash,
    default_param_set,
    resolve_effective_date,
)
from src.assistant.config.presets import (
    CAP_KEYS,
    PARAM_SPECS,
    ParamNotOverridableError,
    ParamRangeError,
    UnknownParamError,
    preset_values,
    validate_all,
    validate_preset_ordering,
)
from src.assistant.enums import Preset


def test_registry_self_check_passes() -> None:
    """注册表自身一致（默认值合法、分组已登记、低权重不超限）。"""
    validate_all()
    assert validate_preset_ordering() == []


def test_preset_ordering_not_inverted() -> None:
    """宽→严必须「拦得更多」：防抖天数升高、箱体宽度上限降低。"""
    loose = preset_values(Preset.LOOSE)
    strict = preset_values(Preset.STRICT)
    assert strict["stage_debounce_days"] >= loose["stage_debounce_days"]
    assert strict["box_width_max"] <= loose["box_width_max"]
    assert strict["plan_max_branches"] <= loose["plan_max_branches"]
    assert strict["probe_enabled"] is False
    assert loose["probe_enabled"] is True


def test_param_hash_stable_and_order_independent() -> None:
    """同数值必得同指纹；与插入顺序无关（「同参数同结论」的前提）。"""
    a = {"advice_margin": 0.1, "box_width_max": 0.12}
    b = {"box_width_max": 0.12, "advice_margin": 0.10}
    assert compute_param_hash(a) == compute_param_hash(b)
    assert compute_param_hash(a) != compute_param_hash({"advice_margin": 0.11})


def test_default_preset_is_standard_and_has_all_keys() -> None:
    ps = default_param_set()
    assert ps.preset is Preset.STANDARD
    assert set(ps.values) == set(PARAM_SPECS)
    assert ps.custom_keys == ()
    assert ps.describe() == "标准档"


def test_override_within_range_and_describe() -> None:
    ps = default_param_set().with_override("advice_margin", 0.12)
    assert ps.get("advice_margin") == pytest.approx(0.12)
    assert ps.custom_keys == ("advice_margin",)
    assert ps.describe() == "标准档 · 1 项自定义"
    # 覆盖改变指纹
    assert ps.param_hash != default_param_set().param_hash


def test_override_out_of_range_rejected() -> None:
    with pytest.raises(ParamRangeError):
        default_param_set().with_override("advice_margin", 0.99)


def test_override_non_numeric_rejected() -> None:
    """只能调数值：枚举/字符串不可单项覆盖（守卫顺序与枚举永不变）。"""
    with pytest.raises(ParamNotOverridableError):
        default_param_set().with_override("push_min_level", "观察")
    with pytest.raises(ParamNotOverridableError):
        default_param_set().with_override("push_schedule", "21:00")


def test_unknown_key_rejected() -> None:
    with pytest.raises(UnknownParamError):
        default_param_set().with_override("not_a_param", 1)


def test_preset_switch_keeps_overrides() -> None:
    ps = default_param_set().with_override("advice_margin", 0.12).with_preset(Preset.STRICT)
    assert ps.preset is Preset.STRICT
    assert ps.get("advice_margin") == pytest.approx(0.12)
    assert ps.get("stage_debounce_days") == 3  # 严格档基线


def test_rollback_restores_baseline() -> None:
    ps = default_param_set().with_override("advice_margin", 0.12).without_override("advice_margin")
    assert ps.get("advice_margin") == pytest.approx(0.10)
    assert ps.custom_keys == ()


def test_cap_keys_flagged() -> None:
    ps = default_param_set().with_override("single_cap", 0.25)
    assert "single_cap" in CAP_KEYS
    assert ps.cap_keys == ("single_cap",)


def test_param_change_row_is_typed_json() -> None:
    """变更记录用 JSON 编码，``True`` 与 ``1`` 不会混淆。"""
    ps = default_param_set()
    change = ps.make_change(
        "probe_enabled", False, op_time=datetime(2026, 11, 24, 19, 0), effective_date=date(2026, 11, 25)
    )
    row = change.as_row()
    assert row["param_value"] == "false"
    assert row["baseline_preset"] == "standard"
    assert row["op_type"] == "override"


def test_effective_date_same_day_or_next() -> None:
    """参数变更下一次快照生成时生效（需求 §5.16.4）。"""
    now = datetime(2026, 11, 24, 19, 30)
    assert resolve_effective_date(now, None) == date(2026, 11, 24)
    assert resolve_effective_date(now, datetime(2026, 11, 23, 18, 0)) == date(2026, 11, 24)
    assert resolve_effective_date(now, datetime(2026, 11, 24, 18, 0)) == date(2026, 11, 25)


def test_preset_scoped_flags_consistent() -> None:
    """标了 loose/strict 的参数才随档变化，其余各档同值。"""
    for key, spec in PARAM_SPECS.items():
        if spec.preset_scoped:
            assert spec.value_for(Preset.LOOSE) != spec.value_for(Preset.STRICT) or key in {
                "box_touch_min",
                "probe_ratio",
                "stage_epsilon",
            }
        else:
            assert spec.value_for(Preset.LOOSE) == spec.value_for(Preset.STRICT) == spec.value_for(Preset.STANDARD)


def test_paramset_is_immutable() -> None:
    ps: ParamSet = default_param_set()
    with pytest.raises(Exception):
        ps.preset = Preset.LOOSE  # type: ignore[misc]
