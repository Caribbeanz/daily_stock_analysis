# 股票分析助手 — 技术实现方案（c 阶段）

| 字段 | 内容 |
| --- | --- |
| 版本 | v0.1（初稿，待用户过目；批准后才正式排期编码） |
| 创建日期 | 2026-11-19 |
| 状态 | **草案 / 技术方案初稿**。本方案在需求 v2.4 **未获用户批准** 前产出，仅供过目；不构成实施授权，不锁定最终实现，不开始编码 |
| 上游依赖 | `docs/requirements/stock-analysis-assistant-requirements-v2.4.md`（需求 v2.4，状态：草案 / 待批准） |
| 适用范围 | 把需求 v2.4 中所有「留 c 阶段 / 留技术方案阶段」的待补项落到具体模块、表结构、接口契约、参数初值与实测前置；**不重复需求口径，只做实现落地** |
| 阅读约定 | 本方案与需求 v2.4 口径必须一致；冲突时以需求 v2.4 为准，本方案按附录 C 变更规则修订。所有「留 d 阶段」的编码细节不在本方案范围 |

> **关键提示**：本方案列出的大量「可复用」文件路径与接口签名，来源于 2026-11-10 需求核查与 2026-11-19 代码库实读。**编码启动前须逐条复核**（库可能已演进）；复核结果走附录 C 变更规则，不静默改写。

---

## §0 文档导航

| 章 | 标题 | 对应需求 | 对应 c 模块 |
| --- | --- | --- | --- |
| §1 | 总则：设计原则、复用优先级、改动护栏 | §10.3 后继步骤、D1、NG-9/10 | — |
| §2 | 系统架构与载体 | §3.5.2、D1 | **C1** |
| §3 | 数据获取与存储 | §4.6、§7.3、§7.4 | **C2** |
| §4 | 决策引擎实现 | §5.1–§5.12、§5.14 | **C3** |
| §5 | 参数框架 | §5.16、§6.8 | **C4** |
| §6 | 输出层 | §3.5、§5.13、§5.15、§6 | **C5** |
| §7 | 验证与回测 | §9.2、§9.4 | **C6** |
| §8 | 接口契约汇总 | §3.5.2、§3.6、§4.1 | — |
| §9 | 待补项与实测前置总表 | §10.2.2、§7.4 | — |
| §10 | 与需求文档的映射矩阵 | 全文 | — |
| 附录 A | 可复用清单总表（代码库实读） | §3.5、§5.12、§5.13 | — |
| 附录 B | 新增文件落点建议 | — | — |
| 附录 C | 降级矩阵与实测不可得时的退路 | §4.6.4、§7.4、§8.3 | — |

---

## §1 总则

### 1.1 本方案要解决什么

需求 v2.4 把所有「怎么算、怎么存、怎么跑、怎么验」的实现细节都显式推迟到 c 阶段。本方案一次性把这些待补项落到：

1. **模块边界**：新增独立模块放在哪、与现有工程怎么隔离（§2）
2. **表结构与存储**：L1–L4 四层存储的具体表、字段、索引、写入时机（§3）
3. **决策引擎实现路径**：守卫链、评分、阶段判定、阶段门、条件计划、风险信号的代码组织（§4）
4. **参数初值**：三档预设的完整数值表与覆盖/变更/留痕机制（§5）
5. **输出层落地**：Web 页面路由、飞书推送渲染、LLM 文案层渠道、飞书入站 route_type（§6）
6. **验证手段**：纸面回看与回测引擎扩展的设计（§7）

### 1.2 设计原则（不可违反）

| # | 原则 | 出处 |
| --- | --- | --- |
| DP-1 | **复用优先**：能用现有能力的，不另起一套；新增 ≠ 修改现有 | D1、NG-9 |
| DP-2 | **口径隔离**：本助手模块与主流程口径隔离，主流程不感知本助手存在 | D1 |
| DP-3 | **单一真值**：盘前引擎只跑一次，WebUI / 通知 / 卡片 / 导出四处读同一份快照，禁止重算 | §4.1 |
| DP-4 | **决策归代码，措辞归 LLM**：LLM 只进文案层，不进评分/advice/风险/回测 | §5.12.2、§5.13 |
| DP-5 | **可复现**：同 `calc_version` + 同快照 → 同结论；参数变更不追溯历史（L3 append-only） | §4.6.2、§5.16 |
| DP-6 | **只收紧不放宽**：阶段门、风险信号都是事后收紧层，不改写评分逻辑 | §5.14.5、R18-Q1 |
| DP-7 | **降级优于编造**：数据不足时标注降级，不推测、不编造、不阻塞报告 | §8.3、NG |
| DP-8 | **硬护栏不可越**：现有 11 条路由、页面组件、Shell、菜单、后端静态服务、主流程 Agent/Skill 不得修改 | NG-9、§5.12.2 |

### 1.3 改动护栏总表（编码时的红线）

| 护栏 | 禁止做的事 | 允许的做法 |
| --- | --- | --- |
| **前端路由** | 修改现有 11 条路由、任何现有页面组件、`<Shell>`、菜单 | `App.tsx` 新增 1 条路由注册 + 新增独立页面组件文件 |
| **后端静态服务** | 改动 `api/app.py` 的 SPA 路由回退与静态服务逻辑 | 新增独立只读 API 路由（不动现有 API） |
| **主流程 Agent/Skill** | 改动 `src/agent/`、`src/analyzer.py` 的现有决策与观点生成 | 本助手以独立模块消费确定化规则；主流程原样保留 |
| **通知发送器** | 新增平行发送器、改既有渠道行为 | 新增 1 个 `route_type`，复用 routing/noise/capabilities |
| **现有数据表** | 用派生结果覆盖 L1 原始行情；修改 `stock_daily` 的 `adjust="qfq"` 落库逻辑（须改为不复权） | 扩展 `stock_daily` 加 `adj_factor` 列或新建因子表；新建 L3 快照表 |
| **配置读取链** | 破坏 `STOCK_LIST` 现有读取链（`src/config.py:refresh_stock_list`） | 新增运行时持久化写入层，读取时合并配置 + 运行时 |
| **战法文件** | 删除 `strategies/*.yaml`、改其 `instructions` | 本助手不加载不启用战法的 LLM 输出；只转写其判定条件为确定性规则 |

### 1.4 与需求 v2.4 的关系

- 本方案**不新增、不修改**任何需求口径；所有口径以需求 v2.4 §1–§10 + 附录为准。
- 本方案只把需求中「留 c 阶段」的待补项**具体化**为可编码的设计。
- 若本方案在细化过程中发现需求口径有缺口或矛盾，**不自行裁定**，登记到 §9 待补表并提请用户决策，走需求文档的变更规则（附录 C）。
- 本方案批准后，需求 v2.4 的「草案 / 待批准」状态方可转为「已批准」，解锁 d 阶段编码。

---

## §2 系统架构与载体（C1）

### 2.1 整体架构

本助手作为**现有仓库的独立扩展模块**落地（D1），不另起工程、不改主流程。整体分为五层：

```
┌─────────────────────────────────────────────────────────────┐
│  展示层（§6）                                                │
│  ├─ Web 独立页面（新路由 + StandaloneRouteBoundary）          │
│  ├─ 飞书推送（复用 notification_* + feishu_sender）           │
│  └─ 飞书入站（复用 feishu_stream + dispatcher）               │
├─────────────────────────────────────────────────────────────┤
│  输出装配层                                                   │
│  ├─ 卡片/列表/详情渲染（读 L3 快照）                          │
│  ├─ 飞书七段图标化渲染（feishuText renderer）                 │
│  └─ LLM 文案层（复用 src/llm/，三处用法）                     │
├─────────────────────────────────────────────────────────────┤
│  决策引擎层（§4）                                             │
│  守卫链 S0→S1→S2→S3→S4→S5→S5.5→S6                            │
│  ├─ S0 数据校验 → S1 基本面 → S2 技术面 → S3 估值             │
│  ├─ S4 风险 → S5 决策表（产出候选 advice）                    │
│  ├─ S5.5 阶段门（方案 A：校验改写 + 留痕）                    │
│  └─ S6 仓位约束 + 条件计划生成                                │
├─────────────────────────────────────────────────────────────┤
│  数据与存储层（§3）                                           │
│  ├─ 取数链路（akshare → 公开接口 → 本地缓存）                 │
│  ├─ L1 原始不复权价 + adj_factor + 周/月线 + 交易日历         │
│  ├─ L2 派生（内存计算，不落库）                               │
│  ├─ L3 快照 append-only                                      │
│  └─ L4 用户输入（持仓/笔记/关注清单/参数覆盖）                │
├─────────────────────────────────────────────────────────────┤
│  调度与配置层                                                 │
│  ├─ 盘前调度（17:30 取数 → 18:00 生成 → 次日 7:30 可查）      │
│  ├─ 参数框架（三档 + 单项覆盖 + param_hash）                  │
│  └─ 三元组一致性 [data_date, snapshot_id, calc_version]       │
└─────────────────────────────────────────────────────────────┘
        ↑ 复用现有工程能力（不改主流程）
        ├─ src/llm/（文案层渠道 fallback）
        ├─ src/notification_*（推送路由/降噪/能力）
        ├─ src/core/trading_calendar.py（交易日历）
        ├─ src/core/backtest_engine.py（回测底座）
        └─ apps/dsa-web（React Router + AuthProvider）
```

### 2.2 模块边界与落点

新增模块统一放在 `src/assistant/`（独立包），与 `src/agent/`（主流程 Agent/Skill）平级、互不依赖。落点建议：

| 子模块 | 路径建议 | 职责 | 复用的现有能力 |
| --- | --- | --- | --- |
| 取数与存储 | `src/assistant/data/` | L1 取数/回补/补洞、L3 快照写入、L4 用户输入读写 | `akshare_fetcher`（改为不复权）、`trading_calendar` |
| 决策引擎 | `src/assistant/engine/` | 守卫链、评分、阶段判定、阶段门、风险信号、条件计划 | `analysis_tools`（取数底座，只读） |
| 参数框架 | `src/assistant/config/` | 三档预设、单项覆盖、变更记录、param_hash | `src/config.py`（只加键，不改读取链） |
| 输出装配 | `src/assistant/render/` | 卡片/列表/详情/飞书七段渲染、LLM 文案调用 | `src/llm/`（文案层）、`SUMMARY_SYSTEM_PROMPT` 范式 |
| Web API | `api/v1/endpoints/assistant.py`（新增） | 只读快照 API（holding_conclusion/plan/risk/snapshot） | `api/app.py`（只加路由，不改 SPA 回退） |
| Web 前端 | `apps/dsa-web/src/pages/assistant/`（新增） | 独立页面组件，不套 Shell | `App.tsx`（+1 路由）、`AuthProvider` |
| 飞书入站 | 复用 `bot/platforms/feishu_stream.py` + 新增 `src/assistant/inbound/` | 四项命令的 dispatcher handler | `feishu_stream`、`dispatcher` |
| 回测扩展 | `src/assistant/backtest/` | 条件计划回测模式 | `backtest_engine`（扩展，不改原引擎） |

