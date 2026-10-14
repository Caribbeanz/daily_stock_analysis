# 高抛低吸助手 · P2 实施说明（v1.2：P2.0 移动底座 + P2.1 对话·读 + P2.2 对话·写）

| 项 | 内容 |
| --- | --- |
| 文档状态 | **P2.0 ✅ / P2.1 ✅ / P2.2 ✅ 已实施**；P2.3（结构化记忆）/ P2.4（审计与实时）待实施 |
| 版本 | v1.2（v1.0 = 仅 P2.0；v1.1 并入 P2.1；v1.2 并入 P2.2） |
| 更新日期 | 2026-10-13 |
| 上游方案 | `docs/solution/high-sell-low-buy-assistant-p2-design-v1.1.md`（§3.1 / §3.2 / §3.6 / §3.7 / §4 / §6 决议） |
| 上游需求 | `docs/requirements/high-sell-low-buy-assistant-requirements-v0.7.md`（FR-13.1 / FR-14.0.2~.4 / FR-14.2.1 / FR-14.2.4~.6 / FR-14.3.1~.9 / FR-16.2 / FR-7 / FR-9 / NFR-6 / NFR-8 / NFR-9 / NFR-11） |
| 前置 | P0 ✅ + P1 ✅（commit `0701bf75`）；P2.0 ✅（commit `1ac291ec`）；P2.1 ✅（commit `49f154be`） |
| 本环境约束 | 前端 **esbuild** 派生的 `vite build` / `vitest` 被本机文件访问策略拦截（`winapi error #5`），构建与前端单测需在**你自己的终端**执行 |

---

## 0. 本期范围与边界

| 项 | P2.0 做 | P2.0 **不做**（留给后续子期） |
| --- | --- | --- |
| 移动布局 | `useMediaQuery` + `MobileShell`（底部四 Tab + 更多抽屉），桌面端沿用侧边栏 | — |
| 个股详情 | 新建 `/m/stocks/:code`：人话结论 + 四要素 + 迷你 K 线 + 多因子明细 + 事件与风险 + 降级说明 | 参数调节表单（**P2.2 已交付，见 §10**） |
| 推送跳转 | 推送内附 H5 深链（`HSLB_H5_BASE_URL` 配置后生效） | — |
| 高频操作 | 一键加/移自选（≤3 步：链接直达 → 一键切换） | **持仓编辑**：属写操作，按决议 **D3** 统一走 `action_service` + 二次确认 → **P2.2 已交付（§10）** |
| 对话 / 记忆 / 审计 | — | P2.1 对话·读（v1.1 交付，见 §9）；**P2.2 对话·写（v1.2 交付，见 §10）**；P2.3 记忆；P2.4 审计与实时 |

> **为什么持仓编辑不放在 P2.0**：决议 D3 明确"**不给 Agent/对话加写通道，写操作统一收敛到 `intent + action_service` 单一写点**"。若 P2.0 单独开一个 HTTP 写口，会出现第二个写路径，与 D3 相悖；因此 P2.0 只做**读**与**既有写口复用**（自选股增删已有端点）。P2.2 才按 D2/D3 正式建立**唯一写点**。

---

## 1. 本期交付（P2.0 文件清单）

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

> **P2.2 扩展（见 §10.4）**：新增 `factor_weights`（当前四因子权重）与 `synthesis`（加权合成结论 + 各维度得分）两组字段，用于详情页展示"权重 × 维度 → 综合判断"。

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
| L1 | ~~**持仓编辑**未做~~ → **已交付（P2.2）**：写操作统一走 `action_service` + 二次确认（§10） | ✅ P2.2 |
| L2 | ~~参数 / 权重调节、`/actions/preview` 未做~~ → **已交付（P2.2）**：`/actions/preview` `/execute` `/cancel`（§10） | ✅ P2.2 |
| L3 | ~~对话入口未接线~~ → **已交付（P2.1）**：H5「就地问」+ `POST /chat/ask` + 飞书只读入站 | ✅ P2.1 |
| L4 | 结构化记忆（`ChatMemory`）、记忆管理页未做 | P2.3 |
| L5 | `ActionAudit` **持久化**与 SSE 端间刷新未做（P2.2 已预留 `audit_sink` 钩子，未落库） | P2.4 |
| L6 | 行情源：本机仅腾讯源可用；实时行情失败时详情页显示"行情暂不可用"（已降级不报错） | 数据侧 |
| L7 | 事件板块依赖情报源配置；未配置时显示"今日无重大事件提醒" | 配置侧 |
| L8 | 前端构建 / 前端单测需在本机终端执行（本环境 esbuild 受限） | 环境侧 |

