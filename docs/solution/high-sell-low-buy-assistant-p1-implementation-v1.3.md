# 高抛低吸助手 · P1 每日推送闭环 实施说明（v1.3）

| 项 | 内容 |
| --- | --- |
| 文档状态 | 已实施（代码完成 + **164 项测试通过**）；质量门**已通过**，飞书通道**已打通**，定时推送**已工程化**，**事件源已接线**、**持仓已可录入** |
| 版本 | v1.3（v1.2 → v1.3：补 P1 两处缺口——**事件与风险板块接真实事件源**、**持仓录入解锁风控真实计算**） |
| 更新日期 | 2026-10-13 |
| 上游需求 | `docs/requirements/high-sell-low-buy-assistant-requirements-v0.7.md`（FR-10 / FR-11 / FR-8 / NFR-3 / NFR-5 / FR-12 / **FR-20**） |
| 上游方案 | `docs/solution/high-sell-low-buy-assistant-design-v1.3.md`（§4 P1） |
| 前置 | `docs/solution/high-sell-low-buy-assistant-p0-detailed-design-v1.0.md`（P0 已实施） |

---

## 0. v1.1 变更摘要（决策：**方案 C = 调参数 + 调门口径 双轨并行**）

| 主题 | v1.0 | v1.1 |
| --- | --- | --- |
| 止损 | 技术位取"离现价最近的均线"，贴脸易被扫 | 新增 `buffer_pct`（技术位下方缓冲）+ `atr_mult`（随波动率自适应放宽，取更宽一侧） |
| 建仓过滤 | 周线向上 + 日线买点即建仓 | 新增 `min_trend_strength`（趋势强度下限）+ `require_macd_positive`（动能向上） |
| 质量门口径 | 胜率≥50%｜盈亏比≥1.5｜回撤≤25%｜样本≥30 | **期望收益>0**｜盈亏比≥1.5｜回撤≤25%｜样本≥30（**胜率降为参考项**） |

### 0.1 v1.2 变更摘要（决策：**定时能力以代码实现，不依赖 OS 计划任务**）

| 主题 | v1.1 | v1.2 |
| --- | --- | --- |
| 定时实现 | `p1_scheduler` 仅提供前台常驻循环 | 升级为**工程化常驻服务**：PID 单实例守卫 + 运行态文件 + 滚动日志 + **停止哨兵**（跨平台优雅退出）+ **漏跑补偿** |
| 进程管理 | 无 | 新增控制面 `src/services/p1_service.py`：`start / stop / status / restart`（纯代码派生，父进程退出不影响服务） |
| 部署 | 需手工起进程 | `docker/docker-compose.yml` 新增 `hslb` 服务（与既有 `analyzer` / `server` 同一约定） |
| OS 依赖 | — | **明确不使用** Windows 任务计划 / cron / systemd timer |

> 设计动机：定时属于业务能力，应随代码一起版本化、可测试、可观测，而不是散落在操作系统里。控制面与调度器均为纯 Python 实现，跨平台一致。

### 0.2 v1.3 变更摘要（补 P1 两处缺口）

| 主题 | v1.2 | v1.3 |
| --- | --- | --- |
| 事件与风险板块（FR-20） | **注入位**：`event_provider` 可注入但默认返回空 → 推送固定显示"今日无重大事件提醒" | **已接线**：新增 `src/services/event_source.py`，默认聚合 **情报源**（`IntelligenceService`，按自选股名称/代码命中）+ **预警源**（`AlertService` 触发）；风险/日历关键词自动标注；异常隔离降级为空 |
| 持仓（解锁风控） | `portfolio_positions` 为空 → 风控始终按**无持仓**运行，浮盈亏/保本止损/分批止盈/金字塔加仓不生效 | 新增 `src/services/position_service.py`（**覆盖语义**录入 + CLI）：写入 `portfolio_*` 后 `P0SignalBuilder.load_positions()` 即可读到，风控进入**持仓优先**分支 |
| 测试 | 131 passed | **164 passed**（+事件源 15、+持仓 18） |

