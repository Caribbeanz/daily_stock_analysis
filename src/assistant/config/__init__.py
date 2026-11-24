# -*- coding: utf-8 -*-
"""参数框架（C4）：三档预设、单项覆盖、param_hash。"""

from __future__ import annotations

from src.assistant.config.overrides import (
    ParamChange,
    ParamSet,
    compute_param_hash,
    default_param_set,
    resolve_effective_date,
)
from src.assistant.config.presets import (
    CAP_KEYS,
    GROUP_ORDER,
    PARAM_SPECS,
    ParamError,
    ParamNotOverridableError,
    ParamRangeError,
    ParamSpec,
    UnknownParamError,
    get_spec,
    preset_values,
    validate_all,
)

__all__ = [
    "CAP_KEYS",
    "GROUP_ORDER",
    "PARAM_SPECS",
    "ParamChange",
    "ParamError",
    "ParamNotOverridableError",
    "ParamRangeError",
    "ParamSet",
    "ParamSpec",
    "UnknownParamError",
    "compute_param_hash",
    "default_param_set",
    "get_spec",
    "preset_values",
    "resolve_effective_date",
    "validate_all",
]
