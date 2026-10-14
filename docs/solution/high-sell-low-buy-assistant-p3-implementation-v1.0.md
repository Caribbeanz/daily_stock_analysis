# 高抛低吸助手 · P3 实施说明（v1.0：周六复盘 + 完整回测报告）

| 项 | 内容 |
| --- | --- |
| 文档状态 | **P3 ✅ 已实施**（FR-11 周六复盘 + FR-12 完整回测报告；后端回归含 P3 全绿） |
| 版本 | v1.0（首版） |
| 更新日期 | 2026-10-14 |
| 上游需求 | `docs/requirements/high-sell-low-buy-assistant-requirements-v0.7.md`（**FR-11.1~.4 / FR-12.1~.5 / FR-14.3.5 / NFR-5 / NFR-8 / NFR-9**） |
| 上游方案 | `docs/solution/high-sell-low-buy-assistant-design-v1.3.md`（§4 P3） |
| 前置 | P0 ✅ + P1 ✅（`0701bf75`）；P2.0~P2.4 ✅（`1ac291ec` / `49f154be` / `d628c29a` / `e7e86081` / `bfc8cc17`） |
| 本环境约束 | 前端 `vite build` / `vitest` 因本机 esbuild 受限需在用户终端执行；本次前端仅做 `tsc -b` 类型校验 |

---

## 0. 本期交付（P3 = 周六复盘 + 策略有效性报告）

| 文件 | 类型 | 作用 |
| --- | --- | --- |
| `src/services/weekly_review_service.py` | 新增 | 周度复盘编排：**① 信号回顾与胜率 ② 持仓盈亏与执行情况 ③ 大盘/板块周度总结 ④ 下周关注与关键价位**；渲染 markdown（含免责声明）；依赖全可注入、板块级异常隔离 |
| `src/storage.py` → `ExecutionFeedback` | 新增表 | 「是否按建议执行」人工回填（FR-11.2 / FR-14.3.5）：`symbol + week_key` 唯一，`execution_status ∈ {executed, ignored, later}` |
| `src/repositories/execution_feedback_repo.py` | 新增 | 回填 upsert（覆盖语义）/ 查询 / 按周列表 / 删除 |
| `api/v1/endpoints/review.py` | 新增 | `GET /api/v1/review/weekly`、`GET|POST /api/v1/review/execution` |
| `api/v1/endpoints/backtest.py` → `/rule-report` | 扩展 | 规则回测报告端点（FR-12.2 四项指标 + FR-12.3 场景分档 + FR-12.4 局限） |
| `src/services/rule_backtest.py` | 扩展 | `Trade.scenario`；**按场景分档统计**（FR-12.3）；**局限性与免责说明**（FR-12.4）；`--report-out` 落报告文件；`profit_factor=∞` 的 JSON 安全序列化 |
| `src/services/p1_scheduler.py` | 扩展 | 内嵌**周六周任务**（非侵入 `add_background_task` 轮询 + 独立 ledger）；`--weekly/--no-weekly/--weekly-time/--weekly-weekday` |
| `src/services/p1_service.py` | 扩展 | 控制面透传周任务参数（`start --no-weekly/--weekly-time/--weekly-weekday`） |
| `apps/dsa-web/src/api/review.ts` | 新增 | `reviewApi.weekly/executionList/setExecution` + `ruleBacktestApi.report` |
| `apps/dsa-web/src/pages/ReviewPage.tsx` | 新增 | 「复盘与回测」页（两个 Tab：周度复盘 / 策略回测） |
| `App.tsx` / `SidebarNav.tsx` / `uiText.ts` | 接线 | 路由 `/review` + 侧边栏「复盘」入口 + 中英文案 |
| `.env.example` | 修改 | 新增 P3 周度复盘与回测报告配置说明段 |
| `tests/test_p3_*.py` ×4 | 新增 | P3 单测（复盘服务 / 回填仓储 / 复盘 API / 回测报告 / 调度） |

---

## 1. 周六复盘（FR-11）

