# 高抛低吸助手 · P4 大屏看板 实施说明

| 项目 | 说明 |
| ---- | ---- |
| 文档状态 | **P4 ✅ 已实施**（FR-14.1 大屏：一屏快照 + 免登录全屏常驻 + 数据截止标注 + 手动/定时刷新） |
| 版本 | v1.0 |
| 更新日期 | 2026-10-14 |
| 上游需求 | `docs/requirements/high-sell-low-buy-assistant-requirements-v0.7.md`（FR-14.1 / FR-14.0.3 / FR-13.5 / NFR-9 / NFR-10 / NFR-12） |
| 上游方案 | `docs/solution/high-sell-low-buy-assistant-design-v1.3.md` §3.7 / §4（P4 大屏 · 二期） |
| 上游分期 | P0 ✅ → P1 ✅ → P2 ✅（P2.0~P2.4）→ P3 ✅（复盘 + 回测）→ **P4 ✅（大屏）** |

---

## 0. 本期交付

| 交付 | 内容 |
| --- | --- |
| 一屏快照服务 | `src/services/screen_service.py`（指数 / 自选股卡片 / 持仓汇总 / 事件 / 复盘摘要 + 数据截止） |
| 快照 API | `GET /api/v1/screen/snapshot`（只读） |
| 免登录常驻 | `/api/v1/screen/snapshot` 进入鉴权白名单；前端 `/screen` 路由在鉴权判断之前渲染 |
| 大屏页面 | `apps/dsa-web/src/pages/ScreenPage.tsx`（`/screen`，无 Shell、全屏、大字号、涨红跌绿） |
| 端间同步 | 复用 P2.4 的 SSE 广播，持仓 / 自选股变更后大屏自动重取（FR-14.0.5 / NFR-9） |
| 配置 | `HSLB_SCREEN_REFRESH_SECONDS`（建议刷新节奏）、`HSLB_SCREEN_CACHE_SECONDS`（快照短缓存） |

> 二期只做**大屏适配**：内容与 H5 **同源同口径**（FR-14.0.4），不新增业务口径。

---

## 1. 定位与范围（FR-14.1）

| 需求 | 落地方式 |
| --- | --- |
| **FR-14.1.1** 收盘后数据与报告的展示，**不做盘中实时监控/分析**（NG4） | 快照是**收盘后一次性聚合**，无行情推送、无 tick、无盘中循环；前端刷新间隔以分钟计（默认 300s，下限 30s），只读全景 |
| **FR-14.1.2** 功能范围与 H5 一致，仅为大屏优化布局（一屏铺开、字号放大、远距离观看） | 指数条 + 持仓汇总 + 自选股卡片（人话结论、四要素、评分、距止损/目标、持仓盈亏）+ 事件提醒 + 本周复盘摘要；卡片结论**直接复用 H5 的 `StockOverviewService`**（NFR-9） |
| **FR-14.1.3** 支持手动 / 定时刷新，并**显著标注数据截止时间** | 顶部固定展示「数据截止（最新交易日）/ 行情时间 / 生成时间」；`立即刷新`（强制绕过缓存）+ 自动刷新倒计时（间隔由后端 `refreshSeconds` 下发） |
| **FR-14.1.4** 浏览器全屏、**免登录常驻**，长期无人值守，**关键信息不得需要交互才能看到** | `/screen` 路由不套 Shell、不经鉴权分支；自带全屏按钮；四要素 / 持仓 / 事件 / 复盘全部**默认展开**，无折叠、无 Tab、无需要点击才可见的关键信息 |

**其它对齐项**

- **FR-14.0.3 / FR-13.5 统一视觉**：涨红跌绿（`text-price-up` / `text-price-down` / `text-price-flat`）、双层信息结构（人话结论 + 明细）、固定免责声明。
- **NFR-9 四端同源**：卡片结论来自 `decision_signals`（经由 H5 同一条聚合链），不重算指标；持仓来自 `portfolio_positions`；复盘摘要来自 P3 服务。
- **NFR-10 远距离可读**：核心数字 `text-3xl ~ text-4xl`（指数、价格），卡片标题 `text-2xl~3xl`，正文 `text-base~2xl`，`tabular-nums` 对齐。
- **NFR-12 内网 / 免登录**：大屏仅内网直连，不引入账号体系。

