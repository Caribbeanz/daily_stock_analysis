# -*- coding: utf-8 -*-
"""高抛低吸助手 · 移动 H5 聚合服务（M1）

只读聚合两个页面所需数据：

1. **列表**：自选股（``watchlist_items``）→ 名称 / 代码 / 当日价 / 涨跌幅 / 操作建议；
2. **详情**：当日四价 + 建议总结 + 五维分析（技术面 / 消息面 / 风险 / 大盘 / 板块）。

设计约束：

- **四端同源**：结论与四要素一律取自已落库的 ``decision_signals``（经
  :class:`StockOverviewService`），本模块不重算指标、不另立口径；
- **失败隔离**：单只标的失败只降级该行（``degraded``），不阻断整页；
- **诚实降级**：板块数据本期未接入，显式 ``available=false``，不伪造结论；
- **无信号 ≠ 观望**：``has_signal=false`` 时 ``display_action`` 为「暂无信号」，
  避免把"还没算"误读成"建议按兵不动"。

需求依据：``docs/requirements/hslb-mobile-h5-refactor-requirements-v0.1.md``。
"""

from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "DISPLAY_ACTIONS",
    "H5Service",
    "build_analysis",
    "display_action",
    "action_tone",
]

# --------------------------------------------------------------------------- #
# 展示口径：8 态 → 6 类（D2 已确认）
# --------------------------------------------------------------------------- #
DISPLAY_ACTIONS: Dict[str, str] = {
    "build": "建仓",
    "buy": "建仓",
    "dip_buy": "建仓",
    "add": "加仓",
    "hold": "持有",
    "reduce": "减仓",
    "sell": "清仓",
    "clear": "清仓",
    "wait": "观望",
    "watch": "观望",
    # ``avoid`` 归入观望并保留风险标记（D2），不单列以免与「清仓」混淆
    "avoid": "观望",
}

NO_SIGNAL_LABEL = "暂无信号"

# 建议标签色调：与涨跌色语义区分——建仓/加仓偏多（up），减仓/清仓偏空（down）
_ACTION_TONES: Dict[str, str] = {
    "建仓": "up",
    "加仓": "up",
    "持有": "flat",
    "减仓": "down",
    "清仓": "down",
    "观望": "flat",
    NO_SIGNAL_LABEL: "muted",
}

# 列表排序：紧急 → 明确操作 → 持有 → 观望 → 无信号
_ACTION_RANK: Dict[str, int] = {
    "清仓": 0,
    "减仓": 1,
    "建仓": 1,
    "加仓": 1,
    "持有": 2,
    "观望": 3,
    NO_SIGNAL_LABEL: 4,
}

_RISK_KEYWORDS = ("风险", "预警", "下修", "亏损", "监管", "处罚", "退市", "违约", "减持", "立案")

_TREND_TEXT = {"up": "向上", "down": "向下", "sideways": "震荡"}


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def format_price(value: Any) -> str:
    number = _finite(value)
    if number is None:
        return "-"
    return f"{number:.2f}" if abs(number) >= 1 else f"{number:.3f}"


def _pct_text(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:+.2f}%"


def display_action(action: str, has_signal: bool) -> str:
    """把后端 8 态动作收敛成页面 6 类；无信号时返回「暂无信号」。"""

    if not has_signal:
        return NO_SIGNAL_LABEL
    return DISPLAY_ACTIONS.get(str(action or "").strip().lower(), "观望")


def action_tone(label: str) -> str:
    return _ACTION_TONES.get(label, "muted")


def _section(
    key: str,
    label: str,
    verdict: str,
    *,
    details: Optional[Sequence[Mapping[str, str]]] = None,
    tone: str = "neutral",
    available: bool = True,
) -> Dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "verdict": verdict,
        "tone": tone,
        "available": available,
        "details": [dict(item) for item in (details or [])],
    }


def _factor_value(factors: Sequence[Mapping[str, Any]], key: str) -> str:
    for row in factors or []:
        if str(row.get("key") or "") == key:
            return str(row.get("value") or "").strip()
    return ""


# --------------------------------------------------------------------------- #
# 首页大盘（M2-A）：指标 / 环境评级 / 总结（纯函数，便于单测）
# --------------------------------------------------------------------------- #
REGIME_UNKNOWN = {
    "level": "unknown",
    "label": "数据不足",
    "tone": "neutral",
    "advice": "指数历史数据不足，暂不评级",
}

_ALIGN_LABELS = {"bull": "多头排列", "bear": "空头排列", "mixed": "均线纠缠"}


