# -*- coding: utf-8 -*-
"""参数注册表与三档预设（需求 §5.16 / 技术方案 §5.1–§5.2）。

设计要点：
1. **一处定义**：键名、分组、三档数值、允许区间、是否随档位变化，全部集中在 :data:`PARAM_SPECS`。
2. **契约校验**：三档相对关系（宽→严：拦得更多、动得更少）是需求契约，由
   :func:`validate_preset_ordering` 强制断言，防止调参时把档位改到倒挂。
3. **越界拒绝**：单项覆盖必须落在 ``[minimum, maximum]``，否则抛 :class:`ParamRangeError`。
4. **只调数值**：枚举/字符串类参数不接受单项覆盖（需求 §5.16.4「只能调数值，不能改枚举/守卫顺序」）。

数值均为**初值**，最终以纸面回看（§9.2）与回测（§9.4）回写；标 ``provisional=True`` 的项
尚无回看结论，实现须允许其变化但不得视为已定口径。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from src.assistant.enums import Preset


class ParamError(ValueError):
    """参数框架的基础异常。"""


class UnknownParamError(ParamError):
    """键不存在。"""


class ParamRangeError(ParamError):
    """取值越界。"""


class ParamNotOverridableError(ParamError):
    """该参数不允许单项覆盖（枚举/字符串/结构类）。"""


# 允许的取值类型：数值（float/int）、布尔、枚举字符串
_NUMERIC_KINDS = ("float", "int", "ratio")
_OVERRIDABLE_KINDS = ("float", "int", "ratio", "bool")


@dataclass(frozen=True)
class ParamSpec:
    """单个参数的定义。

    Attributes:
        key: 参数键名（写入 ``param_override`` 表与 ``param_hash``）。
        group: 分组（对应配置页分组与需求 §5.16.3 的组）。
        label: 中文名（配置页展示）。
        kind: ``float`` / ``int`` / ``ratio``（0–1 小数）/ ``bool`` / ``enum`` / ``str``。
        standard: 标准档数值（**回测基线**）。
        loose: 宽松档数值；``None`` 表示与标准档相同（非档位参数）。
        strict: 严格档数值；``None`` 表示与标准档相同。
        minimum: 允许下界（含）。
        maximum: 允许上界（含）。
        choices: 枚举取值。
        unit: 展示单位。
        strictness: 档位方向 ``"higher"``（严格档更高）/ ``"lower"``（严格档更低）/ ``"same"``。
        provisional: 数值尚未经纸面回看确定。
        requires_confirm: 修改须二次确认（仓位硬上限）。
        description: 说明与需求出处。
    """

    key: str
    group: str
    label: str
    kind: str
    standard: Any
    loose: Any = None
    strict: Any = None
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    unit: str = ""
    strictness: str = "same"
    provisional: bool = False
    requires_confirm: bool = False
    description: str = ""

    # --- 派生属性 ---------------------------------------------------------
    @property
    def preset_scoped(self) -> bool:
        """是否随三档预设变化。"""
        return self.loose is not None or self.strict is not None

    @property
    def overridable(self) -> bool:
        """是否允许单项覆盖（只允许数值与布尔；枚举/字符串不可覆盖）。"""
        return self.kind in _OVERRIDABLE_KINDS

    def value_for(self, preset: Preset) -> Any:
        """取指定档位的数值。非档位参数一律返回标准值。"""
        if preset is Preset.LOOSE and self.loose is not None:
            return self.loose
        if preset is Preset.STRICT and self.strict is not None:
            return self.strict
        return self.standard

    def coerce(self, raw: Any) -> Any:
        """把外部输入转成规范类型，并做区间/枚举校验。"""
        if self.kind == "bool":
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str) and raw.strip().lower() in ("1", "true", "yes", "on", "开"):
                return True
            if isinstance(raw, str) and raw.strip().lower() in ("0", "false", "no", "off", "关"):
                return False
            raise ParamRangeError(f"{self.key} 需要布尔值，收到 {raw!r}")

        if self.kind in _NUMERIC_KINDS:
            try:
                val: float = float(raw)
            except (TypeError, ValueError) as exc:
                raise ParamRangeError(f"{self.key} 需要数值，收到 {raw!r}") from exc
            if self.kind == "int":
                if abs(val - round(val)) > 1e-9:
                    raise ParamRangeError(f"{self.key} 需要整数，收到 {raw!r}")
                out: Any = int(round(val))
            else:
                out = val
            if self.minimum is not None and out < self.minimum:
                raise ParamRangeError(f"{self.key}={out} 低于下界 {self.minimum}")
            if self.maximum is not None and out > self.maximum:
                raise ParamRangeError(f"{self.key}={out} 高于上界 {self.maximum}")
            return out

        if self.kind == "enum":
            if str(raw) not in self.choices:
                raise ParamRangeError(f"{self.key}={raw!r} 不在可选值 {self.choices} 内")
            return str(raw)

        return str(raw)


_S = "stage"      # 阶段判定
_A = "action"     # 动作门槛
_W = "weight"     # 维度权重
_R = "risk"       # 风险
_P = "position"   # 仓位
_PL = "plan"      # 条件计划
_D = "display"    # 展示与推送
_BT = "backtest"  # 回测健康度
_SCH = "schedule"  # 调度（R24，不随三档变化）


# ---------------------------------------------------------------------------
# 参数注册表
# ---------------------------------------------------------------------------
PARAM_SPECS: dict[str, ParamSpec] = {
    # ===== 阶段判定 =====
    "ma_window": ParamSpec(
        key="ma_window", group=_S, label="均线窗口", kind="int",
        standard=20, minimum=10, maximum=60,
        description="MA20；需求 §5.14.2 阈值共用一套",
    ),
    "slope_k": ParamSpec(
        key="slope_k", group=_S, label="斜率回看天数 k", kind="int",
        standard=5, minimum=3, maximum=10,
        description="slope20(k=5)；需求 §5.14.2",
    ),
    "stage_theta": ParamSpec(
        key="stage_theta", group=_S, label="斜率阈值 θ", kind="float",
        standard=0.003, minimum=0.001, maximum=0.01,
        provisional=True,
        description="|slope|≤θ 视为走平；日均相对斜率",
    ),
    "stage_epsilon": ParamSpec(
        key="stage_epsilon", group=_S, label="均线缓冲 ε", kind="ratio",
        standard=0.005, loose=0.005, strict=0.008,
        minimum=0.002, maximum=0.02,
        strictness="higher", provisional=True,
        description="站上/跌破 MA20 的缓冲；严格档更宽=更少误判为趋势",
    ),
    "box_lookback": ParamSpec(
        key="box_lookback", group=_S, label="箱体回看天数", kind="int",
        standard=60, minimum=40, maximum=120,
        description="需求 §5.14.2 箱体成立判定",
    ),
    "box_width_max": ParamSpec(
        key="box_width_max", group=_S, label="箱体宽度上限", kind="ratio",
        # D-6 回看定稿：初值 12% 时 60 日箱体只有 13% 的交易日成立，S5「无结构」占了 58%
        # （实测中位箱宽 19.9%），调成 15% 后 S5 降到 39%、验证区间 33.5%，
        # 计划条数 +16% 而意见切换率只 +1.9pp，是「更少误判为无结构」而不是更吵。
        standard=0.15, loose=0.18, strict=0.12,
        minimum=0.08, maximum=0.20,
        strictness="lower", provisional=False,
        description="(高-低)/低；超限则箱体不成立 → S5",
    ),
    "box_touch_min": ParamSpec(
        key="box_touch_min", group=_S, label="上下沿最少触碰次数", kind="int",
        standard=2, loose=2, strict=3,
        minimum=2, maximum=5,
        strictness="higher", provisional=True,
        description="需求 §5.14.2「上下沿各触碰 ≥2 次」",
    ),
    "stage_debounce_days": ParamSpec(
        key="stage_debounce_days", group=_S, label="阶段防抖天数", kind="int",
        standard=2, loose=1, strict=3,
        minimum=1, maximum=5,
        strictness="higher",
        description="连续 N 日满足新阶段条件才切换（需求 §5.14.2）",
    ),
    "min_valid_bars": ParamSpec(
        key="min_valid_bars", group=_S, label="最少有效 bar 数", kind="int",
        standard=20, minimum=20, maximum=120,
        description="不足即 S0（需求 §5.14.2）",
    ),
    "box_touch_tol": ParamSpec(
        key="box_touch_tol", group=_S, label="箱体触碰容差", kind="ratio",
        standard=0.02, minimum=0.005, maximum=0.05,
        provisional=True,
        description="判定「触碰边界」的相对容差",
    ),
    # ===== 动作门槛 =====
    "advice_margin": ParamSpec(
        key="advice_margin", group=_A, label="档位切换缓冲", kind="ratio",
        standard=0.10, loose=0.05, strict=0.15,
        minimum=0.02, maximum=0.25,
        strictness="higher", provisional=True,
        description="分数差未超缓冲则维持原档（需求 §5.4 S5）",
    ),
    "probe_enabled": ParamSpec(
        key="probe_enabled", group=_A, label="S4 试探仓开关", kind="bool",
        standard=True, loose=True, strict=False,
        strictness="same",
        description="严格档默认关闭（需求 §5.14.3.1 / §5.16）",
    ),
    "probe_ratio": ParamSpec(
        key="probe_ratio", group=_A, label="试探仓额度比例", kind="ratio",
        standard=1 / 3, loose=1 / 3, strict=1 / 3,
        minimum=0.25, maximum=0.5,
        provisional=True,
        description="≤ 该标的计划仓位的 1/3（需求 §5.14.3.1）",
    ),
    "volume_confirm_multiple": ParamSpec(
        key="volume_confirm_multiple", group=_A, label="右侧确认放量倍数", kind="float",
        standard=1.5, loose=1.2, strict=1.8,
        minimum=1.0, maximum=3.0,
        strictness="higher", provisional=True,
        description="站上箱体上沿时的量能确认倍数",
    ),
    "probe_shrink_ratio": ParamSpec(
        key="probe_shrink_ratio", group=_A, label="试探仓缩量比", kind="ratio",
        standard=0.8, minimum=0.3, maximum=1.0,
        provisional=True,
        description="近 3 日均量 ≤ 20 日均量 × 该值（需求 §5.14.3.1 ③）",
    ),
    # ===== 维度权重（需求 §5.1 R1：技术面主导，其余低权重）=====
    # 技术面是 0–100 基准分，不走权重；以下为「加减分幅度」权重，须满足
    # 「技术面权重显著高于其他维度之和」，由 validate_weights() 校验。
    "w_market": ParamSpec(
        key="w_market", group=_W, label="大盘/板块权重", kind="float",
        standard=8.0, minimum=0.0, maximum=20.0,
        provisional=True,
        description="行情结构数据的加减分上限（分），非消息面情绪（R4/R5）",
    ),
    # 板块三档 → 评分：需求 §4.4 P0-4 要求「可配置分值映射表」，不直接用涨跌幅或单日 rs_t。
    # 分值上限落在 w_market 的预算内（偏强/偏弱各 ±2 分），三档之外没有别的档。
    "sector_adj_up": ParamSpec(
        key="sector_adj_up", group=_W, label="板块偏强加分", kind="float",
        standard=2.0, minimum=0.0, maximum=6.0,
        provisional=True,
        description="sector_strength=偏强 时的评分加分（分）；中性固定 0",
    ),
    "sector_adj_down": ParamSpec(
        key="sector_adj_down", group=_W, label="板块偏弱扣分", kind="float",
        standard=2.0, minimum=0.0, maximum=6.0,
        provisional=True,
        description="sector_strength=偏弱 时的评分扣分（分，按负值计入）",
    ),
    "w_fundamental": ParamSpec(
        key="w_fundamental", group=_W, label="基本面权重", kind="float",
        standard=6.0, minimum=0.0, maximum=20.0,
        provisional=True,
        description="财报事实的加减分上限（分），不做情绪打分（NG-4）",
    ),
    # 分红 4.0 → 0.0（2026-11-27 定）：分红与股息率**退出评分**，只保留事实与风险。
    # 依据一（区分度）：红利样本 20 只里 17 只在每个观测日都拿满连续分红加分，
    # 该维度在红利池内几乎不区分标的——它不排序，只把所有标的整体抬高，档位移动来自
    # 「整体平移」而不是「分辨好坏」。依据二（回放）：权重 4→2 时 123 个已成交回合的
    # 净盈亏逐笔差 0 元、胜率/盈亏比完全不变，只少生成 62 条永远等不到成交的计划，
    # 即它在这份样本上是门槛噪声而非收益变量。既然既不排序也不改收益，就不该占分值预算。
    # 保留的是：① DIV-1「分红中断」仍是**严重级且 affects=True**（`dividend_adjustment`
    # 与风险层是两条独立路径，权重为 0 不影响风险）；② DIV-2 除权临近；③ 纸面回看里
    # 连续年数 / TTM 股息率 / 下次除权全部照常展示，只是不再折算成加减分。
    # 想恢复只需把 standard 改回 2 或 4，`calc_version` 会随之变化。
    "w_dividend": ParamSpec(
        key="w_dividend", group=_W, label="分红权重", kind="float",
        standard=0.0, minimum=0.0, maximum=20.0,
        provisional=True,
        description="股息率与连续性的加减分上限（分）：默认 0＝只作事实与风险、不进评分",
    ),
    "w_valuation": ParamSpec(
        key="w_valuation", group=_W, label="估值权重", kind="float",
        standard=4.0, minimum=0.0, maximum=20.0,
        provisional=True,
        description="PE/PB 分位加减分上限（分），与价格位置分离",
    ),
    # ===== 风险 =====
    "risk_warn_consec_days": ParamSpec(
        key="risk_warn_consec_days", group=_R, label="连续 N 日触发天数", kind="int",
        standard=3, loose=4, strict=2,
        minimum=2, maximum=10,
        strictness="lower", provisional=True,
        description="宽松档更难过门槛（不易升级）；严格档更易触发",
    ),
    "risk_severe_drop_pct": ParamSpec(
        key="risk_severe_drop_pct", group=_R, label="严重级跌幅阈值", kind="ratio",
        standard=0.12, minimum=0.05, maximum=0.20,
        provisional=True,
        description="TEC/L2 相关跌幅阈值，待回看",
    ),
    "risk_pctile_low": ParamSpec(
        key="risk_pctile_low", group=_R, label="估值分位低界", kind="ratio",
        standard=0.2, minimum=0.1, maximum=0.3,
        provisional=True,
        description="SEC-2 价值陷阱判定用",
    ),
    "risk_pctile_high": ParamSpec(
        key="risk_pctile_high", group=_R, label="估值分位高界", kind="ratio",
        standard=0.8, minimum=0.7, maximum=0.9,
        provisional=True,
        description="DIV-3 估值与价格双高判定用",
    ),
    "stop_l1_ratio": ParamSpec(
        key="stop_l1_ratio", group=_R, label="L1 减仓比例", kind="ratio",
        standard=0.35, minimum=0.20, maximum=0.50,
        provisional=True,
        description="跌破趋势失效位先减一部分（需求 §5.5）",
    ),
    "stop_l2_pct": ParamSpec(
        key="stop_l2_pct", group=_R, label="L2 再清仓跌幅", kind="ratio",
        standard=0.08, minimum=0.03, maximum=0.15,
        provisional=True,
        description="L1 后继续下跌达该幅度触发 L2（需求 §5.5）",
    ),
    # ===== 仓位（硬上限，改须二次确认）=====
    "single_cap": ParamSpec(
        key="single_cap", group=_P, label="单只上限", kind="ratio",
        standard=0.20, minimum=0.05, maximum=0.50,
        requires_confirm=True,
        description="需求 §5.6 硬上限 20%，不建议改",
    ),
    "total_cap": ParamSpec(
        key="total_cap", group=_P, label="总仓上限", kind="ratio",
        standard=0.80, minimum=0.30, maximum=1.00,
        requires_confirm=True,
        description="需求 §5.6 硬上限 80%",
    ),
    "cash_floor": ParamSpec(
        key="cash_floor", group=_P, label="现金底线", kind="ratio",
        standard=0.20, minimum=0.00, maximum=0.70,
        requires_confirm=True,
        description="需求 §5.6 现金 ≥ 20%",
    ),
    "t_cap": ParamSpec(
        key="t_cap", group=_P, label="T 仓上限", kind="ratio",
        standard=0.10, minimum=0.00, maximum=0.30,
        requires_confirm=True,
        description="需求 §5.7 FR-6.3",
    ),
    "tier_light_max": ParamSpec(
        key="tier_light_max", group=_P, label="轻档上界", kind="ratio",
        standard=0.10, minimum=0.02, maximum=0.15,
        requires_confirm=True,
        description="轻 < 10%（需求 §5.6）",
    ),
    "tier_mid_max": ParamSpec(
        key="tier_mid_max", group=_P, label="中档上界", kind="ratio",
        standard=0.15, minimum=0.08, maximum=0.20,
        requires_confirm=True,
        description="中 10~15%",
    ),
    "tier_heavy_max": ParamSpec(
        key="tier_heavy_max", group=_P, label="重档上界", kind="ratio",
        standard=0.20, minimum=0.12, maximum=0.25,
        requires_confirm=True,
        description="重 15~20%（不得超过单只硬上限）",
    ),
    "concentration_alert": ParamSpec(
        key="concentration_alert", group=_P, label="行业集中度提示阈值", kind="ratio",
        standard=0.25, minimum=0.15, maximum=0.40,
        description="≥ 该值只提示不硬限（需求 §5.6 / POS-2）",
    ),
    # ===== 条件计划 =====
    "plan_max_branches": ParamSpec(
        key="plan_max_branches", group=_PL, label="分支上限", kind="int",
        standard=4, loose=5, strict=3,
        minimum=2, maximum=6,
        strictness="lower",
        description="标准 4 / 宽松 5 / 严格 3（需求 §5.11 / §5.16）",
    ),
    "plan_valid_days": ParamSpec(
        key="plan_valid_days", group=_PL, label="计划有效期（交易日）", kind="int",
        standard=1, minimum=1, maximum=3,
        description="默认仅当日有效（需求 §5.11.4）",
    ),
    "plan_buffer_pct": ParamSpec(
        key="plan_buffer_pct", group=_PL, label="突破容差", kind="ratio",
        standard=0.005, minimum=0.001, maximum=0.03,
        provisional=True,
        description="突破位 = 压力位 ×(1+buffer)（需求 §5.11.3）",
    ),
    # ===== 展示与推送 =====
    "list_sort_primary": ParamSpec(
        key="list_sort_primary", group=_D, label="列表首排序键", kind="enum",
        standard="risk_level", choices=("risk_level", "signal_priority"),
        description="默认 risk_level 降序（需求 §5.10.6 #2）",
    ),
    "push_min_level": ParamSpec(
        key="push_min_level", group=_D, label="推送最低等级", kind="enum",
        standard="警示", choices=("观察", "警示", "严重"),
        description="低于该等级不进推送（需求 §6.3）",
    ),
    # ===== 调度（R24，不随三档变化）=====
    "push_schedule": ParamSpec(
        key="push_schedule", group=_SCH, label="推送时点", kind="str",
        standard="20:00",
        description="HH:MM 逗号分隔或 5 字段 cron，可混用（R24）",
    ),
    "trade_fetch_time": ParamSpec(
        key="trade_fetch_time", group=_SCH, label="取数时点", kind="str",
        standard="17:30",
        description="须早于首推时点（技术方案 §2.3）",
    ),
    "non_trading_action": ParamSpec(
        key="non_trading_action", group=_SCH, label="非交易日处置", kind="enum",
        standard="skip", choices=("skip", "notify_skip"),
        description="两值都不生成快照/卡片（R24）",
    ),
    "schedule_timezone": ParamSpec(
        key="schedule_timezone", group=_SCH, label="调度时区", kind="str",
        standard="Asia/Shanghai",
        description="IANA 时区名，与 trading_calendar 市场时区一致",
    ),
    "schedule_fallback": ParamSpec(
        key="schedule_fallback", group=_SCH, label="表达式非法时动作", kind="enum",
        standard="warn", choices=("warn", "raise"),
        description="回退默认 20:00 并告警，不静默停摆",
    ),
    # ===== 回测健康度（§9.4.2，仅校验用）=====
    "flip_rate_cap": ParamSpec(
        key="flip_rate_cap", group=_BT, label="建议翻转率上限", kind="ratio",
        standard=0.25, loose=0.30, strict=0.20,
        minimum=0.10, maximum=0.40,
        strictness="lower",
        description="需求 §9.4.2；越界回 §9.2 调阈值",
    ),
    "stage_switch_cap": ParamSpec(
        key="stage_switch_cap", group=_BT, label="阶段切换上限（次/年）", kind="int",
        standard=8, loose=10, strict=6,
        minimum=4, maximum=15,
        strictness="lower",
        description="需求 §9.4.2",
    ),
}


#: 分组顺序（配置页展示顺序）
GROUP_ORDER: tuple[tuple[str, str], ...] = (
    (_S, "阶段判定"),
    (_A, "动作门槛"),
    (_W, "维度权重"),
    (_R, "风险"),
    (_P, "仓位（硬上限，改须二次确认）"),
    (_PL, "条件计划"),
    (_D, "展示与推送"),
    (_SCH, "调度"),
    (_BT, "回测健康度"),
)

#: 单只/总仓等硬上限键（修改须二次确认，需求 §5.16.3）
CAP_KEYS: frozenset[str] = frozenset(
    k for k, spec in PARAM_SPECS.items() if spec.requires_confirm
)


def get_spec(key: str) -> ParamSpec:
    """按键取定义，不存在时抛 :class:`UnknownParamError`。"""
    try:
        return PARAM_SPECS[key]
    except KeyError as exc:
        raise UnknownParamError(f"未知参数键：{key}") from exc


def preset_values(preset: Preset) -> dict[str, Any]:
    """展开某档预设的完整参数集（含非档位参数）。"""
    return {key: spec.value_for(preset) for key, spec in PARAM_SPECS.items()}


def iter_group(group: str) -> Iterable[ParamSpec]:
    """按分组迭代参数定义（保持注册顺序）。"""
    return (spec for spec in PARAM_SPECS.values() if spec.group == group)


def validate_preset_ordering() -> list[str]:
    """校验三档相对关系未倒挂，返回问题清单（空 = 通过）。

    契约（需求 §5.16.2）：宽 → 严 = **拦得更多、动得更少**。
    - ``strictness="higher"``：严格档数值必须 ≥ 宽松档（如防抖天数、触碰次数、缓冲）
    - ``strictness="lower"``：严格档数值必须 ≤ 宽松档（如箱体宽度上限、分支上限）
    - 布尔项（``probe_enabled``）按「严格档不得比宽松档更宽松」校验：
      宽松 True / 严格 False 合法，反向非法。
    """
    problems: list[str] = []
    for spec in PARAM_SPECS.values():
        if not spec.preset_scoped:
            continue
        loose = spec.value_for(Preset.LOOSE)
        strict = spec.value_for(Preset.STRICT)
        if spec.strictness == "higher" and not strict >= loose:
            problems.append(f"{spec.key}: 严格档 {strict} 应 ≥ 宽松档 {loose}")
        elif spec.strictness == "lower" and not strict <= loose:
            problems.append(f"{spec.key}: 严格档 {strict} 应 ≤ 宽松档 {loose}")
        elif spec.strictness == "same" and spec.kind == "bool":
            # 严格档放开（True）而宽松档关闭（False）即为倒挂
            if strict and not loose:
                problems.append(f"{spec.key}: 严格档开启而宽松档关闭，属档位倒挂")
    return problems


def validate_specs() -> list[str]:
    """注册表自身的一致性校验（默认值是否落在区间、枚举是否有 choices 等）。"""
    problems: list[str] = []
    for key, spec in PARAM_SPECS.items():
        if spec.key != key:
            problems.append(f"{key}: key 字段与注册键不一致（{spec.key}）")
        for preset in Preset:
            value = spec.value_for(preset)
            try:
                coerced = spec.coerce(value)
            except ParamError as exc:
                problems.append(f"{key}/{preset.value}: 默认值不合法 - {exc}")
                continue
            if spec.kind in _NUMERIC_KINDS and coerced != value:
                problems.append(f"{key}/{preset.value}: 强制转换后数值变化 {value}→{coerced}")
        if spec.kind == "enum" and not spec.choices:
            problems.append(f"{key}: 枚举类型但未声明 choices")
        if spec.minimum is not None and spec.maximum is not None and spec.minimum > spec.maximum:
            problems.append(f"{key}: 下界大于上界")
        if spec.group not in dict(GROUP_ORDER):
            problems.append(f"{key}: 分组 {spec.group} 未登记在 GROUP_ORDER")
    return problems


def validate_weights() -> list[str]:
    """校验「技术面权重显著高于其他维度之和」（需求 §5.1 R1，约束而非具体数值）。

    技术面是 0–100 的基准分；其余维度以「加减分上限」形式配置。
    约束落地为：四个低权重维度之和 ≤ 40 分（技术面基准 100 分，仍为主导）。
    """
    problems: list[str] = []
    low_sum = sum(float(PARAM_SPECS[key].standard) for key in ("w_market", "w_fundamental", "w_dividend", "w_valuation"))
    if low_sum > 40.0:
        problems.append(f"低权重维度之和 {low_sum} 分超过 40 分上限，技术面不再主导")
    return problems


def validate_all() -> None:
    """注册表 + 档位关系 + 权重结构一起校验，不通过则抛 :class:`ParamError`。"""
    problems = validate_specs() + validate_preset_ordering() + validate_weights()
    if problems:
        raise ParamError("参数注册表校验失败：\n- " + "\n- ".join(problems))
