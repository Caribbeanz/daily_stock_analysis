# -*- coding: utf-8 -*-
"""高抛低吸助手 · 规则覆盖写入服务（P2.2 / FR-7、FR-9）

维护两类**用户可调**的规则覆盖，落到 ``config/rules/*.yaml``：

- ``factor_weights.yaml``      四因子权重（FR-7.2，合计必须 1.0）
- ``stop_loss_overrides.yaml`` 单标的止损覆盖（FR-9，优先于技术位计算）

设计要点：
1. **只做持久化与校验**，不承载业务编排；写操作统一由 ``action_service`` 调用
   （决议 D3：单一写点），避免出现第二条写路径；
2. **原子写**：先写临时文件再 ``os.replace``，中途失败不破坏原文件；
3. **可注入目录**：``rules_dir`` 可覆盖，便于单测与多环境（默认取
   ``rule_config.RULES_DIR``，受 ``HSLB_RULES_DIR`` 控制）；
4. **改配置不改代码**（NFR-6）：写入的即是有引擎读取的文件。
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from src.core import rule_config
from src.core.rule_config import FACTOR_KEYS, FACTOR_LABELS

logger = logging.getLogger(__name__)

__all__ = ["RuleOverrideService", "InvalidRuleOverride"]


class InvalidRuleOverride(ValueError):
    """覆盖参数非法（写入口据此拒绝并给出人话原因）。"""


class RuleOverrideService:
    """四因子权重 / 单标的止损覆盖的读写门面。"""

    def __init__(self, rules_dir: Optional[Path] = None) -> None:
        self._rules_dir = Path(rules_dir) if rules_dir is not None else None

    # ---------------- 路径 ---------------- #
    @property
    def rules_dir(self) -> Path:
        if self._rules_dir is not None:
            return self._rules_dir
        return Path(rule_config.RULES_DIR)

    @property
    def factor_weights_path(self) -> Path:
        return self.rules_dir / "factor_weights.yaml"

    @property
    def stop_loss_overrides_path(self) -> Path:
        return self.rules_dir / "stop_loss_overrides.yaml"

    # ---------------- 读 ---------------- #
    def get_factor_weights(self) -> Dict[str, float]:
        """当前四因子权重（已归一化）。"""
        return rule_config.load_factor_weights(self.factor_weights_path).as_dict()

    def get_stop_loss_overrides(self) -> Dict[str, float]:
        """当前单标的止损覆盖表。"""
        return rule_config.load_stop_loss_overrides(self.stop_loss_overrides_path)

    def get_stop_loss(self, symbol: str) -> Optional[float]:
        code = str(symbol or "").strip().upper()
        return self.get_stop_loss_overrides().get(code)

    # ---------------- 写 · 四因子权重 ---------------- #
    def adjust_factor_weights(self, changes: Dict[str, Any], *, base: Optional[Dict[str, float]] = None) -> Dict[str, float]:
        """**局部调整**：只给要改的维度，其余维度**按比例吸收**差额（只算不写）。

        用于"把技术面权重调到 60%"这类口语指令——四维必须合计 100%，
        因此指定维度之外的权重按当前比例缩放，凑齐剩余额度。
        预览会展示"调整后"的完整权重，用户在确认前即可看到全部影响（可解释）。

        Args:
            changes: 要调整的维度（``{"technical": 0.6}``）。
            base: 计算基准（缺省取当前配置）；供复合指令**顺序叠加**使用。

        Raises:
            InvalidRuleOverride: 维度未知 / 数值非法 / 指定维度合计已超 100% /
                其余维度权重为 0（无法按比例分配）。
        """
        if base is not None:
            current = {key: float(base.get(key, 0.0)) for key in FACTOR_KEYS}
        else:
            current = self.get_factor_weights()
        target: Dict[str, float] = dict(current)
        specified: set = set()
        for key, raw in (changes or {}).items():
            norm_key = str(key).strip().lower()
            if norm_key not in FACTOR_KEYS:
                raise InvalidRuleOverride(f"未知维度「{key}」，可选：{'/'.join(FACTOR_KEYS)}")
            target[norm_key] = self._parse_ratio(raw, label=FACTOR_LABELS.get(norm_key, norm_key))
            specified.add(norm_key)

        if not specified:
            return {key: round(current[key], 4) for key in FACTOR_KEYS}

        specified_total = sum(target[key] for key in specified)
        if specified_total > 1.0 + 0.001:
            raise InvalidRuleOverride(
                f"指定维度合计 {specified_total * 100:.0f}%，超过 100%，请下调后再试"
            )
        if abs(sum(target.values()) - 1.0) <= 0.001:
            return self._round_weights(target)

        others = [key for key in FACTOR_KEYS if key not in specified]
        others_total = sum(current[key] for key in others)
        remainder = 1.0 - specified_total
        if others_total <= 0:
            raise InvalidRuleOverride("其余维度当前权重为 0，无法自动分配，请显式给出全部四维权重")
        for key in others:
            target[key] = remainder * (current[key] / others_total)
        return self._round_weights(target)

    @staticmethod
    def _round_weights(weights: Dict[str, float]) -> Dict[str, float]:
        """四舍五入到 4 位，并把舍入误差补到最大维度，保证合计恰为 1.0。"""
        rounded = {key: round(float(weights.get(key, 0.0)), 4) for key in FACTOR_KEYS}
        drift = round(1.0 - sum(rounded.values()), 4)
        if drift:
            biggest = max(FACTOR_KEYS, key=lambda key: rounded[key])
            rounded[biggest] = round(rounded[biggest] + drift, 4)
        return rounded

    def resolve_factor_weights(self, weights: Dict[str, Any]) -> Dict[str, float]:
        """把「局部改动」合并到当前权重并校验（**只算不写**，供预览复用）。

        Args:
            weights: ``{"technical": 0.6, ...}``；未给出的维度沿用当前值。

        Returns:
            合并后的完整权重（合计 1.0）。

        Raises:
            InvalidRuleOverride: 维度未知 / 数值非法 / 合计不为 100%。
        """
        current = self.get_factor_weights()
        merged: Dict[str, float] = dict(current)
        for key, raw in (weights or {}).items():
            norm_key = str(key).strip().lower()
            if norm_key not in FACTOR_KEYS:
                raise InvalidRuleOverride(
                    f"未知维度「{key}」，可选：{'/'.join(FACTOR_KEYS)}"
                )
            merged[norm_key] = self._parse_ratio(raw, label=FACTOR_LABELS.get(norm_key, norm_key))

        total = sum(merged.values())
        if abs(total - 1.0) > 0.001:
            raise InvalidRuleOverride(
                f"四因子权重合计必须为 100%（当前 {total * 100:.1f}%）。"
                f"当前：{'、'.join(f'{FACTOR_LABELS[k]} {merged[k] * 100:.0f}%' for k in FACTOR_KEYS)}"
            )
        return {key: round(merged[key], 4) for key in FACTOR_KEYS}

    def set_factor_weights(self, weights: Dict[str, Any]) -> Dict[str, Any]:
        """整体设置四因子权重（合计须为 1.0，容差 0.001）。

        Args:
            weights: ``{"technical": 0.6, "market": 0.1, ...}``；
                未给出的维度沿用当前值（便于"只调一个维度"）。

        Returns:
            ``{"before": {...}, "after": {...}}``
        """
        current = self.get_factor_weights()
        merged = self.resolve_factor_weights(weights)

        self._write_yaml(
            self.factor_weights_path,
            {"version": 1, "weights": merged},
            header=(
                "# 高抛低吸助手 · 四因子权重（FR-7 / 由 P2.2 写入口维护）\n"
                "# 维度键：technical 技术面 / market 行业·大盘·板块 / news 消息面 / fundamental 基本面\n"
                "# 约束：四项合计为 1.0。\n"
            ),
        )
        return {"before": current, "after": self.get_factor_weights()}

    # ---------------- 写 · 单标的止损覆盖 ---------------- #
    def set_stop_loss(self, symbol: str, price: Any) -> Dict[str, Any]:
        """设置某标的的止损价（覆盖技术位计算值）。"""
        code = str(symbol or "").strip().upper()
        if not code:
            raise InvalidRuleOverride("必须指定标的代码")
        try:
            value = float(price)
        except (TypeError, ValueError):
            raise InvalidRuleOverride(f"止损价「{price}」不是合法数字") from None
        if not value > 0:
            raise InvalidRuleOverride("止损价必须大于 0")

        current = self.get_stop_loss_overrides()
        before = current.get(code)
        current[code] = round(value, 4)
        self._write_stop_loss_overrides(current)
        return {"symbol": code, "before": before, "after": current.get(code)}

    def remove_stop_loss(self, symbol: str) -> Dict[str, Any]:
        """移除某标的的止损覆盖（回落到技术位计算）。"""
        code = str(symbol or "").strip().upper()
        if not code:
            raise InvalidRuleOverride("必须指定标的代码")
        current = self.get_stop_loss_overrides()
        before = current.pop(code, None)
        self._write_stop_loss_overrides(current)
        return {"symbol": code, "before": before, "after": None}

    # ---------------- 内部 ---------------- #
    def _write_stop_loss_overrides(self, overrides: Dict[str, float]) -> None:
        self._write_yaml(
            self.stop_loss_overrides_path,
            {"version": 1, "overrides": dict(sorted(overrides.items()))},
            header=(
                "# 高抛低吸助手 · 单标的止损覆盖（FR-9 / 由 P2.2 写入口维护）\n"
                "# 命中标的直接采用该止损价，优先于技术位/ATR 计算值。\n"
            ),
        )

    @staticmethod
    def _parse_ratio(raw: Any, *, label: str) -> float:
        """接受 0.6 / "0.6" / "60%" 三种写法，返回 0~1 的比例。"""
        if isinstance(raw, str):
            text = raw.strip()
            if text.endswith("%"):
                try:
                    return float(text[:-1]) / 100.0
                except ValueError:
                    raise InvalidRuleOverride(f"{label} 权重「{raw}」不是合法数字") from None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise InvalidRuleOverride(f"{label} 权重「{raw}」不是合法数字") from None
        # 容错：用户直接写 60（想表达 60%）时按百分比折算
        if value > 1.0:
            value = value / 100.0
        if value < 0 or value > 1:
            raise InvalidRuleOverride(f"{label} 权重须在 0%~100% 之间")
        return value

    @staticmethod
    def _write_yaml(path: Path, payload: Dict[str, Any], *, header: str = "") -> None:
        import yaml

        path.parent.mkdir(parents=True, exist_ok=True)
        body = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False)
        text = f"{header}\n{body}" if header else body
        # 原子写：临时文件 → os.replace，避免半截文件
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        logger.info("规则覆盖已写入：%s", path)