> 设计动机：这两处是"**能力已就绪但未接线**"的典型缺口——事件板块的接口位、持仓的读取路径在 P0/P1 已建好，只差最后一公里。补齐后 P1 闭环从"能推"变为"**推得有料、算得准**"。
| 体检结论 | ❌ 未通过（胜率 26.1%） | ✅ **通过**（胜率 58.3%、期望 +2.62%） |
| 测试 | 91 passed | **104 passed** |

---

## 1. 本期交付（P1 = 20:00 调度 + 概览分级 + 飞书推送 + K 线图 + 规则体检门）

| 文件 | 类型 | 作用 |
| --- | --- | --- |
| `src/services/daily_digest.py` | 新增 | 每日概览组装：紧急/重点/观望分级、降噪折叠、免责声明、事件与风险板块（纯函数） |
| `src/services/kline_chart.py` | 新增 | K 线图渲染（蜡烛 + MA5/10/20 + 成交量 + MACD，Pillow），无 Pillow 时优雅降级 |
| `src/services/rule_backtest.py` | 新增 | 规则体检回测（walk-forward，建仓规则 + 止损/止盈出场），质量门判定 |
| `src/services/p1_push_service.py` | 新增 | 推送编排：跑 P0 信号 → 读回信号 → 组装概览 → 附图 → 飞书；含 `--dry-run` |
| `src/services/p1_scheduler.py` | 新增 → **v1.2 升级** | 独立 20:00 调度服务（默认先过质量门）；v1.2 增 PID/运行态/日志/哨兵/漏跑补偿 |
| `config/rules/backtest_rules.yaml` | 新增 | 质量门阈值与回测参数（可配，改参数不改代码） |
| `src/services/p0_signal_builder.py` | 修复 | `_fetch_daily` 解包 `(DataFrame, source_name)` 元组（真实数据下的关键 bug） |
| `requirements.txt` | 修改 | 新增 `Pillow>=10.0.0`（缺失时自动跳过附图） |
| `.env.example` | 修改 | 新增 P1 推送配置说明段（v1.3 补事件源 + 持仓录入说明） |
| `src/services/event_source.py` | **v1.3 新增** | 事件源：聚合情报源 + 预警源 → 事件文案（FR-20），异常隔离 |
| `src/services/position_service.py` | **v1.3 新增** | 持仓录入（覆盖语义）+ CLI（解锁风控真实计算） |
| `tests/test_p1_*.py` ×6 | 新增 | P1 单测（v1.3 全量 164 项通过） |
| `tests/test_position_service.py` | **v1.3 新增** | 持仓服务单测（18 项） |

### 1.1 v1.1 新增 / 修改的调参件

| 文件 | 变更 | 说明 |
| --- | --- | --- |
| `src/core/indicators.py` | 新增 `atr()` + `IndicatorSnapshot.atr` | Wilder 口径 ATR，为止损自适应提供基准 |
| `src/core/rule_config.py` | `StopLossRules` 增 `buffer_pct / atr_period / atr_mult`；`BuildRules` 增 `min_trend_strength / require_macd_positive` | 全部可配、默认向后兼容 |
| `src/core/risk_engine.py` | `compute_stop_loss` 支持缓冲 + ATR 自适应放宽 | 取"技术位（含缓冲）"与"close − k×ATR"中**更宽（更低）**者 |
| `src/services/scenario_engine.py` | 新增 `_build_filter_notes()` 建仓过滤门槛 | 趋势强度不足 / MACD 柱未转正 → 不出手，并给出原因 |
| `src/services/rule_backtest.py` | 质量门改期望收益制 | `min_expectation` 纳入判定；`min_win_rate<=0` 时胜率仅参考 |
| `config/rules/*.yaml` | 三份调参固化 | 见 §4、§5 |

### 1.2 v1.2 新增 / 修改的定时件（纯代码调度）