---

## 2. 后端：一屏快照服务

`src/services/screen_service.py`

### 2.1 数据结构

| 结构 | 说明 |
| --- | --- |
| `ScreenIndex` | 指数一项：`code / name / value / change_pct` |
| `ScreenCard` | 自选股卡片：行情（`price / change_pct / quote_time`）+ 结论（`action / action_label / scenario_label / conclusion / is_urgent / score`）+ 四要素（`entry_zone / stop_loss / target_price / position_ratio_label`）+ 相对价位（`distance_to_stop_pct / distance_to_target_pct`）+ 持仓（`has_position / quantity / avg_cost / pnl_pct / position_value`）+ `degraded` |
| `ScreenPortfolio` | 持仓汇总：`position_count / priced_count / total_cost / market_value / pnl_amount / pnl_pct / winners / losers / complete` |
| `ScreenSnapshot` | 一屏：`generated_at / market_date / quote_as_of / refresh_seconds` + 上述区块 + `action_counts / urgent_count / degraded / disclaimer` |

### 2.2 组装流程（`ScreenService.build`）

```
① 读持仓（portfolio_positions）        → 失败：降级为空 + degraded
② 解析标的 = 显式 symbols ‖ 自选股 ∪ 持仓（去重保序）
③ 并发组装卡片（ThreadPoolExecutor，默认 6 线程；单标的失败只影响该卡片）
     └─ 复用 StockOverviewService.load(symbol)（NFR-9，与 H5 完全同源）
④ 指数条：上证 / 深证 / 创业板（复用 P0 取数口径 → 日涨跌 + 最新交易日）
⑤ 持仓汇总：纯函数 summarize_portfolio（部分缺行情则 complete=false，**不臆造盈亏**）
⑥ 事件提醒：event_source.collect(全部标的) → 截取前 N 条
⑦ 本周复盘摘要：WeeklyReviewService.build()（注入空大盘/热点 provider，关闭结果刷新）
⑧ 汇总 action_counts（按固定动作序）、urgent_count、degraded、disclaimer
```

### 2.3 关键设计

1. **四端同源（NFR-9）**：卡片结论**不重算**，直接取 `StockOverviewService`（其数据源为已落库的 `decision_signals`）；指数复用 **P3 的 `DEFAULT_INDEX_CODES`** 与 P0 取数口径，避免"大屏说 X、H5 说 Y"。
2. **分块异常隔离（NFR-8）**：持仓 / 指数 / 事件 / 复盘任一失败只记入 `degraded` 并省略该区块，**不阻断整屏**；单只标的失败只影响该卡片。降级说明如实呈现（如"部分持仓缺少行情，盈亏暂不展示"）。
3. **诚实口径**：持仓汇总在**存在未取到行情的持仓**时，只给 `total_cost` 与 `complete=false`，**不给** `market_value / pnl_amount / pnl_pct`（避免半真半假的盈亏）。
4. **数据截止可见（FR-14.1.3）**：`market_date` 取指数日线的最后一根日期（最新交易日）；`quote_as_of` 取全部卡片行情时间的最大值并归一成 `YYYY-MM-DD HH:MM:SS`。
5. **可选快照缓存**：`cache_ttl_seconds > 0` 时按入参缓存快照（默认由配置给 15s），防止大屏手动刷新打爆数据源；API 的 `refresh=true` 走**无缓存**实例。
6. **只读**：本服务不写任何业务数据，不引入写通道（回归 D3 边界）。

---

## 3. 后端：API 与免登录

`api/v1/endpoints/screen.py`

```
GET /api/v1/screen/snapshot
    symbols[]      缺省 = 自选股 + 持仓
    with_quote     默认 true（离线排查可关）
    with_weekly    默认 true（附本周复盘摘要）
    limit_events   0~20，默认 6
    refresh        默认 false；true 时绕过服务端快照缓存
```

