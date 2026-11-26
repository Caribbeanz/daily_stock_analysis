# -*- coding: utf-8 -*-
"""D-5 分数带 / D-6 箱体宽度上限的定稿值（回看证据见实现说明 D-28）。

这里钉的是**结论**，不是过程：数值一改，下面的断言就会红，提醒同时更新
``RULES_VERSION`` 与实现说明里的回看记录——否则「悄悄调阈值」会让历史快照不可比。
"""

from __future__ import annotations

from src.assistant.config.overrides import ParamSet
from src.assistant.config.presets import get_spec
from src.assistant.engine.scoring import SCORE_BANDS, band_of
from src.assistant.enums import Preset


def test_score_bands_are_the_reviewed_values() -> None:
    assert [(threshold, name) for threshold, name in SCORE_BANDS] == [
        (80.0, "strong"),
        (62.0, "mild"),
        (40.0, "neutral"),
        (22.0, "weak"),
        (0.0, "bad"),
    ]


def test_band_boundaries_are_closed_on_the_lower_edge() -> None:
    assert band_of(100.0) == "strong"
    assert band_of(80.0) == "strong"
    assert band_of(79.9) == "mild"
    assert band_of(62.0) == "mild"
    assert band_of(61.9) == "neutral"
    assert band_of(40.0) == "neutral"
    assert band_of(39.9) == "weak"
    assert band_of(22.0) == "weak"
    assert band_of(21.9) == "bad"
    assert band_of(0.0) == "bad"


def test_box_width_max_matches_review_and_keeps_order() -> None:
    spec = get_spec("box_width_max")

    assert spec.standard == 0.15
    assert spec.strict == 0.12
    assert spec.loose == 0.18
    assert spec.strict < spec.standard < spec.loose, "严格档更窄、宽松档更宽"
    assert spec.provisional is False, "回看定稿后不该再挂 provisional"


def test_presets_and_overrides_reach_the_engine() -> None:
    standard = ParamSet.build(Preset.STANDARD)
    strict = ParamSet.build(Preset.STRICT)
    loose = ParamSet.build(Preset.LOOSE)

    assert standard.get("box_width_max") == 0.15
    assert strict.get("box_width_max") == 0.12
    assert loose.get("box_width_max") == 0.18
    assert standard.param_hash != strict.param_hash != loose.param_hash