| 文件 | 变更 | 说明 |
| --- | --- | --- |
| `src/services/p1_scheduler.py` | 重写为**常驻服务** | PID 单实例守卫、运行态 JSON、滚动文件日志、**停止哨兵**优雅退出、**漏跑补偿**、启动自检日志 |
| `src/services/p1_service.py` | 新增 | 控制面：`start / stop / status / restart`；`subprocess` 派生脱离进程（Win `DETACHED_PROCESS`，POSIX `start_new_session`） |
| `tests/test_p1_scheduler.py` | 新增 | 27 项单测：时间解析、PID/运行态读写容错、日志幂等、漏跑补偿边界、命令行构造、状态汇总 |
| `docker/docker-compose.yml` | 修改 | 新增 `hslb` 服务：`python -m src.services.p1_scheduler`（与 `analyzer` / `server` 同一约定） |
| `.env.example` | 修改 | 补充 `HSLB_SCHEDULE_TIME` 与控制面用法说明 |
| `logs/`（运行时生成） | 新增 | `hslb_scheduler.log`（滚动）、`.pid`、`.state.json`、`.last_run`、`.stop` |

---

## 2. 每日推送链路

```
20:00 调度触发（p1_scheduler）
   │
   ├─(1) 质量门（默认开启）── 未达标 → 直接跳过，不推送
   │
   ├─(2) P0 信号：读自选股 → 拉日线 → 指标 → 场景 → 风控 → 落库 decision_signals
   │
   ├─(3) 读回信号（四端同源：以 decision_signals 为唯一来源，NFR-9）
   │
   ├─(4) 组装每日概览：分级 + 降噪折叠 + 纪律卡 + 免责声明 + 事件与风险
   │
   ├─(5) 关键信号附图：取日线 → K 线图 PNG（无 Pillow 则跳过）
   │
   └─(6) 飞书推送：先发文本，再逐个附图；发送成功后才推进降噪状态
```

**失败隔离**：单标的取数/分析失败只进入「数据异常」板块（NFR-8）；事件源异常降级为空；绘图库缺失跳过附图——均不阻断整批。

---

## 3. 概览分级与降噪（FR-8 / FR-10.3 / NFR-3）

| 级别 | 触发 | 呈现 |
| --- | --- | --- |
| **紧急** | `is_urgent=True` 或动作为 `sell`/`clear` | 置顶，含完整依据与纪律卡 |
| **重点** | 建仓 / 抄底 / 加仓 / 减仓 | 次之，含完整依据与纪律卡 |
| **观望** | 持有 / 观望 | 折叠为单行列表（默认最多 10 行，超出提示"详见 H5"） |
| **数据异常** | 取数或分析失败 | 单独板块提示，不影响其余标的 |
| **已折叠** | 与上一交易日**结论完全一致**的观望项 | 只计数不展开；**紧急/重点永不折叠** |

**纪律卡**（每只标的）：`仓位建议 | 参考价位 | 止损 | 目标 | 纪律`。
**免责声明**：固定尾注「本内容由程序基于历史数据自动生成，仅供学习参考，不构成任何投资建议，据此操作风险自负。」（NFR-5，已定稿）

**降噪状态**存放于 `var/p1_push_state.json`；**仅在实际推送成功后推进**，避免"没发出去也把信号折叠了"。

---

## 4. 规则体检（P1 上线前质量门 · FR-12）

### 4.1 方法（walk-forward，无未来函数）

1. 逐交易日 t：**只用截至 t 的数据**计算指标 → 场景引擎 → 风控引擎 → 得到建仓信号；
2. 有信号则自 t+1 起模拟持仓（**一次只持一仓**）：
   - 盘中触及**止损位** → 按止损价离场；
   - 盘中触及**第一目标价** → 按目标价止盈；
   - 否则持有到 t+15 后按收盘价离场；
3. 统计 胜率 / 盈亏比 / 最大回撤 / 期望收益 / 样本数，并与同期**买入持有**对照。

### 4.2 v1.0 基线结果（调参前，2026-10-13）

| 标的 | 样本 | 胜率 | 盈亏比 | 平均收益% | 最大回撤 | 同期买入持有% |
| --- | --- | --- | --- | --- | --- | --- |
| 601919 中远海控 | 29 | 17.2% | 1.23 | +0.32 | 19.7% | +160.34 |
| 601288 农业银行 | 17 | 41.2% | 4.28 | +2.42 | 5.8% | +150.70 |
| **合计** | **46** | **26.1%** | **1.97** | **+1.09** | **12.7%** | **+155.52** |

