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

__all__ = ["Intent", "IntentKind", "resolve", "WriteOp", "parse_write_ops"]

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

# --------------------------------------------------------------------------- #
# 写指令：分句 + 参数抽取（P2.2）
# --------------------------------------------------------------------------- #
# 复合指令拆分：连接词 / 顿号外的标点（顿号常用于并列名词，不拆，避免误切）
_CLAUSE_SPLIT = re.compile(
    r"(?:并且|并(?=把|将|帮|给|设|调|改|加|删|移|去掉|清空|录入|更新))"
    r"|(?:同时|然后|再(?=把|将|设|调|改|加|删|移))"
    r"|[；;]|[，,]"
)

_NUM = r"([0-9]+(?:\.[0-9]+)?)"
_PERCENT = re.compile(_NUM + r"\s*%")
_LABELED_VALUE = re.compile(r"(?:设为|改成|改为|设成|调到|调整到|调为|设置为|为|到)\s*" + _NUM)
_COST_VALUE = re.compile(
    r"(?:成本价|成本|均价|买入价|建仓价)\s*(?:设为|设成|为|是|[:：=])?\s*" + _NUM
)
_QTY_VALUE = re.compile(
    r"(?:数量|股数|持仓量|份额|股)\s*(?:设为|设成|为|是|[:：=])?\s*" + _NUM
)

# 维度关键词 → 四因子键（与 rule_config.FACTOR_KEYS 对齐）
_FACTOR_KEYWORDS: Sequence[tuple[str, tuple[str, ...]]] = (
    ("technical", ("技术面", "技术", "k线", "指标面")),
    ("market", ("行业", "大盘", "板块", "市场面", "指数")),
    ("news", ("消息面", "消息", "新闻", "公告", "情绪")),
    ("fundamental", ("基本面", "业绩", "估值", "财务")),
)


def _split_clauses(text: str) -> List[str]:
    """把一句话按连接词 / 标点拆成多条子指令。"""
    parts = [part.strip() for part in _CLAUSE_SPLIT.split(text) if part and part.strip()]
    return parts or [text.strip()]


def _clause_codes(clause: str) -> List[str]:
    try:
        from src.agent.stock_scope import extract_stock_codes

        return [str(code).strip().upper() for code in extract_stock_codes(clause) if code]
    except Exception:  # 解析失败降级为"无标的"
        return []


def _extract_number(clause: str, codes: Sequence[str]) -> Optional[float]:
    """抽取写指令里的目标数值；优先取标签（设为/调到…）后的数字。"""
    labeled = _LABELED_VALUE.search(clause)
    if labeled:
        return float(labeled.group(1))
    candidates = [
        float(m.group(1))
        for m in re.finditer(_NUM, clause)
    ]
    # 排除标代码本身
    code_values = set()
    for code in codes:
        try:
            code_values.add(float(code))
        except (TypeError, ValueError):
            continue
    filtered = [value for value in candidates if value not in code_values]
    return filtered[-1] if filtered else None


def _parse_ratio_value(clause: str, codes: Sequence[str]) -> Optional[float]:
    """抽取权重 / 仓位比例（支持 60% / 0.6 / 60 三种写法）。"""
    percent = _PERCENT.search(clause)
    if percent:
        return float(percent.group(1)) / 100.0
    number = _extract_number(clause, codes)
    if number is None:
        return None
    return number / 100.0 if number > 1.0 else number


def _parse_factor_key(clause: str) -> Optional[str]:
    for key, keywords in _FACTOR_KEYWORDS:
        if any(keyword in clause for keyword in keywords):
            return key
    return None


def _parse_single_write(clause: str) -> Optional[WriteOp]:
    """把单条子指令解析为 ``WriteOp``（含结构化参数）；无法识别返回 ``None``。"""
    text = str(clause or "").strip()
    if not text or not _WRITE_HINT.search(text) or not _MUTATING_VERB.search(text):
        return None

    op_name = ""
    for pattern, op in _WRITE_RULES:
        if pattern.search(text):
            op_name = op
            break
    if not op_name:
        return None

    codes = _clause_codes(clause)
    params: Dict[str, Any] = {}
    if codes:
        params["symbol"] = codes[0]

    if op_name == "set_weight":
        factor = _parse_factor_key(text)
        if factor:
            params["factor"] = factor
        value = _parse_ratio_value(text, codes)
        if value is not None:
            params["value"] = value
    elif op_name == "set_stop_loss":
        value = _extract_number(text, codes)
        if value is not None:
            params["value"] = value
    elif op_name == "set_position_ratio":
        value = _parse_ratio_value(text, codes)
        if value is not None:
            params["value"] = value
    elif op_name == "set_position":
        cost_match = _COST_VALUE.search(text)
        qty_match = _QTY_VALUE.search(text)
        if cost_match:
            params["cost"] = float(cost_match.group(1))
        if qty_match:
            params["quantity"] = float(qty_match.group(1))
        if "cost" not in params and "quantity" not in params:
            # 退化：无标签时若出现两个数值，按"成本, 数量"顺序解析
            numbers = [
                float(m.group(1))
                for m in re.finditer(_NUM, text)
                if m.group(1) not in codes
            ]
            if len(numbers) >= 2:
                params["cost"], params["quantity"] = numbers[0], numbers[1]

    return WriteOp(op=op_name, params=params)


def parse_write_ops(text: str) -> List[WriteOp]:
    """解析一句话中的**全部**写指令（复合指令逐条返回）。

    例：``"把技术面权重调到 60% 并把 600519 止损设为 15 元"`` →
    ``[WriteOp(set_weight, {factor: technical, value: 0.6}),
       WriteOp(set_stop_loss, {symbol: 600519, value: 15.0})]``
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    ops: List[WriteOp] = []
    for clause in _split_clauses(raw):
        parsed = _parse_single_write(clause)
        if parsed is not None:
            ops.append(parsed)
    return ops


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

    # 2) 写操作（识别 + 参数抽取即可，执行在 P2.2 的 action_service）
    if _WRITE_HINT.search(stripped) and _MUTATING_VERB.search(stripped):
        ops = parse_write_ops(stripped)
        if not ops:
            ops = [WriteOp(op="unknown_write")]
        merged_symbols = list(symbols)
        for op in ops:
            code = str(op.params.get("symbol") or "").strip().upper()
            if code and code not in merged_symbols:
                merged_symbols.append(code)
        return Intent(
            kind=IntentKind.WRITE_INTENT,
            text=stripped,
            symbols=merged_symbols,
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
