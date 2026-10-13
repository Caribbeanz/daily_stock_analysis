# 高抛低吸助手 · P0 详细设计（指标与风控内核）

| 项目   | 说明                                                                              |
| ---- | ------------------------------------------------------------------------------- |
| 文档状态 | 详细设计 v1.0（**设计阶段，不含实现、不改业务代码**）                                                 |
| 版本   | v1.0                                                                            |
| 更新日期 | 2026-10-13                                                                      |
| 需求依据 | `docs/requirements/high-sell-low-buy-assistant-requirements-v0.7.md`（需求基线 v0.7） |
| 方案依据 | `docs/solution/high-sell-low-buy-assistant-design-v1.1.md`（方案 v1.1，已通过评审）       |
| 覆盖范围 | **仅 P0**：数据 → 指标 → 场景 → 风控 → 建仓/加仓信号 → 落库。推送（P1）、H5/对话（P2）另行设计                  |
| 设计原则 | **能复用不新建、能配置不写死**；所有规则可回溯、参数可配                                                  |

---

## 0. 三个前置修正（基于对现有代码的实测）

方案 v1.1 的 Gap 判断是在未逐表核对的情况下作出的。本次逐表核对后，**有 3 处需修正**，且都朝着"更省事"的方向：

| #    | 方案 v1.1 的判断             | 实测结果                                                                                                                                                                               | 修正                                       |
| ---- | ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| 修正 1 | 需新建 `signals` 表作为四端同源核心 | **已存在 `decision_signals` 表**，字段与需求高度吻合（`action` / `stop_loss` / `target_price` / `entry_low` / `entry_high` / `confidence` / `score` / `evidence_json` / `status` / `expires_at`…） | **复用 `decision_signals`**，不新建 signals 表  |
| 修正 2 | 持仓"手动维护"、新建存储           | **已存在 `portfolio_accounts` / `portfolio_positions` / `portfolio_position_lots` / `portfolio_trades` 子系统**                                                                          | **复用 portfolio 子系统**，仅需录入                |
| 修正 3 | 对话记忆压缩层需**从零新建**（FR-15） | **已存在 `conversation_messages` + `conversation_summaries`（滚动摘要）**                                                                                                                   | FR-15 由"新建"改为**扩展**（补"结构化长期记忆"，如持仓/偏好变更） |

**修正后结论：P0 真正需要"新建"的只有两块——**

1. **统一风控引擎**（单标的级止损/止盈/仓位；`portfolio_risk_service` 已有"组合级"，但缺"单标的级"）；
2. **自选股表 `watchlist_items`**（承载 `asset_type` 与备注；现有自选股只是 `config.stock_list` 一个字符串列表，无法表达类型）。

> 其余全部为**改造/复用**。

---

## 1. P0 范围与验收目标

### 1.1 范围

| 纳入 P0                           | 不纳入 P0                   |
| ------------------------------- | ------------------------ |
| 数据获取（含 ETF 跟踪指数校验）              | 20:00 推送与飞书（P1）          |
| 指标计算（MA/MACD/KDJ/RSI/BOLL/BIAS） | H5 / AI 对话（P2）           |
| 场景判定（上涨/下跌/震荡/变盘/背离，日线+周线）      | 周六复盘完整版（P3）              |
| **风控引擎**（止损/分批止盈/金字塔仓位）         | 完整回测报告（P3；P0→P1 间只做"体检"） |
| 建仓/加仓信号 + 出"四要素建议+纪律卡"          | 大屏（P4）                   |
| 结果**落库 `decision_signals`**     | 网格自动跟踪等扩展                |

### 1.2 验收目标（DoD）

1. 对首批 8 只标的，能在收盘后跑出一条**可执行的信号记录**并落库 `decision_signals`；
2. 每条信号**可回溯**：`evidence_json` 能还原"依据了哪些指标/阈值/战法"（对应 FR-18）；
3. 所有阈值、权重**来自配置文件**，改配置不改进代码（NFR-6）；
4. 单只标的失败**不影响**其余标的（NFR-8 / FR-2.3）。