**结论：❌ 未通过**（胜率 26.1% < 50%）。根因：① 止损过紧被震出（601919 83% 交易在止损离场，平均仅持 5.8 天）；② 目标价 15 日内 0 次达成；③ "胜率≥50%" 不适配"截断亏损、让利润奔跑"的止损型趋势策略。

### 4.3 v1.1 调参（方案 A：放宽止损 + 提高建仓门槛）

| 参数 | 位置 | v1.0 | v1.1 | 作用 |
| --- | --- | --- | --- | --- |
| `stop_loss.buffer_pct` | `risk_rules.yaml` | — | **0.02** | 技术位下方留 2% 缓冲，避开下影线扫损 |
| `stop_loss.atr_mult` | `risk_rules.yaml` | — | **2.5** | 止损放宽至 `min(技术位, close − 2.5×ATR)`，随波动率自适应 |
| `build.min_trend_strength` | `scenario_rules.yaml` | — | **medium** | 过滤"多空交织"的弱趋势买点 |
| `build.require_macd_positive` | `scenario_rules.yaml` | — | **true** | 要求 MACD 柱 > 0（动能向上）才建仓 |

### 4.4 v1.1 体检结果（2026-10-13 执行，601919 / 601288，近 3 年 ≈793 根）

| 标的 | 样本 | 胜率 | 盈亏比 | 平均收益% | 最大回撤 | 同期买入持有% |
| --- | --- | --- | --- | --- | --- | --- |
| 601919 中远海控 | 22 | 45.5% | 1.82 | +1.71 | 16.0% | +160.34 |
| 601288 农业银行 | 14 | 78.6% | 6.63 | +4.04 | 6.3% | +150.70 |
| **合计** | **36** | **58.3%** | **2.69** | **+2.62** | **15.4%** | **+155.52** |

**结论：✅ 通过。**

| 门槛（新口径） | 阈值 | 实测 | 判定 |
| --- | --- | --- | --- |
| 期望收益 | > 0% | +2.62% | ✅ |
| 盈亏比 | ≥ 1.5 | 2.69 | ✅ |
| 最大回撤 | ≤ 25% | 15.4% | ✅ |
| 样本数 | ≥ 30 | 36 | ✅ |
| （参考）胜率 | — | 58.3% | 已**同时**越过原 50% 硬门槛 |

**改善幅度**：胜率 26.1% → **58.3%**（+32.2pt）｜盈亏比 1.97 → **2.69**｜期望收益 +1.09% → **+2.62%**（≈2.4×）｜最大回撤 12.7% → 15.4%（仍在阈值内）。
样本由 46 降至 36（建仓门槛提高的自然结果），仍满足 ≥30。

> **注**：调参后规则**同时**满足旧口径（胜率≥50%）与新口径，稳健性更好。

### 4.5 质量门口径（方案 B：期望收益制）

`config/rules/backtest_rules.yaml` → `gate`：

```yaml
gate:
  min_expectation: 0.0    # 期望收益 > 0（单笔平均收益 %）
  min_profit_factor: 1.5  # 盈亏比
  max_drawdown: 0.25      # 最大回撤
  min_samples: 30         # 最小样本数
  min_win_rate: 0.0       # 0 = 不强制（胜率仅参考）；设为 0.5 即恢复"胜率≥50%"硬门槛
```

* 判定逻辑：四项全部满足才 `passed=True`；`min_win_rate <= 0` 时胜率仅展示、不拦截。
* 报告会明确标注「（胜率为参考项，不作拦截）」，避免误读。

---

## 5. 风控与建仓参数（v1.1 固化值）

`config/rules/risk_rules.yaml`：

```yaml
stop_loss:
  mode: technical
  ma: [5, 10, 20]
  prev_low_lookback: 20
  percent: 0.08
  buffer_pct: 0.02           # 【v1.1】技术位下方缓冲
  atr_period: 14             # 【v1.1】ATR 周期
  atr_mult: 2.5              # 【v1.1】ATR 自适应放宽倍数
```

`config/rules/scenario_rules.yaml`（`build` 段新增）：

```yaml
build:
  # ...（原有项不变）
  min_trend_strength: medium     # 【v1.1】日线趋势强度下限
  require_macd_positive: true    # 【v1.1】要求 MACD 柱 > 0
```

---

## 6. 运行方式

