# 高抛低吸助手 · P2.0 移动底座 实施说明（v1.0）

| 项 | 内容 |
| --- | --- |
| 文档状态 | **P2.0 已实施**（移动布局层 + 个股详情页 + 推送深链）；P2.1~P2.4 待实施 |
| 版本 | v1.0 |
| 更新日期 | 2026-10-13 |
| 上游方案 | `docs/solution/high-sell-low-buy-assistant-p2-design-v1.1.md`（§3.1 / §3.6 / §3.7 / §4 P2.0 / §6 决议） |
| 上游需求 | `docs/requirements/high-sell-low-buy-assistant-requirements-v0.7.md`（FR-14.0.2 / FR-14.0.3 / FR-14.0.4 / FR-14.2.1 / FR-14.2.5 / NFR-8 / NFR-9） |
| 前置 | P0 ✅ + P1 ✅（commit `0701bf75`） |
| 本环境约束 | 前端 **esbuild** 派生的 `vite build` / `vitest` 被本机文件访问策略拦截（`winapi error #5`），构建与前端单测需在**你自己的终端**执行 |

---

## 0. 本期范围与边界

| 项 | P2.0 做 | P2.0 **不做**（留给后续子期） |
| --- | --- | --- |
| 移动布局 | `useMediaQuery` + `MobileShell`（底部四 Tab + 更多抽屉），桌面端沿用侧边栏 | — |
| 个股详情 | 新建 `/m/stocks/:code`：人话结论 + 四要素 + 迷你 K 线 + 多因子明细 + 事件与风险 + 降级说明 | 参数调节表单（P2.2） |
| 推送跳转 | 推送内附 H5 深链（`HSLB_H5_BASE_URL` 配置后生效） | — |
| 高频操作 | 一键加/移自选（≤3 步：链接直达 → 一键切换） | **持仓编辑**：属写操作，按决议 **D3** 统一走 `action_service` + 二次确认 → **P2.2** |
| 对话 / 记忆 / 审计 | — | P2.1 对话·读；P2.2 对话·写；P2.3 记忆；P2.4 审计与实时 |

> **为什么持仓编辑不放在 P2.0**：决议 D3 明确"**不给 Agent/对话加写通道，写操作统一收敛到 `intent + action_service` 单一写点**"。若 P2.0 单独开一个 HTTP 写口，会出现第二个写路径，与 D3 相悖；因此 P2.0 只做**读**与**既有写口复用**（自选股增删已有端点）。

---

## 1. 本期交付（文件清单）

### 1.1 后端

| 文件 | 类型 | 作用 |
| --- | --- | --- |
| `src/services/stock_overview_service.py` | 新增 | 个股详情聚合：`build_factors()` / `build_overview()` **纯函数** + `StockOverviewService` 编排（依赖可注入、各子块失败隔离）+ CLI |
| `api/v1/schemas/stocks.py` | 修改 | 新增 `StockOverviewResponse` / `StockOverviewFactor` / `StockOverviewQuote` |
| `api/v1/endpoints/stocks.py` | 修改 | 新增 `GET /api/v1/stocks/{code}/overview?with_quote=` |
| `src/services/daily_digest.py` | 修改 | `DailyDigest.h5_base_url` + `detail_url()`；紧急/重点项追加 `↗ 详情 {base}/m/stocks/{symbol}?from=push`（**默认空则完全不变**） |
| `src/services/p1_push_service.py` | 修改 | `h5_base_url` 注入 + `_resolve_h5_base_url()`（显式 > Config > `HSLB_H5_BASE_URL` > 空） |
| `src/config.py` | 修改 | 新增 `hslb_h5_base_url`（读 `HSLB_H5_BASE_URL`） |
| `.env.example` | 修改 | 新增「P2 H5 移动端（内网直连）」说明段 |

### 1.2 前端（`apps/dsa-web`）

