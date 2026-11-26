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
| C2 装配链 | ✅ 取数 → 守卫链 → 当日快照 → 渲染 的端到端编排（幂等） | `src/assistant/pipeline.py` |
| C5 入口接线 | ✅ `main.py --assistant` 显式 opt-in，时点仍由助手调度决定 | `src/assistant/service.py` + `main.py` |
| C5 快照/配置 API | ✅ 8 个只读端点 + 5 个配置端点，三元组校验、只读不重算 | `api/v1/endpoints/assistant.py` |
| C5 飞书渲染 | ✅ 七段图标化文本 + 两档密度 + T 日复盘 | `src/assistant/render/` |
| C5 Web 独立页面 | ✅ `/assistant` 单条路由（不套 `<Shell>`），Tab A 大盘/板块 + Tab B 列表 + 详情 + 配置 | `apps/dsa-web/src/pages/assistant/` |
| C5 卡片与区块 | ✅ 卡片八项固定顺序 + 详情五区块，全部只读快照、前端不重算 | `pages/assistant/components/` |
| C5 板块快照 | ✅ 涉及板块（持仓/自选所属）+ 逐板块指数日线判三档，`rs_t` 只做展示 | `src/assistant/engine/sector.py` |
| C5 个股→板块映射 | ✅ 东财 F10 三级行业 → 同花顺板块名匹配（11/12 实测命中） | `src/assistant/data/sector_map.py` |
| C5 飞书入站 | ✅ 关注增删（白名单 + 二次确认 + 审计）、笔记、当日结论查询、盘后复盘 | `src/assistant/inbound.py` + `bot/commands/assistant.py` |
| C3 决策引擎 | ✅ 守卫链 / 阶段 / 阶段门 / 评分 / 风险 / 条件计划 | `src/assistant/engine/` |
| C4 参数框架 | ✅ 三档预设 + 单项覆盖 + param_hash + 变更记录 | `src/assistant/config/` |
| C5 发送接线 | ✅ 新增 `route_type=assistant_daily` + 去重键＝`snapshot_id` + 纯文本降级 | `src/assistant/notify.py` |
| C5 LLM 文案层 | ✅ 三处用法（飞书 ② 段 / 个股一句话 / 大盘总结）；白名单、计数硬校验、降级、缓存，默认关闭 | `src/assistant/llm.py` |
| C6 条件计划回测 | ✅ 回合模拟层 + 历史计划回放驱动（walk-forward、断点续跑、规模护栏） | `src/assistant/backtest.py` / `backtest_replay.py` |
| C6 战法启停清单 | ✅ 15 条与 `strategies/*.yaml` 一一对应，启用 8 条进 `calc_version`（`rules-2.6.1`） | `src/assistant/strategies.py` |

**没有做的部分**：`sector_strength` 进评分与 SEC-1（映射已就绪，但阈值需纸面回看后再定）、纸面回看调参（D-5/D-6）、adj_factor 正式来源（M15 兜底路径已实现但未启用）。