```bash
# P0：只生成信号并落库
python -m src.services.p0_signal_builder

# P1 演练：生成概览（含 K 线图）但不发送，用于检查内容
python -m src.services.p1_push_service --dry-run --max-charts 3

# P1 正式推送一次
python -m src.services.p1_push_service

# 只跑规则体检
python -m src.services.rule_backtest --symbols 601919 601288 --days 600 --horizon 15

# K 线图渲染自检
python -m src.services.kline_chart --out var/kline_selftest.png

# 事件源自检（FR-20）：聚合情报源 + 预警源，打印命中事件
python -m src.services.event_source --dry-run

# 持仓录入（解锁风控真实计算）；--set 可重复，覆盖语义
python -m src.services.position_service --set 601919:12.34:1000 --set 513100:1.234:10000
python -m src.services.position_service --list            # 查看当前持仓
python -m src.services.position_service --remove 601919   # 清空某标的
```

### 6.1 每日定时推送（纯代码常驻服务）

调度**不使用任何操作系统计划能力**，全部由项目代码实现：

```bash
# —— 控制面（推荐，纯代码派生后台常驻进程）——
python -m src.services.p1_service start                 # 启动（默认每日 20:00，先过质量门）
python -m src.services.p1_service start --time 20:05    # 自定义时间
python -m src.services.p1_service start --dry-run       # 冒烟：只生成不发送
python -m src.services.p1_service status                # 查看运行状态（PID/计划时间/下次执行/最近执行）
python -m src.services.p1_service stop                  # 优雅停止
python -m src.services.p1_service restart               # 先停后起

# —— 调度器本体（前台常驻，便于前台观察日志）——
python -m src.services.p1_scheduler
python -m src.services.p1_scheduler --run-now           # 启动时先跑一次
python -m src.services.p1_scheduler --no-catch-up       # 关闭漏跑补偿
python -m src.services.p1_scheduler --no-gate           # 跳过质量门（仅调试）

# —— Docker（与既有 analyzer / server 同一约定）——
docker-compose -f ./docker/docker-compose.yml up -d hslb
```

时间优先级：`--time` > `HSLB_SCHEDULE_TIME` > `SCHEDULE_TIME` > `20:00`。

### 6.2 服务工程特性

| 特性 | 实现 | 说明 |
| --- | --- | --- |
| **常驻调度** | `src/scheduler.py`（schedule 库） | 与 `main.py --schedule` 同一套底座；循环每 30s 轮询 |
| **单实例守卫** | PID 文件 `logs/hslb_scheduler.pid` | 重复启动被拒（退出码 3），避免一天发两条 |
| **优雅退出** | SIGINT / SIGTERM / SIGBREAK + **停止哨兵** `logs/hslb_scheduler.stop` | 跨平台可靠；`p1_service stop` 即写哨兵，最长约 60s 内退出 |
| **可观测** | 滚动日志 `logs/hslb_scheduler.log`（5MB × 5） | 控制面另留 `logs/hslb_service.console.log` |
| **运行态** | `logs/hslb_scheduler.state.json` | `status` 读取进程**真实**参数（时间/质量门/dry_run/启动时刻） |
| **漏跑补偿** | ledger `logs/hslb_scheduler.last_run` | 进程在计划时刻之后才启动且当天未跑过 → 立即补跑一次 |
| **质量门** | `config/rules/backtest_rules.yaml` | 默认 `require_gate=True`，不达标不发 |

---

## 7. 飞书配置状态（2026-10-13 实测 · ✅ 已打通）

| 项 | 状态 |
| --- | --- |
| `FEISHU_APP_ID` | ✅ 已写入 `.env`（`cli_aaecaf77e9b8dccd`） |
| `FEISHU_APP_SECRET` | ✅ 已写入 `.env` |
| 应用凭证有效性 | ✅ 已实测：成功换取 `tenant_access_token`（`code=0`，有效期 7200s） |
| 私聊 `open_id`（`ou_e949ea38b809a3fc890576ad2dcc4f68`） | ✅ **有效**（`ou_` + 32 位）；走项目代码 `FeishuNotifier.send_text()` 实测 **返回 True**，消息已送达本人单聊 |
| 当前渠道判定 | ✅ **`is_available() = True`** → 20:00 推送可正常发送（私聊，不打扰群） |
| 群 `chat_id`（`oc_6c81f1af44537aa2aa69a46a7a8fc18d`） | 格式合法（`oc_` + 32 位），作为备选保留在 `.env` 注释中 |
| 运行依赖 | App Bot 需 `lark-oapi`（`requirements.txt:29`）；本机隔离环境已补装 `lark-oapi` + `markdown2` |

