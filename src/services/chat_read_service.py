# -*- coding: utf-8 -*-
"""高抛低吸助手 · 对话·读编排（P2.1 / FR-14.3、FR-16.2）

**只做"读"**：问答 / 名词解释 / 个股深度分析 / 持仓体检。
写操作（增删自选、改持仓、调权重、设止损）本模块**只识别、不执行**——
按 P2 决议 **D3**，写操作必须收敛到 P2.2 的 ``action_service`` 单一写点
并二次确认，此处仅给出"暂未开放"的明确回执，绝不新增第二条写路径。

分流（复用既有能力，不重写）：

============  ==========================================================
glossary      本地名词表直接作答（零延迟、零成本）
qa            轻量对话（复用 Agent 执行器）
deep_analysis 个股多智能体流水线（复用 Agent 执行器，full 语义）
portfolio_review 持仓整体体检（复用 Agent 执行器，组合语义）
write_intent  回执"写入通道 P2.2 开放"（不执行）
out_of_scope  明确拒绝并给替代路径（FR-14.3.9）
============  ==========================================================

**合规（FR-14.3.8）**：凡涉及分析与买卖判断的回答，一律强制附加风险提示。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.agent.intent.resolver import Intent, IntentKind, resolve
from src.services import glossary as glossary_service

logger = logging.getLogger(__name__)

__all__ = ["RISK_NOTE", "ReadAnswer", "ChatReadService", "WRITE_PENDING_NOTE"]

RISK_NOTE = (
    "⚠️ 以上内容由 AI 基于公开数据与规则化分析整理，仅供参考，"
    "不构成任何投资建议或个股推荐。投资有风险，决策需谨慎。"
)

WRITE_PENDING_NOTE = (
    "该指令属于**写操作**。按当前版本（P2.1 对话·读）尚未开放执行；"
    "写操作将在 P2.2 通过统一写入口开放，届时均需**二次确认**后才会生效。"
)

OUT_OF_SCOPE_NOTE = (
    "做不到直接下单/委托交易——本助手只做**分析与提醒**，不代为交易，也不会给出确定性收益承诺。\n"
    "替代路径：① 在券商 App 自行下单；② 让我分析「该不该动 / 动多少 / 什么价位动」。"
)

# 会给出（或解释）买卖判断的意图 → 必须附风险提示
_RISK_REQUIRED = {
    IntentKind.QA,
    IntentKind.GLOSSARY,
    IntentKind.DEEP_ANALYSIS,
    IntentKind.PORTFOLIO_REVIEW,
}

# 意图 → 传给 Agent 的模式提示（供执行器/上层读取）
_MODE_HINT = {
    IntentKind.QA: "light",
    IntentKind.DEEP_ANALYSIS: "full",
    IntentKind.PORTFOLIO_REVIEW: "portfolio",
}

# 回答器签名：(message, intent, session_id) -> 回答正文
Answerer = Callable[[str, Intent, str], str]


@dataclass
class ReadAnswer:
    """对话·读的统一返回体。"""

    intent: str
    answer: str
    risk_note: str = ""
    symbols: List[str] = field(default_factory=list)
    terms: List[str] = field(default_factory=list)
    degraded: bool = False
    source: str = ""
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "answer": self.answer,
            "risk_note": self.risk_note,
            "symbols": list(self.symbols),
            "terms": list(self.terms),
            "degraded": self.degraded,
            "source": self.source,
            "reason": self.reason,
        }

    @property
    def full_text(self) -> str:
        """回答 + 风险提示（供飞书等纯文本渠道直接发送）。"""
        if not self.risk_note:
            return self.answer
        return f"{self.answer}\n\n{self.risk_note}"


class ChatReadService:
    """读对话编排：意图 → 分流 → 回答（+ 风险提示）。"""

    def __init__(
        self,
        *,
        answerer: Optional[Answerer] = None,
        llm: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
        resolver: Callable[..., Intent] = resolve,
        config: Any = None,
    ) -> None:
        self._answerer = answerer
        self._llm = llm
        self._resolver = resolver
        self._config = config
        self._lazy_answerer: Optional[Answerer] = None
        self._lazy_failed = False

    # ---------------- 依赖 ---------------- #
    @property
    def answerer(self) -> Optional[Answerer]:
        """惰性构建真实回答器（首次使用时才加载 Agent 依赖）。"""
        if self._answerer is not None:
            return self._answerer
        if self._lazy_answerer is not None or self._lazy_failed:
            return self._lazy_answerer
        try:
            from src.agent.factory import build_agent_chat_executor

            config = self._config
            if config is None:
                from src.config import get_config

                config = get_config()
            executor = build_agent_chat_executor(config)

            def _run(message: str, intent: Intent, session_id: str) -> str:
                context = {
                    "hslb_intent": intent.to_dict(),
                    "hslb_mode_hint": _MODE_HINT.get(intent.kind, "light"),
                }
                if len(intent.symbols) == 1:
                    context["stock_scope"] = intent.symbols[0]
                result = executor.chat(message=message, session_id=session_id, context=context)
                if getattr(result, "success", False):
                    return str(getattr(result, "content", "") or "")
                raise RuntimeError(str(getattr(result, "error", "") or "agent chat failed"))

            self._lazy_answerer = _run
        except Exception as exc:  # 依赖缺失时降级，绝不阻断对话入口
            logger.warning("对话回答器初始化失败，将降级：%s", exc)
            self._lazy_failed = True
        return self._lazy_answerer

    # ---------------- 主流程 ---------------- #
    def ask(
        self,
        text: str,
        *,
        symbol_hint: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> ReadAnswer:
        intent = self._resolver(text, symbol_hint=symbol_hint, llm=self._llm)
        return self.answer_intent(intent, session_id=session_id or "")

    def answer_intent(self, intent: Intent, *, session_id: str = "") -> ReadAnswer:
        base = ReadAnswer(
            intent=intent.kind.value,
            answer="",
            symbols=list(intent.symbols),
            terms=list(intent.terms),
            source=intent.source,
            reason=intent.reason,
        )
        handler = {
            IntentKind.GLOSSARY: self._answer_glossary,
            IntentKind.WRITE_INTENT: self._answer_write_pending,
            IntentKind.OUT_OF_SCOPE: self._answer_out_of_scope,
        }.get(intent.kind, self._answer_via_agent)

        try:
            base.answer = handler(intent, session_id=session_id)
        except Exception as exc:  # 单点失败降级，对话不中断
            logger.warning("对话回答失败（intent=%s）：%s", intent.kind.value, exc)
            base.degraded = True
            base.answer = "抱歉，本次回答生成失败（分析服务暂不可用）。可稍后重试，或在 H5 查看该标的的最新结论与四要素。"

        if intent.kind in _RISK_REQUIRED and not base.degraded:
            base.risk_note = RISK_NOTE
        return base

    # ---------------- 各意图处理 ---------------- #
    def _answer_glossary(self, intent: Intent, *, session_id: str = "") -> str:
        entries = []
        for term in intent.terms:
            entry = glossary_service.lookup(term)
            if entry is not None:
                entries.append(entry)
        if not entries:
            entries = glossary_service.search(intent.text, limit=3)
        if not entries:
            return "没有找到对应的名词解释。可以试试「KDJ 是什么」「布林带怎么理解」。"
        lines = [glossary_service.render_term(entry) for entry in entries]
        lines.append("（术语解释来自产品内置名词表，与推送 / 报告口径一致。）")
        return "\n".join(lines)

    def _answer_write_pending(self, intent: Intent, *, session_id: str = "") -> str:
        ops = "、".join(op.op for op in intent.write_ops) or "未识别的写指令"
        return f"{WRITE_PENDING_NOTE}\n\n本次识别到的写意图：{ops}"

    def _answer_out_of_scope(self, intent: Intent, *, session_id: str = "") -> str:
        return OUT_OF_SCOPE_NOTE

    def _answer_via_agent(self, intent: Intent, *, session_id: str = "") -> str:
        answerer = self.answerer
        if answerer is None:
            raise RuntimeError("agent answerer unavailable")
        return answerer(intent.text, intent, session_id)
