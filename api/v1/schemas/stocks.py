# -*- coding: utf-8 -*-
"""
===================================
股票数据相关模型
===================================

职责：
1. 定义股票实时行情模型
2. 定义历史 K 线数据模型
"""

from typing import Dict, Literal, Optional, List

from pydantic import BaseModel, ConfigDict, Field

from api.v1.schemas.history import HistoryItem
from api.v1.schemas.intelligence import IntelligenceItem
from api.v1.schemas.research_artifact import ResearchArtifact

StockProfileStatus = Literal["fresh", "partial", "unavailable"]


class StockOverviewFactor(BaseModel):
    """个股详情 · 多因子明细的一行。"""

    key: str = Field(..., description="因子键")
    label: str = Field(..., description="因子名称")
    value: str = Field(..., description="因子取值（已格式化）")
    tone: Literal["positive", "negative", "warning", "neutral"] = Field(
        "neutral", description="展示语义（前端决定配色）"
    )


class StockOverviewQuote(BaseModel):
    """个股详情 · 行情快照（可能缺失）。"""

    current_price: Optional[float] = Field(None, description="现价")
    change_percent: Optional[float] = Field(None, description="涨跌幅 (%)")
    update_time: Optional[str] = Field(None, description="行情时间")


class StockOverviewResponse(BaseModel):
    """个股详情聚合视图（H5 主入口 · FR-14.2.1）。

    结论与四要素**同源于 decision_signals**（NFR-9）；
    行情 / 事件缺失时通过 ``degraded`` 显式说明，不伪装成正常。
    """

    symbol: str = Field(..., description="标的代码")
    name: str = Field("", description="标的名称")
    as_of: str = Field("", description="数据日期 YYYY-MM-DD")
    generated_at: str = Field("", description="生成时间")

    action: str = Field("watch", description="动作")
    action_label: str = Field("观望", description="动作中文标签")
    scenario: str = Field("unknown", description="场景")
    scenario_label: str = Field("趋势不明", description="场景中文标签")
    conclusion: str = Field("", description="一句话人话结论（顶部大字号）")
    reason: str = Field("", description="理由")
    score: Optional[int] = Field(None, description="综合评分 0-100")
    confidence: Optional[float] = Field(None, description="置信度 0-1")
    is_urgent: bool = Field(False, description="是否需立即处理")

    entry_low: Optional[float] = Field(None, description="建仓区间下沿")
    entry_high: Optional[float] = Field(None, description="建仓区间上沿")
    entry_zone: str = Field("-", description="建仓区间展示串")
    stop_loss: Optional[float] = Field(None, description="止损价")
    target_price: Optional[float] = Field(None, description="目标价")
    position_ratio: Optional[float] = Field(None, description="仓位建议 0-1")
    position_ratio_label: str = Field("-", description="仓位建议展示串")
    position_reason: str = Field("", description="仓位建议理由")
    discipline: str = Field("", description="操作纪律卡一行")

    watch_conditions: str = Field("", description="纪律 / 观察条件")
    risk_summary: str = Field("", description="风控摘要")
    factors: List[StockOverviewFactor] = Field(default_factory=list, description="多因子明细")

    events: List[str] = Field(default_factory=list, description="事件与风险")
    quote: Optional[StockOverviewQuote] = Field(None, description="行情快照")
    distance_to_stop_pct: Optional[float] = Field(None, description="现价距止损 (%)")
    distance_to_target_pct: Optional[float] = Field(None, description="现价距目标 (%)")

    signal_id: Optional[int] = Field(None, description="信号 ID（溯源）")
    signal_created_at: Optional[str] = Field(None, description="信号生成时间")
    degraded: List[str] = Field(default_factory=list, description="降级说明（数据缺失原因）")
    disclaimer: str = Field("", description="免责声明")


class StockQuote(BaseModel):
    """股票实时行情"""
    
    stock_code: str = Field(..., description="股票代码")
    stock_name: Optional[str] = Field(None, description="股票名称")
    current_price: float = Field(..., description="当前价格")
    change: Optional[float] = Field(None, description="涨跌额")
    change_percent: Optional[float] = Field(None, description="涨跌幅 (%)")
    open: Optional[float] = Field(None, description="开盘价")
    high: Optional[float] = Field(None, description="最高价")
    low: Optional[float] = Field(None, description="最低价")
    prev_close: Optional[float] = Field(None, description="昨收价")
    volume: Optional[float] = Field(None, description="成交量（股）")
    amount: Optional[float] = Field(None, description="成交额（元）")
    update_time: Optional[str] = Field(None, description="更新时间")
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "stock_code": "600519",
            "stock_name": "贵州茅台",
            "current_price": 1800.00,
            "change": 15.00,
            "change_percent": 0.84,
            "open": 1785.00,
            "high": 1810.00,
            "low": 1780.00,
            "prev_close": 1785.00,
            "volume": 10000000,
            "amount": 18000000000,
            "update_time": "2024-01-01T15:00:00"
        }
    })