> 已收口：增量 14 —— 战法启停清单进 `calc_version`（D-25）、股票名称由东财 F10 同一次请求带出（D-26）；
> 增量 15 —— 历史计划回放驱动（D-24 → D-27）。

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
│   ├── market.py          M1 大盘环境（指数日线 → 涨跌 + 2×2 档位）
│   ├── sector.py          板块强度三档（板块指数日线 → 2×2；rs_t 仅展示）
│   ├── plan.py            条件计划（阶段模板 + 同向过滤 + 股数 + 硬上限）
│   └── guard_chain.py     守卫链 S0→S6 主入口（留痕 GuardTrace）
├── versioning.py           calc_version 指纹（规则集 + 参数 + 分数带）
├── render/
│   ├── icons.py            图标语义表（固定映射，三处共用）+ 纯文本剥离
│   ├── review.py           T 日复盘（T-1 计划对照 T 日行情，只读）
│   └── feishu.py           七段图标化渲染（两档密度 + 超长按序降级）
├── pipeline.py             装配链：ensure_snapshot / payload / generate（幂等）
├── llm.py                  文案层：白名单 → 生成 → 校验 → 缓存/降级（默认关闭）
├── stage_text.py           阶段打法/允许/禁止/切换的唯一出处（飞书 ⑤ 段与 Web 详情共用）
├── inbound.py              飞书入站四项：白名单/二次确认/生效时点/只读查询/笔记
├── backtest.py             条件计划回合模拟：触发/成交/出场/成本 + 胜率口径
├── backtest_replay.py     历史计划回放驱动：逐日切片→守卫链→回合→统计（可续跑）
├── strategies.py           战法启停清单（§5.12.3）：静态登记 → calc_version
├── notify.py               发送接线：payload → 既有通知链（route_type / 去重键 / 纯文本降级）
├── config/
│   ├── presets.py          45 项参数注册表 + 三档
│   ├── overrides.py        单项覆盖 + param_hash + append-only 变更
│   └── service.py          配置 API 唯一入口：env 初始 → L4 生效、下一次快照生效
├── mode.py                 main.py 入口判定（零依赖，未启用时不加载取数栈）
├── service.py              运行时装配 + 调度循环（main.py 显式 opt-in）
├── data/
│   ├── models.py          21 张 assistant_ 表（L1 / L3 / L4 + 投递台账 + 防抖状态）
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
| §2.5 三元组 `calc_version` | `versioning.py` + `strategies.py` | 规则集版本 + `param_hash` + 决策表分数带 + 启用战法清单；`temperature` 与展示名称不入版本 |
| B50 ③ 当日快照只生成一次 | `pipeline.py::ensure_snapshot` | 按「快照日 T + param_hash + calc_version」查已有快照直接复用 |
| §5.14.2 连续 N 日防抖 | `data/models.py::StageStateRow` + `pipeline.py` | 防抖状态落库，跨进程/跨日可用（否则每天都是「首次判定」） |
| §4.6.2 来源如实标注 | `pipeline.py::_worst_tag` | 主源失败降级到 D2 时必须标 D2；快照级取最差 |
| §3.5.3.1 七段结构 | `render/feishu.py` | 段序固定可整段省略不可换序；① 必带复盘子行；③ 同股只出现一次取最高等级 |
| §3.5.3.1 图标语义（B47） | `render/icons.py` | 风险用色点、动作用符号、涨跌用 ▲▼；符号只做前缀不替代文字 |
| §3.5.3.1 硬约束 2 | `render/feishu.py::_plan_block` | ⑥ 段触发/动作/有效期三行不含 emoji 与加粗，可整行照抄 |
| §3.5.3.1 硬约束 5 | `render/icons.py::strip_markdown` | 降级纯文本前剥离 `**` 与标题井号 |
| §3.5.3.1 硬约束 7（B48） | `render/feishu.py::fit_feishu` | 完整 1,800 / 精简 1,100；降级顺序＝删风险依据→删阶段证据→删不可设单分支→限分支条数 |
| B48 两档同源 | `render/feishu.py` | 同一函数 + `RenderOptions` 开关；两档触发价/股数逐字一致（测试锁定） |
| §3.5.3.1 ① 复盘子行（R23） | `render/review.py` | T-1 计划对照 T 日高低价判触发；跳空单独标；缺行情记「无法判定」不猜 |
| §6.3 复用通知链（不新增平行发送器） | `notify.py` + `notification_routing.py` | 新增 1 个 `route_type`；发送仍由 `NotificationService`/`feishu_sender` 完成 |
| §2.3 多时点同源 vs 降噪去重 | `notification_noise.py::allow_repeat` + `notify.py` | `dedupe_key=snapshot_id` + `allow_repeat`：有意重复不被内容去重吞掉，静默时段仍生效 |
| §3.5.3.1 硬约束 5（降级剥离 `**`） | `notify.py` + `feishu_sender` 的 `fallback_content` | 卡片失败转 `msg_type=text` 时改发剥离版，不把加粗标记露给用户 |
| D-16 大盘一句接指数 | `engine/market.py` + `pipeline.py::market_line` | 指数日线 → M1 2×2 三档；失败/缓冲带无历史档如实写「数据缺失」，同日缓存保证同源 |
| D-11 配置与入口落地 | `service.py` + `config.py` + `main.py` | 只把总开关放进 `Config`，其余 `ASSISTANT_*` 由各自 owning 模块解析，避免二次解析漂移 |
| §5.13.4 输入白名单 | `llm.py::build_whitelist` | 只读快照字段 + counts；原始行情/新闻/历史文案/未来数据一律不进提示词 |
| §5.13.5 硬约束 1–4 | `llm.py::validate_text` | 数值/日期逐字核对、收益承诺词、新动作词、个股建议反向检查，任一不过即降级 |
| §5.13.5 硬约束 5/8 | `llm.py::LlmTextService` + `data/models.py::LlmTextCacheRow` | 超时/异常/校验失败用规则模板；`assistant_llm_text_cache` 留模型/渠道/用量/校验结果 |
| §5.13.6 成本与延迟 | `LlmTextSettings` + 缓存 | 默认关闭、超时 ≤120s、每日调用上限、同快照同位置只生成一次 |
| §8.1 只读 API | `api/v1/endpoints/assistant.py` | 8 个 GET 只读 L3；统一三元组，不一致 409「待同步」；鉴权走全局中间件 |
| §6.1 Web 独立页面 | `pages/assistant/{Index,Detail,Settings}.tsx` | 单条路由 + `StandaloneRouteBoundary`（与 `/login` 同构），不套 Shell、不改现有 11 条 |
| §6.2 卡片固定顺序 | `pages/assistant/components/blocks.tsx` | 操作意见→趋势→位置→关键价位→理由→风险→仓位→做 T 计划，顺序写死在组件里 |
| §6.2 列表与红涨绿跌 | `components/HoldingTable.tsx` + `format.ts` | 列按 §6.3；涨红跌绿、=0 灰；价格不复权 |
| §5.13.3 第 3 处 LLM | `llm.py::_fallback_market` + `pipeline.payload` | 大盘总结走同一套校验；`sum(by_advice) != total` 时不输出总结段 |
| §4.6 硬约束：单日不得判档 | `engine/sector.py::sector_strength_from_bars` | 三档只吃板块指数**多日序列**；当日涨跌幅只用来算 `rs_t`，两者不混用 |
| §4.6 硬约束：`rs_t` 不进评分 | 同上 + `_pick_sectors` | `rs_t` 与 `rs_label` 只落库展示，不回传评分/建议；板块不给换股建议（NG-7） |
| §5.10 降级口径 | `sector.py` 的 `degraded` / `as_of` | 序列不足、指数滞后、缓冲带无历史档 → 中性 + degraded + 注明截止日，档位不擅自改 |
| §3.5.2 涉及板块 | `data/sector_map.py` + `pipeline._focus_sectors` | 东财 F10 三级行业（去 `Ⅱ/Ⅲ` 后缀）匹配同花顺板块名，二级→一级→三级；未命中如实算未映射（SEC-3 口径），不按相似度硬凑 |
| §3.6.2 入站四能力 | `inbound.py` + `bot/commands/assistant.py` | 关注增删/笔记走写路径（白名单+确认+留痕），查询与复盘只读快照；群聊不回显持仓字段 |
| §3.6.3 二次确认与生效时点 | `inbound.py::confirm` / `_effective_date` | 确认态只存内存（重启即作废）；生效日＝当日无快照则当日，否则下一交易日 |
| §3.6.4 笔记 | `data/models.py::NoteRow` + `storage.save_note` | 原文照存、不做情绪打分；按日期/标的检索；不进评分/计划/回测 |
| §9.4.2 回合口径 | `backtest.py::simulate_rounds` | 一次回合＝触发成交→出场；未闭合**单列不计分母**；胜率只在已闭合回合 ≥30 时给 |
| §9.4.3 防前视与保守假设 | `backtest.py::_trigger_hit` / `_exit_on` | 只用触发日及之前数据；同日先止损；跳空按开盘价成交不追单；T+1 当日不可卖 |
| §9.4.2 分阶段/分标的 | `backtest.py::summarize_rounds` | 强制同时给 `by_stage` 与 `by_code`，单桶样本不足只报计数不报胜率 |
| §8.2 配置 API | `config/service.py` + 端点 | 切档/覆盖/回滚/资金写 L4，append-only 留痕，下一次快照生效 |
| §5.16.4 参数生效语义 | `config/overrides.py::resolve_effective_date` | 当日未生成快照则当日生效；已生成则顺延到下一交易日 |

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