---

## 8. 需求 → 实现映射

| 需求 | 实现位置 |
| --- | --- |
| FR-7 四因子权重可配 | `config/rules/factor_weights.yaml` + `RuleOverrideService.set_factor_weights/adjust_factor_weights`（§10.2） |
| FR-9 单标的止损覆盖 | `config/rules/stop_loss_overrides.yaml` + `risk_engine.compute_stop_loss(override=...)`（§10.2） |
| FR-14.0.2 端间跳转 | `DailyDigest.detail_url()` + `P1PushService._resolve_h5_base_url()` + `/m/stocks/:code` 路由 |
| FR-14.0.3 统一规范（涨红跌绿） | `index.css` 语义变量 + `tailwind.config.js` token + `MiniKLineChart` / `StockDetailPage` |
| FR-14.0.4 响应式 | `useMediaQuery` + `MobileShell` + `Shell` 自动切换 |
| FR-14.2.1 个股详情 | `StockDetailPage` + `GET /stocks/{code}/overview` + `MiniKLineChart` |
| FR-14.2.4 写操作预览 | `ActionService.plan()` + `POST /actions/preview`（返回确认令牌） |
| FR-14.2.5 高频操作 ≤3 步 | 推送深链直达（1 步）→ 一键加/移自选（2 步）；详情页「调整」（表单→预览→确认） |
| FR-14.2.6 二次确认 | `ConfirmationStore`（TTL 令牌，一次性消费）+ `ActionService.execute(confirmed=True)`（§10.3） |
| FR-14.3.1 双入口 | H5 确认卡（`ActionConfirmCard`）+ 飞书本人 `open_id` 回复「确认/取消」 |
| FR-14.3.3 写后回执 | `ActionService` 逐条 `describe()` + `ChatWriteService` 回执模板 |
| FR-14.3.4 触发分析（只读） | 白名单外，不属写操作 |
| FR-14.3.7 复合指令 | `resolver.parse_write_ops()` 逐条拆分 + 共享 `context` 顺序累积（§10.1） |
| FR-14.3.9 越界拒绝 | `REJECTED_OPS`（清空全部 / 仓位比例 / 下单类）明确拒绝并给替代路径 |
| FR-20 事件与风险 | `EventSource.collect([symbol])` → `overview.events` |
| NFR-6 配置化 | 阈值 / 权重 / 止损覆盖全部落在 `config/rules/*.yaml`，改配置不改代码 |
| NFR-8 失败隔离 | `StockOverviewService` 各子块 try/except → `degraded`；`ActionService` 逐条独立、一条失败不影响其余 |
| NFR-9 四端同源 | 结论与四要素取自 `decision_signals`；写操作统一落 `portfolio_*` / `config/rules`（单一写点） |
| NFR-11 可追溯 | `ActionService(audit_sink=...)` 预留审计钩子（P2.4 接持久化与查询） |
| D2 写操作白名单不外扩 | `_WHITELIST_HINT` + `REJECTED_OPS`；全部写操作强制二次确认 |
| D3 单一写点 | 所有写操作经 `ActionService`，无旁路 HTTP 写口 |
| D4 飞书权限 | 本人 `open_id`（`FEISHU_OWNER_OPEN_ID`）可写；群内其他人写请求回复无权限 |
| D6（仅内网） | 无外网映射 / 无 HTTPS / 无 CI；`degraded` 与推送文案自包含兜底 |
| D7（不引入 CI） | 前端沿用本地 `npm build` → `static/` |

---

