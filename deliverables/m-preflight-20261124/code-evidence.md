# 编码前 12 项实测 · 代码层静态事实（2026-11-24）

> 本文是**代码/配置/数据库实读证据**，与 `m-check-report.md`（脚本真实接口实测）分开存放。
> 静态事实**不能替代**接口实测；仅用于让 8 项 BLOCKED 在网络受限时仍有可判定的落地现状。
> 证据均为只读扫描（AST 解析 + 源码阅读 + SQLite 只读查询），未修改任何文件。

## 总览

| 项 | 代码层判定 | 关键证据 |
| --- | --- | --- |
| M5 | 已实现（仅板块名入参） | `src/services/screening/industry.py:215,227-229`、`src/services/screening_service.py:2794-2808` |
| M6 | 未实现（零调用，映射走同源反查） | `src/` 与 `data_provider/` 中 `stock_individual_info_em` 零命中；映射见 `industry.py:242-248` |
| M7 | 部分实现（只取 2 列，缺涨跌家数/领涨股） | `data_provider/akshare_fetcher.py:2218-2223`；`industry.py:586-596` |
| M9 | 部分实现（分红有、股息率自算、PE-PB 分位零调用） | `data_provider/fundamental_adapter.py:468-470`；`data_provider/base.py:4481` |
| M10 | 未实现长周期（默认窗口 120 日） | `src/services/screening/config.py:230,366`；`screening/daily.py:657-663` 用 `adjust="qfq"` |
| M11 | 未实现（8 张规划表全部不存在） | 数据库 37 张表实测无 `market_regime`/`rs_t`/`sector_strength` 等；复核记录 `:66` |
| M12 | 部分实现（取数/展示有，审计意见与连续期判定无） | `fundamental_adapter.py:395-397,440,454`；全库 py「审计意见」零命中 |
| M13 | 已实现（配置链+SDK+收发+渲染齐全，凭据待提供） | `requirements.txt:31`；`src/config.py:1410,2361`；`main.py:1571-1575`；`bot/platforms/feishu_stream.py:40-48,738,752`；`src/notification_sender/feishu_sender.py:138-147,635-641` |
| M14 | 部分实现（配置/fallback/超时有，专用降级模板无） | `src/core/config_registry.py:448-467`；`src/config.py:2508-2518,1021,1647-1651,1013-1014` |
| M15 | 部分实现（adj_factor 有接口无落库，周月线无表） | `src/services/screening/daily.py:856-878`；`stock_daily` 18 列无 `adj_factor`；无周/月表 |
| M16 | 部分实现（只取标题+日期，缺编号/链接） | `src/services/screening/candidate_context.py:229-248` 调 `stock_zh_a_disclosure_report_cninfo` |
| M17 | 部分实现（回补逻辑无，超时/并发保护有，库为空） | `screening/daily.py:52,367,373-374`；`screening_service.py:2803-2807,2355-2372`；`stock_daily` 0 行 |
| M18（附测） | 未实现 cron 解析（仅 HH:MM） | `requirements.txt:9` 仅 `schedule`；`src/scheduler.py:149-151,174-175`；全库 `croniter`/`apscheduler` 零命中 |

## 逐项证据

### M5 · 板块成分接口入参
- `src/services/screening/industry.py:215` 注册 `(industry, ak.stock_board_industry_name_em, ak.stock_board_industry_cons_em)`。
- `:227-229`：`board = board_item["name"]; members = cons_func(symbol=board)` —— **入参是板块名，不是代码**。
- `:586` `_board_items()` 取 `["板块名称","名称","name"]`，无板块代码列。
- 同名封装另见 `screening_service.py:2314-2323,2794-2808`、`hotspot.py:928-931`。
- **判定**：已实现，但只验证了板块名路径；"是否支持板块代码入参"须 `m_preflight_check.py --only M5` 在能出网环境实测。

### M6 · 行业字段口径
- 全库 Python 文件 `stock_individual_info_em` **零调用**（仅文档 4 处提及：需求 v1.0/v2.6、技术方案 v0.3、deliverables/a-share 页面架构）。
- 实际 code→板块映射为同源反查：`src/services/screening/industry.py:242-248`，遍历 `cons_func(symbol=board)` 成员，`_normalize_code(row.get("代码") or row.get("code"))`，写 `item["industry"] = board`。
- **判定**：未实现，符合"实测前严禁用于映射"的约束；接口真实口径须动态实测。

### M7 · 板块家数字段
- `data_provider/akshare_fetcher.py:2218-2223`：`ak.stock_board_industry_name_em()`，仅用 `涨跌幅` 与 `板块名称` 两列。
- `src/services/screening/industry.py:215` 调用同接口取板块列表；`:586-596` 只解析 name/rank/change_pct。
- `screening_service.py:2240-2250` 为自研同名方法（`_fetch_board_changes_with_fallback` / `_fetch_rankings_with_fallback` / `_fetch_board_names`）。
- **判定**：部分实现，R5 内部结构所需字段（上涨家数/下跌家数/领涨股）未取；缺列按 R5 降级"未取得"。