### 1.1 报告结构（严格对齐 FR-11.1~11.4）

| 板块 | 需求 | 数据来源（四端同源 NFR-9） |
| --- | --- | --- |
| ① 本周信号回顾与胜率 | FR-11.1 | `decision_signals`（`source_type=analysis` / `trigger_source=p0_engine`，按本周窗口过滤）+ `decision_signal_outcomes` |
| ② 持仓盈亏与执行情况 | FR-11.2 | `portfolio_positions`（经 `PositionService.list_positions`）+ `execution_feedback`（人工回填 / FR-14.3.5） |
| ③ 大盘 / 板块周度总结 | FR-11.3 | 指数日线（同 P0 取数口径，周初收盘 vs 最新收盘）+ 板块热点（`MarketHotspotService`，尽力而为） |
| ④ 下周关注与关键价位 | FR-11.4 | 自选股 + 各标的**最新有效信号**的买入区间 / 止损 / 目标位 |

### 1.2 时间口径

- **本周** = 本周一 00:00 ~ 当前时刻（`week_window()`）；
- **周标识** `week_key` = 该周周一的 `YYYY-MM-DD`，与 `execution_feedback.week_key` 对齐，供执行回填跨端一致。

### 1.3 胜率口径（诚实呈现）

- 胜率 = **方向正确的已评估信号数 ÷ 已评估信号数**（依据 `outcome.direction_correct`）；
- 无已评估样本时，报告显式输出「暂无胜率样本 / 暂无已评估样本」，**不臆造数字**；
- 每条信号同时展示 `outcome`（hit/miss/neutral）与**信号后涨跌幅**（`stock_return_pct`）。

### 1.4 执行回填（FR-11.2 / FR-14.3.5）

| 项 | 说明 |
| --- | --- |
| 存储 | 新表 `execution_feedback`（**与 `decision_signal_feedback` 语义不同**：后者是"信号是否有用"，前者是"是否按建议执行"） |
| 取值 | `executed`（已执行）/ `ignored`（忽略）/ `later`（稍后） |
| 粒度 | `symbol + week_key` 唯一 → 同一标的同一周仅一条（后写覆盖） |
| 来源 | H5 复盘页按钮（`source=web`）；飞书对话回执可作扩展来源（`source=feishu`） |
| 展示 | 复盘页按标的给出三个状态按钮；报告尾注统计「已执行 N 只｜待回填 M 只」 |

> **D3 边界**：执行回填属**数据管理**（人工登记），不改动持仓 / 权重 / 止损，故**不经** `action_service` 写通道。

### 1.5 异常隔离与降级（NFR-8）

任一板块取数失败**不阻断整份报告**，只在 `notes` 标注「信号板块数据暂缺 / 持仓板块数据暂缺 / 大盘板块数据暂缺」，其余板块照常输出。单只标的失败仅跳过该条。

### 1.6 推送与调度（纯代码，不用 OS 计划能力）

```
常驻服务（python -m src.services.p1_service start）
   ├─ 每日 20:00  → P1 日推（既有）
   └─ 每周六 09:00 → 周度复盘（P3 新增，可配）
                       └─ 后台任务 _weekly_watch（5 分钟粒度）
                            ├─ 今天=目标星期？ 已过计划时刻？ 当天未跑过？（独立 ledger）
                            └─ → WeeklyReviewService.run() → 飞书推送
```

- **非侵入**：不改造共享的 `src/scheduler.py`，用 `add_background_task` 周期轮询 + 独立 ledger
  `logs/hslb_weekly.last_run` 保证「一天最多一次」；
- 时间优先级：`--weekly-time` > `HSLB_WEEKLY_REVIEW_TIME` > **周六 09:00**；
- 星期：`--weekly-weekday` > `HSLB_WEEKLY_REVIEW_WEEKDAY` > **周六**（接受 `0~6` 或 `sat` / `周六`）；
- 开关：`--no-weekly` / `HSLB_WEEKLY_REVIEW_ENABLED=false`；
- 单只/单板块失败不影响每日推送（周任务异常被捕获并记日志）。