*（本文档为 P2 实施说明 v1.2：含 P2.0 移动底座 + P2.1 对话·读 + P2.2 对话·写。P2.3 / P2.4 完成后将继续升版并在 §7 勾销对应待办。）*

---

## 9. P2.1 · AI 对话（读）实施说明

> 目标（方案 §4）：**能问**。范围严格限定为**读**——问答 / 名词解释 / 个股深度分析 / 持仓体检。

### 9.1 交付清单

| 模块 | 路径 | 职责 |
| --- | --- | --- |
| 名词表 | `src/services/glossary.py` | 需求 v0.7 §3 全部 **21 条**名词，精确/别名/关键词查询；纯函数 + CLI |
| 意图解析 | `src/agent/intent/resolver.py` | 自然语言 → 结构化意图；**规则优先 + LLM 兜底（可注入）** |
| 读对话编排 | `src/services/chat_read_service.py` | 意图 → 分流 → 回答；**强制附风险提示**；失败隔离降级 |
| 读 API | `api/v1/endpoints/chat.py` → `POST /api/v1/chat/ask`、`GET /api/v1/chat/glossary` | 统一读入口（无写副作用） |
| 飞书入站 | `src/services/feishu_inbound.py` → `POST /api/v1/chat/feishu/events` | 事件回调 → open_id 白名单 → 只读回复 |
| H5 入口 | `StockDetailPage`「就地问」+ `src/api/chat.ts` | 详情页带上下文跳对话（复用对话页 `?stock=` 追问入口） |

### 9.2 意图分流（FR-14.3 / FR-16.2）

| 意图 | 通道 | 复用 |
| --- | --- | --- |
| `glossary` 名词解释 | **本地名词表直接作答**（零延迟、零成本、不走 Agent） | `glossary` |
| `qa` 日常问答 / 个股现状 | 轻量对话 | `build_agent_chat_executor` |
| `deep_analysis` 个股深度分析 | 多智能体流水线 | 同上（`hslb_mode_hint=full`） |
| `portfolio_review` 持仓体检 | 组合语义 | 同上（`hslb_mode_hint=portfolio`） |
| `write_intent` 写操作 | **P2.1 只识别、不执行**；P2.2 起接入 `action_service`（见 §10） | —（决议 D3） |
| `out_of_scope` 下单/委托 | **明确拒绝 + 替代路径**（FR-14.3.9） | — |

### 9.3 关键规则

1. **规则优先**：可判定场景不消耗 LLM；规则命中时**不外呼** LLM（单测锁定）。
2. **合规（FR-14.3.8）**：`glossary` / `qa` / `deep_analysis` / `portfolio_review` 一律附风险提示；
   写回执与越界拒绝不附（因其不含买卖判断）。
3. **权限（决议 D4）**：飞书**本人 open_id 白名单**（`FEISHU_OWNER_OPEN_ID`）→ 可读；
   群内其他人 → 只读，写请求回复"无权限"。
4. **D3 铁律**：P2.1 **未新增任何写路径**——对话层对写指令只回执，真正执行在 P2.2 的 `action_service`（§10）。
5. **失败隔离（NFR-8）**：回答器异常 → `degraded=true` + 友好兜底文案，且**不冒充**分析结论（不附风险提示）。

### 9.4 新增配置

| 变量 | 说明 |
| --- | --- |
| `FEISHU_OWNER_OPEN_ID` | 飞书机器人对话的本人 open_id 白名单（命中者视为可写；P2.1 写操作仍未开放） |

### 9.5 验收

- 后端全量回归 **265 passed / 0 skipped**（P2.0 时 195 项 + P2.1 新增 70 项；无回归）。
- 前端 `tsc -b` **0 error**。
- 离线覆盖：意图分类（27 例）、读编排（10 例）、飞书入站（10 例）、名词表（9 例）、API 契约（7 例）。

---

## 10. P2.2 · 对话·写 实施说明

> 目标（方案 §4）：**能改**。落点严格遵循决议 **D2 / D3**——**白名单不外扩**、**全部写操作二次确认**、**单一写点 `action_service`**、**不给 Agent 加写工具**。

### 10.1 交付清单