class KLineData(BaseModel):
    """K 线数据点"""
    
    date: str = Field(..., description="日期")
    open: float = Field(..., description="开盘价")
    high: float = Field(..., description="最高价")
    low: float = Field(..., description="最低价")
    close: float = Field(..., description="收盘价")
    volume: Optional[float] = Field(None, description="成交量")
    amount: Optional[float] = Field(None, description="成交额")
    change_percent: Optional[float] = Field(None, description="涨跌幅 (%)")
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "date": "2024-01-01",
            "open": 1785.00,
            "high": 1810.00,
            "low": 1780.00,
            "close": 1800.00,
            "volume": 10000000,
            "amount": 18000000000,
            "change_percent": 0.84
        }
    })


class ExtractItem(BaseModel):
    """单条提取结果（代码、名称、置信度）"""

    code: Optional[str] = Field(None, description="股票代码，None 表示解析失败")
    name: Optional[str] = Field(None, description="股票名称（如有）")
    confidence: str = Field("medium", description="置信度：high/medium/low")


class ExtractFromImageResponse(BaseModel):
    """图片股票代码提取响应"""

    codes: List[str] = Field(..., description="提取的股票代码（已去重，向后兼容）")
    items: List[ExtractItem] = Field(default_factory=list, description="提取结果明细（代码+名称+置信度）")
    raw_text: Optional[str] = Field(None, description="原始 LLM 响应（调试用）")


class StockHistoryResponse(BaseModel):
    """股票历史行情响应"""
    
    stock_code: str = Field(..., description="股票代码")
    stock_name: Optional[str] = Field(None, description="股票名称")
    period: str = Field(..., description="K 线周期")
    data: List[KLineData] = Field(default_factory=list, description="K 线数据列表")
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "stock_code": "600519",
            "stock_name": "贵州茅台",
            "period": "daily",
            "data": []
        }
    })


class StockProfileQuoteBlock(BaseModel):
    status: StockProfileStatus
    data: Optional[StockQuote] = None
    limitations: List[str] = Field(default_factory=list)


class StockProfileHistoryBlock(BaseModel):
    status: StockProfileStatus
    period: Literal["daily"] = "daily"
    data: List[KLineData] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)


class StockProfileResearchData(BaseModel):
    latest_report: Optional[HistoryItem] = None
    recent_reports: List[HistoryItem] = Field(default_factory=list)
    structured_report: Optional[ResearchArtifact] = None


class StockProfileResearchBlock(BaseModel):
    status: StockProfileStatus
    data: StockProfileResearchData = Field(default_factory=StockProfileResearchData)
    limitations: List[str] = Field(default_factory=list)


class StockProfileIntelligenceBlock(BaseModel):
    status: StockProfileStatus
    items: List[IntelligenceItem] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)


class StockProfilePortfolioRelation(BaseModel):
    held: bool = False
    matched_markets: List[str] = Field(default_factory=list)


class StockProfilePortfolioBlock(BaseModel):
    status: StockProfileStatus
    data: StockProfilePortfolioRelation = Field(default_factory=StockProfilePortfolioRelation)
    limitations: List[str] = Field(default_factory=list)


class StockProfileMonitorData(BaseModel):
    total_rule_count: int = 0
    enabled_rule_count: int = 0
    rule_ids: List[int] = Field(default_factory=list)


class StockProfileMonitorBlock(BaseModel):
    status: StockProfileStatus
    data: StockProfileMonitorData = Field(default_factory=StockProfileMonitorData)
    limitations: List[str] = Field(default_factory=list)


class StockProfileEvidenceQuality(BaseModel):
    status: StockProfileStatus
    blocks: Dict[str, StockProfileStatus] = Field(default_factory=dict)
    limitations: List[str] = Field(default_factory=list)


class StockProfileResponse(BaseModel):
    requested_code: str
    canonical_code: str
    market: Literal["cn", "hk", "us", "jp", "kr", "tw"]
    as_of: str
    quote: StockProfileQuoteBlock
    history: StockProfileHistoryBlock
    research: StockProfileResearchBlock
    intelligence: StockProfileIntelligenceBlock
    portfolio: StockProfilePortfolioBlock
    monitors: StockProfileMonitorBlock
    evidence_quality: StockProfileEvidenceQuality
