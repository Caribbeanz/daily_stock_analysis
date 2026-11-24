# 股票分析助手 · 实现说明（d 阶段增量 1）

- 创建时间：2026-11-24
- 对应依据：需求 [v2.6](../requirements/stock-analysis-assistant-requirements-v2.6.md)、技术方案 [v0.3](stock-analysis-assistant-technical-design-v0.3.md)
- 本文性质：**实施记录 + 偏差登记**。技术方案与本文冲突时以技术方案为准，偏差按本文 §4 逐条提请确认。

---

## 1. 本增量交付范围

| 模块 | 状态 | 落点 |
| --- | --- | --- |
| C1 架构骨架 | ✅ 建立独立包 | `src/assistant/`（与 `src/agent/` 平级、零依赖） |
| C2 数据与存储 | 🟡 存储层完成，取数层未做 | `src/assistant/data/` |
| C3 决策引擎 | ✅ 守卫链 / 阶段 / 阶段门 / 评分 / 风险 / 条件计划 | `src/assistant/engine/` |
| C4 参数框架 | ✅ 三档预设 + 单项覆盖 + param_hash + 变更记录 | `src/assistant/config/` |
| C5 输出层 | ⬜ 未开始（Web 页面 / 飞书七段 / LLM 文案） | — |
| C6 验证回测 | ⬜ 未开始（纸面回看 / 条件计划回测） | — |

**没有做的部分**：真实取数（akshare 封装）、调度与交易日门控、快照 API、Web 页面、飞书推送与入站、LLM 文案层、回测扩展。

---

## 2. 代码结构与职责

```
src/assistant/
├── __init__.py            版本与包说明
├── enums.py               阶段码 / 守卫序 / 动作 / 风险码 / 规则号（唯一真值）
├── domain.py              输入输出 dataclass（Bar / StockView / RiskSignal / PlanBranch …）
├── config/
│   ├── presets.py         45 项参数注册表 + 三档预设 + 契约校验
│   └── overrides.py       ParamSet（基线+覆盖）、param_hash、变更记录
├── engine/
│   ├── indicators.py      SMA / 斜率 / 分位 / 箱体 / 关键价位（纯函数）
│   ├── stage.py           阶段判定 S0–S5 + 连续 N 日防抖
│   ├── stage_gate.py      阶段门 S5.5（8 条改写规则 + 只收紧断言）
│   ├── scoring.py         技术面主导评分 + 决策表 + 档位缓冲
│   ├── risk.py            风险 7 类 / 三级联动 / 同类合并
│   ├── plan.py            条件计划（阶段模板 + 同向过滤 + 股数 + 硬上限）
│   └── guard_chain.py     守卫链 S0→S6 主入口（留痕 GuardTrace）
└── data/
    ├── models.py          19 张 assistant_ 表（L1 / L3 / L4）
    └── storage.py         L1 幂等写入、周月线聚合、L3 append-only、L4 变更重放
```

**依赖方向**：`enums` ← `domain` ← `config` / `engine` ← `data`。
`engine` 不 import `data`，因此引擎可用纯 Python 环境直接跑（无 SQLAlchemy / pandas 依赖，已实测）。

---

## 3. 需求 → 代码映射

| 需求条款 | 实现位置 | 关键约束落地 |
| --- | --- | --- |
| §5.14.2 六阶段判定与顺序 | `engine/stage.py::detect_stage` | 短路顺序 S0→S3→S1→S4/S2→S5；S5 置信度上限「中」 |
| §5.14.2 切换防抖 | `engine/stage.py::apply_debounce` | 连续 N 日；S0 与「从 S0 恢复」立即生效 |
| §5.14.5 阶段门 8 条改写 | `engine/stage_gate.py` | `RuleId` 枚举 + `stage_gate_hit` 留痕 + 只收紧断言 |
| §5.4 守卫链顺序 | `engine/guard_chain.py` | `GuardTrace` 按 S0…S6 八步留痕，顺序由测试锁定 |
| §5.1 / §5.4 技术面主导评分 | `engine/scoring.py` | 低权重四项之和 ≤ 40 分（注册表校验） |
| §5.10 风险 7 类 / 三级 | `engine/risk.py` | 数据质量类强制观察级；大盘 ≤ 警示级；同类合并不叠加升级 |
| §5.11 条件计划 | `engine/plan.py` | 只给价格/涨跌幅触发、同向过滤、股数 100 股取整、单条越界不出 |
| §5.14.6 阶段模板 | `engine/plan.py::_template` | S1 回踩/突破、S2 上下沿、S3 反弹减+破位清、S4 确认买+试探、S5 不设买 |
| §5.16 三档参数 | `config/presets.py` | 档位方向校验（严格档不得更宽松） |
| §5.16.4 覆盖与变更 | `config/overrides.py` + `data/storage.py` | param_hash、append-only、回滚也记一条、值类型不丢失 |
| §4.6 存储分层 | `data/models.py` | L1 幂等、L2 不落库、L3 append-only、L4 下次快照生效 |

---

## 4. 偏差与待确认项

> 按技术方案「实施中若发现缺口或矛盾登记并提请决策」的要求登记，**均未擅自改口径**。

