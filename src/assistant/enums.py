# -*- coding: utf-8 -*-
"""assistant 模块的枚举与常量（唯一真值来源）。

> **命名消歧（需求 §5.4）**：本模块有两套互不相干的 ``S + 数字`` 编码：
> - :class:`GuardStep`：**守卫序** S0–S6，表示决策链第几步（S5 = 决策表，S5.5 = 阶段门）
> - :class:`Stage`：**阶段码** S0–S5，表示这只标的当前价格行为状态（S5 = 无结构）
>
> 代码与留痕字段必须分字段存放（``guard_step`` / ``stage``），禁止共用一个变量。
"""

from __future__ import annotations

from enum import Enum


class Preset(str, Enum):
    """参数三档预设（需求 §5.16.2）。"""

    LOOSE = "loose"
    STANDARD = "standard"
    STRICT = "strict"

    @property
    def label(self) -> str:
        return {"loose": "宽松", "standard": "标准", "strict": "严格"}[self.value]


class Stage(str, Enum):
    """阶段码（需求 §5.14.2）。``S5`` 表示**无结构**，不是守卫 S5。"""

    S0 = "S0"
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"
    S4 = "S4"
    S5 = "S5"

    @property
    def label(self) -> str:
        return _STAGE_LABEL[self]


_STAGE_LABEL = {
    Stage.S0: "无法判定",
    Stage.S1: "上升趋势",
    Stage.S2: "高位震荡",
    Stage.S3: "下跌趋势",
    Stage.S4: "低位筑底",
    Stage.S5: "无结构",
}


class StageConfidence(str, Enum):
    """阶段置信度（需求 §5.14.2）。**不是概率**，按同向证据条数分级。"""

    HIGH = "高"
    MEDIUM = "中"
    LOW = "低"


class GuardStep(str, Enum):
    """守卫序（需求 §5.4）。S5.5 是插在 S5 与 S6 之间的阶段门，不是阶段码。"""

    S0 = "S0"  # 数据校验
    S1 = "S1"  # 基本面
    S2 = "S2"  # 技术面
    S3 = "S3"  # 估值
    S4 = "S4"  # 风险等级守卫
    S5 = "S5"  # 决策表
    S5_5 = "S5.5"  # 阶段门（方案 A）
    S6 = "S6"  # 仓位约束 + 条件计划


class Action(str, Enum):
    """动作（比 ``advice`` 多出 BUY / PROBE / NO_OP 三个执行层动作）。

    ``advice`` 的 7 档有效值为 :data:`ADVICE_VALUES`；BUY 只出现在「S4 右侧确认后
    正式建仓」，PROBE 只出现在 S4 试探仓（需求 §5.14.3.1 明确「动作名唯一写作试探建仓」）。
    """

    BUY = "买入"
    ADD = "加仓"
    PROBE = "试探建仓"
    HOLD = "持有"
    TRIM = "减仓"
    CLEAR = "清仓"
    WAIT_BUY = "等待买点"
    WAIT_SELL = "等待卖点"
    WATCH = "观望"
    NO_OP = "不操作"
    UNAVAILABLE = "不可操作"


#: 需求 §5.4：advice 存储层的 7 档有效意见（持仓 4 + 未持仓 3）
ADVICE_VALUES: tuple[Action, ...] = (
    Action.ADD,
    Action.HOLD,
    Action.TRIM,
    Action.CLEAR,
    Action.WAIT_BUY,
    Action.WATCH,
    Action.WAIT_SELL,
)

#: 买入类动作（含试探建仓以外的建仓/加仓）——S3 一律禁止
BUY_SIDE: frozenset[Action] = frozenset({Action.BUY, Action.ADD, Action.WAIT_BUY})

#: 动作的展示顺序（渲染「允许/禁止」与列表排序用；``NO_OP`` 是内部态，不对外展示）
ACTION_ORDER: tuple[Action, ...] = (
    Action.BUY,
    Action.ADD,
    Action.PROBE,
    Action.HOLD,
    Action.TRIM,
    Action.CLEAR,
    Action.WAIT_BUY,
    Action.WAIT_SELL,
    Action.WATCH,
    Action.UNAVAILABLE,
)
#: 卖出类动作
SELL_SIDE: frozenset[Action] = frozenset({Action.TRIM, Action.CLEAR, Action.WAIT_SELL})


class AdviceState(str, Enum):
    """``advice_state``（需求 §5.4）：4 档硬约束态，与「观望」严格区分。"""

    VALID = "有效"
    STALE = "待同步"
    NO_DATA = "无数据"
    NOT_GENERATED = "不生成"
    UNAVAILABLE = "不可操作"


class RiskLevel(str, Enum):
    """风险等级（需求 §5.10.3）。三期联动：观察 / 警示 / 严重。"""

    INFO = "观察"
    WARNING = "警示"
    SEVERE = "严重"

    @property
    def rank(self) -> int:
        return {"观察": 0, "警示": 1, "严重": 2}[self.value]


class RiskCategory(str, Enum):
    """风险信号 7 类（需求 §5.10.2）。"""

    MARKET = "大盘环境"
    SECTOR = "板块与低估陷阱"
    FUNDAMENTAL = "个股基本面"
    TECHNICAL = "技术面"
    DIVIDEND_VALUATION = "分红与估值"
    POSITION = "组合与仓位"
    DATA_QUALITY = "数据质量"