| 文件 | 类型 | 作用 |
| --- | --- | --- |
| `src/hooks/useMediaQuery.ts` | 新增 | `useMediaQuery` / `useIsMobile` / `MOBILE_QUERY`（≤1023px，与 Tailwind `lg` 对齐；**首屏同步求值**，避免移动端闪一下桌面布局） |
| `src/components/mobile/MobileShell.tsx` | 新增 | 移动端壳层：底部四 Tab（自选/持仓/对话/设置）+「更多」抽屉（复用 `SidebarNav`，保证所有页面可达）+ 底部安全区 |
| `src/components/mobile/MiniKLineChart.tsx` | 新增 | 纯 SVG 迷你 K 线（蜡烛 + MA5/MA20 + 成交量），**涨红跌绿**，无第三方依赖 |
| `src/components/mobile/index.ts` | 新增 | 桶文件 |
| `src/pages/StockDetailPage.tsx` | 新增 | 个股详情页（H5 主入口） |
| `src/components/layout/Shell.tsx` | 修改 | 按视口自动切换：移动端 → `MobileShell`，桌面端 → 原侧边栏布局 |
| `src/App.tsx` | 修改 | 新增路由 `/m/stocks/:code` |
| `src/api/stocks.ts` | 修改 | 新增 `stocksApi.getOverview()` / `getHistory()` 及类型 |
| `src/index.css` | 修改 | 新增涨跌语义变量 `--price-up/--price-down/--price-flat`（含暗色提亮） |
| `tailwind.config.js` | 修改 | 注册 `price-up` / `price-down` / `price-flat` 颜色 token |
| `src/i18n/uiText.ts` | 修改 | 新增 `mobile.*`（7 键）与 `stockDetail.*`（31 键）中英文案 |
| `src/hooks/index.ts` | 修改 | 导出新 hook |

### 1.3 测试

| 文件 | 类型 | 覆盖 |
| --- | --- | --- |
| `tests/test_p2_stock_overview.py` | 新增 | 21 项：格式化、多因子明细、四要素、降级、注入与失败隔离、CLI |
| `tests/test_p2_stock_overview_api.py` | 新增 | API 契约（路由注册 / 响应模型 / 400 / 500 包装）；依赖链不全时**整体跳过** |
| `tests/test_p2_push_deeplink.py` | 新增 | 10 项：无基址时**文案与 P1 完全一致**、深链形态、尾斜杠归一、观望项不加链接、**文案自包含**（D6-I1）、基址解析优先级 |
| `src/pages/__tests__/StockDetailPage.test.tsx` | 新增 | 8 项：结论 + 四要素、涨红、因子折叠、事件、降级提示、来自推送、加自选 |
| `src/components/mobile/__tests__/MiniKLineChart.test.tsx` | 新增 | 5 项：渲染 / 空态 / 无效数据 / 涨红跌绿 / 图例 |
| `src/hooks/__tests__/useMediaQuery.test.tsx` | 新增 | 3 项：首屏同步、查询变化、无 `matchMedia` 降级 |

---

## 2. 架构落点

```
飞书推送(P1) ──► /m/stocks/{code}?from=push ──┐
                                              ▼
                            Shell（按视口自动切换）
                              ├─ 桌面端：SidebarNav 侧边栏（原样）
                              └─ 移动端：MobileShell（底部四 Tab + 更多）
                                              │
                                              ▼
                                    StockDetailPage
                                              │
                       ┌──────────────────────┴──────────────────────┐
                       ▼                                             ▼
        GET /stocks/{code}/overview                    GET /stocks/{code}/history
        （结论 + 四要素 + 因子 + 事件 + 降级）              （近 60 日日线 → 迷你 K 线）
                       │
              StockOverviewService
         ┌─────────────┼──────────────┬───────────────┐
         ▼             ▼              ▼               ▼
  DecisionSignalService  EventSource  StockService   （全部懒加载、失败隔离）
  （四端同源：结论与四要素的唯一来源 · NFR-9）
```

**关键约束**

1. **四端同源（NFR-9）**：详情页的结论与四要素**不重算**，一律取自已落库的 `decision_signals`——与推送、AI 建议页同一份数据；避免"推送说减仓、H5 说持有"。
2. **失败隔离（NFR-8）**：行情 / 事件任一失败**只降级不报错**，并把原因写入 `degraded` 显示给用户；无信号时结论显示"暂无决策信号"而非空白。
3. **移动端不新建工程（D1）**：同一套 React 工程 + 媒体查询切换壳层；两端同一份路由与 API。

---

## 3. 接口与数据结构