def _fmt_pct(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{value:+.{digits}f}%"


def _volume_ratio_text(ratio: Optional[float]) -> str:
    if ratio is None:
        return "-"
    if ratio >= 1.1:
        mood = "放量"
    elif ratio <= 0.9:
        mood = "缩量"
    else:
        mood = "平量"
    return f"{ratio:.2f}×（{mood}）"


def _rsi_text(rsi: Optional[float]) -> str:
    if rsi is None:
        return "-"
    if rsi >= 70:
        mood = "偏热"
    elif rsi <= 30:
        mood = "超卖"
    else:
        mood = "中性"
    return f"{rsi:.0f}（{mood}）"


def _range_position_text(position: Optional[float]) -> str:
    if position is None:
        return "-"
    pct = position * 100.0
    if pct >= 70:
        mood = "偏上"
    elif pct <= 30:
        mood = "偏下"
    else:
        mood = "中位"
    return f"{pct:.0f}%（{mood}）"


def judge_market_regime(
    *,
    close: Optional[float],
    ma20: Optional[float],
    ma20_prev: Optional[float],
    volume_ratio: Optional[float],
) -> Dict[str, str]:
    """2×2 矩阵评级：价格相对 MA20 × MA20 斜率。

    v0.1 只覆盖"站上+上行""跌破+下行"两个象限，另两种组合会被误判成震荡，
    这里补齐「趋势回调」与「下跌反弹」，文案与趋势一致。
    """

    if close is None or ma20 is None or ma20_prev is None:
        return dict(REGIME_UNKNOWN)

    above = close >= ma20
    rising = ma20 > ma20_prev

    if above and rising:
        if volume_ratio is not None and volume_ratio >= 1.0:
            return {
                "level": "bullish",
                "label": "偏多（放量）",
                "tone": "up",
                "advice": "趋势与量能同向，回踩 MA20 不破可分批低吸，不追高",
            }
        return {
            "level": "bullish_quiet",
            "label": "偏多（缩量）",
            "tone": "up",
            "advice": "缩量上行，追高性价比低，等回踩再加",
        }
    if above and not rising:
        return {
            "level": "rebound",
            "label": "下跌反弹",
            "tone": "warning",
            "advice": "反弹不追、不重仓；站不稳 MA20 视为未反转",
        }
    if (not above) and rising:
        return {
            "level": "pullback",
            "label": "趋势回调",
            "tone": "warning",
            "advice": "上升趋势中的回调，等站回 MA20 且缩量止跌再加，不追杀",
        }
    return {
        "level": "bear",
        "label": "偏空",
        "tone": "down",
        "advice": "控制仓位，反弹不宜加仓；等站回 MA20 再谈",
    }


def build_market_indicators(
    *,
    ma_dev_pct: Optional[float],
    ma_align: Optional[str],
    volume_ratio: Optional[float],
    rsi: Optional[float],
    range20: Optional[float],
    chg5: Optional[float],
) -> List[Dict[str, str]]:
    """6 格指标（H2~H7）。值缺失一律显示 ``-``，不猜。"""

    if ma_dev_pct is None:
        ma20_value, ma20_tone = "-", "neutral"
    else:
        side = "上方" if ma_dev_pct >= 0 else "下方"
        ma20_value = f"{side} {_fmt_pct(ma_dev_pct)}"
        ma20_tone = "up" if ma_dev_pct > 0 else ("down" if ma_dev_pct < 0 else "neutral")

    align_tone = {"bull": "up", "bear": "down"}.get(ma_align or "", "neutral")

    rsi_tone = "warning" if (rsi is not None and (rsi >= 70 or rsi <= 30)) else "neutral"
    chg_tone = "up" if (chg5 or 0) > 0 else ("down" if (chg5 or 0) < 0 else "neutral")

    return [
        {"key": "ma20", "label": "MA20 位置", "value": ma20_value, "tone": ma20_tone},
        {
            "key": "ma_align",
            "label": "均线排列",
            "value": _ALIGN_LABELS.get(ma_align or "", "-"),
            "tone": align_tone,
        },
        {"key": "volume", "label": "量能比", "value": _volume_ratio_text(volume_ratio), "tone": "neutral"},
        {"key": "rsi", "label": "RSI(14)", "value": _rsi_text(rsi), "tone": rsi_tone},
        {"key": "range20", "label": "20 日位置", "value": _range_position_text(range20), "tone": "neutral"},
        {"key": "chg5", "label": "5 日涨幅", "value": _fmt_pct(chg5), "tone": chg_tone},
    ]


def _count_display_actions(labels: Sequence[str]) -> Dict[str, int]:
    """把列表的展示动作聚成四类计数（与列表口径同源，保证总结数字可核对）。"""

    counts = {"reduce": 0, "increase": 0, "hold": 0, "watch": 0, "none": 0}
    for label in labels:
        if label in {"减仓", "清仓"}:
            counts["reduce"] += 1
        elif label in {"建仓", "加仓"}:
            counts["increase"] += 1
        elif label == "持有":
            counts["hold"] += 1
        elif label == NO_SIGNAL_LABEL:
            counts["none"] += 1
        else:
            counts["watch"] += 1
    return counts


def build_market_summary(
    *,
    index: Mapping[str, Any],
    regime: Mapping[str, str],
    trend_text: str,
    volume_text: str,
    counts: Mapping[str, int],
) -> str:
    """一段人话总结。第三句仅在计数自洽时输出（x+y+z+k == N）。"""

    close = _finite(index.get("close"))
    name = str(index.get("name") or "上证指数")
    if close is None:
        return "大盘数据暂不可用。"

    change = _finite(index.get("change_pct"))
    change_text = f"（{_fmt_pct(change)}）" if change is not None else ""
    first = f"{name}收于 {close:.2f} 点{change_text}，{trend_text}，{volume_text}。"
    second = f"环境{regime.get('label', '数据不足')}，操作上{regime.get('advice', '')}（个股仍按各自纪律执行）。"

    total = sum(int(value or 0) for value in counts.values())
    if total <= 0:
        return first + second

    bits: List[str] = []
    if counts.get("reduce"):
        bits.append(f"{counts['reduce']} 只需减/清")
    if counts.get("increase"):
        bits.append(f"{counts['increase']} 只可加/建")
    if counts.get("hold"):
        bits.append(f"{counts['hold']} 只持有")
    if counts.get("watch"):
        bits.append(f"{counts['watch']} 只观望")
    if counts.get("none"):
        bits.append(f"{counts['none']} 只暂无信号")
    if not bits:
        return first + second
    return first + second + f"自选股 {total} 只中，" + "、".join(bits) + "。"


def build_market_block(core: Mapping[str, Any], display_actions: Sequence[str]) -> Dict[str, Any]:
    """把大盘核心数据 + 列表动作统计组装成首页 ``market`` 字段。"""

    counts = _count_display_actions(display_actions)
    payload = dict(core)
    payload["counts"] = counts
    payload["summary"] = build_market_summary(
        index=payload.get("index") or {},
        regime=payload.get("regime") or REGIME_UNKNOWN,
        trend_text=str(payload.get("trend_text") or ""),
        volume_text=str(payload.get("volume_text") or ""),
        counts=counts,
    )
    # trend_text / volume_text 只用于拼句子，不下发给前端
    payload.pop("trend_text", None)
    payload.pop("volume_text", None)
    return payload



# --------------------------------------------------------------------------- #
# 五维分析组装（纯函数，便于单测）
# --------------------------------------------------------------------------- #
def build_analysis(
    overview: Mapping[str, Any],
    market: Optional[Mapping[str, Any]],
    sector: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """把 overview + 大盘快照 + 板块数据组装成五块分析。

    顺序（M2-B 调整）：技术面 → 板块 → 风险 → 大盘 → 消息面。
    板块回答"是个股问题还是行业问题"，属归因信息，故排在消息面之前。
    """

    factors = overview.get("factors") or []
    events = [str(item).strip() for item in (overview.get("events") or []) if str(item or "").strip()]

    # ---------------- 技术面 ---------------- #
    trend = _factor_value(factors, "trend")
    weekly = _factor_value(factors, "weekly_trend")
    conflict = _factor_value(factors, "multi_period_conflict")
    technical_details: List[Dict[str, str]] = []
    for key in ("scenario", "trend", "weekly_trend", "multi_period_conflict", "score", "confidence", "box_high"):
        value = _factor_value(factors, key)
        if value:
            technical_details.append({"label": _FACTOR_LABELS.get(key, key), "value": value})
    scenario_basis = _factor_value(factors, "scenario_reasons")
    if scenario_basis:
        technical_details.append({"label": "场景依据", "value": scenario_basis})

    verdict_bits = [str(overview.get("scenario_label") or "趋势不明")]
    if trend:
        verdict_bits.append(f"日线{trend}")
    if weekly:
        verdict_bits.append(f"周线{weekly}")
    if conflict.startswith("存在冲突"):
        verdict_bits.append("大小周期方向不一致，按谨慎处理")
    technical_available = bool(technical_details)
    technical_tone = "warning" if conflict.startswith("存在冲突") else (
        "up" if trend == "向上" else ("down" if trend == "向下" else "neutral")
    )
    if not technical_available:
        technical_tone = "neutral"

    # ---------------- 消息面 ---------------- #
    risk_hits = [item for item in events if any(word in item for word in _RISK_KEYWORDS)]
    if events:
        news_verdict = f"今日 {len(events)} 条事件提醒" + (f"，其中 {len(risk_hits)} 条偏风险" if risk_hits else "")
        news_tone = "warning" if risk_hits else "neutral"
    else:
        news_verdict = "今日无事件提醒（消息面中性）"
        news_tone = "neutral"

    # ---------------- 风险 ---------------- #
    risk_reasons = _factor_value(factors, "risk_reasons")
    risk_summary = str(overview.get("risk_summary") or "").strip()
    risk_details: List[Dict[str, str]] = []
    if risk_summary:
        risk_details.append({"label": "风控摘要", "value": risk_summary})
    if risk_reasons:
        risk_details.append({"label": "风控依据", "value": risk_reasons})
    if overview.get("stop_loss") is not None:
        risk_details.append({"label": "止损位", "value": format_price(overview.get("stop_loss"))})
    if overview.get("target_price") is not None:
        risk_details.append({"label": "目标价", "value": format_price(overview.get("target_price"))})
    if overview.get("position_ratio_label") not in (None, "", "-"):
        risk_details.append({"label": "仓位建议", "value": str(overview.get("position_ratio_label"))})
    if overview.get("watch_conditions"):
        risk_details.append({"label": "观察条件", "value": str(overview.get("watch_conditions"))})

    if risk_summary:
        risk_verdict = risk_summary
    elif risk_reasons:
        risk_verdict = risk_reasons
    elif risk_details:
        risk_verdict = "已给出止损位与仓位纪律，按计划执行"
    else:
        risk_verdict = "暂未触发风控条件（暂无止损/仓位建议）"
    risk_tone = "warning" if (risk_summary or risk_reasons) else "neutral"

    # ---------------- 大盘 ---------------- #
    indices = list((market or {}).get("indices") or [])
    priced = [item for item in indices if item.get("change_pct") is not None]
    market_details = [
        {
            "label": str(item.get("name") or item.get("code") or "指数"),
            "value": (
                f"{format_price(item.get('value'))}（{_pct_text(_finite(item.get('change_pct')))}）"
                if item.get("value") is not None
                else "-"
            ),
        }
        for item in indices
    ]
    if priced:
        market_verdict = "｜".join(
            f"{item.get('name')} {_pct_text(_finite(item.get('change_pct')))}" for item in priced
        )
        total_change = sum(_finite(item.get("change_pct")) or 0 for item in priced)
        market_tone = "up" if total_change > 0 else ("down" if total_change < 0 else "neutral")
        market_available = True
    else:
        market_verdict = "指数行情暂不可用（数据源未就绪或非交易时段无数据）"
        market_tone = "neutral"
        market_available = False

    # ---------------- 板块（M2-B 真实数据，取不到则如实降级） ---------------- #
    sector_result = sector if isinstance(sector, Mapping) else {}
    sector_detail = sector_result.get("detail") if isinstance(sector_result.get("detail"), Mapping) else {}
    if sector_result.get("available"):
        sector_details: List[Dict[str, str]] = [
            {"label": "所属行业", "value": str(sector_detail.get("industry") or "-")}
        ]
        if sector_detail.get("change_pct") is not None:
            sector_details.append({"label": "板块当日", "value": _fmt_pct(_finite(sector_detail.get("change_pct")))})
        if sector_detail.get("ranking_available") and sector_detail.get("rank") and sector_detail.get("total"):
            sector_details.append(
                {"label": "行业排名", "value": f"{sector_detail['rank']}/{sector_detail['total']}"}
            )
        if sector_detail.get("relative_pct") is not None:
            sector_details.append(
                {"label": "个股 vs 板块", "value": _fmt_pct(_finite(sector_detail.get("relative_pct")))}
            )
        if sector_detail.get("advancers") is not None and sector_detail.get("decliners") is not None:
            sector_details.append(
                {
                    "label": "板块内宽度",
                    "value": f"上涨 {sector_detail['advancers']} / 下跌 {sector_detail['decliners']}",
                }
            )
        sector_change = _finite(sector_detail.get("change_pct"))
        sector_tone = "up" if (sector_change or 0) > 0 else ("down" if (sector_change or 0) < 0 else "neutral")
        sector_section = _section(
            "sector",
            "板块",
            str(sector_result.get("verdict") or ""),
            details=sector_details,
            tone=sector_tone,
            available=True,
        )
    else:
        sector_section = _section(
            "sector",
            "板块",
            str(sector_result.get("verdict") or "板块数据暂不可用"),
            tone="neutral",
            available=False,
        )
    if sector_detail:
        sector_section["sector_detail"] = dict(sector_detail)

    return [
        _section(
            "technical",
            "技术面",
            "｜".join(bit for bit in verdict_bits if bit),
            details=technical_details,
            tone=technical_tone,
            available=technical_available,
        ),
        sector_section,
        _section("risk", "风险", risk_verdict, details=risk_details, tone=risk_tone, available=bool(risk_details)),
        _section("market", "大盘", market_verdict, details=market_details, tone=market_tone, available=market_available),
        _section("news", "消息面", news_verdict, details=[{"label": "事件", "value": item} for item in events[:6]], tone=news_tone),
    ]


_FACTOR_LABELS = {
    "scenario": "场景判定",
    "trend": "日线趋势",
    "weekly_trend": "周线趋势",
    "multi_period_conflict": "多周期共振",
    "score": "综合评分",
    "confidence": "置信度",
    "box_high": "箱体上沿",
    "risk_reasons": "风控依据",
    "scenario_reasons": "场景依据",
}


# --------------------------------------------------------------------------- #
# 编排服务
# --------------------------------------------------------------------------- #
class H5Service:
    """移动 H5 列表 / 详情聚合。依赖全可注入，便于离线单测。"""

    def __init__(
        self,
        *,
        overview_service: Any = None,
        watchlist_service: Any = None,
        stock_service: Any = None,
        sector_service: Any = None,
        fetch: Optional[Callable[[str, int], Any]] = None,
        max_workers: int = 6,
        cache_ttl_seconds: int = 15,
        market_cache_ttl_seconds: int = 900,
        sector_cache_ttl_seconds: int = 900,
        industry_cache_ttl_seconds: int = 604800,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self._overview_service = overview_service
        self._watchlist_service = watchlist_service
        self._stock_service = stock_service
        self._sector_service = sector_service
        self._fetch = fetch
        self.max_workers = max(1, int(max_workers))
        self.cache_ttl_seconds = max(0, int(cache_ttl_seconds))
        self.market_cache_ttl_seconds = max(0, int(market_cache_ttl_seconds))
        self.sector_cache_ttl_seconds = max(0, int(sector_cache_ttl_seconds))
        self.industry_cache_ttl_seconds = max(0, int(industry_cache_ttl_seconds))
        self._now = now or datetime.now
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._market_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    # ---------------- 依赖 ---------------- #
    @property
    def overview_service(self):
        if self._overview_service is None:
            from src.services.stock_overview_service import StockOverviewService

            self._overview_service = StockOverviewService()
        return self._overview_service

    @property
    def sector_service(self):
        if self._sector_service is None:
            from src.services.sector_service import SectorService

            self._sector_service = SectorService(
                snapshot_ttl_seconds=self.sector_cache_ttl_seconds,
                industry_ttl_seconds=self.industry_cache_ttl_seconds,
            )
        return self._sector_service

    @property
    def watchlist_service(self):
        if self._watchlist_service is None:
            from src.services.watchlist_service import WatchlistService

            self._watchlist_service = WatchlistService()
        return self._watchlist_service

    @property
    def stock_service(self):
        if self._stock_service is None:
            from src.services.stock_service import StockService

            self._stock_service = StockService()
        return self._stock_service

    # ---------------- 取数 ---------------- #
    def _load_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        try:
            quote = self.stock_service.get_realtime_quote(symbol)
            return quote if isinstance(quote, dict) and quote else None
        except Exception as exc:  # pragma: no cover - 依赖外部数据源
            logger.warning("H5 行情获取失败 %s: %s", symbol, exc)
            return None

    def _fetch_daily(self, symbol: str, days: int):  # pragma: no cover - 依赖网络
        if self._fetch is not None:
            return self._fetch(symbol, days)
        from src.services.p0_signal_builder import P0SignalBuilder

        return P0SignalBuilder._fetch_daily(symbol, days)  # noqa: SLF001 - 复用同口径

    # ---------------- 缓存 ---------------- #
    def _cache_get(self, key: str) -> Optional[Dict[str, Any]]:
        if self.cache_ttl_seconds <= 0:
            return None
        hit = self._cache.get(key)
        if not hit:
            return None
        stamp, payload = hit
        if (time.time() - stamp) > self.cache_ttl_seconds:
            self._cache.pop(key, None)
            return None
        return payload

    def _cache_put(self, key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.cache_ttl_seconds > 0:
            self._cache[key] = (time.time(), payload)
        return payload

    def _market_cache_get(self, key: str) -> Optional[Dict[str, Any]]:
        if self.market_cache_ttl_seconds <= 0:
            return None
        hit = self._market_cache.get(key)
        if not hit:
            return None
        stamp, payload = hit
        if (time.time() - stamp) > self.market_cache_ttl_seconds:
            self._market_cache.pop(key, None)
            return None
        return payload

    def _market_cache_put(self, key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.market_cache_ttl_seconds > 0:
            self._market_cache[key] = (time.time(), payload)
        return payload

    # ---------------- 列表 ---------------- #
    def _watchlist_rows(self, degraded: List[str]) -> List[Dict[str, Any]]:
        try:
            rows = list(self.watchlist_service.list_watchlist(enabled_only=True) or [])
        except Exception as exc:
            logger.warning("H5 读取自选股失败：%s", exc)
            degraded.append(f"自选股读取失败（{type(exc).__name__}）")
            return []

        if rows:
            return [row for row in rows if isinstance(row, dict) and str(row.get("symbol") or "").strip()]

        # 自选股表为空时只读回退配置 STOCK_LIST（不含类型，仅保证页面可用）
        try:
            from src.config import get_config

            codes = [str(code).strip() for code in (get_config().stock_list or []) if str(code).strip()]
        except Exception as exc:  # pragma: no cover - 配置读取失败
            logger.warning("H5 回退 STOCK_LIST 失败：%s", exc)
            codes = []
        if codes:
            degraded.append("自选股表为空，已回退配置 STOCK_LIST（名称与类型可能缺失）")
            return [{"symbol": code, "name": "", "asset_type": ""} for code in codes]
        return []

    def _build_card(self, row: Mapping[str, Any], degraded: List[str]) -> Dict[str, Any]:
        symbol = str(row.get("symbol") or "").strip().upper()
        quote = self._load_quote(symbol)
        try:
            # 行情已取过一次：失败时不再让 overview 重试（with_quote=False），
            # 避免列表对同一标的发两次请求。
            overview = self.overview_service.load(
                symbol,
                with_events=False,
                with_quote=quote is not None,
                quote=quote,
            ) or {}
        except Exception as exc:
            logger.warning("H5 列表聚合失败 %s：%s", symbol, exc)
            degraded.append(f"{symbol} 聚合失败（{type(exc).__name__}）")
            overview = {}

        has_signal = overview.get("signal_id") is not None
        action = str(overview.get("action") or "watch")
        label = display_action(action, has_signal)
        price = _finite((quote or {}).get("current_price"))
        change_pct = _finite((quote or {}).get("change_percent"))
        name = str(row.get("name") or overview.get("name") or "").strip() or symbol

        return {
            "symbol": symbol,
            "name": name,
            "asset_type": str(row.get("asset_type") or "").strip(),
            "price": price,
            "change_percent": change_pct,
            "action": action,
            "action_label": str(overview.get("action_label") or ""),
            "display_action": label,
            "action_tone": action_tone(label),
            "has_signal": has_signal,
            "is_urgent": bool(overview.get("is_urgent")),
            "scenario_label": str(overview.get("scenario_label") or ""),
            "signal_created_at": overview.get("signal_created_at"),
            "rank": _ACTION_RANK.get(label, 4),
        }

    def list_watchlist(self, *, force: bool = False, with_market: bool = True) -> Dict[str, Any]:
        """自选股列表（含首页大盘块）：名称 / 代码 / 当日价 / 涨跌幅 / 建议。"""

        if not force:
            cached = self._cache_get("watchlist")
            if cached is not None:
                return cached if with_market else {**cached, "market": None}

        degraded: List[str] = []
        rows = self._watchlist_rows(degraded)

        cards: List[Dict[str, Any]] = []
        if rows:
            symbols = [str(row.get("symbol") or "").strip().upper() for row in rows]
            workers = min(self.max_workers, max(1, len(rows)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(lambda row: self._build_card(row, degraded), rows))
            by_symbol = {str(item.get("symbol") or ""): item for item in results}
            # 保持自选股配置顺序，避免并发返回顺序漂移
            cards = [by_symbol[symbol] for symbol in symbols if symbol in by_symbol]

        cards.sort(key=lambda item: (0 if item.get("is_urgent") else 1, item.get("rank", 4)))
        for card in cards:
            card.pop("rank", None)

        # 列表不重复取行情时间：行情时间在详情页单独标注，避免 N 次额外请求
        as_of = str(self._now().strftime("%Y-%m-%d"))
        priced = [card for card in cards if card.get("price") is not None]
        if cards and not priced:
            degraded.append("全部标的行情不可用：价格已隐藏")

        payload = {
            "as_of": as_of,
            "market": self._market_block([card.get("display_action") or "" for card in cards]),
            "items": cards,
            "count": len(cards),
            "degraded": degraded,
            "disclaimer": _disclaimer(),
        }
        cached_payload = self._cache_put("watchlist", payload)
        if with_market:
            return cached_payload
        # 副本去掉 market，缓存本身保持完整（?with_market=false 供轻量轮询）
        return {**cached_payload, "market": None}

    # ---------------- 首页大盘 ---------------- #
    def _market_block(self, display_actions: Sequence[str]) -> Dict[str, Any]:
        try:
            return build_market_block(self._market_core(), display_actions)
        except Exception as exc:  # pragma: no cover - 大盘异常不应拖垮列表
            logger.warning("首页大盘聚合失败：%s", exc)
            return build_market_block(
                {
                    "index": {"code": "sh000001", "name": "上证指数", "close": None, "change_pct": None},
                    "others": [],
                    "indicators": [],
                    "regime": dict(REGIME_UNKNOWN),
                    "advice": REGIME_UNKNOWN["advice"],
                    "alerts": [],
                    "as_of": self._now().strftime("%Y-%m-%d"),
                    "degraded": ["大盘数据暂不可用"],
                },
                display_actions,
            )

    def _market_core(self) -> Dict[str, Any]:
        """大盘核心数据（评级/指标/建议），按交易日缓存，与列表缓存解耦。"""

        date_key = self._now().strftime("%Y-%m-%d")
        cached = self._market_cache_get(date_key)
        if cached is not None:
            return cached

        degraded: List[str] = []
        snapshot = self._market_snapshot(degraded)
        indices = snapshot.get("indices") or []
        primary = next((item for item in indices if item.get("code") == "sh000001"), None) or {}
        others = [
            {"code": item.get("code"), "name": item.get("name"), "change_pct": item.get("change_pct")}
            for item in indices
            if item.get("code") != "sh000001"
        ]

        index_payload: Dict[str, Any] = {
            "code": "sh000001",
            "name": primary.get("name") or "上证指数",
            "close": _finite(primary.get("value")),
            "change_pct": _finite(primary.get("change_pct")),
        }

        indicators: List[Dict[str, str]] = []
        regime: Dict[str, str] = dict(REGIME_UNKNOWN)
        trend_text = ""
        volume_text = ""
        alerts: List[str] = []

        bars = None
        try:
            bars = self._fetch_daily("sh000001", 240)
        except Exception as exc:  # pragma: no cover - 依赖网络
            logger.warning("上证指数历史取数失败：%s", exc)

        if bars is None or len(bars) < 60:
            degraded.append("指数历史数据不足 60 根，暂不评级")
        else:
            try:
                from src.core.indicators import build_indicator_snapshot

                snap = build_indicator_snapshot(bars, min_bars=60)
                close = _finite(snap.close)
                ma5 = _finite(snap.ma.get(5))
                ma10 = _finite(snap.ma.get(10))
                ma20 = _finite(snap.ma.get(20))
                ma20_prev = _finite(snap.prev_ma.get(20))
                if close is not None and primary.get("value") is None:
                    index_payload["close"] = close

                ma_dev_pct = ((close - ma20) / ma20 * 100.0) if close is not None and ma20 else None
                ma_align = None
                if ma5 is not None and ma10 is not None and ma20 is not None:
                    if ma5 > ma10 > ma20:
                        ma_align = "bull"
                    elif ma5 < ma10 < ma20:
                        ma_align = "bear"
                    else:
                        ma_align = "mixed"
                volume = _finite(snap.volume)
                avg_volume_5 = _finite(snap.avg_volume_5)
                volume_ratio = (volume / avg_volume_5) if (volume is not None and avg_volume_5) else None
                rsi = _finite(snap.rsi)
                high20 = _finite(snap.high_20)
                low20 = _finite(snap.low_20)
                range20 = None
                if close is not None and high20 is not None and low20 is not None and high20 > low20:
                    range20 = (close - low20) / (high20 - low20)

                closes = bars["close"]
                close_5d_ago = _finite(closes.iloc[-6]) if len(bars) >= 6 else None
                chg5 = ((close - close_5d_ago) / close_5d_ago * 100.0) if (close is not None and close_5d_ago) else None

                indicators = build_market_indicators(
                    ma_dev_pct=ma_dev_pct,
                    ma_align=ma_align,
                    volume_ratio=volume_ratio,
                    rsi=rsi,
                    range20=range20,
                    chg5=chg5,
                )
                regime = judge_market_regime(
                    close=close, ma20=ma20, ma20_prev=ma20_prev, volume_ratio=volume_ratio
                )

                trend_text = {
                    "bullish": "站上 MA20 且 MA20 上行",
                    "bullish_quiet": "站上 MA20 且 MA20 上行",
                    "rebound": "站上 MA20 但 MA20 仍下行",
                    "pullback": "跌破 MA20 但 MA20 仍上行",
                    "bear": "跌破 MA20 且 MA20 下行",
                }.get(regime.get("level", ""), "")
                if volume_ratio is None:
                    volume_text = "量能数据缺失"
                else:
                    mood = "放量" if volume_ratio >= 1.1 else ("缩量" if volume_ratio <= 0.9 else "平量")
                    volume_text = f"量能 {volume_ratio:.2f} 倍（{mood}）"

                if volume_ratio is None:
                    alerts.append("量能数据缺失，仅按价格与均线判断")
                if ma5 is not None and ma10 is not None and ma5 < ma10:
                    alerts.append("短期均线转弱")
                if (rsi is not None and rsi >= 70) or (range20 is not None and range20 >= 0.85):
                    alerts.append("短期偏热，注意冲高回落")
                if rsi is not None and rsi <= 30:
                    alerts.append("超卖，留意反弹")
            except Exception as exc:  # pragma: no cover - 指标异常不阻断
                logger.warning("上证指标计算失败：%s", exc)
                degraded.append("指数指标计算失败，暂不评级")

        payload = {
            "index": index_payload,
            "others": others,
            "indicators": indicators,
            "regime": regime,
            "advice": regime.get("advice", ""),
            "alerts": list(dict.fromkeys(alerts))[:2],
            "as_of": snapshot.get("as_of") or date_key,
            "degraded": degraded,
            "trend_text": trend_text,
            "volume_text": volume_text,
        }
        return self._market_cache_put(date_key, payload)

    # ---------------- 详情 ---------------- #
    def _market_snapshot(self, degraded: List[str]) -> Dict[str, Any]:
        cached = self._cache_get("market")
        if cached is not None:
            return cached

        from src.services.weekly_review_service import DEFAULT_INDEX_CODES

        indices: List[Dict[str, Any]] = []
        dates: List[str] = []
        for code, name in DEFAULT_INDEX_CODES:
            try:
                bars = self._fetch_daily(code, 20)
            except Exception as exc:  # pragma: no cover - 依赖网络
                logger.warning("H5 指数取数失败 %s：%s", code, exc)
                bars = None
            item: Dict[str, Any] = {"code": code, "name": name, "value": None, "change_pct": None}
            if bars is not None and len(bars) > 0:
                try:
                    last = _finite(bars["close"].iloc[-1])
                    prev = _finite(bars["close"].iloc[-2]) if len(bars) > 1 else None
                    item["value"] = last
                    if last is not None and prev:
                        item["change_pct"] = round((last - prev) / prev * 100.0, 3)
                    date_value = bars["date"].iloc[-1] if "date" in getattr(bars, "columns", []) else None
                    if date_value is not None and hasattr(date_value, "strftime"):
                        dates.append(date_value.strftime("%Y-%m-%d"))
                except Exception as exc:  # pragma: no cover - 数据格式异常
                    logger.warning("H5 指数解析失败 %s：%s", code, exc)
            indices.append(item)

        if all(item["value"] is None for item in indices):
            degraded.append("指数行情暂不可用（数据源未就绪或非交易时段无数据）")

        payload = {"indices": indices, "as_of": max(dates) if dates else ""}
        return self._cache_put("market", payload)

    def _sector_analyze(self, symbol: str, change_pct: Optional[float]) -> Dict[str, Any]:
        """板块归属 + 行业排行；任何异常只降级板块块（NFR-8）。"""

        try:
            return dict(self.sector_service.analyze(symbol, change_pct) or {})
        except Exception as exc:  # pragma: no cover - 依赖外部数据源
            logger.warning("板块分析失败 %s：%s", symbol, exc)
            return {
                "available": False,
                "detail": {
                    "industry": "",
                    "change_pct": None,
                    "rank": None,
                    "total": None,
                    "relative_pct": None,
                    "advancers": None,
                    "decliners": None,
                    "ranking_available": False,
                },
                "verdict": "板块数据暂不可用",
            }

    def detail(self, symbol: str) -> Dict[str, Any]:
        """详情：四价 + 建议总结 + 五维分析。"""

        normalized = str(symbol or "").strip().upper()
        degraded: List[str] = []

        quote = self._load_quote(normalized)
        stock_change = _finite((quote or {}).get("change_percent"))

        # 板块与指数相互独立：并发取数，避免串行把详情拖到 3~5 秒。
        with ThreadPoolExecutor(max_workers=2) as pool:
            sector_future = pool.submit(self._sector_analyze, normalized, stock_change)
            market_future = pool.submit(self._market_snapshot, degraded)
            try:
                # 已取过行情：成功则注入避免重复取数；失败则关闭内部取数，
                # 由 overview 统一写入「行情暂不可用」的降级说明。
                overview = self.overview_service.load(
                    normalized,
                    with_quote=quote is not None,
                    quote=quote,
                ) or {}
            except Exception as exc:
                logger.error("H5 详情聚合失败 %s：%s", normalized, exc)
                raise

            sector = sector_future.result()
            market = market_future.result()

        sections = build_analysis(overview, market, sector)

        has_signal = overview.get("signal_id") is not None
        action = str(overview.get("action") or "watch")
        label = display_action(action, has_signal)

        quote_payload = None
        distance_to_stop = overview.get("distance_to_stop_pct")
        distance_to_target = overview.get("distance_to_target_pct")
        if quote:
            close_price = _finite(quote.get("current_price"))
            quote_payload = {
                "open": _finite(quote.get("open")),
                "high": _finite(quote.get("high")),
                "low": _finite(quote.get("low")),
                "close": close_price,
                "prev_close": _finite(quote.get("prev_close")),
                "change": _finite(quote.get("change")),
                "change_percent": _finite(quote.get("change_percent")),
                "volume": _finite(quote.get("volume")),
                "update_time": quote.get("update_time"),
            }
            stop = _finite(overview.get("stop_loss"))
            target = _finite(overview.get("target_price"))
            if close_price is not None and stop:
                distance_to_stop = round((close_price - stop) / stop * 100.0, 2)
            if close_price is not None and target:
                distance_to_target = round((close_price - target) / target * 100.0, 2)

        total_degraded = list(overview.get("degraded") or [])
        for note in degraded:
            if note not in total_degraded:
                total_degraded.append(note)

        return {
            "symbol": normalized,
            "name": str(overview.get("name") or "").strip(),
            "as_of": str(overview.get("as_of") or ""),
            "generated_at": str(overview.get("generated_at") or ""),
            "action": action,
            "action_label": str(overview.get("action_label") or ""),
            "display_action": label,
            "action_tone": action_tone(label),
            "has_signal": has_signal,
            "is_urgent": bool(overview.get("is_urgent")),
            "scenario": str(overview.get("scenario") or ""),
            "scenario_label": str(overview.get("scenario_label") or ""),
            "conclusion": str(overview.get("conclusion") or ""),
            "reason": str(overview.get("reason") or ""),
            "score": overview.get("score"),
            "confidence": overview.get("confidence"),
            "quote": quote_payload,
            "entry_low": overview.get("entry_low"),
            "entry_high": overview.get("entry_high"),
            "entry_zone": str(overview.get("entry_zone") or "-"),
            "stop_loss": overview.get("stop_loss"),
            "target_price": overview.get("target_price"),
            "position_ratio": overview.get("position_ratio"),
            "position_ratio_label": str(overview.get("position_ratio_label") or "-"),
            "position_reason": str(overview.get("position_reason") or ""),
            "discipline": str(overview.get("discipline") or ""),
            "watch_conditions": str(overview.get("watch_conditions") or ""),
            "risk_summary": str(overview.get("risk_summary") or ""),
            "distance_to_stop_pct": distance_to_stop,
            "distance_to_target_pct": distance_to_target,
            "market": market,
            "analysis": sections,
            "signal_id": overview.get("signal_id"),
            "signal_created_at": overview.get("signal_created_at"),
            "degraded": total_degraded,
            "disclaimer": str(overview.get("disclaimer") or _disclaimer()),
        }


def _disclaimer() -> str:
    from src.services.daily_digest import DISCLAIMER

    return DISCLAIMER