> **隔离原则**：`src/assistant/` 不 import `src/agent/` 的决策/观点生成；`src/agent/` 不 import `src/assistant/`。唯一例外是飞书入站自由问答可复用 `src/agent/` 的对话能力（§6.5），但其回答**不写回** advice 或任何结构化字段。

### 2.3 调度与运行时序

```
T-1 日 17:30  取数（L1 增量：日线 + adj_factor + 板块/指数 + 公告 event_feed）
             ├─ 缺口检测：≤3 日自动补；>3 日标 stale + 阶段降 S0
             └─ 失败保留旧数据，全链路标 stale
T-1 日 18:00  决策引擎只跑一次
             ├─ L2 派生（内存）：MA/斜率/箱体/分位/阶段/评分/风险
             ├─ 守卫链 S0→S6 产出 advice + 条件计划 + 风险信号
             ├─ 阶段门 S5.5 校验改写 + 留痕 stage_gate_hit
             ├─ L3 快照写入（append-only，带三元组 + param_hash）
             └─ LLM 文案层生成三处文案（超时降级规则模板）
T 日 00:00    参数变更生效（写操作次日盘前生效）
T 日 07:30    WebUI 可查 / 飞书推送发出（走 notification_noise 静默时段）
T 日 08:00    必须可查看（FR-9.2）
非交易日      整链路跳过，不发提示（§8.2）
```

**时序硬约束**：
- 取数（17:30）→ 生成（18:00）→ 推送（07:30）之间有约 13 小时缓冲，LLM 超时上限建议 ≤ 120s/标的，13 只 ≤ 26 分钟，远在窗口内。
- M17 实测若 3 年首次回补超 8:00 窗口 → 改「分批回补 + 回补期阶段记 S0」（§4.6.3）。
- 非交易日 `trading_calendar` 判定后整链路跳过，不进入取数/生成/推送任何环节。

### 2.4 三元组一致性机制

所有共享对象（`holding_conclusion` / `conditional_plan[]` / `risk_signals[]` / `market_snapshot` / `sector_snapshot`）均携带：

```
[data_date, snapshot_id, calc_version]
```

- `data_date`：T-1 收盘日期（数据截止日）
- `snapshot_id`：L3 快照唯一键（同日重跑生成新 id，不覆盖）
- `calc_version`：规则版本指纹（含战法启停清单、参数 param_hash、维度权重、temperature 等）

**渲染前硬校验**（§4.1）：任何位置渲染 advice/涨跌幅/大盘/板块字段前，校验三元组一致；不一致 → 显示「待同步」且不显旧值。这一校验放在输出装配层的统一入口，Web/通知/卡片/导出四处共用。

---

## §3 数据获取与存储（C2）

### 3.1 数据源与取数链路

**现状基线（需求 §4.6.1 实读）**：主源 akshare，经 `akshare_fetcher` 封装；现状取 `adjust="qfq"`（前复权），与本需求「不复权落库」冲突，须修正。

**取数链路设计**：

```
src/assistant/data/fetcher.py（新增，包装现有 akshare 调用）
  ├─ 主源：akshare（锁定版本，免费无 token）
  │   ├─ 日线：stock_zh_a_hist(code, period="daily", adjust="") ← 不复权
  │   ├─ 周线：由本地日线聚合（§3.3），period="weekly" 仅兜底
  │   ├─ 月线：由本地日线聚合，period="monthly" 仅兜底
  │   ├─ adj_factor：优先免费源直取；不可得则除权事件表推算（M15 实测决定）
  │   ├─ 指数：index_zh_a_hist / stock_zh_index_daily
  │   ├─ 板块：stock_board_industry_name_em / stock_board_industry_cons_em
  │   ├─ 分红/估值：stock_a_alert_down / stock_individual_info_em 等（M9 实测）
  │   └─ 公告：notice_* 接口（M16 实测决定字段完整性）
  ├─ 备用：公开接口（同源不同函数或公开 HTTP）
  └─ 兜底：本地缓存（L1 旧数据，标 stale + 数据日期）
```

**source_tag 标记（§4.3）**：每次取数记 `source_tag ∈ {D1 主源, D2 备用, D3 缓存}`，随快照留痕；使用缓存时卡片明示「数据非最新」。

**失败处理（§7.2 / §4.6.3）**：
- 连续 3 个交易日取数失败 → 降级或排除该源
- 关键字段缺失率 > 5% → 降级或排除
- 基本面取数失败 → 不阻塞报告，标注缺失、降置信度

### 3.2 L1–L4 四层存储表结构

**载体**：沿用现有 SQLite `data/stock_analysis.db`。**实存 28 张表**（2026-11-19 实读，不含 schema_migrations；需求 §4.6.1 记 38 张，差异可能因统计口径或库演进，**以实读为准**）。本助手新增/扩展以下表，L1 与 L3 同库不同表，都能按 `code + trade_date` 检索（§4.6.2 硬约束 3）。**实读确认：`holding_conclusion`/`conditional_plan`/`risk_signals`/`market_snapshot`/`sector_snapshot`/`market_regime`/`rs_t`/`sector_strength` 8 张规划表全部不存在，均需新建**（附录 A.0 #1）。

#### L1 原始行情层

| 表 | 用途 | 关键字段 | 索引/约束 | 写入时机 |
| --- | --- | --- | --- | --- |
| `stock_daily`（扩展） | 日线 OHLCV，**不复权** | `code, trade_date, open, high, low, close, volume, amount, adj_factor, source_tag` | `UNIQUE(code, trade_date)` 幂等重写；加 `idx(code, trade_date)` | 每交易日 17:30 增量；首次回补 ≥3 年 |
| `stock_weekly`（新增） | 周线，本地日线聚合 | `code, iso_week, week_end_date, open, high, low, close, volume` | `UNIQUE(code, iso_week)` | 日线写入后聚合 |
| `stock_monthly`（新增） | 月线，本地日线聚合 | `code, year_month, month_end_date, open, high, low, close, volume` | `UNIQUE(code, year_month)` | 日线写入后聚合 |
| `index_daily`（新增/扩展） | 指数行情（上证 sh000001 等） | `code, trade_date, open, high, low, close, volume` | `UNIQUE(code, trade_date)` | 每交易日增量 |
| `sector_daily`（新增） | 板块行情 | `sector_code, sector_name, trade_date, pct_change, up_count, down_count, lead_stock` | `UNIQUE(sector_code, trade_date)` | 每交易日增量 |
| `trading_calendar`（新增/扩展） | 交易日历 | `trade_date, is_open` | `PK(trade_date)` | 定期同步 |
| `dividend_event`（新增） | 分红记录 | `code, ex_date, record_date, payout_date, per_share, source_tag` | `UNIQUE(code, ex_date)` | 季度更新 |
| `valuation_hist`（新增） | PE/PB 历史分位 | `code, trade_date, pe, pb, pe_pctile, pb_pctile, source_tag` | `UNIQUE(code, trade_date)` | 季度更新 + 日频刷新 |
| `event_feed`（新增） | 重大消息（§5.15） | `event_id, code, title, pub_time, source, url, category, source_tag` | `UNIQUE(event_id)` append-only | 每交易日增量 |

> **复权口径修正（§4.6.4）**：`stock_daily` 的 `adjust` 参数从 `"qfq"` 改为 `""`（不复权）；新增 `adj_factor` 列。指标计算与回测用「原始价 × 累积因子」的后复权派生序列（L2 内存计算，不落库）。展示用不复权。**禁止退回前复权落库**。

#### L2 派生层（不落库）

MA / 斜率 / 箱体 / 分位 / 阶段 / 评分 / 风险信号 → 生成时内存计算，结果随 L3 快照留存（§4.6.2）。**L2 不得回写 L1**（硬约束 1）。

#### L3 决策快照层（append-only）

| 表 | 用途 | 关键字段 | 约束 |
| --- | --- | --- | --- |
| `snapshot`（新增） | 盘前快照主表 | `snapshot_id, data_date, calc_version, param_hash, generated_at, bars_used, gap_days, source_tag` | `PK(snapshot_id)` append-only，**不可重算覆盖** |
| `holding_conclusion`（新增） | 个股结论 | `snapshot_id, code, name, type, advice, advice_display, trend, position_tag, est_level, div_yield, stage, stage_confidence, stage_gate_hit` | `FK(snapshot_id)` |
| `conditional_plan`（新增） | 条件计划分支 | `snapshot_id, code, branch_id, scenario, trigger_type, trigger_price, action, shares, est_amount, pct_of_total, valid_days, can_set_order, notes` | `FK(snapshot_id)` |
| `risk_signals`（新增） | 风险信号 | `snapshot_id, code, category, level, human_text, trigger_basis, affects_advice` | `FK(snapshot_id)` |
| `market_snapshot`（新增） | 大盘环境 | `snapshot_id, index_code, index_name, pct_change, market_regime, regime_cells, width` | `FK(snapshot_id)` |
| `sector_snapshot`（新增） | 板块快照 | `snapshot_id, sector_code, sector_name, pct_change, sector_strength, rs_label, rs_t, up_count, down_count, lead_stock` | `FK(snapshot_id)` |
| `llm_text_cache`（新增） | LLM 文案留痕 | `snapshot_id, position(个股/摘要/大盘), text, model, channel, generated_at, consistency_check` | `FK(snapshot_id)`，同快照同位置只生成一次 |

- **保留 ≥ 250 个交易日**（复盘与回测归因）
- 同日重跑生成新 `snapshot_id`，不覆盖历史行（硬约束 2）
- 单条可导出 JSON 供复盘

#### L4 用户输入层