### 3.1 `GET /api/v1/stocks/{code}/overview`

| 参数 | 位置 | 说明 |
| --- | --- | --- |
| `stock_code` | path | 代码（`601919` / `sh601919` / `HK00700` 等，经 `normalize_stock_code` 规范化） |
| `with_quote` | query | 是否附实时行情，默认 `true`；离线场景可置 `false` |

**响应要点**（`StockOverviewResponse`）

| 分组 | 字段 |
| --- | --- |
| 结论 | `conclusion`（人话结论，顶部大字号）、`action/action_label`、`scenario/scenario_label`、`reason`、`score`、`confidence`、`is_urgent` |
| 四要素 | `entry_low/entry_high/entry_zone`、`stop_loss`、`target_price`、`position_ratio/position_ratio_label/position_reason`、`discipline`（纪律卡一行） |
| 依据 | `factors[]`（`{key,label,value,tone}`，tone ∈ positive/negative/warning/neutral）、`watch_conditions`、`risk_summary` |
| 事件 | `events[]` |
| 行情 | `quote{current_price,change_percent,update_time}`、`distance_to_stop_pct`、`distance_to_target_pct` |
| 溯源 | `signal_id`、`signal_created_at` |
| 降级 | `degraded[]`（数据缺失原因，**显式说明**）、`disclaimer` |

**`degraded` 的典型取值**

- `暂无该标的的决策信号：请先运行每日信号（P1 推送）或等待下一次分析落库`
- `行情暂不可用：现价与距止损/目标距离已隐藏`
- `事件源不可用，已省略事件板块` / `读取决策信号失败，已按"暂无信号"展示`

### 3.2 多因子明细（`factors`）的取值来源

全部来自 `decision_signals` 记录本身（`metadata` / `evidence`），**不引入新的计算口径**：

| 因子键 | 来源 | 取值示例 |
| --- | --- | --- |
| `scenario` | `metadata.scenario` | 震荡趋势 |
| `trend` | `metadata.trend` | 向上（positive）/ 向下（negative） |
| `weekly_trend` | `metadata.weekly_trend` | 向上 |
| `multi_period_conflict` | `metadata.multi_period_conflict` | 共振（方向一致）/ 存在冲突 |
| `score` | `score` | 62/100 |
| `confidence` | `confidence` | 0.80 |
| `box_high` | `metadata.box_high` | 13.05 |
| `risk_reasons` | `evidence.risk_reasons` | 已浮盈 8% |
| `scenario_reasons` | `evidence.reasons` | 日线放量滞涨；接近前高 |

---

## 4. 推送 → H5 深链（FR-14.0.2 / D6-I1）

```bash
# .env
HSLB_H5_BASE_URL=http://192.168.1.10:8000
```

配置后，**紧急 / 重点**项在纪律卡下方追加一行：

```
1. 中远海控(601919)｜减仓（震荡趋势） 理由：接近箱体上沿
   → 仓位建议 30% ｜ 参考价位 12.10~12.80 ｜ 止损 11.50 ｜ 目标 14.20
   · 风控依据：已持有 500 股，触及分批止盈第一档
   ↗ 详情 http://192.168.1.10:8000/m/stocks/601919?from=push
```

**三条硬约束**

1. **不配置 = 完全不生成链接**：文案与 P1 逐字一致（已用单测锁定，避免回归）。
2. **文案自包含（D6-I1）**：手机不在内网时链接打不开，因此结论、四要素、事件**必须仍在推送正文里**——链接只是"增强"，不是信息的唯一载体。
3. **观望项不加链接**：避免刷屏（观望项本就折叠展示）。

---

## 5. 测试与验证

| 项 | 结果 |
| --- | --- |
| 后端全量回归 | `PYTHONPATH=. pytest --noconftest tests/test_p0_*.py tests/test_p1_*.py tests/test_position_service.py tests/test_p2_*.py` → **195 passed, 1 skipped**（P0/P1 的 164 项**无回归**） |
| 跳过项说明 | `tests/test_p2_stock_overview_api.py` 需完整依赖链（`api.v1.endpoints.stocks` 会连带 litellm / json_repair / newspaper 等），轻量环境自动 skip；装齐 `requirements.txt` 后执行 |
| 前端类型检查 | `node_modules/.bin/tsc -b` → **0 error**（含新增页面、组件、hook 与前端测试） |
| 后端 CLI 自检 | `python -m src.services.stock_overview_service 601919 --no-quote` → 正确输出结论 / 四要素 / 因子 / 事件（已在真实库上跑通） |
| 前端 `vite build` / `vitest` | **本环境受限**：esbuild 派生进程读取文件被拦截（`Cannot read file "package.json": winapi error #5`，`--no-sandbox` 亦然），需在**你自己的终端**执行 `npm install && npm run build` / `npm test` |