### D-11 配置落地（**已收口：按 owning 模块单一解析**）

- 入口阶段把总开关 `ASSISTANT_ENABLED` 加进 `src/config.py`（含 `from_env` 与
  `.env.example`）；其余 `ASSISTANT_*` 仍由各自模块解析
  （`PipelineSettings.from_env` / `scheduling.config.parse_env_config`）。
- 这样做是不把同一个键解析两遍：若 `Config` 也存一份时的值，设置面板改了而
  owning 模块读的是另一份，就会出现「页面显示 20:30、实际 20:00 推」的漂移。
- `main.py` 新增 `--assistant` / `--assistant-once`，显式请求或被环境变量开启时
  才进入独立调度；与 `--serve/--webui` 互斥，Web 优先（有日志说明）。
- 待办：设置面板若要做助手配置页，直接读写这些 env 键，不要新增第二套字段。

### D-12 资金配置进 L4（**已收口**）

- 需求 §5.6：总资金是「用户填写的固定金额」，不给假默认值。新增
  `assistant_portfolio_setting`（append-only，取最新一行）保存
  `baseline_preset / total_capital / cash_available`，由
  `PUT /api/v1/assistant/params/funds` 与 `/params/preset` 写入。
- `ASSISTANT_*` 环境变量退为**初始值**：从未通过配置页保存时才生效；
  一旦 L4 有行即以 L4 为准。这样本地无配置也能起助手，页面保存后又不会两套值打架。
- 现金缺省仍按 `总资金 − 持仓市值` 推算（在组合视图层），表内留空即表示「自动」。

### D-13 `assistant_snapshot` 增加 `bar_date` 列（**本地开发库需重建**）

- 起因：真实数据跑出两个 bug——① 快照按 `GuardChainResult.data_date`（=最后一根 K 线）
  落库，收盘数据未出那天快照被记到前一交易日，同日第二次调用就新建一份，幂等失效；
  ② 投递台账按 `data_date` 分组，跨日会串号。
- 修法：`data_date` 明确为**快照日（请求日 T）**，新增 `bar_date` 记**数据截止日**；
  幂等键、台账、同源校验一律用 `data_date`，正文标注用 `bar_date`。
- 影响：`assistant_snapshot` 表结构变更。`create_all` 不会给已存在的表加列，
  **本地已有的 `data/stock_analysis.db` 或冒烟库需删表重建**（d 阶段无生产数据，可接受）。

### D-14 股票名称暂用代码兜底

- 取数层返回的行情不含名称列（新浪/东财日线都没有），`StockView.name` 现取
  `self.names.get(code, code)`，即默认显示代码。
- 待办：由关注清单/持仓录入时带上名称，或接一次名称接口后缓存；不影响任何计算。

### D-15 阶段「打法」「切换信号」文案为初值

- 需求 §3.5.3.1 ⑤ 要求展示「打法 / 允许 / 禁止 / 切换信号」，但**只给了字段名，没给原文**。
- 现取逐阶段一句话写在 `render/feishu.py::_STAGE_PLAYBOOK` / `_STAGE_SWITCH`（如 S1
  「回踩不破再买，突破前高追买需确认」、切换「跌破 MA20 且连续 2 日不收回 → 转 S3」）。
- 「允许 / 禁止」不是手写文案，而是**从 `STAGE_ALLOWED` 反推**——这样阶段门改了口径，
  展示自动跟着变，不会出现「文案说可以、引擎却拦下」的矛盾。
- 待你过目措辞；改文案不影响任何计算。

### D-16 ② 段「大盘一句」接指数（**已实现，范围限定**）

- `engine/market.py` 用指数日线实现 M1 的**指数可独立计算部分**：因子 A（价格相对 MA20，
  缓冲带 near 沿用最近一个非 near 交易日有效档）、因子 B（MA 斜率严格二分，走平归 not_up）
  → Q1/Q4 偏强/偏弱、Q2/Q3 中性；`pct_change` 取最后两根收盘。
- 不做的部分：市场宽度、风格、指数共振、量能确认标记——它们需要别的数据能力，缺失时
  **只减少展示项**，不编造档位；量能按需求「第 3 因子仅作确认」，本句不展示。