| 表 | 用途 | 关键字段 | 写入规则 |
| --- | --- | --- | --- |
| `user_holding`（新增） | 持仓与成本 | `code, shares, cost_basis, buy_date, recorded_at` | 用户手工录入；次日生效 |
| `user_note`（新增） | 操作笔记 | `note_id, trade_date, code, content, created_at` | 飞书入站写入；不进评分/回测 |
| `watchlist_runtime`（新增） | 关注清单运行时写入 | `code, action(add/remove), op_time, effective_date, user_id, audit_id` | 飞书二次确认后写入；次日盘前生效；可撤销 |
| `param_override`（新增） | 参数单项覆盖 | `param_key, param_value, baseline_preset, op_time, effective_date, user_id, audit_id` | append-only 变更记录；可回滚（回滚也记一条） |

> **关注清单读取链（§3.6.3 护栏）**：读取时 = `STOCK_LIST` 配置 ∪ `watchlist_runtime` 生效项；**不破坏 `src/config.py:refresh_stock_list` 现有读取链**，在其之上合并运行时写入。

### 3.3 周线/月线聚合与复权派生

**周线聚合（§4.6.3）**：ISO 周，周收盘 = 该周最后交易日收盘，OHLC 取周内聚合。**不额外依赖接口**；与接口兜底值不一致时以日线聚合为准并记日志。

**后复权派生（L2 内存）**：`post_adj_close[t] = raw_close[t] × cum_adj_factor[t]`，用于指标计算与回测，避免除权跳空被误判破位。**不落库**。

**缺口检测（§4.6.3）**：每次生成前比对 `trading_calendar` vs 本地 `stock_daily.trade_date`：
- 单标的缺口 ≤ 3 日 → 自动补
- > 3 日 → 标 `stale`，阶段降 S0，不生成条件计划

### 3.4 实测前置 M15–M17 登记与降级

| 实测项 | 决定什么 | 不可得时的降级（契约，不得推断） |
| --- | --- | --- |
| **M15** 周线/月线 + `adj_factor` 免费源 | §3.3 复权方案是否成立 | 周/月线由日线聚合（已设计）；`adj_factor` 退化为「除权事件表本地推算」，**不得退回前复权落库** |
| **M16** 公告接口字段完整性 + 单日返回量 | §5.15 消息推送能否上线 | 降级为「只显示标题 + 来源，无链接」 |
| **M17** 3 年首次回补耗时 + 限频 | §8.2 的 7:30 产出是否来得及 | 改「分批回补 + 回补期阶段记 S0」 |

> 实测在编码启动前优先完成（§7.4）；结果登记进本表与附录 C，不可得时按上表降级。

### 3.5 风险判定历史序列存储（§7.3）

风险信号 §5.10.2 的「连续 N 日 / 连续 N 期」判定需要历史值。**存储方案**：在 L3 快照表中按 `code + trade_date` 检索历史 `market_regime` / `rs_t` / `sector_strength` / 估值分位 / 破位状态。具体：

- `market_regime` / `sector_strength`：从 `market_snapshot` / `sector_snapshot` 历史行聚合
- 估值分位：从 `valuation_hist` 历史行
- 破位状态：从 `holding_conclusion.trend` 历史行
- 财报连续期：从 `dividend_event` / `valuation_hist` 的 `report_period` 聚合

**缺失处理**：历史不足时该信号记「观察级 · 数据不足」，**不得用当日值冒充连续状态**（§7.3）。M11/M12 实测前置未完成前，相关信号只能标注「数据不足」。

### 3.6 关注清单与笔记存储（§3.6.3 / §3.6.4）

- **笔记** `user_note`：按 `trade_date` / `code` 存储与检索；不进评分、不进回测；不复用 `src/agent/memory.py`（那是分析记忆与置信度校准，用途不同，§3.6.4）
- **关注清单运行时写入** `watchlist_runtime`：append-only、留痕、可撤销、次日盘前生效；与 `STOCK_LIST` 配置合并读取，不破坏现有读取链

---

## §4 决策引擎实现（C3）

### 4.1 守卫链 S0→S6 实现路径

决策引擎按固定顺序的守卫链运行，每一步产出可审计的中间结果。实现入口 `src/assistant/engine/guard_chain.py`：

```
S0 数据校验
  ├─ 三元组完整性、缺口检测（§3.3）、bars_used / gap_days 记录
  ├─ 缺口 >3 日或历史不足 → 阶段记 S0，不生成条件计划，仍出卡片（标「数据不足」）
  └─ 产出：data_quality_flag, bars_used, gap_days
S1 基本面（低权重，§5.1 R1）
  ├─ 财报渐进恶化判定（营收/利润/ROE/现金流/审计意见，M12 实测前置）
  ├─ 分红三件套（股息率/连续性/除权日程，§5.8 R2）
  └─ 产出：fundamental_facts（事实性输入，有权重、可触发风险信号，不做情绪打分）
S2 技术面（主导，§5.2）
  ├─ 趋势三档（偏强/中性/偏弱）：MA20 + 斜率 k=5 + θ=0.003 + ε=0.005
  ├─ 位置四类（价格分位/前高前低回撤/均线偏离/箱体位置），布林带辅助
  ├─ 关键价位三级（支撑1-2/压力1-2/趋势失效位，§5.3）
  └─ 产出：trend, position_tag, key_levels[], tech_score
S3 估值（§5.8 R2）
  ├─ PE/PB 历史分位（与技术面价格位置分离）
  ├─ 估值位置 ≠ 价格位置，分别存储与展示
  └─ 产出：valuation_pctile, valuation_tag
S4 风险（§5.10 R6/R7）
  ├─ 7 类风险信号全覆盖（见 §4.5）
  ├─ 三级联动（观察/警示/严重）
  ├─ 大盘风险＝全局风险系数，最高只到警示级（R5）
  └─ 产出：risk_signals[], risk_level, global_risk_factor
S5 决策表（§5.4）
  ├─ 评分权重表（技术面显著高于其他维度之和，R1）→ 综合分
  ├─ 决策表映射：综合分 + 持仓状态 → 候选 advice（加仓/持有/减仓/清仓/等待买点/观望/等待卖点）
  ├─ advice_margin 缓冲（档位切换分数差未超过则维持原档）
  └─ 产出：candidate_advice, candidate_score
S5.5 阶段门（方案 A，§5.14.5 / §10.4）← 见 §4.4
  ├─ 校验候选 advice 是否落在当前阶段允许集合
  ├─ 冲突 → 改写 + 留痕 stage_gate_hit
  └─ 产出：final_advice, stage_gate_hit
S6 仓位约束 + 条件计划（§5.6 / §5.11）
  ├─ 硬上限校验（单只20%/总仓80%/现金底线20%/T仓10%）
  ├─ 动态基础建议 + 轻/中/重档位
  ├─ 条件计划生成（价格/涨跌幅触发，多分支，§4.6）
  └─ 产出：position_advice, conditional_plan[]
```

**实现要点**：
- 每步产出写入 L3 快照对应表，带 `snapshot_id`，可逐字段回溯（§6.6 八字段技术解释）
- S0–S4 是事实采集与风险判定，不产出 advice；S5 才产出候选 advice；S5.5 收紧；S6 约束仓位与计划
- 主流程 `src/agent/` / `src/analyzer.py` 的 LLM 决策**不接入**本链（§5.12.2）

### 4.2 评分维度与权重（§5.1 R1）

| 维度 | 权重定位 | 是否进 advice 主档 | 备注 |
| --- | --- | --- | --- |
| 技术面（趋势/位置/价位） | **主导**，显著高于其他维度之和 | 是 | 约束，不是具体数值；数值留纸面回看进 calc_version |
| 基本面（财报/分红） | 低权重 | 否（事实性输入） | 可触发风险信号 |
| 估值（PE/PB 分位） | 低权重 | 否（辅助） | 与技术面价格位置分离 |
| 大盘/板块强弱 | 建议层（行情结构数据） | 否（限制加仓/新建仓/买入类） | R4/R5，非消息面情绪 |
| 舆情情绪 | **禁止** | 否 | NG-4 |

**权重表全部可配置、进 `calc_version`**，具体数值留 §9.2 纸面回看确定（§5.1）。

### 4.3 阶段判定 S0–S5（§5.14 R18/R19）

**判定顺序（防兜底错位）**：S0 数据校验 → S3 下跌 → S1 上升 → S2/S4/S5 兜底。

| 阶段 | 判定条件（确定性） | 允许动作集合 | 置信度上限 |
| --- | --- | --- | --- |
| **S0 无法判定** | 数据缺口 >3 日 / 历史不足 / 关键字段缺失 | 不可操作 | — |
| **S1 上升** | 价格站上 MA20 + 斜率 > θ + 位置分位偏高 | 加仓/持有/减仓（趋势跟踪） | 高 |
| **S2 高位震荡** | MA20 附近 + 斜率 ∈ [−θ, θ] + 箱体有效 + 位置偏高 | 持有/箱体高抛低吸（非上沿逆势） | 中-高 |
| **S3 下跌** | 价格跌破 MA20 + 斜率 < −θ | 减仓/清仓/观望/等待卖点（**禁止买入类**） | 中 |
| **S4 低位筑底** | MA20 附近 + 斜率 ∈ [−θ, θ] + 箱体有效 + 位置偏低 | 等待买点/**试探仓（§5.14.3.1）**/持有 | 中 |
| **S5 无结构** | 兜底：有足够数据但既无趋势又无有效箱体（窄幅无序、均线粘合） | 持有/观望 + 风险驱动减仓（**不动**） | 最高「中」 |

**判定阈值（R18-② 初值，纸面回看后回写）**：
- `ma_window=20`、`slope_k=5`、`stage_theta=0.003`、`stage_epsilon=0.005`
- 箱体：`box_lookback=60`、`box_width_max=12%`、`box_touch_min=2`
- `pos20` 以 0.5 二分（高位/低位）
- 连续 2 日防抖（`stage_debounce_days=2`）

**防抖**：阶段切换须连续 N 日（标准档 2 日）满足新条件才切换，避免天天变。

**S5 回合数应显著低于其他阶段**（R19），回测时校验。

### 4.4 阶段门 S5.5（方案 A，§5.14.5 / §10.4）

**位置**：决策表 S5 之后、仓位约束 S6 之前。

**工作方式**：
1. S5 决策表正常产出候选 `candidate_advice`（如「加仓」）
2. 阶段门校验该候选是否 ∈ 当前阶段允许集合（§4.3 表）
3. 冲突 → 改写为允许集合内的动作 + 留痕 `stage_gate_hit`
4. 改写后与 §5.11 同向过滤冲突时，以更严格者为准

**留痕字段**：
```
stage_gate_hit = {
  stage: "S3",
  candidate: "加仓",
  final: "减仓",
  rule_id: "R-S3-no-buy"
}
```

**只收紧不放宽**：阶段门不改写风险等级、不改写股数与仓位上限，只改「动作方向」。评分逻辑不感知阶段（DP-6）。