class RiskCode(str, Enum):
    """风险信号编号（需求 §5.10.2 清单）。"""

    MKT_1 = "MKT-1"
    MKT_2 = "MKT-2"
    MKT_3 = "MKT-3"
    SEC_1 = "SEC-1"
    SEC_2 = "SEC-2"
    SEC_3 = "SEC-3"
    FUN_1 = "FUN-1"
    FUN_2 = "FUN-2"
    FUN_3 = "FUN-3"
    TEC_1 = "TEC-1"
    TEC_2 = "TEC-2"
    TEC_3 = "TEC-3"
    TEC_4 = "TEC-4"
    DIV_1 = "DIV-1"
    DIV_2 = "DIV-2"
    DIV_3 = "DIV-3"
    POS_1 = "POS-1"
    POS_2 = "POS-2"
    POS_3 = "POS-3"
    DQ_1 = "DQ-1"
    DQ_2 = "DQ-2"
    DQ_3 = "DQ-3"


class TriggerType(str, Enum):
    """条件计划触发类型（需求 §5.11.3 穷举）。"""

    PRICE_GTE = "price_gte"
    PRICE_LTE = "price_lte"
    PCT_GTE = "pct_gte"
    PCT_LTE = "pct_lte"
    TIME_AT = "time_at"

    @property
    def is_price_based(self) -> bool:
        """只有价格/涨跌幅类可直接抄进券商条件单。"""
        return self is not TriggerType.TIME_AT

    @property
    def writable(self) -> bool:
        return self in (
            TriggerType.PRICE_GTE,
            TriggerType.PRICE_LTE,
            TriggerType.PCT_GTE,
            TriggerType.PCT_LTE,
        )


class CandidateReason(str, Enum):
    """候选动作的来源原因——阶段门据此区分「趋势跟随减仓」与「箱体逆势高抛」。

    需求 §5.14.3 明确：S1 禁止「用箱体上沿做逆势高抛」，但允许「移动止盈后的减仓」；
    仅凭动作方向无法区分，必须带原因，阶段门才可确定性判定。
    """

    SCORE_TABLE = "决策表"
    BOX_TOP_TRIM = "箱体上沿减（逆势高抛）"
    BOX_BOTTOM_BUY = "箱体下沿买"
    BREAKOUT_ADD = "追突破加仓"
    RISK_DRIVEN = "风险驱动"
    PROBE_TRIGGER = "试探仓触发"
    LEVEL_BREAK = "破位"


class RuleId(str, Enum):
    """阶段门改写规则编号（需求 §5.14.5），随 ``stage_gate_hit`` 留痕。"""

    S3_NO_BUY = "stage_s3_no_buy"
    S4_PROBE = "stage_s4_probe"
    S4_WAIT_CONFIRM = "stage_s4_wait_confirm"
    S4_NO_SCALE_IN = "stage_s4_no_scale_in"
    S2_NO_BREAKOUT = "stage_s2_no_breakout"
    S5_NO_STRUCTURE = "stage_s5_no_structure"
    PROBE_NOT_ALLOWED = "stage_probe_not_allowed"
    S1_NO_COUNTERTREND_TRIM = "stage_s1_no_countertrend_trim"
    NONE = ""


class SourceTag(str, Enum):
    """数据来源标记（需求 §4.3）。"""

    D1 = "D1"  # 主源
    D2 = "D2"  # 备用
    D3 = "D3"  # 缓存


class LotRule(str, Enum):
    """交易单位（需求 §5.11.4）。"""

    A_SHARE = "A股"  # 100 股/手，T+1
    ETF = "ETF"  # 100 份/手，T+0

    @property
    def lot_size(self) -> int:
        return 100

    @property
    def t_plus_one(self) -> bool:
        return self is LotRule.A_SHARE


#: 各阶段允许的动作集合（需求 §5.14.3 枚举式硬约束）
STAGE_ALLOWED: dict[Stage, frozenset[Action]] = {
    # S1 上升：回踩买入、持有、移动止盈后的减仓、正 T（不追高）
    Stage.S1: frozenset({Action.ADD, Action.BUY, Action.HOLD, Action.TRIM, Action.WAIT_BUY, Action.WATCH}),
    # S2 高位震荡：上沿减 T 仓、下沿买、持有
    Stage.S2: frozenset({Action.ADD, Action.TRIM, Action.HOLD, Action.WAIT_BUY, Action.WATCH}),
    # S3 下跌：减仓、清仓、反弹减、反 T；禁止任何买入类
    # 注：``WAIT_BUY``（等待买点）是**被动等待态**而非买入动作——需求 §5.14.5 明确
    # 「S3 评分给出买入类 → 改 等待买点 / 观望」，故它是阶段门的合法输出。
    Stage.S3: frozenset({Action.TRIM, Action.CLEAR, Action.WAIT_SELL, Action.WAIT_BUY, Action.WATCH, Action.HOLD}),
    # S4 低位筑底：观望、试探建仓、确认后买入、持有
    Stage.S4: frozenset(
        {Action.WATCH, Action.PROBE, Action.BUY, Action.HOLD, Action.WAIT_BUY, Action.TRIM}
    ),
    # S5 无结构：观望、持有、风险驱动的减仓/清仓；禁止新建仓/加仓/箱体交易
    Stage.S5: frozenset({Action.WATCH, Action.HOLD, Action.TRIM, Action.CLEAR, Action.WAIT_SELL}),
    # S0 无法判定：只显示状态位
    Stage.S0: frozenset({Action.UNAVAILABLE}),
}
