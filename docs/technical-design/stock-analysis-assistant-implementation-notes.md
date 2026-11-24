# 股票分析助手 · 实现说明（d 阶段实施记录）

- 创建时间：2026-11-24
- 对应依据：需求 [v2.6](../requirements/stock-analysis-assistant-requirements-v2.6.md)、技术方案 [v0.3](stock-analysis-assistant-technical-design-v0.3.md)
- 本文性质：**实施记录 + 偏差登记**。技术方案与本文冲突时以技术方案为准，偏差按本文 §4 逐条提请确认。

---

## 1. 本增量交付范围

| 模块 | 状态 | 落点 |
| --- | --- | --- |
| C1 架构骨架 | ✅ 建立独立包 | `src/assistant/`（与 `src/agent/` 平级、零依赖） |
| C2 数据与存储 | ✅ 取数层 + 存储层 + 投递台账 | `src/assistant/data/` |
| C2 调度与门控 | ✅ 时点解析 / 交易日 fail-closed / 多时点同源投递 / 节拍编排 | `src/assistant/scheduling/` |
| C3 决策引擎 | ✅ 守卫链 / 阶段 / 阶段门 / 评分 / 风险 / 条件计划 | `src/assistant/engine/` |
| C4 参数框架 | ✅ 三档预设 + 单项覆盖 + param_hash + 变更记录 | `src/assistant/config/` |
| C5 输出层 | ⬜ 未开始（Web 页面 / 飞书七段 / LLM 文案） | — |
| C6 验证回测 | ⬜ 未开始（纸面回看 / 条件计划回测） | — |

**没有做的部分**：装配 pipeline（取数 → 守卫链 → 落库 → 渲染的端到端编排）、快照 API、Web 页面、飞书推送与入站、LLM 文案层、回测扩展、adj_factor 正式来源（M15 兜底路径已实现但未启用）。

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
├── data/
│   ├── models.py          20 张 assistant_ 表（L1 / L3 / L4 + 投递台账）
│   └── storage.py         L1 幂等写入、周月线聚合、L3 append-only、L4 变更重放、投递台账
└── scheduling/
    ├── cron.py            5 字段 cron 解析与匹配（自实现，零依赖）
    ├── config.py          时点配置（HH:MM 简写 + cron 混用）、按日触发计划
    ├── gate.py            交易日门控（fail-closed，日历在 decide 内延迟 import）
    ├── delivery.py        多时点同源投递、去重台账、同源不变量校验
    └── runner.py          一次 tick 的完整节拍（门控 → 排程 → 生成 → 投递）