| 模块 | 路径 | 职责 |
| --- | --- | --- |
| 规则覆盖层 | `src/services/rule_override_service.py` | 四因子权重 + 单标的止损覆盖的**原子读写**（`config/rules/*.yaml`）；`set_factor_weights` / `adjust_factor_weights` / `set_stop_loss` / `remove_stop_loss` |
| 因子合成 | `src/services/factor_weight_service.py` | `derive_dimension_scores()` / `synthesize()` / `render_synthesis()`：权重 × 维度 → 综合判断；**缺失维度显式排除、不计权**（诚实降级） |
| 写意图参数化 | `src/agent/intent/resolver.py` | `parse_write_ops()`：复合指令**逐条拆分** + 参数抽取（标的 / 权重维度 / 止损价 / 成本与数量） |
| **写执行门面（单一写点）** | `src/services/action_service.py` | `plan()` 预览 → `execute(confirmed=True)` 执行 → 逐条回执；`OP_LABELS` / `REJECTED_OPS` / 审计钩子 |
| 二次确认令牌 | `src/services/confirmation_store.py` | `ConfirmationStore`（TTL **300s**、**一次性消费**、`latest_for_actor`、可注入时钟）；`is_confirm_text` / `is_cancel_text` |
| 对话·写编排 | `src/services/chat_write_service.py` | `preview` / `confirm` / `cancel` / `has_pending` / `handles_text`；确认卡文案模板 |
| 飞书写确认 | `src/services/feishu_inbound.py`（扩展） | 本人 `open_id` 回复「确认 / 取消」→ 执行 / 撤销；非本人写请求仍拒绝 |
| 写 API | `api/v1/endpoints/actions.py` | `POST /actions/preview` `/execute` `/cancel`（模块级 `ConfirmationStore` 单例） |
| 前端确认卡 | `apps/dsa-web/src/components/actions/ActionConfirmCard.tsx` | 预览 → 二次确认 → 执行 → 回执 UI |
| 前端 API | `apps/dsa-web/src/api/actions.ts` | `actionsApi.preview/execute/cancel` |
| 详情页入口 | `apps/dsa-web/src/pages/StockDetailPage.tsx`（扩展） | 「调整」面板：止损 / 成本 / 数量输入 → 预览 → `ActionConfirmCard` |

### 10.2 规则覆盖层（FR-7 / FR-9 / NFR-6）

> **配置化优先（NFR-6）**：权重与止损覆盖全部落在 `config/rules/*.yaml`，**改配置不改代码**。

| 文件 | 内容 | 默认 |
| --- | --- | --- |
| `config/rules/factor_weights.yaml` | 四因子权重（`technical` / `market` / `news` / `fundamental`） | **45 / 25 / 15 / 15**（合计 1.0） |
| `config/rules/stop_loss_overrides.yaml` | 单标的止损覆盖（`symbol → price`） | 空 |

**风控接线**：`risk_engine.compute_stop_loss(snapshot, position=None, *, stop_loss_override=None)`——命中覆盖时**优先采用用户设定止损**（并以 `close` 为上界封顶）；`P0SignalBuilder` 懒加载 overrides 并在 `_evaluate_risk` 透传。

**权重调整语义**：`set_factor_weights` 要求**合计 = 1.0**（严格校验，破坏合计即 `InvalidRuleOverride`）；`adjust_factor_weights(changes, base=...)` 用于"只改一个维度"——把差值**按比例重分摊**到其余维度，保证合计恒为 1.0。

### 10.3 二次确认流程（FR-14.2.6 / 决议 D2）

```
用户: "把技术面权重调到 60% 并把 601919 止损设为 11.5"
  │
  ├─① 解析  resolver.parse_write_ops → [set_weight(technical,0.6), set_stop_loss(601919,11.5)]
  ├─② 预览  ActionService.plan() → ActionPlan（逐条"将要做什么"，含拒绝项）
  │        → ConfirmationStore.issue(actor, plan) → 返回确认令牌（TTL 300s）
  ├─③ 确认  H5：确认卡点击；飞书：本人回复「确认」
  │        → is_confirm_text 命中 → ConfirmationStore.pop(token)（一次性）
  └─④ 执行  ActionService.execute(confirmed=True) → 逐条 apply → 逐条回执
             「✅ 技术面权重 45%→60%（当前：技术60/大盘10/消息15/基本15）」
             「✅ 601919 止损已设为 11.50」
```