响应即第 2.1 节 `ScreenSnapshot`（Pydantic 强类型，OpenAPI 可见）。

**免登录（FR-14.1.4）**：`api/middlewares/auth.py` 的 `EXEMPT_PATHS` 增加 `/api/v1/screen/snapshot`。
理由与边界：该端点**只读**，仅聚合本人自选股 / 持仓的行情与结论，**不含任何写能力与密钥**；
鉴权若开启（可选能力），大屏仍需常驻可用。**其余 `/api/v1/screen/*` 路径不放行**（已由单测锁定）。

前端配套：`api/index.ts` 的 401 拦截器对 `/screen` 路径**不跳转登录页**，由页面自行降级展示。

---

## 4. 前端：大屏页面

`apps/dsa-web/src/pages/ScreenPage.tsx`（路由 `/screen`）

| 区块 | 说明 |
| --- | --- |
| 顶部条 | 标题 + **数据截止（大字号）** + 行情时间 + 生成时间 + 端间同步指示 + 自动刷新倒计时 + `立即刷新` + `全屏 / 退出全屏` |
| 指数条 | 3 个指数：名称 + 大字号数值 + 涨跌（含趋势图标，涨红跌绿） |
| 持仓汇总 | 总成本 / 市值 / 浮动盈亏 / 盈亏率 / 盈利÷亏损（含持仓只数） |
| 自选股卡片 | 名称·代码 / 大字号价格 / 涨跌 / 评分 / **动作 + 场景 + 人话结论** / 四要素 / 距止损·距目标 / 持仓（数量·成本·盈亏率）；紧急信号加红色边框与「紧急」标记 |
| 事件提醒 | 事件源聚合（空则"今日无重大事件提醒"） |
| 本周复盘 | 结论一句话 + 胜率 / 已评估·待评估 / 本周持仓盈亏 + 跳转完整复盘 |
| 底部 | **降级提示**（未取到的部分不伪造）+ 免责声明 |

**刷新机制**

- 首次进入强制刷新（`refresh=true`）；
- 自动刷新：间隔 = `?refresh=秒` ‖ 后端 `refreshSeconds`（下限 30s，默认 300s），
  顶部显示 `{n}s 后自动刷新`；
- `?auto=0` 关闭自动刷新（仍可手动刷新）；
- 端间同步：订阅 P2.4 的 `invalidate` 广播 → 立即强制刷新（FR-14.0.5）。

**URL 参数**（便于无人值守时的个性化常驻）

```
/screen                                  全部自选股 + 持仓，300s 自动刷新
/screen?refresh=120&auto=1               120s 刷新
/screen?symbols=601169,513100&weekly=0   只看指定标的、不取复盘摘要
/screen?auto=0&events=0                  纯手动刷新、不展示事件
```

**免登录落地**：`App.tsx` 在 `isLoading` / 鉴权判断**之前**短路渲染 `/screen`（不套 Shell，
无侧边栏），因此鉴权开启时也不会被重定向到登录页。侧边栏新增「大屏」入口便于发现。

---

## 5. 配置项

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `HSLB_SCREEN_REFRESH_SECONDS` | `300` | 下发给前端的**建议自动刷新间隔**（≥30）；纯展示节奏，与盘中监控无关 |
| `HSLB_SCREEN_CACHE_SECONDS` | `15` | 相同入参快照的服务端短缓存（0 = 关闭）；防手动刷新打爆数据源 |

> 已同步 `src/config.py` 与 `.env.example`。大屏无其它必填配置；`HSLB_H5_BASE_URL`
> 若已配置，复盘摘要里的"查看完整复盘"会带上内网绝对地址。

---

## 6. 运行方式

```bash
# 1) 起服务（大屏与 H5 同一服务）
python main.py --web            # 或既有 Web 启动方式

# 2) 浏览器打开（内网直连，免登录）
#   http://<内网IP>:8000/screen
#   想长期常驻：进入页面后按「全屏」，或浏览器 F11

# 3) 离线自检（不打网络/不写库）
python -m src.services.screen_service --json            # 完整快照
python -m src.services.screen_service --no-quote        # 跳过行情
python -m src.services.screen_service --no-weekly       # 不取本周复盘摘要
python -m src.services.screen_service --symbols 601169 513100
```