```

**依赖方向**：`enums` ← `domain` ← `config` / `engine` ← `data` ← `scheduling`。
`engine` 不 import `data`；`scheduling` 全程**只用标准库**（cron 自实现、时区用
`zoneinfo`），交易日历只在 `TradingDayGate.decide()` 内部延迟 import，因此调度层可在
无 pandas / 无 SQLAlchemy 的解释器下单独测试与运行（已实测）。

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
| §4.6.4 不复权落库 | `data/fetcher.py` | 三源一律 `adjust=""`；空响应按失败降级，不落前复权 |
| §7.2 取数失败降级 | `data/fetcher.py` | 连续 3 次失败熔断该源 300s；全源失败回落缓存标 D3 |
| §4.6.3 首次回补 ≥3 年 | `data/fetcher.py::backfill` | 批量回补 + 落库 + 周月线重建 + 幂等跳过 |
| FR-9.13 推送时点配置化 | `scheduling/config.py` | `HH:MM` 逗号简写 + 5 字段 cron 混用；逗号双重语义按「含冒号」区分；非法条目逐条回退并告警 |
| FR-9.13 ③④ 多时点同源 | `scheduling/delivery.py` | 同日 payload 只生成一次；各时点逐字同源；已投时点按分钟级 key 拦下 |
| FR-8.6 非交易日开关 | `scheduling/config.py` + `gate.py` | `skip` / `notify_skip` 两值都不生成快照；`notify_skip` 同日只提醒一次 |
| FR-8.7 交易日 fail-closed | `scheduling/gate.py` | 日历 `None` / 抛异常一律按非交易日处理并告警；不用 `is_market_open()`（fail-open） |
| FR-8.7 ④ 时区归一 | `scheduling/gate.py::now` | naive 按配置时区补全，aware 转本地；已测 UTC 20:00 → 次日 04:00 |
| B50 ③ 重启不重投 | `data/storage.py`（`assistant_delivery_log`） | 台账持久化，进程重启后仍能判 `duplicate` |
| §2.3 取数后才生成且才推送 | `scheduling/runner.py` | `now < fetch_time` 不生成当日快照；**早于取数的时点一律不投递**（仅 20:00 推送） |

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

### D-7 取数层新增「指数代码必须带市场前缀」硬校验（实测发现并修复）

- 联网验证时发现真实 bug：`sh000001`（上证指数 3548 点）经个股代码归一化被转成 `sz000001`（平安银行 12 元），**静默取到另一只标的**。
- 修复：新增 `to_index_symbol()`，只接受带市场的写法（`sh000001` / `000001.SH` / `000001sh`），裸 `000001` 直接抛错，不做「猜市场」；`ReferenceFetcher.fetch_index` 入口即校验。
- 已加回归测试（含「必须按 sh 前缀请求」的断言）。

---

### D-8 逗号双重语义：配置串不能「先按逗号切」（**实测踩坑并修复**）

- 配置里逗号既是 `HH:MM` 列表分隔符（`08:00,20:00`），也是 cron 字段的列表运算符
  （`0 8,20 * * *`）。第一版按逗号整体切分，把 `0 8,20 * * *` 切成 `0 8` + `20 * * *`
  两条非法项，**静默回退成默认 20:00**，用户配的 8 点/20 点双推完全没生效。
- 现实现：先按**空格分词**，再按「片段是否含冒号」区分——含冒号的是 `HH:MM` 候选，
  不含冒号的是 cron 字段（同一 token 内的多个片段用逗号拼回一个字段）。
- 已加回归用例：`0 8,20 * * *`、`08:00,0 20 * * 1-5`、`0 20 * * 1-5` 三种混用形态。

### D-9 早于取数时点的推送（**已确认：不投递，仅 20:00 推送**）

- R24 举例 `08:00,20:00` 双推，但当日快照要 17:30 取数后才存在——08:00 时点不可能拿到
  当日快照，B50 ③「各时点 `data_date`/`snapshot_id`/`advice` 逐字相同」在这类配置下与
  R23 的时序天然冲突。
- 曾实现过「早于取数的时点投递当时最新快照（前一交易日晚间那份）」的方案，但那等于
  **用旧结论冒充当日意见**。
- **2026-11-24 用户确认：仅 20:00 推送**。因此改为：早于 `fetch_time` 的时点**一律不投递**，
  在 `DayPlan` 里标 `pre_fetch` 并告警；实际只在取数后的时点（默认 20:00）推送。
- 影响：配置写 `08:00,20:00` 时只有 20:00 生效。多时点能力仍在——取数之后配置多个时点
  （如 `19:00,20:00`）依然逐时点推送且严格同源。

### D-10 cron 采用自实现而非引入 croniter / APScheduler

- 方案 §2.4 把选型留给 d 阶段。选自实现的理由：主流程依赖表无 croniter，新增依赖要动
  部署镜像；需求只要「`0 20 * * 1-5`、`*/N`、指定星期」这几类表达力。
- 覆盖 POSIX 5 字段子集：`*`、`*/N`、`a`、`a-b`、`a-b/N`、`a/N`、逗号列表、月名/星期名、
  `0` 与 `7` 均为周日；**日/周 OR 语义**按 Vixie（以字段首字符 `*` 判定星号字段）。
- 明确不支持并报错：秒字段、6/7 字段、`@daily` 类宏、`L`/`W`/`#` 扩展——需求均未要求。
- 风险：自实现需自己保证语义正确，已用 28 条用例锁定（含 OR 语义、`*/2` 归星号字段、
  闰年 2 月 30 日返回 `None` 而非死循环）。

### D-11 五个配置键尚未落到 `src/config.py`

- 方案 §2.4 建议把 `ASSISTANT_PUSH_SCHEDULE` 等五个键加进 `src/config.py`。本增量**先按
  映射解析**（`parse_env_config(env)`），未改 `src/config.py` 与 `config_registry.py`。
- 原因：改 `Config` dataclass 要同步 `from_env`、`config_registry` 元数据与设置面板，
  属跨模块改动；调度层已可在不改动主流程的前提下独立验证。
- 待装配阶段一并落地：加字段 + 注册表条目 + 设置面板，一次改完再回归主流程。

## 5. 验证

```bash
# 引擎 / 取数 / 存储单测（不需要完整项目依赖，独立 confcutdir 避免加载 tests/conftest.py）
python -m pytest tests/assistant --confcutdir=tests/assistant -q

# 风格
python -m flake8 src/assistant tests/assistant scripts/assistant_fetch_smoke.py

# 真实交易日历验证（需 exchange-calendars；调度层本身不依赖它）
python -c "from src.assistant.scheduling import TradingDayGate; from datetime import datetime; \
print(TradingDayGate().decide(datetime(2026,11,24,20,0)).as_dict())"




# 真实联网冒烟（不进入 pytest 默认套件，需装 akshare）
python scripts/assistant_fetch_smoke.py --codes 600000 000001 --years 3
```