### M9 · 股息率/分红/PE-PB 分位
- 分红取数：`data_provider/fundamental_adapter.py:468-470` 调 `stock_fhps_detail_em`、`stock_history_dividend_detail`、`stock_dividend_cninfo`。
- 股息率为**计算值**：`data_provider/base.py:4481` `ttm_dividend_yield_pct = ttm_yield`；`yfinance_fundamental_adapter.py:364` 同类。
- `stock_a_indicator_lg` / `stock_a_alert_down` / `stock_zh_valuation` 全库零调用；PE 仅作筛选阈值（`screening/filter.py:69-70`）。
- **判定**：分红可得、股息率自算、PE-PB 分位无实现；§5.8 相应项须标缺失降置信度。

### M10 · 长周期序列
- 最长窗口 `DAILY_LOOKBACK_DAYS` 默认 120（`src/services/screening/config.py:230,366`）。
- 复权 `src/services/screening/daily.py:657-663` `ak.stock_zh_a_hist(period="daily", ..., adjust="qfq")`；腾讯源 `data_provider/tencent_fetcher.py:58` 亦 `qfq`。
- 无 hfq/none/adj_factor 长周期实现。
- **判定**：3–5 年序列未实现；长周期位置分位需新建取数，qfq 不等于真实复权因子。

### M11 · 风险历史序列
- SQLite 实测 37 张表，`market_regime`/`rs_t`/`sector_strength`/`market_snapshot`/`sector_snapshot`/`holding_conclusion`/`conditional_plan`/`risk_signals` **全部不存在**（与复核记录 `:66` 一致）。
- 同名 `market_regime` 在 `src/agent/skills/base.py:76`、`src/services/screening/strategy.py:369-372` 是**策略路由标签**，非风险判定历史序列——**不要混淆**。
- **判定**：未实现；§4.5「连续 N 日」与 rs_t 需从零建表与计算，免费源可得性仍须动态实测。

### M12 · 财报渐进恶化
- 取数 `data_provider/fundamental_adapter.py:395-397` 调 `stock_financial_abstract`、`stock_financial_analysis_indicator`；`:440,454` 调 `stock_yjyg_em`、`stock_yjkb_em`。
- 展示 `src/analyzer.py:4574,4588-4590` 有 `roe`、`net_profit_parent`、`operating_cash_flow`；`src/notification.py:2323-2344`。
- 全库 py 文件「审计意见/audit_opinion」**零命中**（`src/analyzer.py:580` 仅 Prompt 黑名单词"审计"）；无连续 N 期恶化判定。
- **判定**：部分实现；连续期与审计意见须 d 阶段实现，信号按契约标"数据不足"。

### M13 · 飞书 Stream
- 依赖 `requirements.txt:31` `lark-oapi>=1.0.0`。
- 配置 `src/config.py:1410` `feishu_stream_enabled: bool = False`；`:2361` 读 `FEISHU_STREAM_ENABLED`；`:1165-1168` chat_id/receive_id_type/domain；注册 `src/core/config_registry.py:2067`。
- 启动 `main.py:1571-1575` 条件启动 `start_feishu_stream_background`。
- 收发 `bot/platforms/feishu_stream.py:40-48` import `lark_oapi`、`P2ImMessageReceiveV1`、`ReplyMessageRequest`；`:738,752` 实际调用；Webhook 备选 `bot/handler.py:208-210`。
- 渲染 `src/notification_sender/feishu_sender.py:138-147` `{"tag":"lark_md","content":content}`；`:635-641` interactive 失败降级 `msg_type=text`；`src/formatters.py` 的 `format_feishu_markdown` 由 `feishu_stream.py:63` 引入。
- **判定**：配置链/SDK/收发/渲染代码齐全；**真实 Stream 连通、app bot 权限、emoji/lark_md 手机/桌面/网页三端渲染一致性必须人工实测**（需凭据）。

### M14 · LLM 渠道
- `src/core/config_registry.py:448-467` 定义 `LLM_CHANNELS`，示例 `deepseek,aihubmix`。
- `src/config.py:2508-2518` 解析 `LLM_CHANNELS` 与 per-channel `LLM_{NAME}_PROTOCOL/BASE_URL/API_KEY/MODELS/ENABLED`，渠道名为任意自定义名（Web 预设 `apps/dsa-web/src/components/settings/LLMChannelEditor.tsx`）。
- fallback `src/config.py:1021` `litellm_fallback_models`、`:1647-1651` `LITELLM_FALLBACK_MODELS`、`:906-911` 顺序；`:1013-1014` `generation_fallback_backend`、超时。
- 通用降级 `template_fallback`（`src/analyzer.py:4006`、`src/market_analyzer.py:810,900`），但**无需求定义的专用降级规则模板**。
- **判定**：配置与 fallback 链齐备；真实延迟/成本/20:00 前产出与目标环境渠道可用性须人工实测。