**回测归因**：`stage_gate_hit` 记录可量化「若无阶段门会是什么」，供 §9.4 分阶段回测。

### 4.5 风险信号 7 类（§5.10 R7）

| 类别 | 信号 | 判定依据 | 级别 |
| --- | --- | --- | --- |
| 大盘 | `market_regime` 全局风险系数 | M1 2×2 环境评级 + 宽度 | 最高警示级（R5，不单独触发严重/清仓） |
| 板块与低估陷阱 | SEC-2 价值陷阱（估值低但持续下行） | 估值分位低 + 趋势下行连续 | 观察/警示 |
| 基本面 | 财报渐进恶化 | 营收/利润/ROE/现金流/审计意见连续 N 期 | 观察/警示/严重 |
| 技术面 | 破位 / 趋势失效 | 价格跌破趋势失效位 | 观察/警示/严重 |
| 分红估值 | 分红中断 / 估值极端 | 分红连续性中断 / 分位极值 | 观察 |
| 组合仓位 | 行业集中度 / 单只超仓 / 总仓超限 | 集中度 ≥25% 提示（不硬限）/ 超硬上限 | 观察/警示 |
| 数据质量 | 缺口 / stale / 缺失率 | gap_days / source_tag / 缺失率 >5% | 观察 |

**三级联动（R6）**：
- 观察：仅提示 + 降置信度
- 警示：加减分 + 禁买入类动作
- 严重：建议降级或触发 L1/L2、禁买入类意见

**风险不改写 advice 主档**（R5），但可限制加仓/新建仓/买入类意见、降置信度。风险信号统一写入 `risk_signals[]`，四处只读同一份（§5.10.6 R8）。

### 4.6 条件计划生成（§5.11 R10）

**四项拍板已落地**：
1. **同向细化**：分支动作须与 advice 同向，反向路径输出「不操作 + 原因」
2. **各分支独立**：用户自行取舍，系统不设优先级/共享额度/自动裁决；每条分支单独满足硬上限，给全触发合计与越界提示
3. **量化粒度全给**：触发条件、触发价、股数（取整100股）、金额、占比、有效期、失效、仓位校验、可设单标记
4. **只给价格/涨跌幅触发**：禁止均线/指标类触发；技术依据放 §6.6

**生成逻辑**（基于 T-1 技术面 + 关键价位）：
```
对每个持仓/关注标的，按 advice 方向产出多分支：
  ├─ 上行突破情景：突破压力位 → 触发价 = 压力位 × (1+buffer)
  ├─ 回踩支撑情景：回踩支撑位 → 触发价 = 支撑位
  ├─ 破位情景：跌破趋势失效位 → 触发价 = 失效位
  └─ 涨跌幅情景：日内涨/跌 X% → 触发
每分支：trigger_type, trigger_price, action, shares(取整100), est_amount, pct_of_total, valid_days=1, can_set_order, notes
全触发合计校验：Σ shares × price ≤ 单只/总仓上限；越界提示但不自动裁决
```

**分支上限**：标准档 4（宽松5/严格3）。有效期 1 交易日。工具不接券商、不盘中监控、不自动下单。

### 4.7 战法转写与启停清单（§5.12 R11）

**处理原则**：复用「策略思想」，不复用「LLM 输出」。把战法 `instructions` 中的判定条件**转写为确定性规则**，进 `calc_version`、走 §6.6 技术解释、可被 §9.4 回测。

**初判清单（c 阶段须逐个核对 `instructions` 后确认）**：

| 类别 | 战法（`strategies/*.yaml`） | 初判 | 转写落到 |
| --- | --- | --- | --- |
| 趋势/均线/量能 | `bull_trend` `ma_golden_cross` `shrink_pullback` `volume_breakout` `bottom_volume` | **优先转写** | §5.2 趋势、§5.3 关键价位、§5.11 条件计划触发价 |
| 箱体/框架 | `box_oscillation` `wave_theory` | **可转写**（去主观描述） | 箱体边界、长周期位置 |
| 质量/价值 | `growth_quality` | **转写为辅助事实** | §5.8 估值/分红、§5.1 低权重维度（不改 advice 主档） |
| 情绪/热点/短线 | `emotion_cycle` `hot_theme` `dragon_head` `event_driven` `one_yang_three_yin` | **本期不启用** | 与 NG-4/NG-6 冲突 |
| 主观框架 | `chan_theory` `expectation_repricing` | **本期不启用** | 判定主观、难确定性转写与回测 |

> 缠论只落地「中枢/箱体、趋势/盘整子集」，笔/线段/级别递归本期不做，`chan_theory.yaml` 仍不启用。

**新增策略准入**：须逐条满足 §5.12.5 八条（确定性/可回测/可解释/可降级/合规/数据准入/验收/登记）。禁止引入 LLM 自由发挥、黑箱打分、需盘中实时数据、回测反推阈值同区间验证。

**c 阶段动作**：逐个读 `strategies/*.yaml` 的 `instructions`，确认转写规则与判定条件，登记启停清单进 `calc_version` 与附录 C。

---

## §5 参数框架（C4）

### 5.1 三档预设初值表（§5.16.2 完整数值）

> 数值为**初值**，最终以 §9.2 纸面回看 + §9.4 回测回写。三档相对关系（宽→严：拦得更多、动得更少）是契约，不得倒挂。**回测基线固定用标准档**，另跑宽松/严格作敏感性对照。

| 维度 | 宽松（少拦，多做） | **标准（默认）** | 严格（宁可错过） |
| --- | --- | --- | --- |
| 定位 | 相信结构、容忍噪音；能天天看盘者 | 平衡；默认档，回测基线 | 只做高确定性；不常看盘、怕回撤者 |
| 档位切换缓冲 `advice_margin` | 0.05 | 0.10 | 0.15 |
| 箱体宽度上限 `box_width_max` | 15% | 12% | 10% |
| 箱体触碰次数 `box_touch_min` | 2 | 2 | 3 |
| 阶段防抖 `stage_debounce_days` | 1 | 2 | 3 |
| 均线缓冲 `ε` | 0.005 | 0.005 | 0.008 |
| S4 试探仓 | 开（额度 1/3） | 开（额度 1/3） | **关** |
| 右侧确认放量倍数 | 1.2× | 1.5× | 1.8× |
| 风险升级门槛 | 更严（不易升级） | 中 | 更松（更易升级） |
| 条件计划分支上限 | 5 | 4 | 3 |
| 建议翻转率上限（回测校验） | 30% | 25% | 20% |
| 阶段切换上限（次/年） | 10 | 8 | 6 |

### 5.2 可配置参数清单（§5.16.3 分组）

| 组 | 参数（示例键名） | 标准档初值 | 允许区间 | 影响 |
| --- | --- | --- | --- | --- |
| 阶段判定 | `ma_window` / `slope_k` | 20 / 5 | 10–60 / 3–10 | §4.3 |
| | `stage_theta` / `stage_epsilon` | 0.003 / 0.005 | 0.001–0.01 / 0.002–0.02 | §4.3 |
| | `box_lookback` / `box_width_max` / `box_touch_min` | 60 / 12% / 2 | 40–120 / 8–20% / 2–5 | §4.3 |
| | `stage_debounce_days` | 2 | 1–5 | §4.3 |
| 动作门槛 | `advice_margin` | 0.10 | 0.02–0.25 | §4.1 S5 |
| | `probe_enabled` / `probe_ratio` | 开 / 1/3 | 开·关 / 1/4–1/2 | §4.3 S4 |
| 风险 | `risk_warn_consec_days` | 3 | 2–10 | §4.5 |
| | `risk_severe_drop_pct` | 由回看定 | 5–20% | §4.5 |
| | `risk_pctile_boundary` | 0.2 / 0.8 | 0.1–0.3 / 0.7–0.9 | §4.5 |
| 仓位 | `single_cap` / `total_cap` / `cash_floor` / `t_cap` | 20% / 80% / 20% / 10% | **不建议改**（改须二次确认） | §4.1 S6 |
| | `stop_l1_ratio` / `stop_l2_pct` | 由回看定 | 20–50% / 3–15% | §5.5 |
| 条件计划 | `plan_max_branches` | 4 | 2–6 | §4.6 |
| | `plan_valid_days` | 1 | 1–3 | §4.6 |
| 展示 | `list_sort_primary` | `risk_level` | 枚举 | §6.2 |
| | `push_min_level` | 警示 | 观察/警示/严重 | §6.3 |

> 仓位类硬上限（20/80/20/10）是 §5.6 护栏，放进配置页只为「集中可见」，修改须二次确认并强提示「改大等于放宽风控」，默认折叠。参数键名是示例，具体命名属编码阶段；**分组与区间边界属于需求契约**。

### 5.3 覆盖、生效与变更记录（§5.16.4）

**基线 + 覆盖**：当前值 = 预设基线（三档之一）+ 用户单项覆盖。页面显示「标准档 · 2 项自定义」，点开可看是哪两项。

**生效规则**：
- 参数变更**次日盘前生效**（当日结论零改动，L3 append-only 不可重算覆盖）
- 越界拒绝（超出允许区间的单项覆盖不予接受）
- 只能调数值，不能改枚举/守卫顺序

**变更记录**（append-only，不引入配置版本号，R21）：
- `param_override` 表：每次变更记一条，含 `param_key, param_value, baseline_preset, op_time, effective_date, user_id, audit_id`
- 回滚也记一条（不删除历史）
- `param_hash`：当前生效参数集的指纹，随 L3 快照留痕

**param_hash 与 calc_version 分离**：
- `param_hash`：参数数值指纹（三档基线 + 单项覆盖的当前值哈希）
- `calc_version`：规则版本指纹（战法启停清单、维度权重、判定结构、temperature 等）
- 两者都随快照留痕，保证同 `calc_version + param_hash + 同快照` → 同结论（DP-5）

### 5.4 配置页实现（§6.8）

**形态**：独立路由或右侧抽屉，入口固定在顶部条。含：
1. 三档预设切换（宽松/标准/严格）
2. 逐项参数编辑（带区间校验、越界拒绝）
3. 变更记录（append-only 列表，可回滚）
4. 仓位硬上限区（默认折叠，改须二次确认）

**只改参数、不改结论**：配置页写 `param_override` 表，不触发当日重算。保存后提示「次日盘前生效」。

**实现落点**：`apps/dsa-web/src/pages/assistant/Settings.tsx`（新增），读/写 `api/v1/endpoints/assistant.py` 的参数接口（新增只读 + 写入 API）。