当前结果：**227 passed**，`flake8` 无告警。

**2026-11-24 联网实测（akshare 1.19.5）**：

| 项 | 结果 |
| --- | --- |
| 东财 `push2his` | `ProxyError`（当前网络下不可用）→ 触发降级 |
| 新浪日线 | **D2 成功**，`600000` / `000001` 各 725 行，覆盖 2023-11-27 ~ 2026-11-24，0.0 丢弃行，约 2.1–2.9s/只 |
| 熔断 | 东财 2 次失败已计数（阈值 3 未触发），符合「连续 3 次才排除」 |
| 指数（新浪） | 81 行，`sh000001` 收盘 **3548.37**（修 bug 前后对比：12.3 → 3548.37） |
| 板块（同花顺） | 90 个板块，涨跌幅/涨跌家数/领涨股齐全，名称→代码映射命中（如 `881267` 能源金属 +2.23%，8/6 家，领涨腾远钴业） |

**2026-11-24 交易日历实测（`exchange-calendars` 4.13.3，真实日历）**：

| 样本 | 判定 |
| --- | --- |
| 2026-11-24 20:00（周二） | 交易日 ✅ |
| 2026-11-26 20:00（周四） | 交易日 ✅ |
| 2026-11-28 20:00（周六） | 非交易日 |
| 2026-10-01 20:00（国庆） | 非交易日 ✅（能识别节假日，不是简单按周末过滤） |

**端到端演练（临时 SQLite，模拟一天 7 次触发 + 跨日 + 进程重启）**：

- 07:00 / 08:05 / 12:00：不生成、不投递；08:00 配置的时点被跳过并告警（仅 20:00 推送）
- 12:00 / 19:59：`duplicate`，不重复投递
- 17:30：生成当日快照（`generated=True`），未到 20:00 不投
- 20:00：投当日快照（全天唯一一条）；20:30 `duplicate`
- 11-28 周六：`notify_skip` 首次 `notified=True`，二次 `False`（同日只提醒一次），全程零生成
- 重启后 20:10：两个时点均判 `duplicate`，**没有重复推送**

测试覆盖的重点契约（均为可回归断言，不是快照）：

- 六阶段判定各自的触发条件 + S5 置信度上限 + S0 不默认成 S2/S5
- 防抖：第 1 日不切换、第 2 日切换、条件中断即清零、S0 立即生效
- 阶段门 8 条规则逐条 + 「不放宽」不变式 + `stage_gate_hit` 序列化
- 风险：大盘 ≤ 警示、数据质量只观察、同类合并、3 条警示不升级、推测词拒收
- 条件计划：同向过滤、只价格触发、100 股取整、越界不出分支、T+1 不混买卖、S5 无买入分支
- 存储：L1 幂等、周线聚合 OHLC、L3 双快照不覆盖、参数回滚重放、`False` 不被解成 `0`
- cron：5 字段逐字段解析、日/周 OR 语义、`*/2` 归星号字段、`0`/`7` 同为周日、不可能表达式返回 `None`
- 配置：默认单推、多时点排序去重、混用形态、非法条目逐条回退、非法时区/动作/取数时点回退
- 门控：交易日 / 非交易日 / `None` / 抛异常 四种输入、`skip` 与 `notify_skip`、UTC 跨日换算
- 投递：同日只生成一次、两时点同源、跨快照 `BLOCKED`、发送失败可重试、重启后台账仍防重
- 节拍：取数前不生成、早于取数的时点不投递、错过的时点补投、非交易日 `notify_skip` 同日只提醒一次

> 未跑：仓库全量测试需要 `fastapi/anyio/httpx` 等完整依赖，本次环境未安装；改动未触及这些模块。

---

## 6. 下一步（建议顺序）

1. **装配**（`pipeline.py`）：取数 → 守卫链 → 快照落库 → 渲染 的端到端编排；同时把
   `generate()` 做成幂等（同日已有 L3 快照则复用，不再重建），补齐 `load_existing()`
2. **配置落地**：五个 `ASSISTANT_*` 键进 `src/config.py` + `config_registry` + 设置面板（D-11）
3. **输出层**（`render/`）：飞书七段图标化 + 卡片渲染（两档密度）
4. **API + Web**（`api/v1/endpoints/assistant.py` + `pages/assistant/`）
5. **回测扩展**（`backtest/plan_backtest.py`）+ 纸面回看
6. `adj_factor` 正式来源（M15）：现已有「同源 qfq/raw 比值推算」函数但默认不启用，待定是否作为长期路径
