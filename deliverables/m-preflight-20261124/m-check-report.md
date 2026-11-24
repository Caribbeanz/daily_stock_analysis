# 股票分析助手 · 编码前实测前置报告

- 生成时间：2026-11-24T19:42:20+08:00
- 数据来源：本脚本真实调用 / 本地代码与数据库实读；**未使用缓存或推断冒充实测**
- 状态定义：PASS 通过 / PARTIAL 部分通过 / MANUAL 需人工凭据或客户端验证 / BLOCKED 环境阻断 / FAIL 未通过

| 项 | 内容 | 状态 | 结论 |
| --- | --- | --- | --- |
| M12 | M12 财报渐进恶化判定字段可得性 | **PASS** | 3/3 财报接口可用；代码库无审计意见字段、无连续 N 期恶化判定逻辑 |
| M18 | M18 cron 解析与交易日门控 fail-closed | **PASS** | cron 解析与门控均可用；未知市场时 is_market_open=True 而 ctx.is_trading_day=None，印证 fail-open 不可用于门控 |
| M11 | M11 风险判定历史序列可得性与存储 | **PARTIAL** | 风险判定专用表在代码中不存在；指数历史可取自免费源但需新建存储与聚合 |
| M15 | M15 周/月线与 adj_factor 可得性 | **PARTIAL** | adj_factor 有接口调用但无落库字段；周/月线无本地聚合表；免费源可提供周线 |
| M16 | M16 公告接口字段完整性与日返回量 | **PARTIAL** | 字段命中 3/4；单标的 7 日 ( 条 |
| M9 | M9 股息率/分红记录/PE-PB 分位接口 | **PARTIAL** | 2/3 个接口可用；股息率为自算值（data_provider/base.py:4481），非接口直出 |
| M13 | M13 飞书 Stream 配置链/SDK/凭据 | **MANUAL** | 配置链与 SDK 代码齐备；真实 Stream 收发与 emoji/lark_md 三端渲染需凭据 + 人工验证 |
| M14 | M14 LLM 渠道可用性/延迟/降级 | **MANUAL** | 渠道配置与 fallback 链齐备；真实延迟/成本/20:00 前产出需凭据 + 目标环境实测 |
| M10 | M10 3-5 年长周期序列与复权口径 | **BLOCKED** | 长周期取数失败 |
| M17 | M17 3 年首次回补耗时/增量更新 | **BLOCKED** | 3 年回补取数失败 |
| M5 | M5 stock_board_industry_cons_em 板块代码入参 | **BLOCKED** | 取板块列表失败 |
| M6 | M6 stock_individual_info_em 行业字段口径 | **BLOCKED** | 接口调用失败 |
| M7 | M7 stock_board_industry_name_em 涨跌家数/领涨股字段 | **BLOCKED** | 接口调用失败 |

---

## M5 · M5 stock_board_industry_cons_em 板块代码入参

- **状态**：BLOCKED
- **结论**：取板块列表失败
- **错误**：ProxyError: HTTPSConnectionPool(host='17.push2.eastmoney.com', port=443): Max retries exceeded with url: /api/qt/clist/get?pn=1&pz=100&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281&fltt=2&invt=2&fid=f3&fs=m%3A90+t%3A2+f%3A%2150&fields=f1%2Cf2%2Cf3%2Cf4%2Cf5%2Cf6%2Cf7%2Cf8%2Cf9%2Cf10%2Cf12%2Cf13%2Cf14%2Cf15%2Cf1

## M6 · M6 stock_individual_info_em 行业字段口径

- **状态**：BLOCKED
- **结论**：接口调用失败
- **错误**：ProxyError: HTTPSConnectionPool(host='push2.eastmoney.com', port=443): Max retries exceeded with url: /api/qt/stock/get?fltt=2&invt=2&fields=f120%2Cf121%2Cf122%2Cf174%2Cf175%2Cf59%2Cf163%2Cf43%2Cf57%2Cf58%2Cf169%2Cf170%2Cf46%2Cf44%2Cf51%2Cf168%2Cf47%2Cf164%2Cf116%2Cf60%2Cf45%2Cf52%2Cf50%2Cf48%2Cf167%2Cf117%2Cf7

## M7 · M7 stock_board_industry_name_em 涨跌家数/领涨股字段

- **状态**：BLOCKED
- **结论**：接口调用失败
- **错误**：ProxyError: HTTPSConnectionPool(host='17.push2.eastmoney.com', port=443): Max retries exceeded with url: /api/qt/clist/get?pn=1&pz=100&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281&fltt=2&invt=2&fid=f3&fs=m%3A90+t%3A2+f%3A%2150&fields=f1%2Cf2%2Cf3%2Cf4%2Cf5%2Cf6%2Cf7%2Cf8%2Cf9%2Cf10%2Cf12%2Cf13%2Cf14%2Cf15%2Cf1

## M9 · M9 股息率/分红记录/PE-PB 分位接口

- **状态**：PARTIAL
- **结论**：2/3 个接口可用；股息率为自算值（data_provider/base.py:4481），非接口直出
- **证据**：
  - 分红明细 stock_fhps_detail_em -> shape=(26, 19) cols=['报告期', '业绩披露日期', '送转股份-送转总比例', '送转股份-送股比例', '送转股份-转股比例', '现金分红-现金分红比例', '现金分红-现金分红比例描述', '现金分红-股息率', '每股收益', '每股净资产', '每股公积金', '每股未分配利润', '净利润同比增长', '总股本']
  - 历史分红 stock_history_dividend_detail -> shape=(26, 8) cols=['公告日期', '送股', '转增', '派息', '进度', '除权除息日', '股权登记日', '红股上市日']
  - PE/PB 分位 stock_a_indicator_lg -> ERR AttributeError: module 'akshare' has no attribute 'stock_a_indicator_lg'
- **注意**：代码库零调用 stock_a_indicator_lg / stock_zh_valuation；PE-PB 分位接口若不可用，§5.8 相应项标注缺失、降置信度

## M10 · M10 3-5 年长周期序列与复权口径

- **状态**：BLOCKED
- **结论**：长周期取数失败
- **错误**：ProxyError: HTTPSConnectionPool(host='push2his.eastmoney.com', port=443): Max retries exceeded with url: /api/qt/stock/kline/get?fields1=f1%2Cf2%2Cf3%2Cf4%2Cf5%2Cf6&fields2=f51%2Cf52%2Cf53%2Cf54%2Cf55%2Cf56%2Cf57%2Cf58%2Cf59%2Cf60%2Cf61%2Cf116&ut=7eea3edcaed734bea9cbfc24409ed989&klt=101&fqt=1&secid=1.600000&beg

## M11 · M11 风险判定历史序列可得性与存储

- **状态**：PARTIAL
- **结论**：风险判定专用表在代码中不存在；指数历史可取自免费源但需新建存储与聚合
- **证据**：
  - 规划表命中=[] 缺失=['market_regime', 'rs_t', 'sector_strength', 'market_snapshot', 'sector_snapshot']
  - 上证指数历史 -> ERR ProxyError: HTTPSConnectionPool(host='push2his.eastmoney.com', port=443): Max retries exceeded with url: /api/qt/stock/k
- **注意**：同名 market_regime 在 src/agent/skills/base.py:76 是策略路由标签，非风险判定历史序列，勿混淆

## M12 · M12 财报渐进恶化判定字段可得性

- **状态**：PASS
- **结论**：3/3 财报接口可用；代码库无审计意见字段、无连续 N 期恶化判定逻辑
- **证据**：
  - 财务摘要 stock_financial_abstract -> shape=(80, 111) cols=['选项', '指标', '20260930', '20260630', '20260331', '20251231', '20250930', '20250630', '20250331', '20241231', '20240930', '20240630', '20240331', '20231231', '20230930', '20230630']
  - 财务指标 stock_financial_analysis_indicator -> shape=(109, 86) cols=['日期', '摊薄每股收益(元)', '加权每股收益(元)', '每股收益_调整后(元)', '扣除非经常性损益后的每股收益(元)', '每股净资产_调整前(元)', '每股净资产_调整后(元)', '每股经营性现金流(元)', '每股资本公积金(元)', '每股未分配利润(元)', '调整后的每股净资产(元)', '总资产利润率(%)', '主营业务利润率(%)', '总资产净利润率(%)', '成本费用利润率(%)', '营业利润率(%)']
  - 业绩报告 stock_yjyg_em -> shape=(598, 11) cols=['序号', '股票代码', '股票简称', '预测指标', '业绩变动', '预测数值', '业绩变动幅度', '业绩变动原因', '预告类型', '上年同期值', '公告日期']
- **注意**：审计意见全库零命中；连续期判定需 d 阶段实现，不可由单次财报快照推断

## M13 · M13 飞书 Stream 配置链/SDK/凭据

- **状态**：MANUAL
- **结论**：配置链与 SDK 代码齐备；真实 Stream 收发与 emoji/lark_md 三端渲染需凭据 + 人工验证
- **证据**：
  - lark_oapi 安装=False
  - src/config.py: feishu_stream_enabled = True
  - main.py: start_feishu_stream_background = True
  - bot/platforms/feishu_stream.py: P2ImMessageReceiveV1 = True
  - src/notification_sender/feishu_sender.py: lark_md = True
  - 凭据环境变量是否设置={"FEISHU_APP_ID": false, "FEISHU_APP_SECRET": false, "FEISHU_STREAM_ENABLED": false}
- **注意**：本脚本不探测/不记录密钥；三端渲染一致性（手机/桌面/网页）必须人工截图比对，不可用本地 HTML 模拟替代

## M14 · M14 LLM 渠道可用性/延迟/降级

- **状态**：MANUAL
- **结论**：渠道配置与 fallback 链齐备；真实延迟/成本/20:00 前产出需凭据 + 目标环境实测
- **证据**：
  - LLM_CHANNELS=''
  - 已设置 API_KEY 的渠道=[]
  - src/config.py: parse_llm_channels = True
  - src/config.py: litellm_fallback_models = True
- **注意**：专用『降级规则模板』在代码中未找到（仅有通用 template_fallback）；d 阶段须补实现并验证超时是否及时

## M15 · M15 周/月线与 adj_factor 可得性

- **状态**：PARTIAL
- **结论**：adj_factor 有接口调用但无落库字段；周/月线无本地聚合表；免费源可提供周线
- **证据**：
  - stock_daily 含 adj_factor = False
  - 周/月线表 = []
  - pro.adj_factor 调用存在 = True
  - adj_factor 列在内存计算后丢弃 = True
  - 周线 period=weekly -> ERR ProxyError: HTTPSConnectionPool(host='push2his.eastmoney.com', port=443): Max retries exceeded with url: /api/qt/stock/k
- **注意**：不可退回前复权落库；不可得时按 §4.6.4 退化为除权事件表推算因子

## M16 · M16 公告接口字段完整性与日返回量

- **状态**：PARTIAL
- **结论**：字段命中 3/4；单标的 7 日 ( 条
- **证据**：
  - 7 日窗口 shape=(2, 5) cols=['代码', '简称', '公告标题', '公告时间', '公告链接']
  - 标题: ['公告标题']
  - 公告编号: []
  - 原文链接: ['公告链接']
  - 日期: ['公告时间']
- **注意**：代码库现只取标题 + 日期（src/services/screening/candidate_context.py:229-248）；缺字段则 §5.15 降级为『只显示标题 + 来源，无链接』

## M17 · M17 3 年首次回补耗时/增量更新

- **状态**：BLOCKED
- **结论**：3 年回补取数失败
- **错误**：ProxyError: HTTPSConnectionPool(host='push2his.eastmoney.com', port=443): Max retries exceeded with url: /api/qt/stock/kline/get?fields1=f1%2Cf2%2Cf3%2Cf4%2Cf5%2Cf6&fields2=f51%2Cf52%2Cf53%2Cf54%2Cf55%2Cf56%2Cf57%2Cf58%2Cf59%2Cf60%2Cf61%2Cf116&ut=7eea3edcaed734bea9cbfc24409ed989&klt=101&fqt=1&secid=1.600000&beg

## M18 · M18 cron 解析与交易日门控 fail-closed

- **状态**：PASS
- **结论**：cron 解析与门控均可用；未知市场时 is_market_open=True 而 ctx.is_trading_day=None，印证 fail-open 不可用于门控
- **证据**：
  - croniter 安装=True；requirements.txt 含 croniter/apscheduler=False
  - 0 20 * * 1-5 -> 2026-11-24 20:00:00
  - */15 9-18 * * 1-5 -> 2026-11-24 11:45:00
  - 2026-11-24 交易日: is_market_open=True ctx.is_trading_day=True
  - 2026-11-28 周六: is_market_open=False ctx.is_trading_day=False
  - 2026-10-01 国庆: is_market_open=False ctx.is_trading_day=False
  - 未知市场: is_market_open=True ctx.is_trading_day=None
- **注意**：门控须判 is_trading_day is True 才推；None/False 一律零产出 + 告警。通知去重对同源多时点须用独立去重键，否则同一文本会被吞