---

## §6 输出层（C5）

### 6.1 Web 独立页面（§3.5.2）

**现状核查（需求实读）**：前端 `apps/dsa-web`（React Router），`App.tsx:90–101` 共 11 条路由，正常页面全部套 `<Shell>`；唯一不套壳的是 `/login`，走 `StandaloneRouteBoundary`。`api/app.py:549` 已有 SPA 路由回退（非 API 路径返回 `index.html`）。

**实现设计**：

| 项 | 设计 |
| --- | --- |
| 路由注册 | `App.tsx` 新增 **1 条**路由（前缀留编码，不得占用现有 11 条）；沿用 `StandaloneRouteBoundary`（与 `/login` 同构），**不套 `<Shell>`** |
| 鉴权 | 复用 `AuthProvider`，**不加免登录白名单**；未登录跳 `/login` 并可回跳 |
| 页面组件 | `apps/dsa-web/src/pages/assistant/` 下新增：`Index.tsx`（Tab A 大盘板块 + Tab B 个股列表）、`Detail.tsx`（详情页，以 `code` 为主键）、`Settings.tsx`（参数配置，§5.4） |
| 只读 API | `api/v1/endpoints/assistant.py`（新增），读 L3 快照：`holding_conclusion` / `conditional_plan[]` / `risk_signals[]` / `market_snapshot` / `sector_snapshot`；带三元组校验；**禁止前端重算** |
| 响应式 | 一套代码 PC/H5 **同 URL、同一份结论**；窄屏切 Tab/卡片纵向，宽屏多栏；断点数值留编码 |
| 单向导航 | 仅新页面 → 现有工程页面（URL 跳转）；**不做反向入口**、不加菜单/链接 |
| 后端静态服务 | **不改** `api/app.py` SPA 回退（已支持） |

**硬护栏**：不得修改现有 11 条路由、任何现有页面组件、`<Shell>`、菜单、后端静态服务。新增 ≠ 修改现有页面。

### 6.2 卡片/列表/详情页渲染（§6）

**卡片结构（§6.4 固定顺序）**：
操作意见 → 趋势结论 → 当前位置 → 关键价位 → 理由 → 风险提示 → 仓位建议（百分比区间 + 轻/中/重）→ 做 T 计划

**列表页（Tab B，§6.3）**：
- 列：名称 / 编码 / 最近交易日价格 / 涨跌幅 / 操作建议
- 风险角标与排序（`list_sort_primary` 默认 `risk_level`）
- 红涨绿跌（>0 红 / <0 绿 / =0 灰）；**不复权展示**（P0-2）

**详情页（§6.4 五区块）**：
1. 操作意见 + 人话结论（LLM 文案层 §6.4）
2. 趋势 / 位置 / 关键价位
3. **阶段定位**（v2.2 新增第 3 区块：阶段码 + 名称 + 置信度 + 打法 + 允许/禁止 + 切换信号）
4. 风险信号结构化块（§5.10.6 R8）
5. **条件计划**（顺延第 5：分支 + 触发价 + 股数 + 有效期）

**技术解释层（§6.6，R9）**：
- 每个数值/结论可回溯八字段：输入、公式口径、生效阈值、命中类别、数据日期、来源
- 人话层只给结论不含公式；技术解释层**默认折叠、两级展开**
- 导出物（Markdown/Excel）同源

**红涨绿跌 + 不复权**：展示价格与涨跌幅均不复权（P0-2）；除权日标注「除权日，价格不可直接比较」+ 前收调整说明。

### 6.3 飞书推送（七段图标化，§3.5.3.1）

**复用链**：`notification_routing`（新增 1 个 `route_type`）+ `notification_noise`（降噪/去重/静默/严重度）+ `notification_capabilities`（能力画像/渲染预设）+ `feishu_sender`（发送）。**不新增平行发送器、不改既有渠道行为**。

**现状核查**：`feishu_sender._send_feishu_message` 主路径发 `interactive` 卡片（正文 `lark_md`，支持加粗），失败降级 `msg_type=text`。降级时须**剥离 `**`**（硬约束 5）。

**七段结构（顺序固定，可整段省略，不得换序/合并）**：

| 段 | 图标 | 内容 | 省略条件 |
| --- | --- | --- | --- |
| ① 头部 | 📊 🗓 | 交易日 + 三元组 + 数据源 | 不可省 |
| ② 一句话 | 🧭 | 大盘一句 + 自选动作计数 | 不可省 |
| ③ 风险 | 🔥 | 按等级排序，同股只出现一次（取最高归并）；每条三行 | 无信号写「⚪ 今日无风险信号」 |
| ④ 待办 | ✅ | 今日需处理标的，动作符号前置，带股数/原因 | 无待办写「今日无待办」 |
| ⑤ 阶段 | 📍 | 阶段码 + 名称 + 置信度 + 打法 + 允许/禁止 + 切换 | 可省 |
| ⑥ 计划 | 📋 | 条件计划分支，每条三行：触发/动作股数/有效期 | 不可省；无计划写原因 |
| ⑦ 声明 | 🔒 ℹ️ ⚠️ | 静态计划不下单 + 关键字段不可删 + 免责 | 不可省 |

**图标语义（固定，不得同义换符）**：
- 风险等级：🔴 严重 / 🟠 警示 / 🔵 观察 / ⚪ 无（只用于风险）
- 动作：➕ 买入·加仓 / ⏳ 等待买点 / ➖ 减仓 / 🔻 清仓 / ⏸ 持有 / 👀 观望 / ⛔ 不可操作
- 涨跌：▲ / ▼（不用红绿圆点，避免「红＝涨」与「红＝危险」冲突）
- 段标题：📊🧭🔥✅📍📋💬🔒 固定

**两档密度（同一份数据、同一段结论，不重算）**：
- 完整版（默认）：≤ 1,800 字，七段全给
- 精简版：≤ 1,100 字，风险压一行、阶段保留「阶段+打法+禁止」、计划只留可设单分支
- 同一渲染函数产出，`mode` 只控制段落取舍

**硬约束**：
1. 图标只做前缀不替代文字（后跟中文标签）
2. ⑥ 段数值行不加修饰（触发价/股数/金额/有效期/失效行不加 emoji/加粗，保证可照抄）
3. 最多三层缩进
4. 分隔线只用于头尾
5. 加粗只用于段标题与结论词；降级纯文本前剥离 `**`
6. 不用表格/标题语法/`<font color>` 等未实测标签（以 M13 实测为准）
7. 超长走 `notification_capabilities` 压缩，顺序＝先删风险依据→再删阶段证据→再删不可设单分支；**触发条件/股数/风险等级/数据日期四类不得删**

**档位选择**：通道能力配置（`notification_capabilities`），不进 §5.16 配置页、不进 `calc_version`、不影响 advice。

### 6.4 LLM 文案层（§5.13）

**分层（DP-4）**：决策归代码，措辞归 LLM。LLM 不参与取数/指标计算/评分/守卫/advice/条件计划股数/风险定级；输入只能是已固化的结构化快照，输出只能是文字。

**复用现有工程**：
- `src/llm/`（`generation_backend.py`、`litellm_backend.py`、`backend_factory.py`、`usage.py`）：统一生成后端、多渠道 fallback、用量统计 → **复用调用入口与 fallback**
- `src/agent/chat_context.py` 的 `SUMMARY_SYSTEM_PROMPT`（「只总结已有内容，不新增、不推断」）→ **复用其约束范式**作为本助手总结提示词基础
- `src/analyzer.py`（主分析层，LLM 决策）→ **不复用其结论**（与 §5.12.2 冲突），仅参考工程实现
- 配置 `LLM_CHANNELS`（openai/anthropic/gemini/vertex/deepseek/ollama）→ **复用**

**本期三处用法（R16）**：

| # | 位置 | LLM 输出 | 降级文案 |
| --- | --- | --- | --- |
| 1 | 个股人话结论（详情页/卡片区块1） | 一句「今天要不要动、动什么」 | 规则模板句（advice + 条件计划摘要拼装） |
| 2 | 飞书推送摘要（§6.3 ② 段附加） | 大盘+风险+待办+计划压成一段 | 规则模板摘要 |
| 3 | 大盘与板块总结（Tab A 上半 + 板块 R7） | 一段结构描述性文字 | 现有 R7 拼装式结论 |

> **本期不做**：§6.6 技术解释层的「LLM 白话版」（保持纯结构化、可回溯）。

**输入契约（只读白名单，§5.13.4）**：LLM 只收到已固化的 `data_date/snapshot_id/calc_version` + `market_snapshot` + `sector_snapshot` + `holding_conclusion` + `conditional_plan[]` + `risk_signals[]` + counts/待办/组合校验。**不得**传入原始行情、新闻原文、LLM 自由文本或未来数据。

**硬约束（§5.13.5 八条）**：
1. 不新增事实  2. 不改数字（数值一致性校验，不一致即失败降级）  3. 不给新建议  4. 禁用词沿用  5. 失败降级（超时/异常/校验不通过/渠道不可用 → 规则模板，不阻塞）  6. 标注「AI 生成摘要，以结构化字段为准」  7. 不进评分/回测  8. 可追溯（输入快照ID/模型/渠道/生成时间/校验结果，同快照可复现，temperature 进 calc_version）

**留痕**：`llm_text_cache` 表，同快照同位置只生成一次并缓存；四处展示（WebUI/通知/卡片/导出）读同一段文案，不各自调用 LLM。

**渠道/模型/成本/超时/temperature**：留编码阶段确定（M14 实测前置）；超时上限建议 ≤ 120s/标的，复用 `usage.py` 用量统计。

### 6.5 飞书入站交互（§3.6 R17）

**现状核查（实读）**：`bot/platforms/feishu_stream.py` 已有飞书官方 `lark-oapi` WebSocket Stream 模式，订阅 `P2ImMessageReceiveV1`，无需公网 IP；`main.py` + `src/config.py` 有 `FEISHU_STREAM_ENABLED` + `feishu_app_id/app_secret` + `start_feishu_stream_background()`；`feishu_stream.py` 有 `reply_text`/`send_to_chat`/交互卡片/`@用户`/分块发送；`bot/handler.py` 有 `handle_feishu_webhook` 等；`tests/test_feishu_stream.py` 已有覆盖；工程已有 `dispatcher` 命令框架。

**本期四项能力**：