---

## 2. 数据模型设计

### 2.1 复用表（**不改结构**）

| 表                                                                                             | 用途                     | 对应需求              |
| --------------------------------------------------------------------------------------------- | ---------------------- | ----------------- |
| `stock_daily`                                                                                 | 日线行情（周线由日线聚合或复用现有周线获取） | FR-2.1/2.2/2.5    |
| **`decision_signals`**                                                                        | **信号主表（四端同源核心）**       | FR-8 / NFR-9      |
| `portfolio_accounts` / `portfolio_positions` / `portfolio_position_lots` / `portfolio_trades` | 持仓、成本、批次（FIFO）         | FR-1.3 / FR-9     |
| `conversation_messages` / `conversation_summaries`                                            | 对话留档 + 滚动摘要            | FR-15 / FR-14.3   |
| `intelligence_items` / `intelligence_sources`                                                 | 消息面/情报                 | FR-7 / FR-20      |
| `fundamental_snapshot`                                                                        | 基本面快照                  | FR-7 / FR-20.3    |
| `alert_rules` / `alert_triggers` / `alert_cooldowns`                                          | 事件与技术告警                | FR-20 / NFR-3     |
| `backtest_results` / `backtest_summaries`                                                     | 回测                     | FR-12 / P0→P1 质量门 |

### 2.2 新建表 `watchlist_items`（P0 唯一新增表）

现有自选股仅来自 `config.stock_list`（`src/config.py`，逗号分隔字符串）。P0 需表达**标的类型**（个股/ETF/LOF）、备注、启用状态，故新增一张轻表：

```sql
-- 走现有 schema_migrations 迁移机制新增
CREATE TABLE watchlist_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        VARCHAR(16)  NOT NULL,             -- 规范代码（复用 canonical_stock_code）
    name          VARCHAR(64),
    asset_type    VARCHAR(8)   NOT NULL DEFAULT 'stock',  -- stock | etf | lof
    market        VARCHAR(8)   NOT NULL DEFAULT 'cn',
    track_index   VARCHAR(64),                        -- ETF 跟踪指数（个股为空）
    enabled       BOOLEAN      NOT NULL DEFAULT 1,
    sort_order    INTEGER      NOT NULL DEFAULT 0,
    note          TEXT,
    created_at    DATETIME,
    updated_at    DATETIME,
    UNIQUE (symbol, asset_type)
);
CREATE INDEX ix_watchlist_enabled_sort ON watchlist_items (enabled, sort_order);
```

**首批数据（8 只，P0 验收样本）**

| symbol | name           | asset_type | track_index（填法示意） |
| ------ | -------------- | ---------- | ----------------- |
| 601169 | 北京银行           | stock      | —                 |
| 601818 | 光大银行           | stock      | —                 |
| 601919 | 中远海控           | stock      | —                 |
| 000725 | 京东方A           | stock      | —                 |
| 513100 | 纳指ETF(国泰)      | etf        | 纳斯达克100           |
| 513180 | 恒生科技ETF(华夏)    | etf        | 恒生科技              |
| 513530 | 港股通红利ETF(华泰柏瑞) | etf        | 中证港股通高股息          |
| 520660 | 港股通央企红利ETF(南方) | etf        | 中证国新港股通央企红利       |

> 兼容性：`config.stock_list` 保留为"快速配置入口"，启动时与 `watchlist_items` 做**单向同步**（配置 → 表），表中手工增删不受影响。

### 2.3 `decision_signals` 字段 → 需求语义映射

**不新增列**，用现有字段承载（P0 零迁移；若后续需强查询再走迁移加列）：