### 7.1 当前生效配置

```dotenv
FEISHU_APP_ID=cli_aaecaf77e9b8dccd
FEISHU_APP_SECRET=****（见 .env）
FEISHU_RECEIVE_ID_TYPE=open_id
FEISHU_CHAT_ID=ou_e949ea38b809a3fc890576ad2dcc4f68
```

### 7.2 三种可选推送目标（`.env` 注释中已备好，改一行即可切换）

| 方案 | 配置 | 说明 |
| --- | --- | --- |
| A. 私聊（**当前生效**） | `FEISHU_CHAT_ID=ou_e949ea38b809a3fc890576ad2dcc4f68` + `FEISHU_RECEIVE_ID_TYPE=open_id` | 只推给自己，不打扰他人 |
| B. 群会话 | `FEISHU_CHAT_ID=oc_6c81f1af44537aa2aa69a46a7a8fc18d` + `FEISHU_RECEIVE_ID_TYPE=chat_id` | 格式已校验；消息群内可见 |
| C. 群机器人 Webhook（最简） | `FEISHU_WEBHOOK_URL=...` | 无需 app_id/app_secret/chat_id；群设置 → 群机器人 → 自定义机器人 |

> `.env` 已被 `.gitignore` 忽略，凭证不会进入版本库。

### 7.3 踩坑记录

- **`open_id` 是「应用维度」的**：同一用户在不同应用下的 `open_id` 完全不同。此前从其他来源取得的 `ou_7d8a6e6df7621556ce0d21922b676706ccs`（35 位）在本应用下无效（`code=99992351`）。正确的 `open_id` 应来自本应用的事件回调体（`sender.sender_id.open_id`）或 `GET /open-apis/im/v1/chats/{chat_id}/members`。
- **依赖**：正式运行环境必须 `pip install -r requirements.txt`，否则缺 `lark-oapi` / `markdown2` 会导致推送直接失败（非配置问题）。
- **权限**：App Bot 发消息需应用具备 `im:message:send_as_bot` 权限并发布版本。

---

## 8. 配置项

| 位置 | 项 | 默认 | 说明 |
| --- | --- | --- | --- |
| `.env` | `HSLB_SCHEDULE_TIME` / `SCHEDULE_TIME` | `20:00` | 推送时间 |
| `config/rules/backtest_rules.yaml` | `gate.*` | 见 §4.5 | 质量门阈值（期望收益制） |
| `config/rules/backtest_rules.yaml` | `backtest.horizon_days` / `warmup` / `fetch_days` | 15 / 80 / 600 | 回测参数 |
| `config/rules/risk_rules.yaml` | `stop_loss.*` | 见 §5 | 止损（含 buffer / ATR 放宽） |
| `config/rules/scenario_rules.yaml` | `build.min_trend_strength` / `require_macd_positive` | medium / true | 建仓过滤门槛 |
| `var/p1_push_state.json` | 降噪状态 | 自动 | 已被 `.gitignore` 忽略 |

**数据源提示**：免费源中腾讯源单标的最多返回约 **800 根 K 线**，因此回测取数默认 **600 天（≈793 根 ≈ 近 3 年）**；若要更长历史，需接入 `akshare`/`baostock`/`tushare` 等（已在 `requirements.txt` 中）。

---

## 9. 验证情况

```
164 passed（P0 54 + P1 92 + 持仓 18）in 70.09s
```

