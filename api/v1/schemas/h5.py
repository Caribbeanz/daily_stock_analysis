# -*- coding: utf-8 -*-
"""高抛低吸助手 · 移动 H5 响应模型（M1）

只读接口，供独立移动页 `/h5`、`/h5/stocks/:code` 使用。
字段与 ``src/services/h5_service.py`` 输出一一对应，均为 snake_case，
前端经 ``toCamelCase`` 统一转换。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class H5WatchlistItem(BaseModel):
    """列表行：名称 / 代码 / 当日价 / 涨跌幅 / 操作建议。"""

    symbol: str = Field(..., description="标的代码")
    name: str = Field("", description="标的名称")
    asset_type: str = Field("", description="标的类型 stock/etf/lof")
    price: Optional[float] = Field(None, description="当日价格（收盘后为收盘价）")
    change_percent: Optional[float] = Field(None, description="涨跌幅 (%)")
    action: str = Field("watch", description="后端原始动作")
    action_label: str = Field("", description="后端原始动作标签")
    display_action: str = Field("", description="页面展示动作：建仓/加仓/持有/减仓/清仓/观望/暂无信号")
    action_tone: str = Field("flat", description="色调 up/down/flat/muted")
    has_signal: bool = Field(False, description="是否存在已落库信号（false ≠ 建议观望）")
    is_urgent: bool = Field(False, description="是否紧急（止损/清仓）")
    scenario_label: str = Field("", description="场景中文标签")
    signal_created_at: Optional[str] = Field(None, description="信号生成时间")


class H5WatchlistResponse(BaseModel):
    """自选股列表 + 首页大盘块。"""

    as_of: str = Field("", description="数据日期 YYYY-MM-DD")
    market: Optional["H5MarketBlock"] = Field(None, description="首页大盘（M2-A）")
    items: List[H5WatchlistItem] = Field(default_factory=list)
    count: int = Field(0, description="标的数量")
    degraded: List[str] = Field(default_factory=list, description="降级说明")
    disclaimer: str = Field("", description="免责声明")


class H5MarketIndex(BaseModel):
    code: str = ""
    name: str = ""
    close: Optional[float] = Field(None, description="收盘点位")
    change_pct: Optional[float] = Field(None, description="当日涨跌幅 (%)")


class H5MarketOtherIndex(BaseModel):
    code: str = ""
    name: str = ""
    change_pct: Optional[float] = None


class H5MarketIndicator(BaseModel):
    key: str
    label: str
    value: str = "-"
    tone: str = Field("neutral", description="up/down/warning/neutral")


class H5MarketRegime(BaseModel):
    level: str = Field("unknown", description="bullish/bullish_quiet/rebound/pullback/bear/unknown")
    label: str = Field("数据不足", description="偏多/偏空/趋势回调/下跌反弹/数据不足")
    tone: str = "neutral"
    advice: str = ""


class H5MarketBlock(BaseModel):
    """首页大盘：点位 + 6 指标 + 环境评级 + 建议 + 总结。"""

    index: H5MarketIndex = Field(default_factory=H5MarketIndex)
    others: List[H5MarketOtherIndex] = Field(default_factory=list)
    indicators: List[H5MarketIndicator] = Field(default_factory=list)
    regime: H5MarketRegime = Field(default_factory=H5MarketRegime)
    advice: str = ""
    alerts: List[str] = Field(default_factory=list)
    summary: str = ""
    counts: Dict[str, int] = Field(default_factory=dict, description="列表动作统计（减/加/持有/观望/无信号）")
    as_of: str = ""
    degraded: List[str] = Field(default_factory=list)


class H5SectorDetail(BaseModel):
    """板块结构化字段（前端直接渲染，不解析 verdict 文案）。"""

    industry: str = ""
    change_pct: Optional[float] = None
    rank: Optional[int] = None
    total: Optional[int] = None
    relative_pct: Optional[float] = None
    advancers: Optional[int] = None
    decliners: Optional[int] = None
    ranking_available: bool = False


class H5Quote(BaseModel):
    """当日四价与涨跌。"""

    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = Field(None, description="收盘价；盘中为最新价")
    prev_close: Optional[float] = None
    change: Optional[float] = None
    change_percent: Optional[float] = None
    volume: Optional[float] = None
    update_time: Optional[str] = Field(None, description="行情时间")


class H5AnalysisDetail(BaseModel):
    label: str = ""
    value: str = ""


class H5AnalysisSection(BaseModel):
    """分析过程的一块（技术面 / 板块 / 风险 / 大盘 / 消息面）。"""

    key: str
    label: str
    verdict: str = Field("", description="一句话结论")
    tone: str = Field("neutral", description="up/down/warning/neutral")
    available: bool = Field(True, description="该维度是否有可用依据")
    details: List[H5AnalysisDetail] = Field(default_factory=list)
    sector_detail: Optional["H5SectorDetail"] = Field(None, description="板块结构化字段（仅 sector 块）")


class H5IndexItem(BaseModel):
    code: str = ""
    name: str = ""
    value: Optional[float] = None
    change_pct: Optional[float] = None


class H5Market(BaseModel):
    indices: List[H5IndexItem] = Field(default_factory=list)
    as_of: str = ""


class H5DetailResponse(BaseModel):
    """个股详情：四价 + 建议总结 + 五维分析。"""

    symbol: str
    name: str = ""
    as_of: str = ""
    generated_at: str = ""

    action: str = "watch"
    action_label: str = ""
    display_action: str = ""
    action_tone: str = "flat"
    has_signal: bool = False
    is_urgent: bool = False

    scenario: str = ""
    scenario_label: str = ""
    conclusion: str = ""
    reason: str = ""
    score: Optional[int] = None
    confidence: Optional[float] = None

    quote: Optional[H5Quote] = None

    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    entry_zone: str = "-"
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None
    position_ratio: Optional[float] = None
    position_ratio_label: str = "-"
    position_reason: str = ""
    discipline: str = ""
    watch_conditions: str = ""
    risk_summary: str = ""
    distance_to_stop_pct: Optional[float] = None
    distance_to_target_pct: Optional[float] = None

    market: H5Market = Field(default_factory=H5Market)
    analysis: List[H5AnalysisSection] = Field(default_factory=list)

    signal_id: Optional[int] = None
    signal_created_at: Optional[str] = None
    degraded: List[str] = Field(default_factory=list)
    disclaimer: str = ""


# 前向引用：H5WatchlistResponse / H5AnalysisSection 引用了后定义的板块与大盘模型
H5WatchlistResponse.model_rebuild()
H5AnalysisSection.model_rebuild()