- 降级：序列不足、非有限值、near 且无历史档、指数取数失败 → `available=False`，
  渲染层如实写「大盘数据缺失（本次不判断环境档）」；指数当日未发布则在标签上标
  「截至 MM-DD」，不把旧值说成今天。
- 同日缓存：同一天只取一次指数，保证「同源多时点」与「二次渲染逐字相同」。
- 真实联网实测（新浪指数源 D1）：`sh000001` 136 根日线，末根 2026-11-24 收盘 3548.369，
  `pct_change=+0.27%`、`regime=偏弱`；请求 11-25 时取到当日新 bar 得 `-2.03%`，缓存二次调用逐字相同。
- 阈值（MA 窗口 / 斜率回看 / eps / theta）是初值，纸面回看确定后应进入 `calc_version`；
  目前未入版本，因为环境档只影响展示，未参与评分与守卫链。

### D-17 极端超长时的最后一级：限制分支条数

- 硬约束 7 只规定了「先删风险依据 → 再删阶段证据 → 再删不可设单分支」，但当标的数很多
  （如 8 只 × 4 分支）时，仅靠前三步仍会超过 1,100 字。
- 因此加最后一级：**只列前 6 条分支**，其余以「另有 N 条分支未展开（查看 WebUI）」一行
  交代，**整条略去、不截断半句**；已列出的分支仍保留触发条件/股数/金额。
- 判断依据：硬上限是硬约束，宁可少列分支也不发超长消息；且真正照抄场景在 WebUI 与
  完整版里都有全量分支。

### D-18 为「同源重复投递」与「纯文本降级」对共享通知链做了两处增量改动

- 两件事都发生在 `src/notification*` 内部、助手层拿不到钩子：① 多时点同源投递不能被
  `notification_noise` 的内容去重静默吞掉（方案 §2.3）；② 飞书卡片失败降级
  `msg_type=text` 时须先剥离 `**`（硬约束 5）。
- 处理方式：两处都是**默认关闭的可选参数**，既有三条路由不传即完全走原逻辑
  （`allow_repeat=False` / `fallback_content=None`）：
  1. `notification_noise.evaluate_notification_noise(..., allow_repeat=False)`：为真时跳过
     **去重与冷却**（两者都是「抑制重复」规则），静默时段与最低级别照旧——
     那是「什么时候发、多大声音」，不是「要不要重复」。`NotificationService` 的
     `evaluate_noise_control / send_with_results / send` 逐层透传。
  2. `send_with_results / send(..., fallback_content=None)` →
     `feishu_sender.send_to_feishu(..., fallback_content=...)`，仅作用于
     `msg_type=text` 降级分支；助手层传入 `strip_markdown(正文)`。
- 为什么不写一个平行发送器：方案 §1.2 明确「不新增平行发送器、不改既有渠道行为」。
  平行发送器意味着路由、降噪、能力画像、分片、关键词签名全都要重写一遍，以后改飞书
  发送逻辑必然分叉。
- 已核对影响面：`flake8` 在五个共享文件上的告警数与改动前**逐文件相同**
  （`config.py` 37 / `notification.py` 5 / `feishu_sender.py` 2，其余 0），未引入新问题；
  助手侧新增 15 项测试，既有通知测试断言未改。
- 未覆盖：飞书的**分片**（超长分批）路径不传 `fallback_content`——助手正文上限 1,800 字
  ≈5KB，远低于单条消息上限，实际进不到分片分支。

### D-19 LLM 文案指纹与决策 `calc_version` 分开

- 需求 §5.13.5 硬约束 8 写「temperature 等参数进 `calc_version`」。这里按语义拆成两个指纹：
  **决策 `calc_version` 不含 temperature**（它只改措辞，进了会让同一份结构化结论换快照 ID，
  与 B50 ③「当日快照只生成一次」冲突）；**文案指纹 `fingerprint` 含提示词版本 + 模型 +
  temperature**，随文案一起存进 `assistant_llm_text_cache`，同快照可复现同一段文案。
- `assistant_llm_text_cache` 增加 `fingerprint` / `usage_json` 两列（`create_all` 不改老表，
  本地开发库需重建——与 D-13 同一处理方式）。
- 三处已全部接上：飞书 ② 段摘要（`llm_summary` 可选参数，默认 None 时输出逐字不变）、
  个股一句话、大盘总结（`position="market"`，Web Tab A 展示）。
- **三处都在 `pipeline.payload` 一次写完**：详情页与 Web 只 GET 缓存，不在读接口里现调模型
  ——否则第一次打开页面与飞书推送会是两段不同的文案，违反「同快照只生成一次」。
  代价是 20:00 那次要串行调 `1 + 1 + N` 次；N 受 `ASSISTANT_LLM_MAX_CALLS_PER_DAY`（默认 20）
  约束，超出的标的直接落规则模板，不会把推送拖成不可控长尾。
- 后端复用 `src/llm/backend_factory`，主流程 Analyzer 的渠道 router 没有直接复用（它绑定
  Analyzer 实例），M14 实测完成前默认关闭；渠道不可用时一律规则模板降级，不阻塞推送。
- 超时用线程池 + `future.result(timeout=)` 硬截断，**不在 `__exit__` 里等后台线程**，否则
  卡死的渠道会拖住 20:00 推送；后端自身仍应设 timeout。
- 每日调用上限是**进程内软限制**（重启归零）；硬上限依赖「同快照同位置只生成一次」的
  缓存与每日快照数量。仓库目前没有按 $/token 的成本预算机制，本增量不假装做了金额封顶。

### D-20 大盘与板块快照均已落库（**已收口**）

