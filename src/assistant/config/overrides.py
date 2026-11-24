# -*- coding: utf-8 -*-
"""参数解析：三档基线 + 单项覆盖 + param_hash（需求 §5.16.4 / 技术方案 §5.3）。

三条硬规则：
1. **基线 + 覆盖**：当前值 = 预设基线（三档之一）+ 用户单项覆盖，页面显示「标准档 · 2 项自定义」。
2. **只调数值**：枚举/字符串参数不可单项覆盖（守卫顺序与枚举永远不随配置变化）。
3. **append-only**：任何变更（含回滚）都产生一条 :class:`ParamChange`，不删除历史；
   ``param_hash`` 是「当前生效参数集」的指纹，随 L3 快照留痕，用于「同参数同结论」核验。

``param_hash`` 与 ``calc_version`` 分离（R21）：
- ``param_hash`` → 参数**数值**指纹（本模块）
- ``calc_version`` → 规则**版本**指纹（战法启停清单、维度权重结构、temperature 等），由调用方传入
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping

from src.assistant.config.presets import (
    CAP_KEYS,
    ParamNotOverridableError,
    UnknownParamError,
    get_spec,
    preset_values,
    validate_all,
)
from src.assistant.enums import Preset


def canonical_scalar(value: Any) -> str:
    """把参数值转成稳定字符串（用于哈希与留痕）。"""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return f"{value:.6f}"
    if isinstance(value, int):
        return str(value)
    return str(value)


def compute_param_hash(values: Mapping[str, Any]) -> str:
    """计算参数集指纹：键按字典序、值规范化后拼接再 sha256。

    同一组数值必得同一指纹（与插入顺序、dict 实现无关），便于「同参数同结论」校验。
    """
    items = sorted((key, canonical_scalar(val)) for key, val in values.items())
    payload = "|".join(f"{key}={val}" for key, val in items)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ParamChange:
    """一条 append-only 参数变更记录（对应 ``param_override`` 表一行）。"""

    param_key: str
    param_value: Any
    baseline_preset: Preset
    op_time: datetime
    effective_date: date
    user_id: str = ""
    audit_id: str = ""
    op_type: str = "override"  # override | rollback
    previous_value: Any = None

    def as_row(self) -> dict[str, Any]:
        """转成可直接落库的字典（值用 JSON 编码，保留类型，避免 ``1`` / ``True`` 混淆）。"""
        return {
            "param_key": self.param_key,
            "param_value": json.dumps(self.param_value, ensure_ascii=False),
            "baseline_preset": self.baseline_preset.value,
            "op_time": self.op_time,
            "effective_date": self.effective_date,
            "user_id": self.user_id,
            "audit_id": self.audit_id,
            "op_type": self.op_type,
            "previous_value": None if self.previous_value is None else json.dumps(self.previous_value, ensure_ascii=False),
        }


@dataclass(frozen=True)
class ParamSet:
    """当前生效参数集（基线 + 覆盖），不可变；变更返回新实例。"""

    preset: Preset = Preset.STANDARD
    overrides: Mapping[str, Any] = field(default_factory=dict)
    _values: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    # --- 构造 -------------------------------------------------------------
    @classmethod
    def build(cls, preset: Preset = Preset.STANDARD, overrides: Mapping[str, Any] | None = None) -> "ParamSet":
        """由预设 + 覆盖构造，构造时逐项校验（越界/不可覆盖）。"""
        validate_all()
        base = preset_values(preset)
        clean: dict[str, Any] = {}
        for key, raw in (overrides or {}).items():
            clean[key] = _validate_override(key, raw)
        merged = dict(base)
        merged.update(clean)
        return cls(preset=preset, overrides=clean, _values=merged)

    # --- 读取 -------------------------------------------------------------
    def get(self, key: str) -> Any:
        try:
            return self._values[key]
        except KeyError as exc:
            raise UnknownParamError(f"未知参数键：{key}") from exc

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    @property
    def values(self) -> Mapping[str, Any]:
        """完整参数集（只读视图）。"""
        return dict(self._values)

    @property
    def param_hash(self) -> str:
        return compute_param_hash(self._values)

    @property
    def custom_keys(self) -> tuple[str, ...]:
        """被单项覆盖的键（配置页显示「N 项自定义」用）。"""
        return tuple(sorted(self.overrides))

    @property
    def cap_keys(self) -> tuple[str, ...]:
        """其中属于仓位硬上限、修改须二次确认的键。"""
        return tuple(sorted(k for k in self.overrides if k in CAP_KEYS))

    def describe(self) -> str:
        if not self.overrides:
            return f"{self.preset.label}档"
        return f"{self.preset.label}档 · {len(self.overrides)} 项自定义"

    # --- 变更（返回新实例，append-only 由仓储层记录）-------------------------
    def with_preset(self, preset: Preset) -> "ParamSet":
        """切档：保留已有覆盖（覆盖是相对「当前档基线」的绝对数值，切档后仍生效）。"""
        return ParamSet.build(preset, self.overrides)

    def with_override(self, key: str, value: Any) -> "ParamSet":
        """新增/修改单项覆盖。"""
        clean = dict(self.overrides)
        clean[key] = _validate_override(key, value)
        return ParamSet.build(self.preset, clean)

    def without_override(self, key: str) -> "ParamSet":
        """撤销单项覆盖（回滚：值回到当前档基线）。"""
        get_spec(key)  # 键不存在时抛错
        clean = dict(self.overrides)
        clean.pop(key, None)
        return ParamSet.build(self.preset, clean)

    # --- 变更记录 ---------------------------------------------------------
    def make_change(
        self,
        key: str,
        value: Any,
        *,
        op_time: datetime,
        effective_date: date,
        user_id: str = "",
        audit_id: str = "",
        op_type: str = "override",
    ) -> ParamChange:
        """生成一条变更记录（不落库；落库由 storage 层负责）。"""
        spec = get_spec(key)
        return ParamChange(
            param_key=key,
            param_value=spec.coerce(value),
            baseline_preset=self.preset,
            op_time=op_time,
            effective_date=effective_date,
            user_id=user_id,
            audit_id=audit_id,
            op_type=op_type,
            previous_value=self._values.get(key),
        )


def _validate_override(key: str, raw: Any) -> Any:
    """校验单项覆盖：键存在、可覆盖、在区间内。"""
    spec = get_spec(key)
    if not spec.overridable:
        raise ParamNotOverridableError(
            f"{key} 是 {spec.kind} 类型，不允许单项覆盖（只能调数值；枚举与守卫顺序不可变）"
        )
    if spec.kind == "bool":
        return spec.coerce(raw)
    return spec.coerce(raw)


def resolve_effective_date(now: datetime, last_snapshot_generated_at: datetime | None) -> date:
    """参数变更的生效日（需求 §5.16.4）。

    「下一次快照生成时生效」：
    - 当日尚未生成快照 → 当日生效；
    - 当日已生成 → 次日起生效（调用方可按交易日历顺延到下一交易日）。

    注意：本函数只做日历日判断，**不判断是否交易日**——交易日顺延由调度层负责
    （技术方案 §2.4 门控独立于调度）。
    """
    today = now.date()
    if last_snapshot_generated_at is None:
        return today
    if last_snapshot_generated_at.date() < today:
        return today
    return date.fromordinal(today.toordinal() + 1)


def default_param_set(preset: Preset = Preset.STANDARD) -> ParamSet:
    """默认参数集（标准档，无覆盖）——回测基线。"""
    return ParamSet.build(preset, {})
