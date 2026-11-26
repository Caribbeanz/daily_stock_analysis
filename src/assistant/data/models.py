# -*- coding: utf-8 -*-
"""L1–L4 存储表定义（技术方案 §3.2）。

**为何独立 metadata**：现有 `src/storage.py` 的 ``stock_daily`` 存的是**前复权**数据
（主流程 ``akshare_fetcher`` 固定 ``adjust="qfq"``）。本助手要求**不复权 + adj_factor**
（需求 §4.6.4），两者语义冲突，直接扩展同一张表会污染主流程口径。

因此：
- 使用独立 ``AssistantBase``（同库不同表，仍可 ``JOIN``）与 ``assistant_`` 表名前缀；
- 主流程表结构、ORM、读写链**零改动**；
- 若后续确认要合并到 ``stock_daily``，只需迁移一次（本模块的读写接口不变）。
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base

AssistantBase = declarative_base()


class StockDailyRaw(AssistantBase):
    """L1 日线（**不复权** + adj_factor），按 ``(code, trade_date)`` 幂等重写。"""

    __tablename__ = "assistant_stock_daily"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(16), nullable=False)
    trade_date = Column(Date, nullable=False)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False, default=0.0)
    amount = Column(Float, nullable=False, default=0.0)
    adj_factor = Column(Float, nullable=False, default=1.0)
    source_tag = Column(String(4), nullable=False, default="D1")
    updated_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("code", "trade_date", name="uq_assistant_stock_daily"),
        Index("idx_assistant_stock_daily_code_date", "code", "trade_date"),
    )


class StockWeekly(AssistantBase):
    """L1 周线（由本地日线聚合，不额外依赖接口）。"""

    __tablename__ = "assistant_stock_weekly"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(16), nullable=False)
    iso_week = Column(String(10), nullable=False)
    week_end_date = Column(Date, nullable=False)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False, default=0.0)

    __table_args__ = (UniqueConstraint("code", "iso_week", name="uq_assistant_stock_weekly"),)


class StockMonthly(AssistantBase):
    """L1 月线（由本地日线聚合）。"""

    __tablename__ = "assistant_stock_monthly"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(16), nullable=False)
    year_month = Column(String(7), nullable=False)
    month_end_date = Column(Date, nullable=False)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False, default=0.0)

    __table_args__ = (UniqueConstraint("code", "year_month", name="uq_assistant_stock_monthly"),)


class IndexDaily(AssistantBase):
    """L1 指数行情。"""

    __tablename__ = "assistant_index_daily"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(16), nullable=False)
    trade_date = Column(Date, nullable=False)
    open = Column(Float, nullable=True)
    high = Column(Float, nullable=True)
    low = Column(Float, nullable=True)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False, default=0.0)
    source_tag = Column(String(4), nullable=False, default="D1")

    __table_args__ = (
        UniqueConstraint("code", "trade_date", name="uq_assistant_index_daily"),
        Index("idx_assistant_index_daily", "code", "trade_date"),
    )


class SectorDaily(AssistantBase):
    """L1 板块行情（涨跌幅 / 涨跌家数 / 领涨股）。"""

    __tablename__ = "assistant_sector_daily"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sector_code = Column(String(16), nullable=False)
    sector_name = Column(String(64), nullable=False, default="")
    trade_date = Column(Date, nullable=False)
    pct_change = Column(Float, nullable=True)
    up_count = Column(Integer, nullable=True)
    down_count = Column(Integer, nullable=True)
    lead_stock = Column(String(32), nullable=True)
    source_tag = Column(String(4), nullable=False, default="D1")

    __table_args__ = (
        UniqueConstraint("sector_code", "trade_date", name="uq_assistant_sector_daily"),
        Index("idx_assistant_sector_daily", "sector_code", "trade_date"),
    )


class DividendEvent(AssistantBase):
    """L1 分红事件（adj_factor 不可得时的推算依据）。"""

    __tablename__ = "assistant_dividend_event"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(16), nullable=False)
    ex_date = Column(Date, nullable=False)
    record_date = Column(Date, nullable=True)
    payout_date = Column(Date, nullable=True)
    per_share = Column(Float, nullable=True)
    source_tag = Column(String(4), nullable=False, default="D1")

    __table_args__ = (UniqueConstraint("code", "ex_date", name="uq_assistant_dividend_event"),)


class ValuationHist(AssistantBase):
    """L1 估值历史分位（PE/PB 分位）。"""

    __tablename__ = "assistant_valuation_hist"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(16), nullable=False)
    trade_date = Column(Date, nullable=False)
    pe = Column(Float, nullable=True)
    pb = Column(Float, nullable=True)
    pe_pctile = Column(Float, nullable=True)
    pb_pctile = Column(Float, nullable=True)
    dividend_yield = Column(Float, nullable=True)
    source_tag = Column(String(4), nullable=False, default="D1")

    __table_args__ = (UniqueConstraint("code", "trade_date", name="uq_assistant_valuation_hist"),)


class EventFeed(AssistantBase):
    """L1 重大消息（append-only，只取事实与时间戳，不做情绪判断）。"""

    __tablename__ = "assistant_event_feed"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(64), nullable=False)
    code = Column(String(16), nullable=True)
    title = Column(String(512), nullable=False)
    pub_time = Column(DateTime, nullable=True)
    source = Column(String(64), nullable=True)
    url = Column(String(1024), nullable=True)
    category = Column(String(32), nullable=True)
    source_tag = Column(String(4), nullable=False, default="D1")

    __table_args__ = (UniqueConstraint("event_id", name="uq_assistant_event_feed"),)


# ---------------------------------------------------------------------------
# L3 决策快照层（append-only）
# ---------------------------------------------------------------------------
class Snapshot(AssistantBase):
    """交易日快照主表。``snapshot_id`` 为主键，**同日重跑生成新 id，不覆盖**。"""

    __tablename__ = "assistant_snapshot"

    snapshot_id = Column(String(40), primary_key=True)
    #: 快照所属交易日（生成日 T）——幂等键与台账分组都用它
    data_date = Column(Date, nullable=False)
    #: 数据截止日（T 收盘；取数不全时可能更早）。与 data_date 分开存，否则
    #: 「收盘数据未出」那天快照会被记到前一交易日，幂等判断跨日失效。
    bar_date = Column(Date, nullable=True)
    calc_version = Column(String(32), nullable=False)
    param_hash = Column(String(64), nullable=False)
    param_preset = Column(String(16), nullable=False, default="standard")
    generated_at = Column(DateTime, nullable=False)
    bars_used = Column(Integer, nullable=False, default=0)
    gap_days = Column(Integer, nullable=False, default=0)
    source_tag = Column(String(4), nullable=False, default="D1")
    notes = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_assistant_snapshot_date", "data_date"),
        Index("idx_assistant_snapshot_version", "calc_version", "param_hash"),
    )


class HoldingConclusionRow(AssistantBase):
    """个股结论（三元组随快照继承）。"""

    __tablename__ = "assistant_holding_conclusion"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id = Column(String(40), nullable=False)
    code = Column(String(16), nullable=False)
    name = Column(String(64), nullable=False, default="")
    advice = Column(String(16), nullable=True)
    advice_display = Column(String(16), nullable=True)
    advice_state = Column(String(16), nullable=False, default="有效")
    stage = Column(String(4), nullable=False, default="S0")
    stage_confidence = Column(String(4), nullable=False, default="低")
    stage_gate_hit = Column(Text, nullable=True)  # JSON
    trend = Column(String(8), nullable=False, default="中性")
    position_tag = Column(String(8), nullable=False, default="不明")
    score = Column(Float, nullable=False, default=0.0)
    is_holding = Column(Boolean, nullable=False, default=False)
    est_level = Column(String(16), nullable=True)
    div_yield = Column(Float, nullable=True)
    pnl_pct = Column(Float, nullable=True)
    #: 最近交易日收盘与涨跌幅（列表页展示用；不复权，需求 P0-2）。老库无此列需重建
    last_close = Column(Float, nullable=True)
    pct_change = Column(Float, nullable=True)
    #: 关键价位 JSON（[{price, kind, source, detail}]，需求 §5.3 每个价位须带来源）
    key_levels = Column(Text, nullable=True)
    #: 仓位建议（S6 硬上限裁剪后）：轻/中/重 + 目标占比 + 裁剪原因
    position_tier = Column(String(4), nullable=True)
    position_pct = Column(Float, nullable=True)
    position_reasons = Column(Text, nullable=True)
    risk_level = Column(String(8), nullable=False, default="观察")
    human_text = Column(Text, nullable=True)
    degraded = Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("idx_assistant_holding_snapshot", "snapshot_id"),
        Index("idx_assistant_holding_code", "code", "snapshot_id"),
    )


class ConditionalPlanRow(AssistantBase):
    """条件计划分支（每条分支独立、单独通过硬上限）。"""

    __tablename__ = "assistant_conditional_plan"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id = Column(String(40), nullable=False)
    code = Column(String(16), nullable=False)
    branch_id = Column(String(8), nullable=False)
    scenario = Column(String(64), nullable=False)
    trigger_type = Column(String(16), nullable=False)
    trigger_price = Column(Float, nullable=True)
    pct_change = Column(Float, nullable=True)
    action = Column(String(16), nullable=False)
    shares = Column(Integer, nullable=False, default=0)
    est_amount = Column(Float, nullable=False, default=0.0)
    pct_of_total = Column(Float, nullable=False, default=0.0)
    valid_days = Column(Integer, nullable=False, default=1)
    settable = Column(Boolean, nullable=False, default=True)
    notes = Column(Text, nullable=True)
    technical_basis = Column(Text, nullable=True)
    position_check = Column(Text, nullable=True)  # JSON
    is_probe = Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("idx_assistant_plan_snapshot", "snapshot_id"),
        UniqueConstraint("snapshot_id", "code", "branch_id", name="uq_assistant_plan_branch"),
    )


class RiskSignalRow(AssistantBase):
    """风险信号（四处展示只读这一份）。"""

    __tablename__ = "assistant_risk_signal"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id = Column(String(40), nullable=False)
    risk_id = Column(String(32), nullable=False)
    code = Column(String(16), nullable=True)
    risk_code = Column(String(8), nullable=False)
    category = Column(String(32), nullable=False)
    level = Column(String(8), nullable=False)
    trigger_basis = Column(Text, nullable=False)
    data_date = Column(Date, nullable=False)
    affects_advice = Column(Boolean, nullable=False, default=False)
    action = Column(String(64), nullable=True)
    human_text = Column(Text, nullable=True)
    source_tag = Column(String(4), nullable=False, default="D1")
    degraded = Column(Boolean, nullable=False, default=False)

    __table_args__ = (Index("idx_assistant_risk_snapshot", "snapshot_id"),)


class MarketSnapshotRow(AssistantBase):
    """大盘环境快照。"""

    __tablename__ = "assistant_market_snapshot"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id = Column(String(40), nullable=False)
    index_code = Column(String(16), nullable=False)
    index_name = Column(String(64), nullable=False, default="")
    pct_change = Column(Float, nullable=True)
    market_regime = Column(String(8), nullable=False, default="中性")
    regime_cells = Column(String(32), nullable=True)
    width = Column(Float, nullable=True)
    degraded = Column(Boolean, nullable=False, default=False)

    __table_args__ = (Index("idx_assistant_market_snapshot", "snapshot_id"),)


class SectorSnapshotRow(AssistantBase):
    """板块快照。"""

    __tablename__ = "assistant_sector_snapshot"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id = Column(String(40), nullable=False)
    sector_code = Column(String(16), nullable=False)
    sector_name = Column(String(64), nullable=False, default="")
    pct_change = Column(Float, nullable=True)
    sector_strength = Column(String(8), nullable=True)
    rs_label = Column(String(16), nullable=True)
    rs_t = Column(Float, nullable=True)
    up_count = Column(Integer, nullable=True)
    down_count = Column(Integer, nullable=True)
    lead_stock = Column(String(32), nullable=True)
    #: 板块指数序列最后一根的交易日（可能早于快照日：同花顺板块指数晚一天发布）
    as_of = Column(Date, nullable=True)
    #: 数据降级（序列不足 / 指数滞后 / 缓冲带无历史档）——降级只改置信度，不改档位
    degraded = Column(Boolean, nullable=False, default=False)

    __table_args__ = (Index("idx_assistant_sector_snapshot", "snapshot_id"),)


class LlmTextCacheRow(AssistantBase):
    """LLM 文案缓存：同快照同位置只生成一次。"""

    __tablename__ = "assistant_llm_text_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id = Column(String(40), nullable=False)
    position = Column(String(16), nullable=False)  # stock / summary / market
    code = Column(String(16), nullable=True)
    text = Column(Text, nullable=False)
    model = Column(String(64), nullable=True)
    channel = Column(String(32), nullable=True)
    generated_at = Column(DateTime, nullable=True)
    consistency_check = Column(String(16), nullable=True)
    #: 文案指纹：提示词版本 + 模型 + temperature；与决策 calc_version 分开（D-19）
    fingerprint = Column(String(64), nullable=True)
    #: 后端返回的用量（可追溯，不参与任何决策）
    usage_json = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("snapshot_id", "position", "code", name="uq_assistant_llm_text"),
    )


# ---------------------------------------------------------------------------
# L4 用户输入层
# ---------------------------------------------------------------------------
class PortfolioSettingRow(AssistantBase):
    """运行设置（基线档位 + 资金）——append-only，取最新一行作为当前生效值。

    env 里的 ``ASSISTANT_*`` 只作初始 fallback；Web 配置页写入后以本表为准（D-12）。
    """

    __tablename__ = "assistant_portfolio_setting"

    id = Column(Integer, primary_key=True, autoincrement=True)
    baseline_preset = Column(String(16), nullable=False, default="standard")
    total_capital = Column(Float, nullable=False)
    cash_available = Column(Float, nullable=True)
    updated_at = Column(DateTime, nullable=False)


class UserHolding(AssistantBase):
    """持仓与成本（用户手工录入，下一次快照生成时生效）。"""

    __tablename__ = "assistant_user_holding"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(16), nullable=False)
    shares = Column(Integer, nullable=False, default=0)
    cost_basis = Column(Float, nullable=False, default=0.0)
    buy_date = Column(Date, nullable=True)
    recorded_at = Column(DateTime, nullable=True)

    __table_args__ = (UniqueConstraint("code", name="uq_assistant_user_holding"),)


class UserNote(AssistantBase):
    """操作笔记（不进评分、不进回测）。"""

    __tablename__ = "assistant_user_note"

    id = Column(Integer, primary_key=True, autoincrement=True)
    note_id = Column(String(40), nullable=False)
    trade_date = Column(Date, nullable=False)
    code = Column(String(16), nullable=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=True)

    __table_args__ = (UniqueConstraint("note_id", name="uq_assistant_user_note"),)


class WatchlistRuntime(AssistantBase):
    """关注清单运行时写入（append-only，可撤销）。"""

    __tablename__ = "assistant_watchlist_runtime"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(16), nullable=False)
    action = Column(String(8), nullable=False)  # add / remove
    op_time = Column(DateTime, nullable=False)
    effective_date = Column(Date, nullable=True)
    user_id = Column(String(64), nullable=True)
    audit_id = Column(String(40), nullable=True)
    revoked = Column(Boolean, nullable=False, default=False)

    __table_args__ = (Index("idx_assistant_watchlist_code", "code", "op_time"),)


class ParamOverrideRow(AssistantBase):
    """参数单项覆盖变更记录（**append-only**，回滚也记一条）。"""

    __tablename__ = "assistant_param_override"

    id = Column(Integer, primary_key=True, autoincrement=True)
    param_key = Column(String(48), nullable=False)
    param_value = Column(String(64), nullable=False)
    previous_value = Column(String(64), nullable=True)
    baseline_preset = Column(String(16), nullable=False, default="standard")
    op_type = Column(String(16), nullable=False, default="override")  # override / rollback / preset
    op_time = Column(DateTime, nullable=False)
    effective_date = Column(Date, nullable=False)
    user_id = Column(String(64), nullable=True)
    audit_id = Column(String(40), nullable=True)

    __table_args__ = (Index("idx_assistant_param_override_key", "param_key", "op_time"),)


class DeliveryLogRow(AssistantBase):
    """投递台账（多时点去重 + 同源校验，需求 B50 ③）。

    ``slot_key`` 精确到分钟（``2026-11-24T20:00``）且唯一：同一时点重复触发只会命中
    同一行（重启后仍能防重），不同时点各自独立、互不吞并。

    ``status``：``claimed`` 已领取未发送 / ``sent`` 已发送 / ``failed`` 发送失败（可重试）。
    """

    __tablename__ = "assistant_delivery_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slot_key = Column(String(32), nullable=False, unique=True)
    slot_label = Column(String(64), nullable=False)  # 命中的配置原文（HH:MM 或 cron）
    data_date = Column(Date, nullable=False)
    snapshot_id = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default="claimed")
    attempts = Column(Integer, nullable=False, default=1)
    first_attempt_at = Column(DateTime, nullable=False)
    last_attempt_at = Column(DateTime, nullable=False)
    sent_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)

    __table_args__ = (Index("idx_assistant_delivery_date", "data_date", "status"),)


class StageStateRow(AssistantBase):
    """跨日阶段防抖状态（需求 §5.14.2「连续 N 日」）。

    必须落库：状态只在内存里的话，进程每天重启都算「首次判定 → 直接确认」，
    连续 2 日防抖等于不存在——基线回测里「阶段切换 27.63 次/年」的越界就是这么来的。
    """

    __tablename__ = "assistant_stage_state"

    code = Column(String(16), primary_key=True)
    stage = Column(String(4), nullable=True)          # 已确认阶段
    pending_stage = Column(String(4), nullable=True)  # 待确认阶段
    pending_days = Column(Integer, nullable=False, default=0)
    as_of = Column(Date, nullable=True)               # 该状态对应的交易日
    updated_at = Column(DateTime, nullable=False)