- 大盘：`pipeline._persist_market` 在快照生成后写 `assistant_market_snapshot`
  （指数 / 涨跌 / 三档 / 两因子 / degraded）。数据不可用或写表异常时**不写行**，
  `/market` 继续返回 `available:false`，页面写「大盘数据缺失」，不编造档位。
- 板块：`pipeline._persist_sectors` 写 `assistant_sector_snapshot`（行业板块，按当日
  涨幅取最强 N + 最弱 N，N 默认 6/3）。数据源只有同花顺一家（东财 `push2` 系列被风控、
  雪球要 token），取不到即不写行，页面写「未接入」，**不拿空档凑数**。
- 板块表补 `as_of` / `degraded` 两列：同花顺**板块一览有当日涨跌幅，板块指数日线却晚
  一天发布**（2026-11-26 实测：一览“贵金属 +4.22%”，指数末根停在 11-25）。若用序列
  自算当日差，`rs_t` 会得到 −3.35（方向反了）。因此 `rs_t` **只认一览的当日涨跌幅**，
  三档只认序列，滞后就标 `as_of` + `degraded`，页面写「（截至 11-25）」。
- 另：`/params/funds` 是方案 §8.2 之外的补充端点——配置页必须能保存资金（D-12），
  否则「只推 env」无法做到「页面改参数」。

### D-21 结论表补 6 列（列表与卡片要的字段原先只在内存里）

- 需求 §6.3 列表要「最近交易日价格 + 涨跌幅」、§6.4 卡片要「关键价位」「仓位建议」，
  这些值引擎早就算出来了（`view.last_close` / `result.key_levels` / `position_advice`），
  但 `holding_conclusion` 没存，Web 端只能去读原始 K 线重算——那会绕过「单一真值」。
- 处理：结论表加 `last_close` / `pct_change` / `key_levels`(JSON) / `position_tier` /
  `position_pct` / `position_reasons`(JSON)，由 `write_conclusion` 一次写全，
  读回时组装成 `position` 子对象。
- 代价：`create_all` 不会给老库补列，**本地开发库需重建**（同 D-13 的处理方式）；
  生产尚无助手库，无迁移负担。
- 关键价位的来源说明（`source`）一并落库，页面不自己编「这是箱体上沿」。

### D-22 个股 ↔ 板块映射（**映射已收口，评分未接**）

- 需求 §3.5.2：Tab A 下板块区展示**涉及板块**（持仓/自选所属行业）的结构位置。
- 数据源绕了一圈才通：东财 `push2` 系列（`stock_individual_info_em`）被风控、雪球
  `stock_individual_basic_info_xq` 要 `xq_a_token`、遍历板块成分股要近百次请求；
  **可用的是 `datacenter-web` 的 F10 接口** `RPT_F10_CORETHEME_BOARDTYPE`（此前 M 系列
  实测里「datacenter-web 正常」这条结论在这里兑现了）。
- 名称对齐是这一步的真难点：东财给三级行业且带罗马后缀（`银行` / `银行Ⅱ` / `股份制银行Ⅲ`），
  同花顺板块名不带后缀且大致相当于二级（`银行` / `白酒` / `电池`）。处理：按
  「二级 → 一级 → 三级」顺序、剥掉 `Ⅱ/Ⅲ` 后缀后**精确**匹配；不中即未映射。
  2026-11-26 实测 12 只样本命中 11 只（`002594` 的「乘用车」不在同花顺 90 个板块里，
  如实算未映射，不按相似度硬凑）。
- 板块区默认 `ASSISTANT_SECTOR_MODE=focus` 只列涉及板块；一只都没映射上（或没注入
  解析器）时退回 `market`（全市场最强/最弱两端）并写日志，不静默变成空板块区。
- 映射结果按 TTL 12 小时落盘缓存（`data/cache/assistant_industry_map.json`），
  跨进程重启也只付一次 17 次请求。
- **仍未做**：`sector_strength` 接进评分层与风险 SEC-1。原因不是数据缺失，而是分值映射表与
  「连续 N 日偏弱」的阈值属纸面回看调参范畴（D-5/D-6 同一批），先接进去会把未定阈值
  固化成 `calc_version` 的一部分。详情页 5b 的 `#sector-{code}` 锚点同理，等 Web 详情页
  补板块区块时一起接。

### D-23 飞书入站：确认态在内存、自由问答仍走原 /ask

- 需求 §3.6.3 要求「二次确认 + 超时作废」。确认态放在**进程内存**（`AssistantInbound._pending`），
  没有落库。理由：待确认是**秒级交互**，重启后继续执行一条旧指令比让用户重发更危险；
  已生效的写入与审计都在库里，不受影响。若将来要支持多实例，再改成带 `owner + 过期时间`
  的表，语义不变。
- 需求 §3.6.2 第 4 项「盘后复盘问答」：本增量的 `/复盘` 给的是**确定性摘要**（阶段分布、
  条件计划条数、风险计数、当日笔记）+「仅回看不构成新建议」，自由问答仍走仓库原有的
  `/ask`。没有把 LLM 接进快照问答，是因为那需要一套独立的接地（grounding）白名单与
  数字校验，而现有 `llm.py` 的三个位置（summary/stock/market）语义都不匹配；宁可先给
  确定的摘要，也不让模型自由发挥当日结论。
- 无斜杠文本（「确认」「关注 601919」）走 `CommandDispatcher.register_text_matcher` 钩子，
  在自然语言路由**之前**尝试。钩子列表默认空，`ASSISTANT_ENABLED` 未开启时助手命令与钩子
  都不注册，现有机器人行为零变化（已回归 `tests/test_bot_*`）。