**硬约束**

1. **无确认不落库**：`execute` 必须显式 `confirmed=True`（由上层令牌校验后传入），否则拒绝。
2. **令牌一次性 + TTL**：确认令牌 `pop` 即失效，超时（300s）自动作废。
3. **逐条独立**：一条失败不影响其余（复合指令逐条回执）。
4. **失败不改状态**：`plan()` 阶段即对非法指令给出拒绝项，**不进入执行**。

### 10.4 写操作白名单（决议 D2，**不外扩**）

| op | 动作 | 复用服务 | 二次确认 |
| --- | --- | --- | --- |
| `watchlist_add` | 加入自选股 | `WatchlistService` | **是** |
| `watchlist_remove` | 移出自选股 | `WatchlistService` | **是** |
| `set_position` | 设置 / 覆盖持仓 | `PositionService` | **是** |
| `remove_position` | 清空持仓 | `PositionService` | **是** |
| `set_stop_loss` | 设置单标的止损（覆盖） | `RuleOverrideService` | **是** |
| `remove_stop_loss` | 移除止损覆盖 | `RuleOverrideService` | **是** |
| `set_weight` | 调四因子权重 | `RuleOverrideService` | **是**（先预览） |

**明确拒绝（`REJECTED_OPS`，FR-14.3.9）**：`clear_all` 清空全部（风险过高，需手工处理）、`set_position_ratio` 仓位比例（不在白名单）、未识别写指令、**任何"直接下单 / 委托"类请求**——一律给"做不到"的明确说明与替代路径，不得臆造。

### 10.5 权限与入口（决议 D4）

| 入口 | 读 | 写 |
| --- | --- | --- |
| H5（内网，免登录） | ✅ | ✅（确认卡二次确认） |
| 飞书 · 本人 `open_id`（`FEISHU_OWNER_OPEN_ID`） | ✅ | ✅（回复「确认」后执行） |
| 飞书 · 群内其他人 | ✅ | ❌ **只读**，写请求回复"无权限" |

### 10.6 合规（FR-14.3.8）

- 涉及**买卖判断**的回复**强制附风险提示**；
- **写操作回执**与**越界拒绝**不附（因其不含买卖判断）——与 P2.1 口径一致；
- 写操作执行前**必须回显"将要做什么"**（预览），执行后**逐条回执"做了什么 + 当前状态"**。

### 10.7 测试与验收

| 文件 | 类型 | 覆盖 |
| --- | --- | --- |
| `tests/test_p2_rule_override.py` | 新增 | 19 项：权重原子读写 / 合计校验 / 重分摊 / 止损覆盖 / 非法值 |
| `tests/test_p2_action_service.py` | 新增 | 17 项：白名单 / 拒绝项 / 未确认拒绝 / 逐条独立 / 顺序累积 / 审计钩子 |
| `tests/test_p2_chat_write.py` | 新增 | 12 项：预览 / 确认 / 取消 / 令牌超时与一次性 / 回执文案 |
| `tests/test_p2_actions_api.py` | 新增 | 6 项：`/preview` `/execute` `/cancel` 契约 |
| `tests/test_p2_chat_intent.py` | 扩展（+8） | 复合指令拆分 / 参数抽取（成本+数量 / 权重维度 / 止损价） |
| `tests/test_p2_feishu_inbound.py` | 扩展 | 本人「确认 / 取消」；非本人写请求仍拒绝 |
| `src/components/actions/__tests__/ActionConfirmCard.test.tsx` | 新增 | 预览 → 确认 → 执行 → 回执（前端） |

**验收结果**

- 后端全量回归 **329 passed / 0 skipped**（P2.1 时 265 项 + P2.2 新增 64 项；**无回归**）。
- 前端 `tsc -b` **0 error**。
- **D3 校验**：未新增任何旁路写口——所有写操作（H5 / 飞书 / 对话）均经 `ActionService`。