| # | 命令 | 输入 | 输出 | 写入 |
| --- | --- | --- | --- | --- |
| 1 | 加入/移除关注 | 股票代码 | 二次确认 → 确认结果 | `watchlist_runtime`（次日生效） |
| 2 | 记录操作思路/心得 | 自然语言笔记 | 确认存入 | `user_note`（不进评分/回测） |
| 3 | 查询当日结论/条件计划 | 股票代码或「今日」 | 当日快照摘要 | 只读 L3 |
| 4 | 盘后复盘问答 | 自然语言提问 | 复盘回答（标「仅回看，不构成新建议」） | 只读，**不写回 advice** |

**写操作护栏（§3.6.3）**：
- `user_id` 白名单 + **二次确认** + 幂等 + 审计 + 可撤销 + **次日盘前生效**（当日结论零改动）
- 关注清单写入不破坏 `STOCK_LIST` 现有读取链
- 笔记不进评分/回测
- 入站视为通知通道扩展（修订 NG-10）；`notification_sender` 仍只负责出站

**route_type 命名**：留编码阶段确定，复用现有 routing/noise/capabilities。

**自由问答**：可复用现有 `src/agent/` 对话能力，但其回答**不得写回** advice 或任何结构化字段（§5.12.2）。

### 6.6 M13 实测与 emoji 渲染（§3.5.3.1 硬约束 6 / §7.4）

**实测内容**：
- `lark-oapi` SDK 与飞书 Stream 长连接在目标部署环境的可用性
- app bot 权限（接收/回复消息）
- `FEISHU_STREAM_ENABLED` 配置链
- **卡片正文 emoji / `lark_md` 标签的客户端渲染一致性**（手机/桌面/网页三端分别验证）

**决定什么**：
- §6.3 七段图标化推送能否上线（emoji 是否在三端一致渲染）
- §6.5 飞书入站能否启用
- 哪些 `lark_md` 标签可用（未验证的一律不用，硬约束 6）

**不可得时降级**：
- Stream 不可用 → 飞书入站关闭，只保留出站推送（§3.6 标注「未启用时安全关闭」）
- emoji 渲染不一致 → 回退纯文本标签（如「[严重]」「[减仓]」），保留语义不依赖图形

---

## §7 验证与回测（C6）

### 7.1 纸面回看两步法（§9.2 D2）

**目的**：验证「同数据 → 同结论」、阈值合理性、无前后矛盾。**不是回测胜率**（那是 §7.2）。

**第一步：1 只样例逐字段**
- 选 1 只标的，取一个交易日快照
- 逐字段核对：输入数据 → 规则计算 → 中间结果 → 最终 advice/条件计划/风险
- 每个数值回溯 §6.6 八字段（输入/公式/阈值/命中/日期/来源）
- 成功标准：结论稳定、字段完整、可解释

**第二步：10 只 × 5 日**
- 10 只标的（覆盖不同阶段 S1–S5 + S0）× 5 个交易日
- 批量跑规则，检查跨标的、跨日的一致性与边界情况
- 成功标准：无前后矛盾、无同数据不同结论、S0 显式标因、S5 不落成假震荡

**实现落点**：`src/assistant/review/paper_review.py`（新增），读 L1 历史 + 跑守卫链 → 输出对照表。

### 7.2 回测引擎扩展（§9.4）

**现状复用**：`src/core/backtest_engine.py`（`Long-only daily-bar backtesting engine`，`EvaluationConfig(eval_window_days, neutral_band_pct, engine_version)`，输出 `win_rate_pct`）；`src/services/backtest_service.py`、`api/v1/endpoints/backtest.py`、`src/repositories/backtest_repo.py` 已有。

**关键缺口**：现有引擎为后验评估，**不模拟条件单成交**（§9.4.5）。须扩展「条件计划回测模式」。

**条件计划回测模式（需扩展，设计属本方案）**：

| 能力 | 说明 |
| --- | --- |
| 价格/涨跌幅触发模拟 | 按 §4.6 条件计划分支，模拟历史日内是否触及触发价 |
| 股数与资金占用 | 分支股数 × 触发价 vs 单只/总仓上限 |
| T+1 | 买入次日可卖；部分成交/跳空处理 |
| 多分支组合校验 | 同标的多分支全触发的合计与越界 |
| 分红与成本 | 含分红再投；成本基于 user_holding |
| 未闭合仓位 | 跨日持有、T 仓未闭合 |
| 聚合 | 分标的/分板块/分年度 |

**胜率与配套指标**：
- 完整回合胜率（含分红、扣成本）
- 盈亏比、最大回撤、年化收益
- **样本内外分离**：阈值只在样本内用 §9.2 回看确定；样本外只做验证（walk-forward 或时间切分，方式留编码）
- **样本充足度**：已闭合回合数 ≥ 下限（参考 `evaluated >= 30`，本助手按回测口径在编码阶段确定）；不足只报计数不报胜率

**结果用途**：仅作验证记录、不承诺收益、不进评分（R13）。

**实现落点**：`src/assistant/backtest/plan_backtest.py`（新增），扩展 `backtest_engine` 但不改原引擎。

### 7.3 回测健康度（§9.4.2）

| 指标 | 标准档上限 | 宽松 | 严格 |
| --- | --- | --- | --- |
| 建议翻转率 | ≤ 25% | 30% | 20% |
| 阶段切换 | ≤ 8 次/年 | 10 | 6 |
| 动作分布 | 无单一动作占比异常 | 同 | 同 |
| 改写归因 | `stage_gate_hit` 占比可解释 | 同 | 同 |
| 连续观望天数 | 无超长连续观望（可配置） | 同 | 同 |

**强制分阶段统计**（R18）：回测结果按 S1–S5 分阶段统计胜率/盈亏比/回合数；S5 回合数应显著低于其他阶段（R19）。

---

## §8 接口契约汇总

### 8.1 Web 只读 API（新增，`api/v1/endpoints/assistant.py`）

| 端点 | 方法 | 入参 | 出参 | 说明 |
| --- | --- | --- | --- | --- |
| `/api/v1/assistant/snapshot` | GET | `data_date?` | `{snapshot_id, data_date, calc_version, param_hash, generated_at, bars_used, gap_days}` | 当日快照主信息 |
| `/api/v1/assistant/market` | GET | `snapshot_id` | `market_snapshot` | 大盘环境 |
| `/api/v1/assistant/sectors` | GET | `snapshot_id` | `sector_snapshot[]` | 板块快照列表 |
| `/api/v1/assistant/holdings` | GET | `snapshot_id` | `holding_conclusion[]` | 个股结论列表（Tab B） |
| `/api/v1/assistant/holding/{code}` | GET | `snapshot_id` | `holding_conclusion + conditional_plan[] + risk_signals[]` | 详情页 |
| `/api/v1/assistant/plans/{code}` | GET | `snapshot_id` | `conditional_plan[]` | 条件计划 |
| `/api/v1/assistant/risks` | GET | `snapshot_id` | `risk_signals[]` | 风险信号 |
| `/api/v1/assistant/llm_text` | GET | `snapshot_id, position` | `{text, model, channel, generated_at}` | LLM 文案（读缓存） |

**统一约束**：
- 所有响应带三元组 `[data_date, snapshot_id, calc_version]`
- 渲染前校验三元组一致（§2.4）；不一致返回「待同步」
- **只读**，禁止前端重算
- 鉴权：复用现有 AuthProvider，未登录 401

### 8.2 参数配置 API（新增）

| 端点 | 方法 | 入参 | 出参 | 说明 |
| --- | --- | --- | --- | --- |
| `/api/v1/assistant/params` | GET | — | `{baseline_preset, overrides[], param_hash}` | 当前生效参数 |
| `/api/v1/assistant/params/preset` | PUT | `{preset: loose|standard|strict}` | `{ok, effective_date}` | 切换三档（次日生效） |
| `/api/v1/assistant/params/override` | PUT | `{key, value}` | `{ok, effective_date}` 或 `{error: out_of_range}` | 单项覆盖（越界拒绝） |
| `/api/v1/assistant/params/override` | DELETE | `{key}` | `{ok, effective_date}` | 撤销单项覆盖（记一条回滚） |
| `/api/v1/assistant/params/history` | GET | — | `param_override[]` | 变更记录（append-only） |

**约束**：写操作次日盘前生效；append-only；仓位类改须二次确认。

### 8.3 飞书入站命令接口（复用 dispatcher）

| 命令 | 输入 | 输出 | 写入 |
| --- | --- | --- | --- |
| `关注 <code>` / `移除 <code>` | 股票代码 | 二次确认卡片 → 确认后写入 | `watchlist_runtime` |
| `笔记 <内容>` | 自然语言 | 确认存入 | `user_note` |
| `查询 <code>` 或 `今日` | 代码或关键字 | 当日快照摘要 | 只读 L3 |
| `复盘 <问题>` | 自然语言 | 复盘回答 | 只读，不写回 advice |

**约束**：`user_id` 白名单 + 二次确认 + 幂等 + 审计 + 可撤销 + 次日生效。

### 8.4 LLM 文案层接口（内部）

| 调用点 | 输入 | 输出 | 缓存 |
| --- | --- | --- | --- |
| 个股人话结论 | `holding_conclusion + plan + risk` | 一句话 | `llm_text_cache(position=stock)` |
| 飞书摘要 | `market + risks + todos + plans` | 一段话 | `llm_text_cache(position=summary)` |
| 大盘板块总结 | `market_snapshot + sector_snapshot` | 一段话 | `llm_text_cache(position=market)` |

**约束**：同快照同位置只生成一次；数值一致性校验；失败降级规则模板；超时 ≤ 120s。

---

## §9 待补项与实测前置总表

> 汇总需求 §10.2.2 与 §7.4 中所有待补项，标注本方案的处理路径。

### 9.1 需用户提供的材料

| 项 | 用途 | 状态 |
| --- | --- | --- |
| 飞书 webhook 具体值 | §6.3 推送 | 待用户提供（配置材料，非口径） |
| 飞书 app bot 凭据（`FEISHU_STREAM_ENABLED` + `feishu_app_id/app_secret`） | §6.5 入站 | 待用户提供 / 部署时配置 |

### 9.2 需实测前置（编码启动前优先）