| 需求语义                    | `decision_signals` 字段                                     | 说明                                                                              |
| ----------------------- | --------------------------------------------------------- | ------------------------------------------------------------------------------- |
| 建仓 / 加仓 / 减仓 / 清仓       | `action`                                                  | 复用八态枚举：`buy` / `add` / `reduce` / `sell` / `hold` / `watch` / `avoid` / `alert` |
| **止盈 / 抄底**（无独立 action） | `metadata_json.sub_action`                                | 取 `take_profit` / `dip_buy`，`action` 用 `reduce/sell` 或 `buy/add` 承载             |
| 止损位 / 目标价 / 买点区间        | `stop_loss` / `target_price` / `entry_low` / `entry_high` | 已有                                                                              |
| **仓位建议**（如"减 3 成"）      | `metadata_json.position_advice`                           | 表无独立列，先用 JSON                                                                   |
| 依据可回溯（FR-18）            | `evidence_json`                                           | 存"指标值 + 命中阈值 + 战法名"                                                             |
| 场景标签                    | `market_phase`                                            | 上涨/下跌/震荡/变盘                                                                     |
| 紧急分级                    | `metadata_json.is_urgent`（P1 推送用）                         | P0 先写                                                                           |
| 有效期                     | `expires_at`                                              | 建议 T+3 自然日失效                                                                    |

> **四端同源**由"只写这张表、四端只读这张表"保证（NFR-9）。

---

## 3. 风控引擎设计（P0 核心新建件）

### 3.1 定位与边界

- **新建** `src/core/risk_engine.py`，负责**单标的级**风险规则；
- 现有 `src/services/portfolio_risk_service.py` 负责**组合级**（集中度、回撤、止损接近度）——**保留，两者组合使用**，不重复造；
- 引擎**只产出决策**（止损位/止盈计划/仓位建议），**不下单**（NG6）。

### 3.2 接口（示意）

```python
# src/core/risk_engine.py

@dataclass(frozen=True)
class PositionView:          # 由 portfolio_* 组装
    symbol: str
    avg_cost: float
    quantity: float
    position_ratio: float    # 占总仓比例
    peak_price: float        # 持有期最高价（移动止盈用）

@dataclass(frozen=True)
class IndicatorView:         # 由指标层组装
    close: float
    ma: Dict[int, float]     # {5:..,10:..,20:..,60:..}
    bias: float
    rsi: float
    kdj_j: float
    macd_hist: float
    boll_upper: float
    boll_lower: float

@dataclass(frozen=True)
class RiskRuleSet:           # 来自 config/rules/risk_rules.yaml
    stop_loss_mode: str                    # technical | percent
    stop_loss_ma: tuple[int, ...]          # (5,10,20)
    take_profit_targets: tuple[float, ...] # (0.10, 0.15, 0.20, 0.25)
    take_profit_ratios: tuple[float, ...]  # (1/3, 1/3, ...)
    trail_activate: float                  # 0.10
    trail_drawdown: float                  # 0.08
    build_max_ratio: float                 # 0.30
    position_cap: float                    # 0.65
    dip_max_ratio: float                   # 0.25

@dataclass
class RiskDecision:
    stop_loss: float
    take_profit_plan: list[dict]   # [{trigger, price, ratio, reason}]
    position_advice: dict          # {action, ratio, reason}
    is_urgent: bool
    reasons: list[str]

class RiskEngine:
    def __init__(self, rules: RiskRuleSet): ...
    def evaluate_hold(self, pos: PositionView, ind: IndicatorView) -> RiskDecision: ...
    def evaluate_build(self, ind: IndicatorView, scenario: str) -> RiskDecision: ...
    def evaluate_dip_buy(self, ind: IndicatorView, stop_ref: float) -> RiskDecision: ...
    def compute_stop_loss(self, ind: IndicatorView, pos: PositionView | None) -> float: ...
    def compute_take_profit_plan(self, ind: IndicatorView, pos: PositionView) -> list[dict]: ...
    def position_sizing(self, kind: str) -> float: ...   # build|add|dip_buy
```

### 3.3 规则来源（全部配置化）

规则不写死在代码里，落到 **`config/rules/risk_rules.yaml`**（P0 新建），内容即需求附录默认参数：

