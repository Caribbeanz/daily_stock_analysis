# -*- coding: utf-8 -*-
"""高抛低吸助手 · 意图解析器（P2 方案 §3.2 / FR-14.3）

把用户的一句自然语言翻译成**结构化意图**，供对话层分流。设计原则：

1. **规则优先**：可判定的场景（名词解释 / 明确写指令 / 越界下单）一律走规则，
   不消耗 LLM、结果可预期、可单测；
2. **LLM 兜底**：规则无法判定且调用方提供了 ``llm`` 时才外呼（可注入，测试可离线）；
3. **只读为主**：P2.1 只实现"读"（问答 / 术语 / 深度分析 / 组合体检）；
   写意图仅**识别并回绝**，真正的执行落在 P2.2 的 ``action_service``（决议 D3）。

输出意图种类：

===================  ============================================================
``glossary``         名词解释（FR-14.3.2）——本地名词表直接作答
``qa``               日常问答 / 个股现状（FR-14.3.2）——轻量 LLM + 上下文
``deep_analysis``    个股多维度深度分析（FR-14.3.4）——走 AgentOrchestrator full
``portfolio_review`` 持仓整体风险体检（FR-16.2）——走组合分析
``write_intent``     写操作（增删自选 / 改持仓 / 调权重 / 设止损）→ P2.2 才开放
``out_of_scope``     越界（如直接下单）→ 明确拒绝并给替代路径（FR-14.3.9）
===================  ============================================================
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence

from src.services import glossary as glossary_service

__all__ = ["Intent", "IntentKind", "resolve", "WriteOp"]

# LLM 兜底签名：接收用户原话，返回 {"kind": str, "symbols": [...], "terms": [...]}
LLMFallback = Callable[[str], Optional[Dict[str, Any]]]


class IntentKind(str, Enum):
    GLOSSARY = "glossary"
    QA = "qa"
    DEEP_ANALYSIS = "deep_analysis"
    PORTFOLIO_REVIEW = "portfolio_review"
    WRITE_INTENT = "write_intent"
    OUT_OF_SCOPE = "out_of_scope"


@dataclass
class WriteOp:
    """一条写指令（P2.2 由 action_service 执行；P2.1 仅用于回绝文案）。"""

    op: str
    params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"op": self.op, **self.params}


@dataclass
class Intent:
    kind: IntentKind
    text: str
    symbols: List[str] = field(default_factory=list)
    terms: List[str] = field(default_factory=list)
    write_ops: List[WriteOp] = field(default_factory=list)
    confidence: float = 0.5
    source: str = "rule"  # rule | llm | default
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "text": self.text,
            "symbols": list(self.symbols),
            "terms": list(self.terms),
            "write_ops": [op.to_dict() for op in self.write_ops],
            "confidence": self.confidence,
            "source": self.source,
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- #
# 词表
# --------------------------------------------------------------------------- #
# 越界：直接下单 / 委托交易（FR-14.3.9 明确拒绝）
_OUT_OF_SCOPE_PATTERNS = (
    r"(帮我|给我|替我|直接|立即|马上)(买|卖|下单|挂单|委托)",
    r"(市价|限价)(单|委托)",
    r"(买入|卖出)委托",
    r"一键(买入|卖出|清仓)",
    r"(我要|我想|想要)(下单|挂单|委托|买入|卖出)",
)

# 写操作关键词 → op 名称
_WRITE_RULES: Sequence[tuple[re.Pattern, str]] = (
    (re.compile(r"(添加|加入|新增|加自选|关注)"), "watchlist_add"),
    (re.compile(r"(删除|移除|去掉|取消自选)"), "watchlist_remove"),
    (re.compile(r"(清空|全部删除)"), "clear_all"),
    (re.compile(r"(录入|设置|修改|改|更新)?持仓(成本|价)?(改为|设为|设为|设成|为)?"), "set_position"),
    (re.compile(r"(止损)(位)?(改为|设为|设成|调整到|调到|为)"), "set_stop_loss"),
    (re.compile(r"(权重|因子权重|四因子)(调整|改为|设为|调到|设置)"), "set_weight"),
    (re.compile(r"(仓位|持仓比例)(调整|改为|设为|调到)"), "set_position_ratio"),
)
_WRITE_HINT = re.compile(r"(自选股|自选|持仓|权重|止损|仓位)")
_MUTATING_VERB = re.compile(r"(添加|加入|新增|删除|移除|去掉|清空|改为|设为|设成|调整|调到|设置|录入|更新)")

# 深度分析（FR-14.3.4）
_DEEP_PATTERNS = (
    r"(深度|全面|详细|多维度|多角度|系统性)(地)?(分析|评估|研究)",
    r"帮我(深度)?分析",
    r"该不该(买|卖|加|减|补|清)",
    r"(能|可以|要不要)(买|卖|加仓|减仓|补仓|清仓)",
    r"值不值得(买|卖|持有)",
    r"(投资|买入|卖出)(建议|意见)$",
)

# 组合体检（FR-16.2）
_PORTFOLIO_PATTERNS = (
    r"(我的)?持仓(体检|诊断|分析|复盘|怎么样|情况)",
    r"(组合|整体|全部)(风险|体检|诊断|分析|仓位)",
    r"我(持有|持仓)的(股票|标的|票)",
    r"(全部|所有)(持仓|自选)",
    r"仓位(体检|诊断|分析|结构)",
)

# 名词解释触发
_GLOSSARY_QUESTION = re.compile(r"(是什么|啥意思|什么意思|含义|解释|怎么理解|科普|啥是|什么是|代表什么)")
_GLOSSARY_PURE = re.compile(r"^[\s\w/（）()+\-]{1,20}$")


def _extract_symbols(text: str, symbol_hint: Optional[str]) -> List[str]:
    """抽取标的代码；失败不阻断（只影响上下文，不影响分流）。"""
    codes: List[str] = []
    hint = str(symbol_hint or "").strip().upper()
    if hint:
        codes.append(hint)
    try:
        from src.agent.stock_scope import extract_stock_codes

        for code in extract_stock_codes(text):
            if code and code not in codes:
                codes.append(code)
    except Exception:  # 解析失败降级为"无标的"，不影响意图分类
        pass
    return codes


def _match_any(patterns: Sequence[str], text: str) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _rule_resolve(text: str, symbols: List[str], terms: List[str]) -> Optional[Intent]:
    stripped = text.strip()

    # 1) 越界 → 明确拒绝（优先级最高：宁可拒绝也不误判成写操作）
    if _match_any(_OUT_OF_SCOPE_PATTERNS, stripped):
        return Intent(
            kind=IntentKind.OUT_OF_SCOPE,
            text=stripped,
            symbols=symbols,
            terms=terms,
            confidence=0.9,
            source="rule",
            reason="命中下单/委托类越界指令",
        )

    # 2) 写操作（识别即可，执行在 P2.2）
    if _WRITE_HINT.search(stripped) and _MUTATING_VERB.search(stripped):
        ops = [WriteOp(op=op) for pattern, op in _WRITE_RULES if pattern.search(stripped)]
        if not ops:
            ops = [WriteOp(op="unknown_write")]
        return Intent(
            kind=IntentKind.WRITE_INTENT,
            text=stripped,
            symbols=symbols,
            terms=terms,
            write_ops=ops,
            confidence=0.8,
            source="rule",
            reason="命中写操作动词 + 写对象",
        )

    # 3) 名词解释（纯名词，或"X 是什么"式提问）
    if terms and (_GLOSSARY_QUESTION.search(stripped) or _GLOSSARY_PURE.match(stripped)):
        return Intent(
            kind=IntentKind.GLOSSARY,
            text=stripped,
            symbols=symbols,
            terms=terms,
            confidence=0.85,
            source="rule",
            reason="命中名词表且为解释型提问",
        )

    # 4) 组合体检
    if _match_any(_PORTFOLIO_PATTERNS, stripped):
        return Intent(
            kind=IntentKind.PORTFOLIO_REVIEW,
            text=stripped,
            symbols=symbols,
            terms=terms,
            confidence=0.75,
            source="rule",
            reason="命中持仓/组合整体类关键词",
        )

    # 5) 个股深度分析
    if _match_any(_DEEP_PATTERNS, stripped):
        return Intent(
            kind=IntentKind.DEEP_ANALYSIS,
            text=stripped,
            symbols=symbols,
            terms=terms,
            confidence=0.8,
            source="rule",
            reason="命中深度分析类关键词",
        )

    return None


def _apply_llm(result: Optional[Dict[str, Any]], text: str, symbols: List[str], terms: List[str]) -> Optional[Intent]:
    if not isinstance(result, dict):
        return None
    raw_kind = str(result.get("kind") or "").strip().lower()
    try:
        kind = IntentKind(raw_kind)
    except ValueError:
        return None
    merged_symbols = list(symbols)
    for code in result.get("symbols") or []:
        code = str(code).strip().upper()
        if code and code not in merged_symbols:
            merged_symbols.append(code)
    merged_terms = list(terms)
    for term in result.get("terms") or []:
        term = str(term).strip()
        if term and term not in merged_terms:
            merged_terms.append(term)
    return Intent(
        kind=kind,
        text=text,
        symbols=merged_symbols,
        terms=merged_terms,
        confidence=float(result.get("confidence") or 0.6),
        source="llm",
        reason=str(result.get("reason") or "LLM 兜底判定"),
    )


def resolve(
    text: str,
    *,
    symbol_hint: Optional[str] = None,
    llm: Optional[LLMFallback] = None,
) -> Intent:
    """把用户原话解析为 :class:`Intent`。

    Args:
        text: 用户原话。
        symbol_hint: 当前上下文标的（如个股详情页传来的代码）。
        llm: 可选的 LLM 兜底回调；为空则纯规则（测试与离线场景）。
    """
    stripped = str(text or "").strip()
    if not stripped:
        return Intent(
            kind=IntentKind.QA,
            text="",
            confidence=0.2,
            source="default",
            reason="空输入",
        )

    symbols = _extract_symbols(stripped, symbol_hint)
    terms = [entry.term for entry in glossary_service.search(stripped, limit=3)]

    ruled = _rule_resolve(stripped, symbols, terms)
    if ruled is not None:
        return ruled

    if llm is not None:
        try:
            llm_result = _apply_llm(llm(stripped), stripped, symbols, terms)
        except Exception:  # LLM 失败降级为 QA，对话仍可继续
            llm_result = None
        if llm_result is not None:
            return llm_result

    return Intent(
        kind=IntentKind.QA,
        text=stripped,
        symbols=symbols,
        terms=terms,
        confidence=0.5,
        source="default",
        reason="未命中规则，按日常问答处理",
    )