| 实测 | 决定什么 | 不可得时降级 | 本方案章节 |
| --- | --- | --- | --- |
| M5 板块代码入参 | code→板块映射 | R5 降级 | §3.1 |
| M6 行业字段口径 | 映射链路 | 严禁用于映射 | §3.1 |
| M7 板块家数字段 | R5 内部结构 | R5 降级「未取得」 | §3.1 |
| M9 分红/估值接口 | §5.8 能否落地 | 标注缺失、降置信度 | §3.1 |
| M10 长周期序列 | 长周期分位 | 阶段记 S0 | §3.2 |
| M11 风险历史序列 | §4.5 连续判定 | 信号标「数据不足」 | §3.5 |
| M12 财报渐进恶化 | §4.5 基本面风险 | 信号标「数据不足」 | §4.5 |
| M13 Stream + emoji 渲染 | §6.3/§6.5 能否上线 | 入站关闭 / emoji 回退纯文本 | §6.6 |
| M14 LLM 渠道可用性 | §6.4 能否按时产出 | 降级规则模板 | §6.4 |
| M15 周/月线 + adj_factor | §3.3 复权方案 | 日线聚合 + 事件表推算 | §3.4 |
| M16 公告接口字段 | §5.15 消息推送 | 只显示标题+来源 | §3.4 |
| M17 3 年回补耗时 | §8.2 7:30 产出 | 分批回补 + S0 | §3.4 |

### 9.3 需纸面回看回写（编码后跑 §7.1）

| 项 | 初值来源 | 回写目标 |
| --- | --- | --- |
| 趋势/位置阈值 | §4.3 / §5.1 | `calc_version` |
| 阶段判定阈值（含宽松/严格两档） | §5.1 三档表 | `calc_version` |
| 风险判定连续天数/分位/跌幅/等级边界 | §4.5 | `calc_version` |
| 条件计划档位比例与触发价折算 | §4.6 | `calc_version` |
| 维度权重表 | §4.2 | `calc_version` |
| 三档最终数值 | §5.1 | `calc_version` |

### 9.4 需逐个核对（编码时）

| 项 | 动作 | 登记 |
| --- | --- | --- |
| 15 个战法 `instructions` | 逐个核对 → 启停 + 转写规则 | `calc_version` + 附录 C |
| 逐券商条件单可用触发类型 | 用户自行确认（本期只输出价格/涨跌幅两类） | — |

### 9.5 留编码阶段确定

| 项 | 章节 |
| --- | --- |
| 独立页面 URL 前缀与响应式断点 | §6.1 |
| 通知新增 `route_type` 命名与内容模板 | §6.3 / §6.5 |
| LLM 渠道/模型/成本上限/超时/temperature | §6.4 |
| 回测样本内外切分方式（walk-forward） | §7.2 |
| 已闭合回合数下限 | §7.2 |

---

## §10 与需求文档的映射矩阵

| 本方案章节 | c 模块 | 对应需求章节 | 对应决议 |
| --- | --- | --- | --- |
| §1 总则 | — | §10.3、D1、NG-9/10 | — |
| §2 系统架构 | C1 | §3.5.2、D1 | D1、R14 |
| §3 数据获取与存储 | C2 | §4.6、§7.3、§7.4 | R20（数据侧）、Q8 |
| §4 决策引擎 | C3 | §5.1–§5.12、§5.14 | R1–R11、R18–R20 |
| §4.3 阶段判定 | C3 | §5.14 | R18、R19 |
| §4.4 阶段门 | C3 | §5.14.5、§10.4 | R18-Q1 |
| §4.5 风险信号 | C3 | §5.10 | R5–R8 |
| §4.6 条件计划 | C3 | §5.11 | R10 |
| §4.7 战法转写 | C3 | §5.12 | R11 |
| §5 参数框架 | C4 | §5.16、§6.8 | R21 |
| §6.1 Web 页面 | C5 | §3.5.2 | R14 |
| §6.3 飞书推送 | C5 | §3.5.3、§3.5.3.1 | R15、R22 |
| §6.4 LLM 文案层 | C5 | §5.13 | R16 |
| §6.5 飞书入站 | C5 | §3.6 | R17 |
| §7 验证回测 | C6 | §9.2、§9.4 | D2、R13 |

**口径一致性承诺**：本方案每一处设计都可回溯到需求 v2.4 的章节与决议号；无新增口径、无修改口径。冲突时以需求 v2.4 为准。

---

## 附录 A：可复用清单总表（代码库实读）

> 来源：需求 v2.4 的 2026-11-10 核查结论 + 2026-11-19 代码库实读（Explore 全量探查 8 模块）。**编码启动前须逐条复核**（库可能已演进）。

### A.0 实读核查关键发现（2026-11-19，与需求/方案基线的差异）

> 以下为 Explore 实读代码库后的**关键修正与确认**，直接影响 §3–§7 的实现设计。

| # | 发现 | 与基线差异 | 影响章节 | 处理 |
| --- | --- | --- | --- | --- |
| 1 | **8 张规划表全部不存在**：`market_regime`/`rs_t`/`sector_strength`/`holding_conclusion`/`conditional_plan`/`risk_signals`/`market_snapshot`/`sector_snapshot` 均需新建 | 需求 §4.6.1 记 38 张表，实读**实存 28 张**（不含 schema_migrations）；差异可能因统计口径或库演进 | §3.2 | 以实读为准；8 张表按 §3.2 设计新建 |
| 2 | **`source_tag`(D1/D2/D3) 不存在**：代码中无此标记，仅 `docs/requirements` 与 `deliverables/` 中规划；`snapshot.py` 的 `df.attrs["snapshot_source"]`(L106) 可作派生基础 | 需求/方案多处引用 source_tag | §3.1 | 新建：`Pick`/`ScreenResult` 加 `source_tag` 字段 |
| 3 | **`calc_version` 不存在**：代码层只有 `backtest_engine_version: str = "v1"`（config L1296）；无独立 calc_version 字段 | 方案 §2.4/§5.3 大量引用 calc_version | §2.4/§5.3 | 复用 `backtest_engine_version` 或新增 `calc_version` 字段（走 Config 迁移） |
| 4 | **复权**：`akshare_fetcher` 全部 `adjust="qfq"`（前复权），无独立 `adj_factor` 接口；`TICKFLOW_KLINE_ADJUST` 有 5 值但默认 `none` | 与 §3.2 修正一致 | §3.2 | 确认需改 `adjust=""` + 新建 adj_factor 取数 |
| 5 | **通知渠道 13 个**（非需求记的 15+）：WECHAT/FEISHU/DINGTALK/TELEGRAM/EMAIL/PUSHOVER/NTFY/GOTIFY/PUSHPLUS/SERVERCHAN3/CUSTOM/DISCORD/SLACK/ASTRBOT；`route_type` 三类 `report/alert/system_error` | 数字修正 | §6.3 | 以实读为准；新增 route_type 在 `NOTIFICATION_ROUTE_CONFIGS`(L31) 注册 |
| 6 | **回测 `win_rate_pct` 口径**：分母 = `win_count + loss_count`（**排除 neutral**）；`neutral_rate_pct` 单独算；`_MIN_CALIBRATION_SAMPLES=30` 在 `src/agent/memory.py` L32 | 方案 §7.2 引用 evaluated>=30 | §7.2 | 确认落点；条件计划回测模式须自定胜率口径 |
| 7 | **回测不支持条件单模拟**：`BacktestResultLike` 有 `hit_stop_loss/take_profit/first_hit` 但无 conditional order；`_classify_outcome` 仅 win/loss/neutral 三分类 | 与 §7.2 缺口判断一致 | §7.2 | 确认需扩展条件计划回测模式 |
| 8 | **LLM fallback 闭集**：`SUPPORTED_GENERATION_FALLBACK_BACKENDS = frozenset({LITELLM_BACKEND_ID})`，fallback 只能到 litellm；**无独立成本($/token)预算机制**，只有 token 用量统计（`usage.py` → `LLMUsage` 表） | 方案 §6.4 提渠道/成本留编码 | §6.4 | 成本上限需新建 `src/llm/cost_budget.py` |
| 9 | **条件计划雏形**：`dashboard.phase_decision.watch_conditions + next_check_time`(executor.py L148-149) 是雏形，但**无独立 `conditional_plan` 字段或表** | 方案 §4.6 新建 | §4.6 | 确认需新建 conditional_plan 表 + 生成逻辑 |
| 10 | **风险信号未持久化**：`risk_flags` 只是 `AgentContext` 内存字段(protocols.py L173)，`add_risk_flag()` 传递但未落库 | 方案 §4.5 新建 risk_signals 表 | §4.5 | 确认需新建 risk_signals 表 |
| 11 | **持仓建议**：无 `holding_conclusion` 字段；通过 `position_advice.no_position/has_position`(executor.py L121-124) | 方案 §3.2 新建 | §3.2 | 确认需新建 holding_conclusion 表 |
| 12 | **前端无 `ScreenPage`**：只有 `StockScreeningPage`(/screening)；11 条路由精确列表见 A.1 | 需求 §5.12.1 提到 ScreenPage | §6.1 | 以实读为准 |
| 13 | **战法 15 个确认**：`bull_trend` 是 `default_active`；内置目录硬编码 `strategies/`（base.py L23） | 与需求 §5.12 一致 | §4.7 | 确认 |
| 14 | **选股策略 10 个确认**：完整文件名见 A.4 | 与需求 §3.4 一致 | — | 下一阶段复用 |

### A.1 前端与路由

| 现有能力 | 位置 | 复用方式 | 改动护栏 |
| --- | --- | --- | --- |
| React Router 路由体系 | `apps/dsa-web/src/App.tsx`（11 条路由） | 新增 1 条路由注册 | 不得修改现有 11 条 |
| 独立渲染边界 | `StandaloneRouteBoundary`（`/login` 同构） | 新页面沿用此机制不套 Shell | — |
| 鉴权 | `AuthProvider` | 复用，跟随现有登录 | 不加免登录白名单 |
| SPA 路由回退 | `api/app.py` 已有 SPA fallback（非 API 路径返回 index.html） | 不改 | 不改后端静态服务 |
| 选股/回测页面（下一阶段） | `StockScreeningPage`(`/screening`)、`BacktestPage`(`/backtest`) | 下一阶段选股 Tab 复用 | 本版不涉及；**无 `ScreenPage`**（实读确认） |

**现有 11 条路由精确列表（实读，新增路由不得占用）**：

| # | 路径 | 组件 |
| --- | --- | --- |
| 1 | `/` | HomePage |
| 2 | `/chat` | ChatPage |
| 3 | `/portfolio` | PortfolioPage |
| 4 | `/decision-signals` | DecisionSignalsPage |
| 5 | `/screening` | StockScreeningPage |
| 6 | `/data` | DataCenterPage |
| 7 | `/backtest` | BacktestPage |
| 8 | `/alerts` | AlertsPage |
| 9 | `/usage` | TokenUsagePage |
| 10 | `/settings` | SettingsPage |
| 11 | `*` | NotFoundPage |

