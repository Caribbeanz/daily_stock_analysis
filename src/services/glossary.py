# -*- coding: utf-8 -*-
"""高抛低吸助手 · 名词表（FR-14.3.2 / FR-13.1）

面向"金融小白"的术语通俗解释。术语来自需求文档 ``requirements-v0.7`` §3
（名词解释表），内置为纯数据 + 纯函数，**不触网、不读库**，可直接被对话
（H5 / 飞书）与报告复用，保证"四端同一套解释"（NFR-9）。

设计要点：
- **单一事实源**：术语只在这里定义一次，对话与报告都从这里取，避免各处口径漂移；
- **别名归一**：``MA`` / ``均线`` / ``移动平均线`` 指向同一条，便于关键词命中；
- **无副作用**：全部为纯函数，便于单测与嵌入。

用法::

    python -m src.services.glossary --list
    python -m src.services.glossary KDJ
    python -m src.services.glossary --search "MACD 金叉是什么意思"
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = [
    "GlossaryEntry",
    "GLOSSARY",
    "list_entries",
    "lookup",
    "search",
    "render_term",
]


@dataclass(frozen=True)
class GlossaryEntry:
    """单条名词解释。"""

    term: str
    explanation: str
    aliases: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, object]:
        return {
            "term": self.term,
            "explanation": self.explanation,
            "aliases": list(self.aliases),
        }


# --------------------------------------------------------------------------- #
# 数据（需求 v0.7 §3）
# --------------------------------------------------------------------------- #
GLOSSARY: Tuple[GlossaryEntry, ...] = (
    GlossaryEntry(
        "K 线",
        "每根柱子记录一段时间（日线即一天）的开盘、收盘、最高、最低价。",
        ("K线", "蜡烛图", "日K", "k线"),
    ),
    GlossaryEntry(
        "均线（MA5/10/20/60）",
        "最近 N 天收盘价的平均值连线，代表“平均成本”，用来判断趋势方向。",
        ("MA", "均线", "移动平均线", "MA5", "MA10", "MA20", "MA60"),
    ),
    GlossaryEntry(
        "多头排列",
        "MA5 > MA10 > MA20 且向上，说明近期买盘强、趋势向上。",
        ("多头排列",),
    ),
    GlossaryEntry(
        "MACD",
        "衡量上涨/下跌动能的指标；红柱放大=动能增强，绿柱放大=动能衰竭；金叉偏多、死叉偏空。",
        ("MACD", "金叉", "死叉"),
    ),
    GlossaryEntry(
        "KDJ",
        "衡量短期超买超卖的震荡指标；J 值 > 100 偏超买，J 值 < 0 偏超卖；金叉/死叉给短期买卖提示。",
        ("KDJ", "随机指标"),
    ),
    GlossaryEntry(
        "RSI",
        "相对强弱指标；> 70（或 80）偏超买，< 30（或 20）偏超卖。",
        ("RSI", "相对强弱"),
    ),
    GlossaryEntry(
        "布林带（BOLL）",
        "以均线为中轨、上下各加减标准差形成的通道，价格常在中轨与上下轨间波动。",
        ("BOLL", "布林带", "布林线"),
    ),
    GlossaryEntry(
        "乖离率（BIAS）",
        "现价偏离均线的百分比；正乖离过大易回调，负乖离过大易反弹。",
        ("BIAS", "乖离率", "乖离"),
    ),
    GlossaryEntry(
        "缩量 / 放量",
        "成交量比前几日明显减少 / 放大，反映买卖双方力量的强弱。",
        ("缩量", "放量", "量能"),
    ),
    GlossaryEntry(
        "背离",
        "价格创新高（新低）但指标没跟着创新高（新低），常预示趋势要反转。",
        ("背离", "顶背离", "底背离"),
    ),
    GlossaryEntry(
        "支撑位 / 压力位",
        "价格下跌时容易止跌的位置 / 上涨时容易受阻的位置。",
        ("支撑位", "压力位", "支撑", "压力", "阻力"),
    ),
    GlossaryEntry(
        "箱体震荡",
        "价格在一个上下边界明确的区间内反复波动，适合“贴底买、近顶卖”。",
        ("箱体震荡", "箱体", "震荡"),
    ),
    GlossaryEntry(
        "做 T",
        "在已有持仓上，日内或短线先卖后买（或先买后卖），赚取波动差价。",
        ("做T", "做t", "T+0", "日内回转"),
    ),
    GlossaryEntry(
        "止损",
        "亏损达到预设条件时无条件卖出，防止小亏变大亏。",
        ("止损", "止损位", "割肉"),
    ),
    GlossaryEntry(
        "仓位 / 几成",
        "你投入某只股票的资金占计划总资金的比例，如“3 成”=30%。",
        ("仓位", "几成", "持仓比例", "仓位建议"),
    ),
    GlossaryEntry(
        "破位 / 变盘",
        "价格有效跌破关键支撑（或突破关键压力），原趋势可能失效、需要换策略。",
        ("破位", "变盘", "跌破", "突破"),
    ),
    GlossaryEntry(
        "场内基金（ETF / LOF）",
        "在交易所像股票一样买卖的基金；ETF 跟踪某指数（如 513530 跟踪港股通红利），"
        "LOF 既可申赎也可场内交易。",
        ("ETF", "LOF", "场内基金"),
    ),
    GlossaryEntry(
        "折溢价率",
        "ETF 场内交易价相对其净值的偏离；溢价高说明买贵了、折价说明买便宜了，偏离过大时不宜追。",
        ("折溢价率", "溢价", "折价", "溢价率"),
    ),
    GlossaryEntry(
        "跟踪指数 / 成分股",
        "ETF 所跟踪的指数及其成份股；基金没有个股财报，需用指数 / 成分股与基金分红替代。",
        ("跟踪指数", "成分股", "成份股"),
    ),
    GlossaryEntry(
        "事件日历",
        "财报预约披露、分红除权、解禁、股东大会等已知日期的提醒清单。",
        ("事件日历", "日历", "解禁", "除权", "分红"),
    ),
    GlossaryEntry(
        "超预期 / 不及预期",
        "财报实际业绩与市场预期（或同比 / 环比）的比较，超预期偏多、不及预期偏空。",
        ("超预期", "不及预期", "业绩预告"),
    ),
)


# --------------------------------------------------------------------------- #
# 索引（模块级一次性构建，纯计算）
# --------------------------------------------------------------------------- #
def _build_index() -> Dict[str, GlossaryEntry]:
    index: Dict[str, GlossaryEntry] = {}
    for entry in GLOSSARY:
        for key in (entry.term, *entry.aliases):
            normalized = str(key).strip().lower()
            if normalized:
                index.setdefault(normalized, entry)
    return index


_INDEX: Dict[str, GlossaryEntry] = _build_index()
# 长词优先，避免 "MA" 抢先命中 "MACD"
_SORTED_KEYS: Tuple[str, ...] = tuple(sorted(_INDEX, key=len, reverse=True))


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def list_entries() -> List[GlossaryEntry]:
    """返回全部名词（保持文档顺序）。"""
    return list(GLOSSARY)


def lookup(term: str) -> Optional[GlossaryEntry]:
    """按名词或别名精确查询（大小写不敏感）。"""
    key = str(term or "").strip().lower()
    if not key:
        return None
    if key in _INDEX:
        return _INDEX[key]
    # 容忍括号/空格差异，如 "均线（MA5/10/20/60）" → "均线"
    for entry in GLOSSARY:
        if key and (key in entry.term.lower() or entry.term.lower() in key):
            return entry
    return None


def search(text: str, *, limit: int = 3) -> List[GlossaryEntry]:
    """从一段自然语言中按关键词命中名词（长词优先、去重、保序）。"""
    haystack = str(text or "").lower()
    if not haystack:
        return []
    hits: List[GlossaryEntry] = []
    seen: set[int] = set()
    for key in _SORTED_KEYS:
        if key and key in haystack:
            entry = _INDEX[key]
            if id(entry) not in seen:
                seen.add(id(entry))
                hits.append(entry)
            if len(hits) >= max(1, int(limit)):
                break
    return hits


def render_term(entry: GlossaryEntry) -> str:
    """渲染为对话可读的一句话。"""
    return f"【{entry.term}】{entry.explanation}"


def render_many(entries: Sequence[GlossaryEntry]) -> str:
    return "\n".join(render_term(entry) for entry in entries)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(description="高抛低吸助手 · 名词表（FR-14.3.2）")
    parser.add_argument("term", nargs="?", help="要查询的名词，如 KDJ")
    parser.add_argument("--search", help="在一段话里匹配名词")
    parser.add_argument("--list", action="store_true", help="列出全部名词")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    if args.list:
        entries = list_entries()
    elif args.search:
        entries = search(args.search, limit=5)
    elif args.term:
        entry = lookup(args.term)
        entries = [entry] if entry else []
    else:
        parser.print_help()
        return 1

    if args.json:
        print(json.dumps([e.to_dict() for e in entries], ensure_ascii=False, indent=2))
    elif not entries:
        print("未找到匹配的名词。用 --list 查看全部。")
    else:
        print(render_many(entries))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