> **提示**：前端依赖已安装到 `apps/dsa-web/node_modules`（已被 `.gitignore` 忽略，不入库）。若你本机执行 `npm run build` 成功，产物会落到根 `static/`，由 FastAPI 直接托管（D7：不引入 CI）。

---

## 6. 运行方式（内网直连 · D6）

```bash
# 1) 启动服务（同一进程同时提供 API 与 H5 静态产物）
python -m api.app            # 监听 0.0.0.0:8000

# 2) 手机与本机连同一 WiFi，访问：
#    http://<本机内网IP>:8000/m/stocks/601919
```

**使用前提（务必知晓）**

| # | 前提 | 说明 |
| --- | --- | --- |
| 1 | 手机与主机**同一局域网** | 收不到就检查 WiFi 是否同段（如 `192.168.1.x`） |
| 2 | 主机**防火墙放行** 8000 端口 | Windows 首次监听会弹窗，需选"允许" |
| 3 | **不做公网映射**（D6 决议） | 4G/5G 下链接**打不开**——所以推送文案必须自带完整摘要 |
| 4 | 免登录 | 沿用现状（内网 + 单机使用，无账号体系） |

---

## 7. 限制与后续

| # | 限制 / 待办 | 归属 |
| --- | --- | --- |
| L1 | **持仓编辑**（FR-14.2.2）未做：写操作须走 `action_service` + 二次确认 | **P2.2** |
| L2 | 参数 / 权重调节、`/actions/preview` 未做 | P2.2 |
| L3 | 对话入口（H5 内嵌 + 飞书读写分流）未接线 | P2.1 |
| L4 | 结构化记忆（`ChatMemory`）、记忆管理页未做 | P2.3 |
| L5 | `ActionAudit` 审计与 SSE 端间刷新未做 | P2.4 |
| L6 | 行情源：本机仅腾讯源可用；实时行情失败时详情页显示"行情暂不可用"（已降级不报错） | 数据侧 |
| L7 | 事件板块依赖情报源配置；未配置时显示"今日无重大事件提醒" | 配置侧 |
| L8 | 前端构建 / 前端单测需在本机终端执行（本环境 esbuild 受限） | 环境侧 |

---

## 8. 需求 → 实现映射

| 需求 | 实现位置 |
| --- | --- |
| FR-14.0.2 端间跳转 | `DailyDigest.detail_url()` + `P1PushService._resolve_h5_base_url()` + `/m/stocks/:code` 路由 |
| FR-14.0.3 统一规范（涨红跌绿） | `index.css` 语义变量 + `tailwind.config.js` token + `MiniKLineChart` / `StockDetailPage` |
| FR-14.0.4 响应式 | `useMediaQuery` + `MobileShell` + `Shell` 自动切换 |
| FR-14.2.1 个股详情 | `StockDetailPage` + `GET /stocks/{code}/overview` + `MiniKLineChart` |
| FR-14.2.5 高频操作 ≤3 步 | 推送深链直达（1 步）→ 一键加/移自选（2 步）；持仓编辑见 L1 |
| FR-20 事件与风险 | `EventSource.collect([symbol])` → `overview.events` |
| NFR-8 失败隔离 | `StockOverviewService` 各子块 try/except → `degraded` |
| NFR-9 四端同源 | 结论与四要素取自 `decision_signals`（`DecisionSignalService.get_latest_active`），不重算 |
| D6（仅内网） | 无外网映射 / 无 HTTPS / 无 CI；`degraded` 与推送文案自包含兜底 |

---

*（本文档为 P2.0 实施说明 v1.0；P2.1~P2.4 完成后将升版并在 §7 勾销对应待办。）*
