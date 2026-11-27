# -*- coding: utf-8 -*-
"""战法启停清单（§5.12.3）与它在 ``calc_version`` 里的登记。"""

from __future__ import annotations

from pathlib import Path

from src.assistant import strategies
from src.assistant.strategies import (
    DISABLED,
    ENABLED,
    STRATEGY_REGISTRY,
    disabled_strategy_ids,
    enabled_strategy_ids,
    registry_ids,
)
from src.assistant.versioning import RULES_VERSION, RuleFingerprint, build_calc_version

ROOT = Path(__file__).resolve().parents[2]


class TestRegistry:
    def test_covers_every_bundled_strategy(self) -> None:
        """清单必须与 `strategies/*.yaml` 一一对应，否则启停状态会漏登。"""
        on_disk = {path.stem for path in (ROOT / "strategies").glob("*.yaml")}

        assert on_disk, "仓库里应有自带战法"
        assert set(registry_ids()) == on_disk

    def test_status_values_are_known(self) -> None:
        assert {entry.status for entry in STRATEGY_REGISTRY} <= {ENABLED, DISABLED}

    def test_no_duplicate_ids(self) -> None:
        ids = [entry.strategy_id for entry in STRATEGY_REGISTRY]
        assert len(ids) == len(set(ids))

    def test_enabled_matches_requirement_5_12_3(self) -> None:
        """§5.12.3 初判：趋势/量能、箱体框架、质量可转写；情绪短线与主观框架不启用。"""
        assert set(enabled_strategy_ids()) == {
            "bull_trend",
            "ma_golden_cross",
            "shrink_pullback",
            "volume_breakout",
            "bottom_volume",
            "box_oscillation",
            "wave_theory",
            "growth_quality",
        }
        assert set(disabled_strategy_ids()) == {
            "emotion_cycle",
            "hot_theme",
            "dragon_head",
            "event_driven",
            "one_yang_three_yin",
            "chan_theory",
            "expectation_repricing",
        }

    def test_enabled_entries_point_at_transposition(self) -> None:
        """启用的战法必须写清转写落点，否则无法按 §6.6 解释来源。"""
        for entry in STRATEGY_REGISTRY:
            if entry.enabled:
                assert entry.transposed_into, entry.strategy_id
            else:
                assert entry.note, f"{entry.strategy_id} 不启用须写明原因"

    def test_ids_sorted_for_stable_digest(self) -> None:
        ids = enabled_strategy_ids()
        assert list(ids) == sorted(ids)


class TestFingerprint:
    def test_rules_version_bumped_for_strategy_ledger(self) -> None:
        # 2.6.1 收入战法清单；2.6.2 分数带定稿（D-5）；2.6.3 板块三档进评分
        assert RULES_VERSION == "rules-2.6.3"

    def test_default_fingerprint_carries_enabled_ids(self) -> None:
        assert RuleFingerprint(param_hash="p").strategy_ids == enabled_strategy_ids()

    def test_toggle_changes_digest(self) -> None:
        """启停一条战法＝规则变更：摘要必须变，否则回测无法区分两套映射。"""
        base = RuleFingerprint(param_hash="p", strategy_ids=("a", "b"))
        dropped = RuleFingerprint(param_hash="p", strategy_ids=("a",))

        assert base.digest() != dropped.digest()

    def test_disabled_ids_never_enter_fingerprint(self) -> None:
        payload = RuleFingerprint(param_hash="p").as_dict()

        assert not set(payload["strategy_ids"]) & set(disabled_strategy_ids())

    def test_build_calc_version_defaults_to_registry(self) -> None:
        explicit = build_calc_version("p", strategy_ids=enabled_strategy_ids())

        assert build_calc_version("p") == explicit
        assert explicit.startswith("cv1-")
        assert len(explicit) <= 32

    def test_empty_list_is_distinct_from_default(self) -> None:
        """显式空清单是「不带战法口径」，与默认启用清单不能混为一谈。"""
        assert build_calc_version("p", strategy_ids=()) != build_calc_version("p")

    def test_registry_importable_as_module(self) -> None:
        assert strategies.enabled_strategy_ids() == enabled_strategy_ids()