- 入站命令懒加载 SQLite（`_inbound()` 在 `execute()` 内构造），机器人启动不被助手存储拖慢。

### D-24 回测只做到「回合模拟层」，没有接历史计划回放（**已在增量 15 收口，见 D-27**）

- 需求 §9.4.4 点名的缺口是「现有引擎不模拟条件单成交回合」，本增量补的正是这段：
  `backtest.py` 输入「某日的条件计划分支 + 该标的日线」，输出回合与统计，纯函数、无网络。
- **没有**接「从历史快照里逐日取计划」的驱动。要做完整的 walk-forward，需要在 250 个交易日
  上逐日重跑守卫链（每天 × 每只标的），这是一条独立的批处理链路（含断点续跑、进度、
  与大参数网格的组合爆炸控制），塞进本轮会变成半成品。当前调用方可以喂
  **已导出的历史快照计划**，接口不变。
- 口径上刻意保守：涨跌幅类触发（`pct_lte/pct_gte`）**不自己折算价格**——需要前收盘，
  模块不猜，直接判不触发并留痕；卖出类分支不生成回合（它不是回合起点）；分红**不计入**
  收益（缺分红数据时少算，不虚构）。
- `not_modelled` 会随统计一起返回（做 T、分级止损多档、部分成交、停牌涨跌停、分红再投、
  组合仓位联动），避免读者把这些当成已覆盖。
- 需求 §9.4.5 的使用边界照旧：回测结果只作方案验证记录，**不进评分、不进卡片、不进推送**。

### D-25 战法启停清单：静态常量 + 进 `calc_version`，不做运行期开关

- 需求 §5.12.3 要求「逐战法最终启停清单在 c 阶段核对 `instructions` 后登记进
  `calc_version` 与附录 C」。落地为 `src/assistant/strategies.py` 的
  `STRATEGY_REGISTRY`：**15 条与 `strategies/*.yaml` 一一对应**，8 条启用、7 条不启用。
- 每条启用项必须写 `transposed_into`（落到哪个需求小节 / 哪个引擎模块），每条不启用项
  必须写 `note`（违反哪条 NG）。测试断言这两点，避免「说启用了但找不到落点」。
- **为什么是常量而不是配置项**：启停变化会改变阶段/计划/风险口径，属规则变更，
  按 §5.12.5 第 8 条要走变更记录（附录 C）并 bump `RULES_VERSION`；做成运行期开关会让
  同一天的两份快照可能按两套战法映射产出，回测不可比。
- `RULES_VERSION` 由 `rules-2.6.0` → `rules-2.6.1`：指纹里从「空清单」变成 8 条，
  摘要必然变化，不 bump 会让旧快照被 `find_snapshot` 当成同规则命中，等于用旧结论冒充新规则。
- 兼容性：`RuleFingerprint(strategy_ids=...)` 仍可显式传值（回测复现、单测用）；
  `build_calc_version(..., strategy_ids=())` 表示「显式不带战法口径」，与默认清单**不同**，
  这条差异也有测试钉住，避免以后误把「空」当默认。
- 代码侧没有改 `engine/` 任何判定：这轮只把「已经在用的规则」登记清楚，不引入新规则，
  所以纸面结论与增量 13 一致。

### D-26 股票名称走东财 F10 同一次请求（D-14 收口）

- 现状：`IndustryResolver` 为板块映射请求 `RPT_F10_CORETHEME_BOARDTYPE`，该响应每行都带
  `SECURITY_NAME_ABBR`（2026-11-26 实测 `600000.SH` → `浦发银行`），此前被丢弃，
  导致推送/页面只能显示代码。
- 改法：`_fetch_from_eastmoney` 顺手把简称写入内存与磁盘缓存（与行业行同一个
  `{ts, updated_at, rows, name}` 条目，写行业行时保留已有 `name`），新增 `name(code)`
  三级查找（内存 → 磁盘 → 取数）。**不新增请求**：先用板块映射再用名称时命中同一份缓存；
  反过来先用名称时也只发一次。
- 展示层 `pipeline._display_name` 三级兜底「注入的 `names` → 解析器 → 代码」，异常只退代码；
  名称**不进**评分、不进 `calc_version`、不进 LLM 白名单（避免模型拿名称编故事）。
- 实测：`name('600000')` 首次 0.32s，换实例走磁盘 0.003s；12 只样本行业映射仍是 11 命中，
  未映射的那只（`002594`）名称照常能拿到——名称与映射是两条独立字段。
- 仍缺：退市/更名后的简称以 F10 为准（TTL 12h），不做本地改名校验；北交所个别标的
  F10 无该字段时如实退回代码。
- 顺带修掉一处路径缺陷：`build_runtime(db_path=":memory:")` 时 `Path(":memory:").parent` 是 `.`，
  缓存会落在**当前工作目录**的 `cache/` 下（单测留垃圾文件、临时库互相污染）。现在内存库
  不落盘（`cache_path=None`），并新增 `industry_resolver` 注入点让单测完全不联网。

### D-27 历史计划回放驱动（D-24 收口）：逐日切片的五条硬约束

- 落点：`src/assistant/backtest_replay.py`（纯逻辑，不联网）+ `scripts/assistant_backtest_replay.py`
  （批处理入口）。**回合口径一个字没改**：成交价、跳空、T+1、同日先止损、未闭合不计分母、
  样本 ≥30 才给胜率，全部复用 `backtest.simulate_rounds` / `summarize_rounds`。