---

## 2. 完整回测报告（FR-12）

### 2.1 新增能力

| 需求 | 实现 |
| --- | --- |
| FR-12.2 至少输出胜率/盈亏比/最大回撤/样本数 | P1 已具备；P3 通过 `POST /api/v1/backtest/rule-report` 对外暴露（含 `overall` 与分标的） |
| **FR-12.3 按场景（上涨/下跌/震荡）分档统计** | `Trade.scenario` 落库 → `_scenario_breakdown()` 分组聚合；报告新增「按场景分档表现」表（上涨/下跌/震荡/突破/变盘/未分类） |
| **FR-12.4 局限性与免责说明** | 报告新增「局限性与免责说明（FR-12.4）」章节（`LIMITATIONS` 常量，5 条） |
| FR-12.5 近 3 年 / 601919 + 601288 | 默认参数（`fetch_days=600 ≈ 793 根 ≈ 近 3 年`） |
| 完整报告落盘 | CLI `--report-out reports/backtest-YYYYMMDD.md` |

### 2.2 局限性说明（诚实口径）

1. 仅评估「建仓侧 + 出场风控」规则，不含持仓中的加仓 / 减仓时序；
2. 单一持仓假设（同一时间只持一仓），未模拟组合层面的资金分配；
3. 按「止损 / 目标价优先于到期离场」撮合，未计交易成本与滑点；
4. 免费数据源单标的最多约 800 根 K 线，回测区间上限 ≈ 近 3 年；
5. 历史表现不代表未来收益。

### 2.3 工程修正

- **JSON 安全**：`profit_factor = +∞`（无亏损）由数据类保留，`to_dict()` 层序列化为 `null`
  （否则 `Infinity` 不是合法 JSON，会破坏前端 `JSON.parse`）；渲染层仍显示语义。
- **标题兼容**：保留 P1 既有标题「规则体检报告」，避免破坏既有消费者（回归测试已覆盖）。

---

## 3. API 契约

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/review/weekly` | 生成本周复盘（`refresh_outcomes` / `limit_next_week` / `symbols`），返回结构化数据 + `markdown` |
| GET | `/api/v1/review/execution` | 读取某周执行回填（缺省本周） |
| POST | `/api/v1/review/execution` | 写入 / 覆盖执行回填（`symbol` / `execution_status` / `week_key?` / `note?`） |
| POST | `/api/v1/backtest/rule-report` | 规则回测报告（`symbols` / `fetch_days` / `horizon_days`） |

> 复盘为**只读聚合**；执行回填为数据管理写操作（非交易写通道）。

---

## 4. 配置项

| 位置 | 项 | 默认 | 说明 |
| --- | --- | --- | --- |
| `.env` | `HSLB_WEEKLY_REVIEW_ENABLED` | `true` | 周度复盘开关 |
| `.env` | `HSLB_WEEKLY_REVIEW_TIME` | `09:00` | 周度复盘时间 |
| `.env` | `HSLB_WEEKLY_REVIEW_WEEKDAY` | `sat` | 周度复盘星期（0~6 / mon..sun / 周一..周日） |
| `logs/hslb_weekly.last_run` | 周任务 ledger | 自动 | 已被 `.gitignore` 忽略 |
| `config/rules/backtest_rules.yaml` | `gate.*` / `backtest.*` | 见 P1 §4.5 | 回测阈值与参数（沿用 P1） |

**P3 未新增 `config.py` 配置项**（周任务参数经环境变量直读，保持 NFR-6 可配）。

---

## 5. 运行方式

```bash
# 周度复盘：生成不发送（演练）
python -m src.services.weekly_review_service --dry-run
python -m src.services.weekly_review_service --symbols 601919 513180   # 指定下周关注
python -m src.services.weekly_review_service --json                    # 结构化输出
python -m src.services.weekly_review_service --no-refresh-outcomes     # 不刷新信号结果（更快）