### M15 · 周月线与复权因子
- `src/services/screening/daily.py:856-878`：`pro.adj_factor(... fields="trade_date,adj_factor")`；`:878` `merged.drop(columns=["adj_factor"])` —— **仅内存计算 qfq/hfq，无落库**。
- SQLite：`stock_daily` 18 列（`id,code,date,open,high,low,close,volume,amount,pct_chg,ma5,ma10,ma20,volume_ratio,data_source,canonical_id,created_at,updated_at`）无 `adj_factor`；无 weekly/monthly 表。
- 除权事件表仅 `portfolio_corporate_actions`（`account_id/symbol/action_type/cash_dividend_per_share/split_ratio`），属**持仓账本**非行情复权事件表，0 行。
- 周/月线聚合仅设计于 `docs/technical-design/stock-analysis-assistant-technical-design-v0.3.md:260-261,302-306`，代码无实现。
- **判定**：部分实现；不可退回前复权落库，不可得时按 §4.6.4 退化为除权事件表推算。

### M16 · 公告接口字段
- `src/services/screening/candidate_context.py:229-248` 调 `ak.stock_zh_a_disclosure_report_cninfo(symbol, market="沪深京", start_date, end_date)`。
- `:244-245` 只取 `["公告标题","标题","announcementTitle","title"]` 与 `["公告时间","公告日期","date"]`，**无公告编号、原文链接、来源**。
- 未调用 `stock_notice_report`；`data_provider/fundamental_adapter.py:470` 的 cninfo 调用是分红用途。
- **判定**：部分实现；缺字段则 §5.15 降级"只显示标题+来源，无链接"。

### M17 · 回补耗时与限频
- 无 3 年首次回补逻辑；窗口上限见 M10（`DAILY_LOOKBACK_DAYS=120`）。
- 保护：`screening/daily.py:52` `_DAILY_HISTORY_CACHE_TTL_SECONDS=24*60*60`；`:22,367,373-374` `call_with_timeout`/`parse_source_timeout_seconds`；`screening_service.py:2803-2807` `_akshare_call_with_timeout(..., timeout=...)`；`:2355-2372` ThreadPoolExecutor + `_CONSTITUENT_WORKER_SLOTS` 信号量。
- 本地库：`data/stock_analysis.db` 1.36 MB / 37 表；`stock_daily`、`fundamental_snapshot`、`analysis_history`、`backtest_results`、`portfolio_corporate_actions` **均 0 行**。
- **判定**：部分实现；首次回补逻辑缺失，13 只全量耗时须在能出网环境实测（动态脚本 M17）。

### M18（附测）· cron 与门控
- `requirements.txt` 仅 `schedule>=1.2.0`（`:9`），无 `croniter`/`apscheduler`；全库 Python 两者零命中。
- `src/scheduler.py:149-151,174-175` 只校验/注册 HH:MM：`_is_valid_schedule_time` 与 `every().day.at(schedule_time)`；无 5 字段 cron 解析。
- cron 仅出现于 GitHub Actions、部署文档与技术方案设计。
- **本地实测（动态脚本 M18）**：`build_market_phase_context` 在交易日返回 `is_trading_day=True`，周六/国庆返回 `False`；未知市场 `xx` 时 `is_market_open=True`（fail-open）而 `ctx.is_trading_day=None`（fail-closed），**印证 R24 门控必须取后者**。
- **判定**：cron 解析未实现（须选型 `croniter`/APScheduler），交易日门控能力已具备且 fail-closed 得到实测支持。

## 结论（截至 2026-11-24 本机）

- **可判定（代码层）**：M6 未实现、M10 未实现长周期、M11 未实现、M13 配置链齐备、M15 部分实现、M16 部分实现、M17 部分实现、M18 cron 未实现但门控可用。
- **必须动态实测才能判定**：M5 板块代码入参、M6 行业字段真实口径、M7 板块字段、M9 分红/估值接口字段、M10 长周期真实可得性、M12 财报字段、M16 字段与返回量、M17 真实耗时、M13 飞书三端渲染、M14 LLM 延迟与成本。
- **本机阻断原因**：`akshare`/`tushare`/`croniter` 未安装；PyPI 经代理不可达（`pypi:000`）、腾讯行情接口返回 501、东财 `ProxyError`。上一轮基线回测所用腾讯 K 线当时可达，现已不可复现。
- **不得把静态事实写成接口实测通过**，也不得把 BLOCKED 写成 FAIL；待用户在能出网环境运行 `scripts/m_preflight_check.py` 后回填。