---

## 7. 验证情况

| 文件 | 类型 | 覆盖 |
| --- | --- | --- |
| `tests/test_p4_screen.py` | 新增 | 25 项：持仓汇总（足额/部分缺行情不臆造/空仓与零仓）/ 符号解析（合并去重、显式优先、自选股失败、持仓读取失败）/ 卡片映射（行情·四要素·持仓盈亏·名称兜底）/ 指数口径与最新交易日 / 指数全缺与取数异常降级 / 事件截断与降级 / 复盘摘要与降级与 H5 基址 / 单卡失败隔离 / 串行与并发两分支顺序 / 动作分布与紧急计数 / 降级透传 / `with_quote` 透传 / 快照缓存（命中·清空·默认关闭）/ 行情时间归一 |
| `tests/test_p4_screen_api.py` | 新增 | 9 项：路由注册 / 真实应用挂载路径 / 鉴权白名单（含尾斜杠与"其余路径不放行"）/ 完整响应 / 查询参数透传 / `limit_events` 越界 422 / `refresh` 强制刷新 / 服务异常→500 / 工厂读配置 |
| `apps/dsa-web/src/pages/__tests__/ScreenPage.test.tsx` | 新增 | 6 项：数据截止·指数·卡片·持仓·事件·复盘·免责渲染 / 涨红跌绿 / 紧急与持仓块 / 手动刷新 / URL 覆盖 / 加载失败提示 |

**验收结果**

- 后端全量回归：**472 passed / 0 skipped**（P3 基线 438 **无回归**，P4 新增 34 项）。
- 前端 `tsc -b`（`--force` 全量）**0 error**。
- 真实数据冒烟（2026-10-14）：8 只真实持仓 + 自选股聚合成功；三大指数取数正常
  （上证 3767.23 / -0.885%），`market_date=2026-10-14`；4 只 ETF 行情缺失时
  如实给出 `complete=false` + 降级说明，**未臆造盈亏**。
- 前端单测（`vitest`）需在**用户本机终端**执行：本环境 `esbuild` 派生进程被拦截
  （`Cannot read file "package.json": winapi error #5`），无法运行 `vitest` / `vite build`。

---

## 8. 回归结果

```text
=======================================================================
 472 passed in 97.31s (0:01:36)
=======================================================================
```

| 阶段 | 用例数 |
| --- | --- |
| P0 + P1 | 164 |
| P2.0 → P2.4 | 195 → 265 → 329 → 357 → 395 |
| P3 | 438 |
| **P4** | **472（438 + 34）** |

> P3 的 438 项**无回归**；P4 新增 34 项（25 服务层 + 9 API 契约）。

---

## 9. 已知限制与后续

1. **不做盘中监控**（设计如此）：大屏只展示收盘后数据；非交易日 / 盘前会显示上一交易日截止时间。
2. **行情覆盖取决于数据源**：本机仅腾讯源可用，部分 ETF 实时行情取不到 → 卡片显示"—"、
   持仓汇总 `complete=false` 并给出降级说明（**不伪造**）。
3. **快照缓存按入参维度**：不同 URL 参数各自缓存；命中缓存期间数据变更最多滞后
   `HSLB_SCREEN_CACHE_SECONDS`（默认 15s）—— 但端间写操作广播会触发 `refresh=true` 强制刷新。
4. **复盘摘要有额外取数成本**：会为每只持仓取一次日线（用于本周盈亏）。若追求极致轻量，
   用 `/screen?weekly=0` 关闭。
5. **大屏为只读展示**：所有修改仍在 H5 / 对话完成（D3 单一写点），大屏不提供写入口。
6. **未做**：多屏轮播、图表化 K 线放大展示（如需可在后续按需增量）。

---

*（本文档为 P4 实施说明 v1.0，与需求 v0.7、方案 v1.3、P1 实施 v1.3、P2 实施 v1.4、P3 实施 v1.0 配套。）*