### A.2 通知与飞书

| 现有能力 | 位置 | 复用方式 | 改动护栏 |
| --- | --- | --- | --- |
| 多渠道发送器 | `src/notification_sender/`（feishu/dingtalk/wechat/telegram/discord/slack/email/custom_webhook 等 15+） | 复用 `feishu_sender` | 不新增平行发送器 |
| 路由 | `src/notification_routing.py`（按 `route_type` 配渠道） | 新增 1 个 `route_type` | 不改既有渠道行为 |
| 降噪 | `src/notification_noise.py`（降噪/去重/静默/严重度） | 复用 | — |
| 能力画像 | `src/notification_capabilities.py`（能力/渲染预设/压缩） | 复用（两档密度压缩） | — |
| 飞书配置 | `src/config.py:1158–1168`（`feishu_webhook_url/secret/keyword`） | 复用 | — |
| 飞书卡片发送 | `feishu_sender._send_feishu_message`（interactive 卡片 + lark_md，失败降级 text） | 复用，降级时剥离 `**` | — |
| 飞书 Stream 入站 | `bot/platforms/feishu_stream.py`（lark-oapi WebSocket，订阅 P2ImMessageReceiveV1） | 复用 + 新增 dispatcher handler | 不改平台适配器 |
| 入站启动 | `main.py` + `src/config.py`（`FEISHU_STREAM_ENABLED` + `feishu_app_id/app_secret` + `start_feishu_stream_background()`） | 复用 | — |
| Webhook 备选 | `bot/handler.py`（`handle_feishu_webhook` 等） | 备选 | — |
| 入站测试 | `tests/test_feishu_stream.py` | 已有覆盖 | — |

### A.3 数据与存储

| 现有能力 | 位置 | 复用方式 | 改动护栏 |
| --- | --- | --- | --- |
| SQLite 库 | `data/stock_analysis.db`（38 张表） | 同库新增/扩展表 | 不破坏现有表 |
| 日线表 | `stock_daily`（13 只、3677 行、UNIQUE(code,date)） | 扩展加 `adj_factor` 列 + 改 `adjust=""` | 禁止前复权落库 |
| 取数封装 | `akshare_fetcher`（现状 `adjust="qfq"`） | 包装为不复权取数 | 改 adjust 参数 |
| 增量策略 | `has_today_data(code, date)` | 复用防重复 + 新增补洞 | — |
| 取数管线 | `pipeline.py`（现状 `days=30`） | 扩展窗口至 ≥3 年 + 周月线聚合 | — |
| 交易日历 | `src/core/trading_calendar.py` | 复用 | — |

### A.4 决策与工具

| 现有能力 | 位置 | 复用方式 | 改动护栏 |
| --- | --- | --- | --- |
| 技术分析工具 | `src/agent/tools/analysis_tools.py`、`data_tools.py`（`analyze_trend`/`get_daily_history`/`get_sector_rankings`） | 作为规则化取数底座（只读） | 不接入其 LLM 观点 |
| 战法文件 | `strategies/*.yaml`（15 个自然语言战法） | 转写判定条件为确定性规则 | 不加载 LLM 输出；不删文件 |
| 多策略契约 | `docs/multi-strategy-contract.md`（Outcome、`evaluated >= 30`） | 复用防作弊与统计口径 | — |
| 选股引擎 | `src/services/screening/` + `strategies/*.yaml`（10 个） | 下一阶段选股 Tab 复用 | 本版不涉及 |
| 分析记忆 | `src/agent/memory.py` | **不复用**（用途不同） | — |

### A.5 LLM

| 现有能力 | 位置 | 复用方式 | 改动护栏 |
| --- | --- | --- | --- |
| 统一后端 | `src/llm/`（`generation_backend.py`/`litellm_backend.py`/`backend_factory.py`/`usage.py`） | 复用调用入口 + fallback + 用量 | — |
| 总结提示词范式 | `src/agent/chat_context.py` `SUMMARY_SYSTEM_PROMPT` | 复用约束范式 | — |
| 主分析层 | `src/analyzer.py`（LLM 决策） | **不复用其结论** | 仅参考工程实现 |
| 渠道配置 | `LLM_CHANNELS`（openai/anthropic/gemini/vertex/deepseek/ollama） | 复用 | 渠道/模型/成本留编码 |
| 多 Agent/对话 | `src/agent/` | 飞书入站自由问答可复用 | 回答不写回 advice |

### A.6 回测

| 现有能力 | 位置 | 复用方式 | 改动护栏 |
| --- | --- | --- | --- |
| 回测引擎 | `src/core/backtest_engine.py`（Long-only daily-bar，`EvaluationConfig`，`win_rate_pct`） | 复用日线回放；扩展条件计划模式 | 不改原引擎 |
| 回测服务 | `src/services/backtest_service.py`、`api/v1/endpoints/backtest.py`、`src/repositories/backtest_repo.py` | 复用 | — |

**回测口径实读（2026-11-19）**：
- `win_rate_pct = win_count / (win_count + loss_count) × 100`，**分母排除 neutral**；`neutral_rate_pct` 单独计算
- `_MIN_CALIBRATION_SAMPLES = 30`（样本下限）落在 `src/agent/memory.py` L32，非 backtest_engine 内
- `BacktestResultLike` 有 `hit_stop_loss/take_profit/first_hit` 但**无 conditional order 模拟**；`_classify_outcome` 仅 win/loss/neutral 三分类
- `engine_version = "v1"`（config L1296），即 `calc_version` 对应物；方案 §2.4 的新 `calc_version` 须复用或扩展此字段
- 条件计划回测模式（§7.2）须**自定胜率口径**（含分红、扣成本、T+1、部分成交），不复用现有 win/loss/neutral 三分类

---

## 附录 B：新增文件落点建议

| 模块 | 路径 | 职责 |
| --- | --- | --- |
| 取数与存储 | `src/assistant/data/fetcher.py` | 不复权取数、回补、补洞 |
| | `src/assistant/data/storage.py` | L1–L4 表读写、周月线聚合、缺口检测 |
| 决策引擎 | `src/assistant/engine/guard_chain.py` | 守卫链 S0→S6 主入口 |
| | `src/assistant/engine/scoring.py` | 评分维度与权重 |
| | `src/assistant/engine/stage.py` | 阶段判定 S0–S5 + 防抖 |
| | `src/assistant/engine/stage_gate.py` | 阶段门 S5.5（方案 A） |
| | `src/assistant/engine/risk.py` | 风险信号 7 类 |
| | `src/assistant/engine/plan.py` | 条件计划生成 |
| | `src/assistant/engine/strategies.py` | 战法转写规则（确定性） |
| 参数框架 | `src/assistant/config/presets.py` | 三档预设 |
| | `src/assistant/config/override.py` | 单项覆盖 + 变更记录 + param_hash |
| 输出装配 | `src/assistant/render/card.py` | 卡片/列表/详情渲染 |
| | `src/assistant/render/feishu_text.py` | 七段图标化渲染（两档密度） |
| | `src/assistant/render/llm_text.py` | LLM 文案层调用（三处） |
| Web API | `api/v1/endpoints/assistant.py` | 只读快照 API + 参数配置 API |
| Web 前端 | `apps/dsa-web/src/pages/assistant/Index.tsx` | Tab A + Tab B |
| | `apps/dsa-web/src/pages/assistant/Detail.tsx` | 详情页 |
| | `apps/dsa-web/src/pages/assistant/Settings.tsx` | 参数配置页 |
| 飞书入站 | `src/assistant/inbound/handlers.py` | 四项命令 dispatcher handler |
| 回测扩展 | `src/assistant/backtest/plan_backtest.py` | 条件计划回测模式 |
| 纸面回看 | `src/assistant/review/paper_review.py` | 两步法核对 |
| 数据库迁移 | `src/assistant/data/migrations/` | 新增表 DDL |

> **隔离原则**：`src/assistant/` 不 import `src/agent/` 的决策/观点生成（飞书自由问答例外，但回答不写回结构化字段）。

---

## 附录 C：降级矩阵与实测不可得时的退路

| 场景 | 触发 | 降级动作 | 契约出处 |
| --- | --- | --- | --- |
| 数据缺口 ≤3 日 | gap_days ≤ 3 | 自动补 | §4.6.3 |
| 数据缺口 >3 日 | gap_days > 3 | 标 stale + 阶段 S0 + 不生成条件计划 | §4.6.3 |
| 取数连续 3 日失败 | source 连续失败 | 降级或排除该源 | §7.2 |
| 关键字段缺失 >5% | 缺失率 | 降级或排除 | §7.2 |
| 基本面取数失败 | 分红/估值接口失败 | 标注缺失、降置信度、不阻塞报告 | §7.3 |
| adj_factor 免费源不可得 | M15 不通过 | 除权事件表本地推算，**不得前复权落库** | §4.6.4 |
| 公告接口字段不全 | M16 不通过 | 只显示标题 + 来源，无链接 | §5.15 |
| 3 年回补超时 | M17 不通过 | 分批回补 + 回补期阶段 S0 | §4.6.3 |
| 风险历史序列不足 | M11/M12 未完成 | 信号标「观察级 · 数据不足」 | §7.3 |
| LLM 超时/异常/校验失败 | §5.13.5 | 规则模板文案，不阻塞 | §6.4 |
| 飞书 Stream 不可用 | M13 不通过 | 入站关闭，只保留出站推送 | §6.5 |
| emoji 渲染不一致 | M13 三端验证 | 回退纯文本标签（[严重][减仓]） | §6.3 |
| LLM 渠道不可用 | M14 不通过 | 降级规则模板 | §6.4 |
| 非交易日 | trading_calendar | 整链路跳过，不发提示 | §8.2 |
| webhook 未配置 | FEISHU_WEBHOOK 缺 | 只生成本地 HTML/WebUI，不报错 | FR-9.6 |

> **降级原则（DP-7）**：数据不足时标注降级，不推测、不编造、不阻塞报告。所有降级留痕（source_tag / stale / 降级文案），供复盘。

---

## 文档状态说明

- 本方案为 **c 阶段技术方案初稿 v0.1**，在需求 v2.4 **未获用户批准** 前产出，仅供过目。
- 本方案不构成实施授权；用户过目并批准需求 v2.4 后，本方案方可转为正式技术方案、进入 d 阶段编码。
- 本方案与需求 v2.4 口径一致；若过目中发现缺口或矛盾，登记到 §9 并提请用户决策，走需求变更规则。
- 附录 A 的文件路径与接口签名须在编码启动前逐条复核（库可能已演进）。