- **① 不碰未来数据**：每天把 K 线截到 T 再进守卫链；传给守卫链的交易日历也只给到 T
  （未来日历虽不含价格，也没必要提前泄露）。**T 日 K 线未发布就跳过该标的**
  （`skipped_stale_bars`），绝不拿 T-1 的结论冒充 T 日。
- **② 跨日状态必须累积**：阶段防抖（连续 N 日才切换）与「上一日趋势/建议」顺着日子传递，
  否则每天都是「第一天」，防抖规则形同虚设。检查点里连同 `stages` / `previous` 一起存，
  续跑后接着累积，不重置。
- **③ 出场价来自计划结构，不自己造阈值**：计划分支本身只带触发价（§5.11 的失效条件是文字），
  回放取「入场价下方最近支撑」作止损、「上方最近阻力」作止盈（§5.3 结构破坏口径）；
  取不到就只剩 `max_hold_days` 强平，并在 `exit_rules_missing` 里计数——**不加百分比止损**，
  那等于凭空造一条没经纸面回看的规则。
- **④ 组合层约束用保守近似**：同一标的已有未平回合时，后续触发不再建仓
  （`overlap_skipped`），避免同一段行情被重复计成多次暴露；资金曲线本身不建模，
  固定的 `PortfolioView` 意味着「资金耗尽后不再买」这类约束缺失，已写进 `not_modelled`。
- **⑤ 大盘缺失如实标记**：不注入指数序列时 `market=None`，M1 环境档与「大盘 ≤ 警示」
  不会触发，结果偏乐观；`counters.market_missing_days` 按「真正跑过守卫链的日子」计数，
  不把「没跑成的日子」也算进去。
- 规模与续跑：一次运行只允许一套参数（参数网格请多次调用），`max_days` 截断并标
  `truncated`；`checkpoint_every` 落盘、`last_day` 保证已回放过的交易日不重复跑。
- 统计口径补了三个字段：`triggered` / `untriggered` / `open_positions`。原先
  `summarize_rounds` 的 `open` 把「未触发」也算进去，会让人误以为有一堆持仓没平。
- 真实数据实测（主库 `stock_daily` 经 `--import-main-db` 复制进 L1，601919 + 601818，
  2025-10-08 ~ 2026-10-22，`max_days=200`）：200 个交易日、399 次守卫链、184 条计划、
  164 个回合（触发 5、未触发 159），闭合 5 < 30 故只报计数不报胜率；`market_missing_days=200`
  如实标注，`exit_rules_missing=92`（那些回踩买点下方没有支撑位，只剩 max_hold）。
- 仍未做：参数网格并行、组合层资金曲线、分红、做 T、分级止损多档、大盘序列注入
  （接口已预留 `market_by_day`，但没有可靠的离线指数源，不硬编）。

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

# 历史计划回放（只读 L1；--import-main-db 可把主库 stock_daily 复制进 L1）
python scripts/assistant_backtest_replay.py --codes 601919 601818 \
    --start 2025-10-08 --end 2026-10-22 --min-bars 60 --lookback 220 \
    --import-main-db data/stock_analysis.db --checkpoint data/cache/replay.json
