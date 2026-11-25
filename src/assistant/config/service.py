# -*- coding: utf-8 -*-
"""助手配置服务（L4 运行设置 + 参数覆盖，配置 API 的唯一入口）。

三条口径：

1. **env 只是初始值**：`ASSISTANT_PARAM_PRESET` / `ASSISTANT_TOTAL_CAPITAL` /
   `ASSISTANT_CASH_AVAILABLE` 在用户从未通过配置页保存时生效；一旦
   `assistant_portfolio_setting` 有行，以最新一行为准（D-12）。
2. **下一次快照生成时生效**：`resolve_effective_date` 判断当日快照是否已生成，
   再用交易日历顺延；返回的是「最早生效日」，不改写已生成快照（append-only）。
3. **参数变更 append-only**：单项覆盖/回滚走 `ParamSet.make_change` + 仓储写表，
   不覆盖历史；档位切换写设置表新行，也不覆盖历史。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Mapping

from src.assistant.config.overrides import ParamSet, resolve_effective_date
from src.assistant.config.presets import GROUP_ORDER, PARAM_SPECS, get_spec
from src.assistant.enums import Preset

__all__ = ["AssistantConfigService", "ConfigValidationError"]

logger = logging.getLogger(__name__)


class ConfigValidationError(ValueError):
    """配置请求不合法（越界/未知键/资金缺失）。"""


def param_registry() -> list[dict]:
    """参数注册表的只读视图（配置页据此渲染表单与区间校验）。

    前端不写死 45 个键与区间：注册表是唯一出处，接口只做序列化，避免
    「页面校验一套、后端拒绝另一套」。
    """
    rows: list[dict] = []
    for group, group_label in GROUP_ORDER:
        for spec in PARAM_SPECS.values():
            if spec.group != group:
                continue
            rows.append(
                {
                    "key": spec.key,
                    "group": spec.group,
                    "group_label": group_label,
                    "label": spec.label,
                    "kind": spec.kind,
                    "unit": spec.unit,
                    "minimum": spec.minimum,
                    "maximum": spec.maximum,
                    "choices": list(spec.choices),
                    "standard": spec.standard,
                    "loose": spec.value_for(Preset.LOOSE),
                    "strict": spec.value_for(Preset.STRICT),
                    "preset_scoped": spec.preset_scoped,
                    "requires_confirm": spec.requires_confirm,
                    "provisional": spec.provisional,
                    "description": spec.description,
                }
            )
    return rows


def _next_trading_day(day: date) -> date:
    """交易日历不可用时按下一自然日返回，不影响「下一次快照生效」的语义。"""
    try:
        from src.core.trading_calendar import get_next_trading_date

        result = get_next_trading_date("cn", day)
        if result is not None:
            return result
    except Exception as exc:  # noqa: BLE001 - 日历是增强项，不阻断配置写入
        logger.warning("参数生效日无法读取交易日历，按自然日顺延：%s", exc)
    return day + timedelta(days=1)


@dataclass
class AssistantConfigService:
    store: Any
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    clock: Callable[[], datetime] = datetime.now
    trading_probe: Callable[[date], date] | None = None

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def _settings(self) -> dict:
        saved = self.store.current_portfolio_settings() or {}
        preset_raw = (
            saved.get("baseline_preset")
            or (self.env.get("ASSISTANT_PARAM_PRESET") or "").strip()
            or Preset.STANDARD.value
        )
        try:
            preset = Preset(str(preset_raw).strip().lower())
        except ValueError:
            logger.warning("基线档位 %r 非法，回退 standard", preset_raw)
            preset = Preset.STANDARD

        def money(key: str, env_key: str) -> float | None:
            if key in saved and saved[key] is not None:
                return float(saved[key])
            raw = (self.env.get(env_key) or "").strip()
            if not raw:
                return None
            try:
                return float(raw)
            except ValueError:
                logger.warning("%s=%r 不是数字，按未配置处理", env_key, raw)
                return None

        return {
            "baseline_preset": preset,
            "total_capital": money("total_capital", "ASSISTANT_TOTAL_CAPITAL"),
            "cash_available": money("cash_available", "ASSISTANT_CASH_AVAILABLE"),
            "source": "l4" if saved else "env",
            "updated_at": saved.get("updated_at", ""),
        }

    def current(self) -> dict:
        settings = self._settings()
        overrides = self.store.current_overrides()
        params = ParamSet.build(settings["baseline_preset"], overrides)
        return {
            "baseline_preset": settings["baseline_preset"].value,
            "overrides": [
                {"key": key, "value": value, "label": get_spec(key).label}
                for key, value in sorted(overrides.items())
            ],
            "param_hash": params.param_hash,
            "values": dict(params.values),
            "total_capital": settings["total_capital"],
            "cash_available": settings["cash_available"],
            "config_source": settings["source"],
            "updated_at": settings["updated_at"],
            "describe": params.describe(),
            "registry": param_registry(),
            "groups": [{"key": key, "label": label} for key, label in GROUP_ORDER],
        }

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def _effective_date(self) -> date:
        now = self.clock()
        last = self.store.latest_snapshot(now.date())
        generated = None
        if last and last.get("generated_at"):
            try:
                generated = datetime.fromisoformat(last["generated_at"])
            except ValueError:
                generated = None
        day = resolve_effective_date(now, generated)
        return (self.trading_probe or _next_trading_day)(day)

    def _require_funds(self) -> dict:
        settings = self._settings()
        if settings["total_capital"] is None:
            raise ConfigValidationError(
                "总资金未配置：请先在配置页填写 total_capital（不接受默认假金额）"
            )
        return settings

    def set_preset(self, preset_raw: str) -> dict:
        try:
            preset = Preset(str(preset_raw).strip().lower())
        except ValueError as exc:
            raise ConfigValidationError(
                f"非法档位 {preset_raw!r}（需 loose | standard | strict）"
            ) from exc
        settings = self._require_funds()
        self.store.save_portfolio_settings(
            baseline_preset=preset.value,
            total_capital=settings["total_capital"],
            cash_available=settings["cash_available"],
            now=self.clock(),
        )
        return {
            "ok": True,
            "baseline_preset": preset.value,
            "effective_snapshot_date": self._effective_date().isoformat(),
        }

    def set_funds(self, total_capital: Any, cash_available: Any = None) -> dict:
        """配置页保存资金（D-12：从 env 迁到 L4）。"""
        try:
            total = float(total_capital)
        except (TypeError, ValueError) as exc:
            raise ConfigValidationError("total_capital 必须是数字") from exc
        cash: float | None = None
        if cash_available is not None and str(cash_available).strip():
            try:
                cash = float(cash_available)
            except (TypeError, ValueError) as exc:
                raise ConfigValidationError("cash_available 必须是数字或留空") from exc
        try:
            self.store.save_portfolio_settings(
                baseline_preset=self._settings()["baseline_preset"].value,
                total_capital=total,
                cash_available=cash,
                now=self.clock(),
            )
        except ValueError as exc:
            raise ConfigValidationError(str(exc)) from exc
        return {
            "ok": True,
            "total_capital": total,
            "cash_available": cash,
            "effective_snapshot_date": self._effective_date().isoformat(),
        }

    def set_override(self, key: str, value: Any, *, user_id: str = "") -> dict:
        current = self.current()
        params = ParamSet.build(
            Preset(current["baseline_preset"]),
            {item["key"]: item["value"] for item in current["overrides"]},
        )
        try:
            updated = params.with_override(key, value)
        except ValueError as exc:
            raise ConfigValidationError(str(exc)) from exc
        day = self._effective_date()
        change = updated.make_change(
            key,
            updated.overrides[key],
            op_time=self.clock(),
            effective_date=day,
            user_id=user_id,
        )
        self.store.record_param_change(change)
        return {
            "ok": True,
            "key": key,
            "value": updated.overrides[key],
            "param_hash": updated.param_hash,
            "effective_date": day.isoformat(),
            "requires_confirm": get_spec(key).requires_confirm,
        }

    def rollback_override(self, key: str, *, user_id: str = "") -> dict:
        current = self.current()
        params = ParamSet.build(
            Preset(current["baseline_preset"]),
            {item["key"]: item["value"] for item in current["overrides"]},
        )
        if key not in params.overrides:
            raise ConfigValidationError(f"{key} 当前没有单项覆盖，无需回滚")
        try:
            reverted = params.without_override(key)
        except ValueError as exc:
            raise ConfigValidationError(str(exc)) from exc
        day = self._effective_date()
        change = reverted.make_change(
            key,
            params.overrides[key],
            op_time=self.clock(),
            effective_date=day,
            user_id=user_id,
            op_type="rollback",
        )
        self.store.record_param_change(change)
        return {
            "ok": True,
            "key": key,
            "param_hash": reverted.param_hash,
            "effective_date": day.isoformat(),
        }