```yaml
stop_loss:
  mode: technical            # technical | percent
  ma: [5, 10, 20]
  percent: 0.08              # 百分比模式（可选）
take_profit:
  targets: [0.10, 0.15, 0.20, 0.25]
  ratios:  [0.333, 0.333, 0.334]
  ma_break: [5, 10, 20]      # 破线止盈
  trail: { activate: 0.10, drawdown: 0.08 }
position:
  build_max: 0.30
  cap: 0.65
  dip_buy_max: 0.25
  add_ladder: [0.30, 0.20, 0.10]   # 金字塔
```

---

## 4. 场景引擎与指标层

| 层    | 落点                                                                            | 说明                                                                                                                                                    |
| ---- | ----------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| 指标计算 | 复用 `src/utils/data_processing.py` + `src/services/alert_indicators.py`        | 后者已有 `TECHNICAL_ALERT_TYPES`（`ma_price_cross` / `rsi_threshold` / `macd_cross` / `kdj_cross` / `cci_threshold`）与 `IndicatorEvaluation`，可直接作为**阈值判定器** |
| 场景判定 | 扩展 `src/core/market_strategy.py` + `src/services/market_structure_service.py` | 复用现有趋势/结构判定，补"变盘/背离/多周期冲突"                                                                                                                            |
| 战法匹配 | 复用 `strategies/*.yaml`（15 个）加载器                                               | `bull_trend`（回踩低吸）/ `box_oscillation` / `bottom_volume` / `shrink_pullback`                                                                           |
| 基金校验 | 新增轻函数                                                                         | ETF 用 `track_index` 趋势校验场内价格趋势（折溢价/规模**本期不做**，R1 已决）                                                                                                  |

**FR-17 建仓信号（写死口径）**

- 趋势条件：周线趋势向上（周线 MA5>MA10，或站稳 MA60 且 MA60 走平向上）；
- 买点条件（其一）：① 放量突破平台后回踩 MA20/MA60 不破；② 底部放量 + MACD 金叉/底背离 + 重新站上 MA20；
- 排除：下跌趋势 / 趋势不明 / 当日异动（涨跌停、跳空）；**不用单一金叉**。

---

## 5. 端到端时序（20:00，P0 只到"落库"）

```
20:00  scheduler 触发（SCHEDULE_TIME=20:00，复用 src/scheduler.py）
  │
  ├─1 读标的：watchlist_items(enabled=1) ∪ portfolio_positions
  ├─2 拉数据：data_provider 日线/周线/指数（失败降级，FR-2.3）
  ├─3 落库：stock_daily（断点续传，复用现有智能更新）
  ├─4 指标：MA5/10/20/60、MACD、KDJ、RSI、BOLL、BIAS
  │        + ETF：track_index 趋势校验
  ├─5 场景：上涨/下跌/震荡/变盘/背离（日线+周线一致性）
  ├─6 战法：匹配 strategies/*.yaml，给出倾向
  ├─7 多因子打分：技术45/大盘板块25/消息15/基本15（可配）
  ├─8 风控：risk_engine → 止损位 / 分批止盈计划 / 仓位建议
  ├─9 组装信号：action + sub_action + price_zone + stop_loss + target + position_advice
  │             + evidence_json（可回溯）+ is_urgent
  ├─10 落库：decision_signal_service.save()  ← 四端同源唯一写入点
  └─11 退出（P0 到此为止；推送在 P1）
```

**失败隔离**：步骤 2~10 对每只标的**独立 try/except**，单只失败记录日志并跳过，不中断整批（NFR-8）。

---

## 6. P0 改动清单