```

当前结果：**529 passed**（助手 Python 测试）+ **6 passed**（`pages/assistant` 前端用例），`flake8` / `tsc` / `eslint` 均无告警（共享通知链的
历史告警数与改动前逐文件相同，见 D-18）。

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

**2026-11-24 装配链真实联网（akshare 1.19.5，两只标的 + 一只持仓）**：

| 项 | 结果 |
| --- | --- |
| 取数 | 东财 `push2his` `ProxyError` → 新浪 D2；`600000` 747 根、`000001` 747 根，`gap=0`，数据截止 **2026-11-24 收盘** |
| 快照 | `20261124-xxxx`，`bars_used=747`，`source=D2`（如实标降级，不再冒充 D1） |
| 幂等 | 同一天第二次 `ensure_snapshot` → `created=False`，`snapshot_id` 不变 |
| 同源 | 两次 `payload` 的 `same_source=True`，正文逐字相同 |
| 持仓区分 | `600000`（持 1000 股 @8.50）→ 建议「加仓」；`000001`（未持仓）→「等待买点」 |
| 防抖状态 | 两只均写入 `assistant_stage_state`，`as_of=2026-11-24` |
| 三日连续（11-20/23/24） | 快照日与请求日一致；11-23 因当日 K 线未出 `gap=1` 但阶段稳定 S1；11-24 拿到新数据后评分由 90 → 82，阶段仍 S1 |

**2026-11-24/25 真实渲染（同一库连续两日）**：

- 11-24：`数据截止 2026-11-24 收盘`、来源 D2、复盘行写「无可用 T-1 快照」、待办 `➕ 加仓 600000 7200 股 · 约 7.43 万`、其余 `⏳ 等待买点 000001`
- 11-25：`数据截止 2026-11-24 收盘`（当日 K 线未发布）、来源 D1、复盘行写「未触发 6 条（对照快照 20261124-54a2f982b8ab）」、风险段如实写「数据缺口 1 日」
- 两日均满足：七段齐全、计划数值行无修饰、`600000` 名称缺省时不再重复显示代码

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
- 装配：同日同参数复用快照、换参数生成新快照、增量取数不截断历史、降级来源如实标注、全标的不可用则抛错不产出、防抖状态跨进程可用
- 渲染：七段顺序固定、① 必带复盘子行、风险同股归并取最高、计划数值行无 emoji/加粗、两档触发价与股数逐字一致、精简版只留可设单分支、超长按序降级不超限、纯文本剥离 `**`
- 复盘：触发/未触发/跳空/无法判定四类判定、缺行情不猜、无 T-1 快照明确说明
- 大盘一句：四象限闭合、Q2/Q3 中性、缓冲带沿用历史档、无历史/序列不足/NaN 降级、取数失败降级、同日缓存、滞后指数标注日期
- 大盘快照落库：可用时写行（含两因子与 degraded）、不可用/空序列时不写、重复写跳过、写表异常不影响当日结论
- 板块：四象限三档与闭合、短序列不判档、NaN 降级、缓冲带沿用历史档、`rs_t` 只认当日涨跌幅（序列自算会反向）、指数滞后标 degraded+截止日、选板取强弱两端且跳过缺代码、同日缓存、开关关闭与取数失败都返回空集、落库幂等、读回按写入顺序（不按代码打散）
- 板块映射：`Ⅱ/Ⅲ/II/III` 后缀剥离、`SECUCODE` 推断（沪深北）、二级优先/一级后备/未命中不猜、内存与磁盘缓存与 TTL 过期、取数失败返回空；focus 模式只列涉及板块且按涨跌排序、无解析器或全部未映射时退回 market、market 模式不查映射、解析异常只降级
- 飞书入站：白名单拦住写指令且不落库、关注/移除非确认不写、生效日（当日有快照→下一交易日）、确认超时作废、重复添加与移除不存在的提示、取消也留痕、笔记原文与检索、当日结论只读且群聊隐藏持仓字段、复盘含笔记且标注不构成新建议；命令层确认/取消/「关注 601919」无斜杠可命中、钩子异常不影响分发、既有 bot 命令未受影响
- 条件计划回测：`price_lte`/`price_gte` 触发与成交价、跳空按开盘价、同日先止损、T+1 当日不可卖、到期强平、双边成本、卖出分支不建回合、股数为 0 只记触发、涨跌幅触发不猜；统计侧样本不足不报胜率、分阶段单桶不足只报计数、未闭合不计分母、`not_modelled` 如实列出
- 历史计划回放：T 日无 K 线即跳过不猜、根数不足不判、守卫链日历只到当天、计划次日才可触发、止损取最近支撑/止盈取最近阻力、动作只认买入类、已持仓不重复建仓、未触发不被去重吃掉、跨日 stage 累积、max_days 截断、检查点往返与续跑不重复跑、坏检查点按从头开始、统计把未触发与未平仓拆开
- 战法启停清单：清单与 `strategies/*.yaml` 一一对应且无重复、启用项必须有转写落点、不启用项必须写明 NG 原因、启用集合与 §5.12.3 逐条对齐、指纹默认带启用集合、启停变化改摘要、不启用项绝不进指纹、显式空清单与默认可区分、`RULES_VERSION` 已 bump
- 股票名称：名称与行业共用一次 F10 请求、行业请求先到也能捕获名称、注入取数器只取一次、失败返回空串不猜、磁盘缓存往返、写行业行不抹掉已缓存名称；装配层「注入名优先 / 解析器名次之 / 异常退代码」
- Web 页面：大盘页环境档与总结段、列表红涨绿跌与点行进入详情、空快照给人话提示、详情五区块与价位来源、配置页按注册表渲染并提示生效时点
- 大盘文案：关闭时走规则模板、不可用时不编档位、动作词被拒、提示词含 market_snapshot、计数不一致时不输出总结段
- 服务装配：总开关与参数解析、gate/dispatcher/pipeline 接线、20:00 投一次、同时点二次判重复、取数前时点不投、`run_loop` 中断退出
- main 接线：从 `main.py` 直接抽取 `parse_arguments` 执行，锁定新 flag 可解析且默认不启用助手
- 配置服务：env 初始 → L4 覆盖、切档/覆盖/回滚 append-only、越界拒绝、下一次快照生效（当日已生成则顺延）
- API：8 个只读端点的三元组 404/409、LLM 缓存命中与未命中、资金 L4 覆盖 env、切档/覆盖/回滚、越界键 400
- LLM 文案：白名单字段不越界、数值/承诺/新动作/反向建议四类校验、超时与异常降级、同快照复用缓存、每日预算、② 段接线与默认关闭时输出不变
- 发送接线：参数翻译（路由/去重键＝`snapshot_id`/`allow_repeat`）、正文为空不发、服务失败如实返回失败、
  两个时点各投一次、同时点二次判 `duplicate`、非交易日提醒按日去重、`allow_repeat` 仍受静默时段与最低级别约束

> 未跑：仓库全量测试需要 `fastapi/anyio/httpx` 等完整依赖，本次环境未安装；改动未触及这些模块。

---

## 6. 下一步（建议顺序）

1. **发送接线**：把七段文本接到现有 `feishu_sender`（`interactive` 卡片 + `msg_type=text`
   降级剥离 `**`）、`notification_routing` 新增 `route_type`、`notification_noise` 用
   `dedupe_key=snapshot_id` + `allow_repeat=True` 防止同源重投被吞
2. **大盘一句话**（D-16）：接入指数日线填 `MarketLine`，改动只在装配层
3. **LLM 文案层**（§5.13）：只改措辞、不改数值；失败降级模板
4. **配置落地**：五个 `ASSISTANT_*` + 资金键进 `src/config.py` / `config_registry` / 设置面板（D-11、D-12）
5. **API + Web**（`api/v1/endpoints/assistant.py` + `pages/assistant/`）
6. 纸面回看（D-5/D-6 阈值定稿）→ 再接 `sector_strength` 进评分与 SEC-1；回放已可用于出样本
7. `adj_factor` 正式来源（M15）：现已有「同源 qfq/raw 比值推算」函数但默认不启用，待定是否作为长期路径