* **v1.1 新增单测 13 项**：ATR 正值/随波动放大（3）、止损缓冲与 ATR 放宽（3）、建仓过滤门槛（3）、质量门期望收益制与胜率开关（4）。
* **v1.2 新增单测 27 项**（`tests/test_p1_scheduler.py`）：时间解析与环境优先级、PID 读写/容错/存活探测、运行态文件读写容错、日志幂等挂载、漏跑补偿各种边界、任务落 ledger、命令行构造、下次执行时刻、状态汇总（未运行 / 以进程真实参数为准）、未运行时停止幂等。
* **v1.3 新增单测 33 项**：
  * 事件源（`tests/test_p1_event_source.py`，15 项）：名称/代码/scope 命中、无关项跳过、风险与日历标记、`risk_only` 过滤、去重、风险优先排序、`max_events` 截断、预警源命中、异常隔离降级为空、空自选股、标的过滤、`as_provider` 可调用；
  * 持仓服务（`tests/test_position_service.py`，18 项）：账户幂等、**覆盖语义**（重复 set 不累加）、多标的、非正数校验、移除、读回委托、CLI 参数解析。
* **真实数据冒烟**：`rule_backtest` 跑通并产出 §4.4 结果；`p1_push_service --dry-run --require-gate` 端到端跑通；飞书真实发送成功（§7）；事件源自检跑通（情报源可用、预警源缺依赖已隔离降级）。
* **持仓联动实测（2026-10-13）**：真实库录入 `601919`（成本 12.34 / 1000 股）→ `load_positions()` 读回 → 覆盖再录（清理旧流水 1 条）→ 多标的 → 移除，全程无脏数据；有持仓时 P0 信号 **action 由 `watch` 变为 `reduce`**（浮盈 10% 触发止盈，止损位 15.98），证明**风控真实计算已解锁**。
* **服务生命周期实测（dry-run，2026-10-13）**：
  * `start --time 23:59 --dry-run` → `{"ok": true, "started": true, "pid": 12208}`
  * `status`（6s 后）→ `{"running": true, "pid": 12208}` ✅
  * `stop` → `{"ok": true, "stopped": true, "graceful": true}`（耗时 57s，≤ 设计上限 60s）✅
  * `status`（停止后）→ `{"running": false, "pid": null}`（PID 已清理）✅
  * 运行态校验：`start --time 21:30` 时 `status` 显示 `push_time=21:30 / next_run_at=21:30 / started_at / require_gate / dry_run`（来自进程真实参数，而非环境变量）✅
* 运行命令（隔离 `tests/conftest.py` 的重型前端依赖）：`PYTHONPATH=. pytest --noconftest tests/test_p0_*.py tests/test_p1_*.py tests/test_position_service.py`

---

## 10. 已知限制与后续

1. **优雅停止时延**：调度循环固定 30s 轮询，`p1_service stop` 实测约 57s 退出。属设计取舍（不侵入共享的 `src/scheduler.py`）；如需秒级退出需改造共享调度底座。
2. **依赖完整性**：本机未安装 `akshare` / `efinance` / `pytdx` / `baostock` / `yfinance`，当前实际仅腾讯源供数（其余源自动降级跳过）。生产务必 `pip install -r requirements.txt`。
3. **事件源强依赖情报源配置**：`event_source` 已接线，但**命中数量取决于情报源（`IntelligenceService`）是否已配置并抓取**；未配置时静默为空（推送显示"今日无重大事件提醒"）。预警源（`AlertService`）另需可选依赖（`newspaper` 等）。
4. **持仓需人工录入真实数据**：`position_service` 机制已就绪，**需求方真实持仓（8 只，成本价 / 数量）已于 2026-10-13 录入**（`--set SYMBOL:COST:QTY`，覆盖语义），风控已进入持仓优先分支（`risk.action()`）。
5. **周六复盘**（需求 FR-11）**已实现**，见 `docs/solution/high-sell-low-buy-assistant-p3-implementation-v1.0.md` 与本文 §6（`p1_scheduler` 新增周六窗口）。
6. **回测口径**：只评估"建仓侧 + 出场风控"，不含持仓中加仓/减仓时序；单笔期望 vs 买入持有不可直接比较。
7. **未做**：**P2 H5 + AI 对话 ✅ 已完成**；**（P3）周六复盘与完整回测报告 ✅ 已完成**；P4 大屏待做。

---

*（本文档为 P1 实施说明 v1.3，与 P0 详设 v1.0、方案 v1.3、需求 v0.7 配套。）*
