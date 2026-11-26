# -*- coding: utf-8 -*-
"""规则版本指纹 ``calc_version``（需求 §5.4 三元组 / 技术方案 §2.5）。

三元组 ``[data_date, snapshot_id, calc_version]`` 里的 ``calc_version`` 要能回答一件事：
**「这条结论是按哪套规则算出来的」**。因此它必须覆盖所有会改变结论的输入：

- 规则集版本（本文件的 ``RULES_VERSION``，规则改动须手动 bump）
- 参数指纹 ``param_hash``（45 项参数 + 单项覆盖的哈希，已包含四个维度权重）
- 决策表分数带 ``SCORE_BANDS``（数值改了但参数没动，也必须变版本）
- 启停的战法清单 ``strategy_ids``（§5.12 战法启停直接影响阶段与计划；清单唯一真值在
  :mod:`src.assistant.strategies`，默认取其中 ``enabled`` 的条目）
- LLM 文案层的 ``temperature``（只影响措辞——**不进** ``calc_version``，见下）

关于 ``temperature``：需求明确「LLM 只做措辞、不改结论」，措辞温度变化不改变
``advice`` / 股数 / 触发价，所以它**不参与** ``calc_version``；把它算进去会让同一套
规则因为换了个温度就变成「另一个版本」，反而破坏回测可比性。

落库长度约束：``assistant_snapshot.calc_version`` 是 ``String(32)``，所以只存
``cv1-<10 位摘要>``；完整指纹用 :meth:`RuleFingerprint.as_dict` 记日志或审计，
需要复现时按字段重算摘要即可比对。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from src.assistant.engine.scoring import SCORE_BANDS
from src.assistant.strategies import enabled_strategy_ids

__all__ = ["RULES_VERSION", "RuleFingerprint", "build_calc_version"]

#: 规则集版本。**任何会改变 advice / 阶段 / 计划 / 风险判定口径的代码改动都要 bump**。
#: 约定：``rules-<需求版本>.<序号>``，便于和需求文档版本对齐。
#: 2.6.1：战法启停清单（§5.12.3）正式进入指纹——同一套参数在补入清单前后摘要必然不同，
#: 因此必须 bump，否则新旧快照会被误判成「同一规则」。
RULES_VERSION = "rules-2.6.1"

#: 摘要位数（``cv1-`` 前缀 4 字符 + 10 位 = 14，远小于列宽 32）
_DIGEST_LEN = 10


@dataclass(frozen=True)
class RuleFingerprint:
    """一次计算所用的规则指纹。"""

    param_hash: str
    rules_version: str = RULES_VERSION
    strategy_ids: Sequence[str] = field(default_factory=enabled_strategy_ids)
    score_bands: Sequence[Sequence[Any]] = field(default=SCORE_BANDS)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rules_version": self.rules_version,
            "param_hash": self.param_hash,
            "strategy_ids": sorted(self.strategy_ids),
            "score_bands": [list(band) for band in self.score_bands],
        }

    def canonical(self) -> str:
        """规范化 JSON（排序键、紧凑分隔），保证同样的输入得到同样的摘要。"""
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()[:_DIGEST_LEN]

    def calc_version(self) -> str:
        return f"cv1-{self.digest()}"


def build_calc_version(
    param_hash: str,
    *,
    strategy_ids: Sequence[str] | None = None,
    rules_version: str = RULES_VERSION,
) -> str:
    """便捷入口：由参数指纹与战法清单生成 ``calc_version``。

    ``strategy_ids=None`` 表示「按 :mod:`src.assistant.strategies` 的启用清单」，
    而不是「空清单」——空清单会让指纹漏掉战法口径，回测时无法区分两套映射。
    """
    ids = enabled_strategy_ids() if strategy_ids is None else tuple(strategy_ids)
    return RuleFingerprint(
        param_hash=param_hash, rules_version=rules_version, strategy_ids=ids
    ).calc_version()


def fingerprint_from_mapping(data: Mapping[str, Any]) -> RuleFingerprint:  # pragma: no cover - 审计辅助
    """从日志/审计记录还原指纹（复现历史结论时用）。"""
    return RuleFingerprint(
        param_hash=str(data["param_hash"]),
        rules_version=str(data.get("rules_version", RULES_VERSION)),
        strategy_ids=tuple(data.get("strategy_ids") or ()),
        score_bands=tuple(tuple(band) for band in data.get("score_bands") or SCORE_BANDS),
    )
