# -*- coding: utf-8 -*-
"""高抛低吸助手 · 每日概览组装（P1 · FR-10 / FR-8 / NFR-3 / NFR-5）

把 P0 生成的信号组装成一条「每日概览」推送，核心能力：

1. **分级**（FR-10.3）：紧急 / 重点 / 观望三级——紧急置顶，重点次之，观望折叠；
2. **降噪**（NFR-3）：与上一交易日**完全相同的观望/持有**信号折叠为「无变化 N 只」，
   避免天天刷屏；紧急/重点信号**永不折叠**；
3. **双层信息**（FR-8）：一句话人话结论 + 可展开的专业依据；
4. **纪律卡**（FR-8.4）：每只标的给出「动作 / 价位 / 仓位 / 止损」；
5. **免责声明**（NFR-5）：固定尾注；
6. **事件与风险**（FR-20）：可选板块，未接入事件源时降级为"暂无"。

本模块**纯函数、无 IO**（events 由调用方注入），便于单测。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

__all__ = [
    "DISCLAIMER",
    "ACTION_LABELS",
    "SCENARIO_LABELS",
    "DigestItem",
    "DailyDigest",
    "normalize_signal",
    "classify_severity",
    "build_digest",
]

DISCLAIMER = (
    "本内容由程序基于历史数据自动生成，仅供学习参考，不构成任何投资建议，据此操作风险自负。"
)

ACTION_LABELS = {
    "build": "建底仓",
    "buy": "建仓/买入",
    "dip_buy": "超跌抄底",
    "add": "加仓",
    "hold": "持有",
    "reduce": "减仓",
    "sell": "清仓",
    "clear": "清仓",
    "wait": "观望",
    "watch": "观望",
    "avoid": "回避",
}

SCENARIO_LABELS = {
    "up": "上涨趋势",
    "down": "下跌趋势",
    "sideways": "震荡趋势",
    "breakout": "突破待确认",
    "divergence": "背离预警",
    "volatile": "高波动/异动",
    "unknown": "趋势不明",
}

# 需要"操作"的动作（进入重点及以上）
_ACTIONABLE = {"build", "buy", "dip_buy", "add", "reduce", "sell", "clear"}
# 触发即紧急的动作（风控触发才清仓，属需要立即处理）
_URGENT_ACTIONS = {"sell", "clear"}


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _price(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}" if abs(value) >= 1 else f"{value:.3f}"


def _pct(value: Any) -> str:
    number = _finite(value)
    if number is None:
        return "-"
    return f"{number * 100:.0f}%"


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class DigestItem:
    symbol: str
    name: str = ""
    action: str = "watch"
    scenario: str = "unknown"
    severity: str = "watch"          # urgent | highlight | watch
    is_urgent: bool = False
    reason: str = ""
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None
    position_ratio: Optional[float] = None
    position_reason: str = ""
    watch_conditions: str = ""
    risk_summary: str = ""
    confidence: Optional[float] = None
    score: Optional[int] = None
    asset_type: str = "stock"
    track_index: Optional[str] = None
    error: Optional[str] = None

    # ---------------- 展示 ---------------- #
    @property
    def display_name(self) -> str:
        if self.name and self.name != self.symbol:
            return f"{self.name}({self.symbol})"
        return self.symbol

    @property
    def action_label(self) -> str:
        return ACTION_LABELS.get(self.action, self.action)

    @property
    def scenario_label(self) -> str:
        return SCENARIO_LABELS.get(self.scenario, self.scenario or "趋势不明")

    def entry_zone(self) -> str:
        if self.entry_low is None and self.entry_high is None:
            return "-"
        if self.entry_low is not None and self.entry_high is not None and self.entry_low != self.entry_high:
            return f"{_price(self.entry_low)}~{_price(self.entry_high)}"
        return _price(self.entry_high if self.entry_high is not None else self.entry_low)

    def one_line(self) -> str:
        """一句话人话结论（小白优先）。"""
        parts = [f"{self.display_name}｜{self.action_label}（{self.scenario_label}）"]
        detail = self.reason or self.position_reason
        if detail:
            parts.append(f"理由：{detail}")
        return " ".join(parts)

    def discipline_line(self) -> str:
        """操作纪律卡：动作 / 价位 / 仓位 / 止损。"""
        bits = [f"仓位建议 {_pct(self.position_ratio)}"]
        zone = self.entry_zone()
        if zone != "-":
            bits.append(f"参考价位 {zone}")
        if self.stop_loss is not None:
            bits.append(f"止损 {_price(self.stop_loss)}")
        if self.target_price is not None:
            bits.append(f"目标 {_price(self.target_price)}")
        if self.watch_conditions:
            bits.append(f"纪律：{self.watch_conditions}")
        return " ｜ ".join(bits)

    def detail_lines(self) -> List[str]:
        lines = []
        if self.risk_summary:
            lines.append(f"风控依据：{self.risk_summary}")
        if self.confidence is not None or self.score is not None:
            conf = f"{self.confidence:.2f}" if self.confidence is not None else "-"
            lines.append(f"置信度 {conf}｜评分 {self.score if self.score is not None else '-'}")
        if self.asset_type and self.asset_type != "stock":
            extra = f"品种 {self.asset_type.upper()}"
            if self.track_index:
                extra += f"｜跟踪 {self.track_index}"
            lines.append(extra)
        return lines

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "action": self.action,
            "action_label": self.action_label,
            "scenario": self.scenario,
            "scenario_label": self.scenario_label,
            "severity": self.severity,
            "is_urgent": self.is_urgent,
            "reason": self.reason,
            "entry_low": self.entry_low,
            "entry_high": self.entry_high,
            "stop_loss": self.stop_loss,
            "target_price": self.target_price,
            "position_ratio": self.position_ratio,
            "confidence": self.confidence,
            "score": self.score,
            "asset_type": self.asset_type,
            "track_index": self.track_index,
            "error": self.error,
        }


@dataclass
class DailyDigest:
    as_of: str = ""
    generated_at: str = ""
    urgent: List[DigestItem] = field(default_factory=list)
    highlight: List[DigestItem] = field(default_factory=list)
    watch: List[DigestItem] = field(default_factory=list)
    failed: List[DigestItem] = field(default_factory=list)
    collapsed: List[DigestItem] = field(default_factory=list)
    events: List[str] = field(default_factory=list)
    state: Dict[str, Any] = field(default_factory=dict)
    # H5 基址（P2 · FR-14.0.2）：内网直连地址，如 http://192.168.1.10:8000。
    # 为空时**不生成任何链接**（推送文案自带完整摘要，链接仅作增强 · D6-I1）。
    h5_base_url: str = ""

    @property
    def counts(self) -> Dict[str, int]:
        return {
            "urgent": len(self.urgent),
            "highlight": len(self.highlight),
            "watch": len(self.watch),
            "failed": len(self.failed),
            "collapsed": len(self.collapsed),
            "total": len(self.urgent) + len(self.highlight) + len(self.watch) + len(self.failed),
        }

    @property
    def has_actions(self) -> bool:
        return bool(self.urgent or self.highlight)

    def headline(self) -> str:
        c = self.counts
        if c["urgent"]:
            return f"⚠️ 今日有 {c['urgent']} 条【紧急】信号，请及时处理"
        if c["highlight"]:
            return f"★ 今日有 {c['highlight']} 条重点信号"
        return "今日无操作信号，持仓按纪律持有即可"

    # ---------------- 渲染 ---------------- #
    def detail_url(self, symbol: str) -> str:
        """H5 个股详情深链（FR-14.0.2）；未配置基址时返回空串。"""
        base = (self.h5_base_url or "").strip().rstrip("/")
        if not base or not symbol:
            return ""
        return f"{base}/m/stocks/{symbol}?from=push"

    def to_markdown(self, *, max_watch: int = 10) -> str:
        lines: List[str] = []
        lines.append(f"【高抛低吸助手 · 每日概览】{self.as_of}")
        lines.append("")
        lines.append(self.headline())
        lines.append("")

        if self.urgent:
            lines.append(f"一、⚠️ 紧急（{len(self.urgent)}）")
            for idx, item in enumerate(self.urgent, 1):
                lines.extend(_render_item(idx, item, with_detail=True, detail_url=self.detail_url(item.symbol)))
            lines.append("")

        if self.highlight:
            lines.append(f"二、★ 重点信号（{len(self.highlight)}）")
            for idx, item in enumerate(self.highlight, 1):
                lines.extend(_render_item(idx, item, with_detail=True, detail_url=self.detail_url(item.symbol)))
            lines.append("")

        if self.watch:
            shown = self.watch[: max(0, int(max_watch))]
            extra = len(self.watch) - len(shown)
            lines.append(f"三、· 观望 / 持有（{len(self.watch)}）")
            for item in shown:
                lines.append(f"· {item.display_name}｜{item.action_label}（{item.scenario_label}）")
            if extra > 0:
                lines.append(f"· …另有 {extra} 只，详见 H5")
            lines.append("")

        if self.collapsed:
            lines.append(f"（{len(self.collapsed)} 只与上一交易日结论一致，已自动折叠）")
            lines.append("")

        if self.failed:
            lines.append(f"四、⚠ 数据异常（{len(self.failed)}）")
            for item in self.failed:
                lines.append(f"· {item.display_name}：分析失败（{item.error or '未知原因'}），请留意")
            lines.append("")

        lines.append("五、📅 事件与风险")
        if self.events:
            for event in self.events:
                lines.append(f"· {event}")
        else:
            lines.append("· 今日无重大事件提醒")
        lines.append("")

        lines.append("—— 免责声明 ——")
        lines.append(DISCLAIMER)
        return "\n".join(lines).strip()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "as_of": self.as_of,
            "generated_at": self.generated_at,
            "counts": self.counts,
            "headline": self.headline(),
            "urgent": [i.to_dict() for i in self.urgent],
            "highlight": [i.to_dict() for i in self.highlight],
            "watch": [i.to_dict() for i in self.watch],
            "failed": [i.to_dict() for i in self.failed],
            "collapsed": [i.to_dict() for i in self.collapsed],
            "events": list(self.events),
            "markdown": self.to_markdown(),
        }


def _render_item(
    index: int,
    item: DigestItem,
    *,
    with_detail: bool,
    detail_url: str = "",
) -> List[str]:
    lines = [f"{index}. {item.one_line()}"]
    discipline = item.discipline_line()
    if discipline:
        lines.append(f"   → {discipline}")
    if with_detail:
        for detail in item.detail_lines():
            lines.append(f"   · {detail}")
        if detail_url:
            lines.append(f"   ↗ 详情 {detail_url}")
    return lines


# --------------------------------------------------------------------------- #
# 归一化与分级
# --------------------------------------------------------------------------- #
def classify_severity(action: str, is_urgent: bool) -> str:
    """紧急 > 重点 > 观望。"""
    normalized = (action or "watch").strip().lower()
    if is_urgent or normalized in _URGENT_ACTIONS:
        return "urgent"
    if normalized in _ACTIONABLE:
        return "highlight"
    return "watch"


def normalize_signal(raw: Dict[str, Any]) -> DigestItem:
    """把 ``decision_signals`` 记录 / 信号载荷 / ``SymbolResult`` 归一为 ``DigestItem``。

    对缺字段容错（``.get`` + 类型清洗），不抛异常。
    """
    if raw is None:
        raise ValueError("raw signal is None")

    metadata = raw.get("metadata") or raw.get("metadata_json") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    position_advice = raw.get("position_advice") or metadata.get("position_advice") or {}
    if not isinstance(position_advice, dict):
        position_advice = {}

    action = raw.get("action") or raw.get("sub_action") or "watch"
    scenario = raw.get("scenario") or metadata.get("scenario") or "unknown"
    is_urgent = bool(raw.get("is_urgent") or metadata.get("is_urgent") or False)

    position_ratio = _finite(position_advice.get("ratio"))
    reason = raw.get("reason") or position_advice.get("reason") or ""

    item = DigestItem(
        symbol=str(raw.get("symbol") or raw.get("stock_code") or "").strip().upper(),
        name=str(raw.get("name") or raw.get("stock_name") or ""),
        action=str(action),
        scenario=str(scenario),
        severity=classify_severity(str(action), is_urgent),
        is_urgent=is_urgent,
        reason=str(reason),
        entry_low=_finite(raw.get("entry_low")),
        entry_high=_finite(raw.get("entry_high")),
        stop_loss=_finite(raw.get("stop_loss")),
        target_price=_finite(raw.get("target_price")),
        position_ratio=position_ratio,
        position_reason=str(position_advice.get("reason") or ""),
        watch_conditions=str(raw.get("watch_conditions") or ""),
        risk_summary=str(raw.get("risk_summary") or ""),
        confidence=_finite(raw.get("confidence")),
        score=int(raw["score"]) if isinstance(raw.get("score"), (int, float)) else None,
        asset_type=str(raw.get("asset_type") or metadata.get("asset_type") or "stock"),
        track_index=raw.get("track_index") or metadata.get("track_index"),
        error=str(raw["error"]) if raw.get("error") else None,
    )
    # 失败项统一归入 failed
    if item.error:
        item.severity = "failed"
    return item


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #
def build_digest(
    signals: Sequence[Dict[str, Any]],
    *,
    as_of: Optional[str] = None,
    events: Optional[Iterable[str]] = None,
    previous_state: Optional[Dict[str, Any]] = None,
    collapse_repeat_watch: bool = True,
    now: Optional[datetime] = None,
    h5_base_url: str = "",
) -> DailyDigest:
    """组装每日概览。

    Args:
        signals: 原始信号列表（``decision_signals`` 记录 dict 或信号载荷 dict）。
        as_of: 概览日期（缺省用 ``now`` 的日期）。
        events: 事件与风险文案列表（由调用方从事件源注入）。
        previous_state: 上一交易日的 ``{"symbols": {symbol: action}}``，用于降噪折叠。
        collapse_repeat_watch: 是否折叠"与昨日一致"的观望/持有项。
        h5_base_url: H5 基址（内网直连），配置后紧急/重点项会附带个股详情深链；
            留空则**完全保持原文案**（D6-I1：链接只是增强，关键信息必须在文案里）。
    """
    now = now or datetime.now()
    digest = DailyDigest(
        as_of=as_of or now.strftime("%Y-%m-%d"),
        generated_at=now.strftime("%Y-%m-%d %H:%M:%S"),
        events=[str(e) for e in (events or []) if str(e).strip()],
        h5_base_url=str(h5_base_url or "").strip(),
    )

    previous_symbols: Dict[str, str] = {}
    if isinstance(previous_state, dict):
        raw_prev = previous_state.get("symbols")
        if isinstance(raw_prev, dict):
            previous_symbols = {str(k).upper(): str(v) for k, v in raw_prev.items()}

    new_symbols: Dict[str, str] = {}
    for raw in signals or []:
        item = normalize_signal(raw)
        if not item.symbol:
            continue
        new_symbols[item.symbol] = item.action

        if item.severity == "failed":
            digest.failed.append(item)
            continue

        if (
            collapse_repeat_watch
            and item.severity == "watch"
            and previous_symbols.get(item.symbol) == item.action
        ):
            digest.collapsed.append(item)
            continue

        if item.severity == "urgent":
            digest.urgent.append(item)
        elif item.severity == "highlight":
            digest.highlight.append(item)
        else:
            digest.watch.append(item)

    # 稳定排序：紧急按评分降序，重点按动作优先级 + 评分
    digest.urgent.sort(key=lambda i: (i.score if i.score is not None else 0), reverse=True)
    digest.highlight.sort(key=lambda i: (i.score if i.score is not None else 0), reverse=True)
    digest.watch.sort(key=lambda i: i.symbol)

    digest.state = {"as_of": digest.as_of, "symbols": new_symbols}
    return digest


def load_previous_state(path: str) -> Optional[Dict[str, Any]]:  # pragma: no cover - IO 薄封装
    """从 JSON 文件读取上一次的降噪状态（供推送服务使用）。"""
    import json
    from pathlib import Path

    file = Path(path)
    if not file.exists():
        return None
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_state(path: str, state: Dict[str, Any]) -> None:  # pragma: no cover - IO 薄封装
    """写入降噪状态。"""
    import json
    from pathlib import Path

    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