### D-1 存储表用 `assistant_` 前缀 + 独立 metadata（**已确认：维持分表**）

> **决议（2026-11-24，用户拍板「选 1」）**：维持 `assistant_` 分表方案。
> 理由：口径隔离最干净，主流程前复权口径零改动；代价是同一标的日线在两处各存一份，接受。

- 方案原文写「扩展 `stock_daily`（加 `adj_factor` 列，`adjust` 改 `""`）」。
- 实际冲突：现有 `stock_daily` 存的是**前复权**数据（`akshare_fetcher` 固定 `adjust="qfq"`），主流程回测与指标都按该口径。若改为不复权，会改变主流程全部历史结论；若同表混存两种口径，则无法区分。
- 落地做法：新建 `assistant_stock_daily`（不复权 + `adj_factor`），与主流程同库不同表，主流程表结构与读写链**零改动**。
- 影响：`JOIN` 仍可行；不采用「改主流程为不复权」与「主表加 `adjust_mode` 多口径行」两个备选。

### D-2 S3 允许集合包含「等待买点」

- §5.14.3 表把 S3 允许动作写成「减仓/清仓/反弹减/反 T」，禁止「任何买入类」。
- 但 §5.14.5 明确「S3 评分给出买入类 → 改 **等待买点** / 观望」。
- 实现：`WAIT_BUY` 作为**被动等待态**纳入 S3 允许集合，`BUY` / `ADD` / `PROBE` 仍严格禁止。已加注释与测试锁定。

### D-3 「加仓」与「买入」在「只收紧不放宽」断言中同级

- §5.14.5 要求 S4 右侧确认后把「加仓」统一改写为「确认后建仓」——这是**措辞归一**，不是放宽风险。
- 若把 BUY 视为比 ADD 更激进，该改写会触发「不放宽」断言。实现中两者同级（rank 3），仅 `WATCH(0) < WAIT_BUY(1) < PROBE(2) < BUY=ADD(3)`。

### D-4 新增参数 `box_touch_tol`（箱体触碰容差，标准值 2%）

- 需求只给「上下沿各触碰 ≥2 次」，未定义「怎样算触碰」。
- 实现新增该参数（区间 0.5%–5%，初值 2%），进注册表与 `param_hash`；若你认为应固定不可配，可移出注册表。

### D-5 决策表分数带为初值（**待纸面回看**）

- 需求只锁「7 档 + 决策表结构」，未给分数边界。实现取 `≥72 / ≥58 / ≥42 / ≥28 / <28` 五档，对应强/偏强/中性/偏弱/差。
- 属 `SCORE_BANDS` 常量，回看后回写；未进参数注册表（避免被误认为已定口径）。

### D-6 部分参数为占位初值（`provisional=True`）

| 参数 | 初值 | 说明 |
| --- | --- | --- |
| `risk_severe_drop_pct` | 12% | 需求写「由回看定」 |
| `stop_l1_ratio` | 35% | 需求写「留纸面回看」 |
| `stop_l2_pct` | 8% | 同上 |
| `stage_theta` / `stage_epsilon` | 0.003 / 0.005 | 需求已给初值，仍待回看确认 |
| 四个权重 | 8 / 6 / 4 / 4 分 | 需求只给「技术面主导」约束 |

---

## 5. 验证

```bash
# 引擎与存储单测（不需要完整项目依赖，独立 confcutdir 避免加载 tests/conftest.py）
python -m pytest tests/assistant --confcutdir=tests/assistant -q

# 风格
python -m flake8 src/assistant tests/assistant
```

当前结果：**109 passed**，`flake8` 无告警。

测试覆盖的重点契约（均为可回归断言，不是快照）：

- 六阶段判定各自的触发条件 + S5 置信度上限 + S0 不默认成 S2/S5
- 防抖：第 1 日不切换、第 2 日切换、条件中断即清零、S0 立即生效
- 阶段门 8 条规则逐条 + 「不放宽」不变式 + `stage_gate_hit` 序列化
- 风险：大盘 ≤ 警示、数据质量只观察、同类合并、3 条警示不升级、推测词拒收
- 条件计划：同向过滤、只价格触发、100 股取整、越界不出分支、T+1 不混买卖、S5 无买入分支
- 存储：L1 幂等、周线聚合 OHLC、L3 双快照不覆盖、参数回滚重放、`False` 不被解成 `0`

> 未跑：仓库全量测试需要 `fastapi/anyio/httpx` 等完整依赖，本次环境未安装；改动未触及这些模块。

---

## 6. 下一步（建议顺序）

1. **取数层**（`data/fetcher.py`）：不复权日线 + 多源降级（东财 `push2` → 同花顺/腾讯/新浪）+ `source_tag`，接 M15–M17 实测结论
2. **调度与门控**（`scheduling/`）：`HH:MM` / cron 解析、交易日 fail-closed、多时点同源
3. **输出层**（`render/`）：飞书七段图标化 + 卡片渲染（两档密度）
4. **API + Web**（`api/v1/endpoints/assistant.py` + `pages/assistant/`）
5. **回测扩展**（`backtest/plan_backtest.py`）+ 纸面回看