| 文件                                        | 动作        | 说明                                                      |
| ----------------------------------------- | --------- | ------------------------------------------------------- |
| `src/storage.py`                          | **扩展**    | 新增 `WatchlistItem` 模型 + 迁移（`schema_migrations`）         |
| `src/repositories/watchlist_repo.py`      | **新建**    | 自选股 CRUD                                                |
| `src/services/watchlist_service.py`       | **新建**    | 与 `config.stock_list` 单向同步                              |
| `src/core/risk_engine.py`                 | **新建**    | 单标的级风控引擎（P0 核心）                                         |
| `config/rules/risk_rules.yaml`            | **新建**    | 风控规则配置                                                  |
| `config/rules/scenario_rules.yaml`        | **新建**    | 场景阈值配置（FR-18 映射固化）                                      |
| `src/core/rule_config.py`                 | **新建**    | 规则配置加载/校验                                               |
| `src/services/scenario_engine.py`         | **新建/扩展** | 场景判定编排（底层复用 market_strategy / market_structure_service） |
| `src/services/p0_signal_builder.py`       | **新建**    | 组装 `decision_signals` 落库（P0 编排入口）                       |
| `src/scheduler.py`                        | 复用        | 仅把 `SCHEDULE_TIME` 配为 `20:00`                           |
| `strategies/*.yaml`                       | 复用        | 三场景战法，不改                                                |
| `src/services/decision_signal_service.py` | 复用        | 信号落库（已有）                                                |

> 命名均为**示意**，实施时可按现有目录习惯微调；**唯一硬约束**是"复用 `decision_signals`、复用 `portfolio_*`、复用 `conversation_summaries`"。

---

## 7. P0 → P1 质量门（回测体检）

按 R3 决策，回测**不单独占一期**，而是作为 P0 与 P1 之间的**质量门**：

1. 用现有 `src/core/backtest_engine.py` + `services/backtest_service.py`；
2. 样本：**601919 中远海控 / 601288 中国农业银行**，区间**近 3 年**；
3. 输出：**胜率 / 盈亏比 / 最大回撤 / 样本数**（FR-12.2），并按上涨/下跌/震荡分场景统计；
4. **通过标准（建议，可调）**：整体胜率 ≥ 50%、盈亏比 ≥ 1.5、最大回撤 ≤ 25%、样本数 ≥ 30；


1. 达标 → 开 P1 推送；不达标 → 调 `config/rules/*.yaml` 参数重测，不硬上线。

---

## 8. P0 待办与风险

| # | 事项                         | 状态 / 处理                       |
| - | -------------------------- | ---------------------------- |
| 1 | 520660 交易制度（T+0/T+1）核验 | ✅ **已决：T+0**（上交所上市交易公告"实施日内回转交易"）；首批 4 只 ETF 均为 T+0，4 只个股 T+1 |
| 2 | 首批持仓初始录入（成本价/数量）           | ✅ **已决：P0 先留空**（只出通用信号，不含个性化仓位）；**P2 做 H5 时录入** |
| 3 | 免责声明文案（R5）                 | ✅ **已决：采用需求 NFR-5 默认文案**（"本内容由程序基于历史数据自动生成，仅供学习参考，不构成任何投资建议，据此操作风险自负"） |
| 4 | 周线数据来源                     | ✅ **已决：由日线聚合**（`data_provider` 仅提供日线；`indicators.resample_weekly` 已实现） |
| 5 | ETF `track_index` 取数与校验    | ✅ **已决**：配置静态映射（种子已填），P0 冒烟校验；不做折溢价（R1 降级） |

---

## 9. P0 验收清单（DoD 逐项）

- [ ] 8 只标的均能生成信号并落库 `decision_signals`（含 ETF）
- [ ] 每条信号 `evidence_json` 可还原指标依据（FR-18）
- [ ] 阈值/权重全部来自 `config/rules/*.yaml`（NFR-6）
- [ ] 单标的失败不影响整批（NFR-8）
- [ ] 风控输出含**止损位 + 分批止盈计划 + 仓位建议**，且买入类建议**强制带止损**（FR-9）
- [ ] 回测体检达标（§7）后方可进入 P1
- [ ] **全程不新增/改动业务代码**（本设计阶段约束）

---

*（本文档为 P0 详细设计 v1.0，属设计阶段产物；实施需需求方明确授权。所有新增文件为示意，最终以实施时的代码评审为准。）*