# 常驻服务（含每日推送 + 周六复盘）
python -m src.services.p1_service start                       # 默认 20:00 日推 + 周六 09:00 复盘
python -m src.services.p1_service start --weekly-time 08:30 --weekly-weekday sat
python -m src.services.p1_service start --no-weekly           # 关闭周任务
python -m src.services.p1_scheduler --no-weekly               # 前台常驻，便于观察日志

# 策略回测报告（FR-12）
python -m src.services.rule_backtest --symbols 601919 601288 --days 600 --horizon 15
python -m src.services.rule_backtest --report-out reports/backtest-20261014.md
```

---

## 6. 验证情况

| 文件 | 类型 | 覆盖 |
| --- | --- | --- |
| `tests/test_p3_weekly_review.py` | 新增 | 22 项：时间窗口 / 胜率与均值 / 场景取 metadata / 未评估计数 / 板块降级 / 结果读取异常 / 持仓周涨跌与盈亏 / 执行标签映射 / 大盘与下周关注 / 指定标的 / 渲染（四板块 + 免责）/ dry-run / 推送 / 无渠道 / 回填仓储（覆盖语义、大写归一、非法值、空参、按周列表与删除） |
| `tests/test_p3_review_api.py` | 新增 | 6 项：路由注册 / 回填写入与列表 / 覆盖 / 非法状态 400 / 缺省本周 / 复盘端点返回 markdown |
| `tests/test_p3_backtest_report.py` | 新增 | 7 项：`Trade.scenario` / 分档分组与排序 / 排除 trades / ∞ JSON 安全 / 报告可序列化 / 渲染含分档 + 局限 + 兼容标题 / 注入信号端到端 |
| `tests/test_p3_scheduler_weekly.py` | 新增 | 8 项：默认值 / 时间优先级 / 星期解析（含中文与越界回退）/ 开关 / 窗口判定（未到点、非目标星期、已跑）/ 非法时间 / 任务落 ledger / `run_service` 接收周参数 |
| `apps/dsa-web/src/pages/__tests__/ReviewPage.test.tsx` | 新增 | 3 项：四个板块渲染 / 执行回填提交 / 回测运行与结论（前端） |

**验收结果**

- 后端全量回归：**438 passed / 0 skipped**（P2.4 的 395 项**无回归**，P3 新增 43 项）。
- 前端 `tsc -b` **0 error**。
- 新路由已在 `/api/v1` 注册（OpenAPI：`/api/v1/review/weekly`、`/api/v1/review/execution`、`/api/v1/backtest/rule-report`）。

---

## 7. 回归结果

```
PYTHONPATH=. pytest --noconftest tests/test_p0_*.py tests/test_p1_*.py \
  tests/test_position_service.py tests/test_p2_*.py tests/test_p3_*.py -q
```

```text
=======================================================================
 438 passed in 82.38s (0:01:22)
=======================================================================
```

> **结果**：**438 passed / 0 skipped**（P2.4 基线 **395 项无回归**，P3 新增 **43 项**：22 + 6 + 7 + 8）。
> 明细同步记录于 `.workbuddy/memory/2026-10-14.md`。

---

## 8. 已知限制与后续

1. **大盘周度总结依赖数据源**：指数周涨跌走 P0 同口径取数（腾讯源）；板块热点依赖 `MarketHotspotService`，失败时静默跳过该部分（不阻断报告）。
2. **执行回填以标的 + 周为粒度**：不做"逐信号 × 逐笔交易"的精细对账；`signal_id` 字段已预留，后续可细化。
3. **回测仍只评估建仓侧 + 出场**：不含持仓中加仓 / 减仓时序（见 §2.2）。
4. **周任务窗口精度 5 分钟**：调度循环 30s 轮询、周任务 300s 检查；周六 09:00 附近的执行落在窗口内即触发（一天最多一次）。
5. **前端构建/单测**：本机 esbuild 受限，需用户在自有终端执行 `npm run build` / `npm test`。

---

*（本文档为 P3 实施说明 v1.0，与需求 v0.7、方案 v1.3、P1 实施 v1.3、P2 实施 v1.4 配套。）*
