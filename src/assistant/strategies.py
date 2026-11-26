# -*- coding: utf-8 -*-
"""战法启停清单（需求 §5.12.3，登记进 ``calc_version``）。

本模块回答一个审计问题：**「这次结论用的是哪几个战法的思想」**。

背景（§5.12.2）：仓库自带的 15 个战法是**自然语言 YAML（LLM Skill）**，``instructions``
由大模型解读，输出不可复现，不能直接接入 ``advice``。本助手的做法是把战法的**判定条件
转写成确定性规则**，规则落在 ``engine/`` 各模块里；因此这里的清单不是「运行哪个 Skill」，
而是「哪几个战法的思想已经被转写进来、哪几个明确不启用」。

为什么必须登记：

- 启停变化会改变阶段/计划/风险口径（§5.12.3 的「本期不启用」一旦放开就是规则变更），
  所以它和参数、分数带一样是 ``calc_version`` 的组成部分；
- 回测要能回答「这版结论用的是哪套战法映射」，否则胜率不可比。

清单是**静态常量**而非配置项：§5.12.5 第 8 条要求新增/启用策略走变更记录（附录 C），
改动须同时 bump :data:`src.assistant.versioning.RULES_VERSION`，不做运行期开关。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "STRATEGY_REGISTRY",
    "StrategyEntry",
    "disabled_strategy_ids",
    "enabled_strategy_ids",
    "registry_ids",
]

#: 启停状态取值
ENABLED = "enabled"
DISABLED = "disabled"


@dataclass(frozen=True)
class StrategyEntry:
    """一条战法启停登记。

    :param strategy_id: 与 ``strategies/<id>.yaml`` 同名，便于双向核对
    :param status: ``enabled``（思想已转写为确定性规则）/ ``disabled``（本期不启用）
    :param transposed_into: 转写后的落点（需求章节号或引擎模块），审计时按此追溯
    :param note: 不启用或不直接进建议层的原因，写「为什么」而不是「是什么」
    """

    strategy_id: str
    status: str
    transposed_into: Sequence[str] = ()
    note: str = ""

    @property
    def enabled(self) -> bool:
        return self.status == ENABLED


#: 15 个自带战法的启停清单（§5.12.3 初判 → 本模块定稿）。
#: 顺序按「趋势/量能 → 箱体框架 → 质量 → 情绪短线 → 主观框架」排，便于人工核对。
STRATEGY_REGISTRY: tuple[StrategyEntry, ...] = (
    # ---- 趋势 / 均线 / 量能：优先转写 ----
    StrategyEntry(
        "bull_trend",
        ENABLED,
        ("§5.2 趋势三档", "engine/stage.py", "engine/scoring.py"),
        "多头排列 + 回踩不破 → 阶段 S1 与趋势档位的判定条件",
    ),
    StrategyEntry(
        "ma_golden_cross",
        ENABLED,
        ("§5.2 趋势三档", "engine/indicators.py"),
        "均线金叉/死叉只作趋势方向的确认项，不单独触发动作",
    ),
    StrategyEntry(
        "shrink_pullback",
        ENABLED,
        ("§5.3 关键价位", "§5.11.3 触发价", "engine/plan.py"),
        "缩量回踩 → 条件计划的回踩买入触发价与量能确认标注",
    ),
    StrategyEntry(
        "volume_breakout",
        ENABLED,
        ("§5.11.2 上行突破分支", "engine/plan.py"),
        "放量突破前高 → 上行突破情景分支的触发条件",
    ),
    StrategyEntry(
        "bottom_volume",
        ENABLED,
        ("§5.14.3.1 S4 试探仓", "engine/stage.py"),
        "底部放量 → 阶段 S4 的识别条件（只允许试探仓）",
    ),
    # ---- 箱体 / 框架：可转写 ----
    StrategyEntry(
        "box_oscillation",
        ENABLED,
        ("§5.3 关键价位", "§5.14 阶段 S2", "engine/indicators.py"),
        "箱体上下沿 → 阶段 S2 与箱体触碰容差 box_touch_tol（D-4）",
    ),
    StrategyEntry(
        "wave_theory",
        ENABLED,
        ("§6.4 长周期位置", "engine/indicators.py"),
        "只取「长周期相对位置」这一可量化部分，波形主观划分不转写",
    ),
    # ---- 质量 / 价值：辅助事实，不改主档 ----
    StrategyEntry(
        "growth_quality",
        ENABLED,
        ("§5.8 估值与分红", "§5.1 低权重维度"),
        "质量/成长只作辅助事实与低权重维度，不参与 advice 主档",
    ),
    # ---- 情绪 / 热点 / 短线：与 NG-4 / NG-6 冲突 ----
    StrategyEntry("emotion_cycle", DISABLED, (), "NG-4：不做消息面情绪打分与情绪周期判断"),
    StrategyEntry("hot_theme", DISABLED, (), "NG-6：不做热点轮动快的短线"),
    StrategyEntry("dragon_head", DISABLED, (), "NG-6：龙头战法依赖盘中情绪与连板，确定性不可复现"),
    StrategyEntry("event_driven", DISABLED, (), "NG-4：事件驱动须判消息面，本期不做"),
    StrategyEntry("one_yang_three_yin", DISABLED, (), "NG-6：短线形态博弈，换手与情绪的判定主观"),
    # ---- 主观框架：难确定性转写 ----
    StrategyEntry("chan_theory", DISABLED, (), "背驰/中枢判定含主观阈值，只作观察注记（§5.3）"),
    StrategyEntry("expectation_repricing", DISABLED, (), "预期重构依赖叙事判断，无法写成决策表"),
)


def _unique_ids(entries: Sequence[StrategyEntry]) -> tuple[str, ...]:
    seen: list[str] = []
    for entry in entries:
        if entry.strategy_id not in seen:
            seen.append(entry.strategy_id)
    return tuple(sorted(seen))


def enabled_strategy_ids() -> tuple[str, ...]:
    """进入 ``calc_version`` 的战法清单（排序后，保证同一份清单摘要稳定）。"""
    return _unique_ids([entry for entry in STRATEGY_REGISTRY if entry.enabled])


def disabled_strategy_ids() -> tuple[str, ...]:
    """明确不启用的战法（不进 ``calc_version``，只用于审计与文档核对）。"""
    return _unique_ids([entry for entry in STRATEGY_REGISTRY if not entry.enabled])


def registry_ids() -> tuple[str, ...]:
    """清单里登记过的全部战法 id（含不启用）。"""
    return _unique_ids(STRATEGY_REGISTRY)
