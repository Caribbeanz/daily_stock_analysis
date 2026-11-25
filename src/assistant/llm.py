# -*- coding: utf-8 -*-
"""LLM 文案层（需求 §5.13 / 方案 §6.4，DP-4：决策归代码，措辞归 LLM）。

**只读白名单 → 生成 → 数值/禁用词校验 → 缓存；任何一步失败都用规则模板降级**，
不阻塞报告与推送，不参与评分 / advice / 条件计划 / 风险定级 / 回测。

三处用法中本模块覆盖两处（WebUI 详情页的「个股人话结论」与飞书 ② 段摘要），
第三处「大盘与板块总结」要等 WebUI 的大盘 Tab，留接口不空写。

两条易踩的口径：

- **temperature 不进决策 `calc_version`**：它只影响措辞，进了会让同一份结构化
  结论换一个快照 ID。文案指纹（提示词版本 + 模型 + temperature）单独存进
  `assistant_llm_text_cache.fingerprint`，满足"同快照可复现"（见 D-19）。
- **默认关闭**：`ASSISTANT_LLM_ENABLED=false` 时根本不构造后端、不发请求；
  渠道未配置 / 超时 / 校验不过都降级，不把半句话发出去。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Mapping, Optional

from src.assistant.render.icons import strip_markdown

__all__ = [
    "LlmTextSettings",
    "LlmTextService",
    "TextOutcome",
    "PROMPT_VERSION",
    "build_whitelist",
    "validate_text",
]

logger = logging.getLogger(__name__)

PROMPT_VERSION = "assistant-summary-v1"
NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?%?")
ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

#: §6.1 禁用词表（仅限大盘区自产文案；引用白名单里已有的枚举标签不算新建议）
BANNED_PROMISE = ("必涨", "必跌", "稳赚", "保本", "翻倍")
BANNED_ACTION = ("加仓", "减仓", "清仓", "建仓", "买入", "卖出", "几成仓", "建议总仓位")
SELL_WORDS = ("减仓", "清仓", "卖出")
BUY_WORDS = ("加仓", "建仓", "买入", "试探")

SYSTEM_PROMPT = (
    "你是股票分析助手的文案整理器，只能把给定的结构化快照改写成通顺中文。"
    "硬规则：不新增任何事实（不写行情、新闻、财报、政策）；"
    "所有数字、日期、股数、百分比、风险等级必须与输入逐字一致；"
    "不给出快照以外的新买卖建议、目标价或收益承诺；"
    "大盘总结不得出现动作词与仓位建议；个股结论不得与给定建议反向；"
    "只输出正文本身，不要解释、不要 Markdown 标题、不要半句未完的话。"
)


# ---------------------------------------------------------------------------
# 配置与指纹
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LlmTextSettings:
    enabled: bool = False
    timeout_seconds: float = 20.0
    max_calls_per_day: int = 20
    temperature: float = 0.0
    summary_max_chars: int = 220
    stock_max_chars: int = 120
    backend_id: str = ""
    fallback_backend_id: str = ""

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "LlmTextSettings":
        environ: Mapping[str, str] = os.environ if env is None else env

        def number(key: str, default: float, *, minimum: float, maximum: float) -> float:
            raw = (environ.get(key) or "").strip()
            if not raw:
                return default
            try:
                value = float(raw)
            except ValueError:
                logger.warning("%s=%r 不是数字，回退 %s", key, raw, default)
                return default
            if not minimum <= value <= maximum or not math.isfinite(value):
                logger.warning("%s=%r 超出 [%s, %s]，回退 %s", key, raw, minimum, maximum, default)
                return default
            return value

        def integer(key: str, default: int, *, minimum: int, maximum: int) -> int:
            return int(number(key, float(default), minimum=minimum, maximum=maximum))

        return cls(
            enabled=(environ.get("ASSISTANT_LLM_ENABLED") or "").strip().lower() == "true",
            timeout_seconds=number("ASSISTANT_LLM_TIMEOUT_SECONDS", 20.0, minimum=1.0, maximum=120.0),
            max_calls_per_day=integer("ASSISTANT_LLM_MAX_CALLS_PER_DAY", 20, minimum=0, maximum=200),
            temperature=number("ASSISTANT_LLM_TEMPERATURE", 0.0, minimum=0.0, maximum=1.0),
            summary_max_chars=integer("ASSISTANT_LLM_SUMMARY_MAX_CHARS", 220, minimum=40, maximum=600),
            stock_max_chars=integer("ASSISTANT_LLM_STOCK_MAX_CHARS", 120, minimum=20, maximum=300),
            backend_id=(environ.get("ASSISTANT_LLM_BACKEND") or "").strip(),
            fallback_backend_id=(environ.get("ASSISTANT_LLM_FALLBACK_BACKEND") or "").strip(),
        )

    def fingerprint(self, model: str = "") -> str:
        """文案指纹；temperature 在这里，不在决策 `calc_version` 里。"""
        raw = f"{PROMPT_VERSION}|{model}|{self.temperature:.4f}|{self.summary_max_chars}|{self.stock_max_chars}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class TextOutcome:
    text: str
    degraded: bool
    source: str  # "llm" / "template"
    model: str = ""
    channel: str = ""
    consistency: str = ""  # "passed" / "fallback:<reason>"
    usage: dict = field(default_factory=dict)
    fingerprint: str = ""


# ---------------------------------------------------------------------------
# 白名单与校验
# ---------------------------------------------------------------------------
def _compact(value: Any) -> Any:
    """只保留白名单字段里的简单值，杜绝把原始行情 / 新闻原文塞进提示词。"""
    if isinstance(value, Mapping):
        return {str(key): _compact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_compact(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def build_whitelist(
    content: Mapping[str, Any],
    *,
    market: Any = None,
    code: str | None = None,
) -> dict:
    """从已固化快照里挑字段；**不传原始行情、新闻原文、历史 LLM 文本或未来数据**。"""
    keys = ("data_date", "snapshot_id", "calc_version")
    payload = {key: _compact(content.get(key)) for key in keys}

    conclusions = list(content.get("conclusions") or [])
    if code is not None:
        conclusions = [row for row in conclusions if row.get("code") == code]
    payload["holding_conclusion"] = [
        {
            key: row.get(key)
            for key in ("code", "name", "advice", "advice_display", "trend", "position_tag",
                        "score", "stage", "risk_level")
        }
        for row in conclusions
    ]
    payload["conditional_plan"] = [
        {
            key: row.get(key)
            for key in ("code", "scenario", "trigger", "action", "shares", "est_amount",
                        "pct_of_total", "notes")
        }
        for row in (content.get("plans") or [])
        if code is None or row.get("code") == code
    ]
    payload["risk_signals"] = [
        {key: row.get(key) for key in ("code", "category", "level", "human_text", "affects_advice")}
        for row in (content.get("risks") or [])
        if code is None or row.get("code") == code
    ]
    if market is not None:
        payload["market_snapshot"] = {
            key: getattr(market, key, None)
            for key in ("index_name", "pct_change", "regime", "available")
        }
    payload["counts"] = {
        "conclusions": len(conclusions),
        "plans": len(payload["conditional_plan"]),
        "risks": len(payload["risk_signals"]),
    }
    return _compact(payload)


def _tokens(whitelist: Mapping[str, Any]) -> set[str]:
    text = json.dumps(whitelist, ensure_ascii=False, sort_keys=True)
    tokens = set(NUMBER_RE.findall(text)) | set(ISO_DATE_RE.findall(text))
    # 百分数去掉 % 后也应能对上（模型常把 12.3% 写成 12.3 个百分点）
    tokens |= {token.rstrip("%") for token in tokens}
    return tokens


def validate_text(text: str, whitelist: Mapping[str, Any], *, position: str,
                  max_chars: int) -> tuple[bool, str]:
    """硬约束 1/2/3/4 的机器校验；任一不过返回 ``(False, 原因)``。"""
    cleaned = (text or "").strip()
    if not cleaned:
        return False, "empty"
    if len(cleaned) > max_chars:
        return False, "too_long"
    if "AI 生成摘要" in cleaned:  # 标注由展示层统一追加，模型不得自造
        return False, "self_label"

    allowed = _tokens(whitelist)
    for token in set(NUMBER_RE.findall(cleaned)) | set(ISO_DATE_RE.findall(cleaned)):
        if token not in allowed and token.rstrip("%") not in allowed:
            return False, f"number_mismatch:{token}"

    source_text = json.dumps(whitelist, ensure_ascii=False)
    if any(word in cleaned for word in BANNED_PROMISE):
        return False, "promise"

    if position == "stock":
        advices = [
            str(row.get("advice") or "") for row in whitelist.get("holding_conclusion", [])
        ]
        advice = advices[0] if advices else ""
        # 先判方向再判「新动作」：给出更具体的降级原因，便于排查提示词问题
        if advice in SELL_WORDS and any(word in cleaned for word in BUY_WORDS):
            return False, "opposite_advice"
        if advice in BUY_WORDS and any(word in cleaned for word in SELL_WORDS):
            return False, "opposite_advice"

    if position in ("stock", "summary"):
        new_actions = [word for word in BANNED_ACTION if word in cleaned and word not in source_text]
        if new_actions:
            return False, f"new_action:{new_actions[0]}"

    return True, "passed"


# ---------------------------------------------------------------------------
# 降级模板（不发半句话、不带数字以外的信息）
# ---------------------------------------------------------------------------
def _fallback_summary(content: Mapping[str, Any], market: Any) -> str:
    conclusions = list(content.get("conclusions") or [])
    plans = list(content.get("plans") or [])
    risks = list(content.get("risks") or [])
    todo = sum(
        1 for row in conclusions
        if str(row.get("advice") or "") in {"买入", "加仓", "减仓", "清仓", "试探建仓"}
    )
    regime = f" · {getattr(market, 'regime', '')}" if getattr(market, "available", False) else ""
    return (
        f"自选 {len(conclusions)} 只（待办 {todo} 只） · 风险 {len(risks)} 项"
        f" · 条件计划 {len(plans)} 条{regime}；以上按结构化字段执行。"
    )


def _fallback_stock(content: Mapping[str, Any], code: str) -> str:
    rows = [row for row in (content.get("conclusions") or []) if row.get("code") == code]
    if not rows:
        return "本标的无可用结论，按结构化字段查看。"
    row = rows[0]
    plans = [plan for plan in (content.get("plans") or []) if plan.get("code") == code]
    plan_text = f"；条件计划 {len(plans)} 条" if plans else "；暂无条件计划"
    advice = row.get("advice_display") or row.get("advice") or "—"
    return f"{row.get('name') or code}（{code}）：{advice}{plan_text}。"


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------
class LlmTextService:
    """文案生成入口；`backend` 可注入（生成结果需有 ``.text/.model/.provider/.backend/.usage``）。"""

    def __init__(
        self,
        store: Any,
        settings: LlmTextSettings | None = None,
        *,
        backend: Any = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.store = store
        self.settings = settings or LlmTextSettings.from_env()
        self._backend = backend
        self._clock = clock
        self._calls: dict[date, int] = {}

    def _build_backend(self, backend_id: str = "") -> Any:
        """延迟构造：默认关闭时不会导入 litellm / local_cli_backend。"""
        from src.config import get_config
        from src.llm.backend_factory import create_generation_backend
        from src.llm.backend_registry import LITELLM_BACKEND_ID

        config = get_config()
        resolved = backend_id or self.settings.backend_id or getattr(
            config, "generation_backend", LITELLM_BACKEND_ID
        )

        def litellm_completion(**kwargs):
            import litellm

            return litellm.completion(**kwargs)

        return create_generation_backend(
            resolved, config=config, litellm_completion_callable=litellm_completion
        )

    def _call_backend(self, backend: Any, prompt: str, cfg: dict, system_prompt: str) -> Any:
        # 不用 `with`：超时后 __exit__ 会 wait=True 把超时的调用又等回来。
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(backend.generate, prompt, cfg, system_prompt=system_prompt)
            return future.result(timeout=self.settings.timeout_seconds)
        finally:
            # 超时后后台线程可能仍在跑（后端自己应设 timeout）；这里不再等待，
            # 否则 20:00 推送会被一个卡死的渠道拖住。
            pool.shutdown(wait=False, cancel_futures=True)

    def _generate(self, prompt: str, whitelist: Mapping[str, Any], position: str) -> Any:
        cfg = {
            "model": os.getenv("LITELLM_MODEL", ""),
            "temperature": self.settings.temperature,
            "timeout": self.settings.timeout_seconds,
        }
        max_chars = (
            self.settings.stock_max_chars if position == "stock" else self.settings.summary_max_chars
        )
        system_prompt = f"{SYSTEM_PROMPT}\n输出不超过 {max_chars} 字。"
        backend = self._backend if self._backend is not None else self._build_backend()
        try:
            return self._call_backend(backend, prompt, cfg, system_prompt)
        except Exception as primary_exc:  # noqa: BLE001 - 复用主流程 fallback 语义
            fallback_id = self.settings.fallback_backend_id
            if not fallback_id or self._backend is not None:
                raise
            logger.warning("LLM 主后端失败，尝试 fallback %s：%s", fallback_id, primary_exc)
            return self._call_backend(self._build_backend(fallback_id), prompt, cfg, system_prompt)

    def text(
        self,
        position: str,
        content: Mapping[str, Any],
        *,
        market: Any = None,
        code: str | None = None,
    ) -> TextOutcome:
        """取文案：缓存优先 → 预算/白名单 → 生成 → 校验 → 缓存；失败一律模板降级。"""
        snapshot_id = str(content.get("snapshot_id") or "")
        if not snapshot_id:
            raise ValueError("LLM 文案需要 snapshot_id（不可凭空生成）")
        if position not in ("summary", "stock", "market"):
            raise ValueError(f"未知文案位置：{position!r}")

        cached = self.store.load_llm_text(snapshot_id, position, code)
        if cached is not None:
            return TextOutcome(
                text=cached["text"],
                degraded=cached["consistency"] != "passed",
                source="llm" if cached["model"] else "template",
                model=cached["model"],
                channel=cached["channel"],
                consistency=cached["consistency"],
                usage=cached["usage"],
                fingerprint=cached["fingerprint"],
            )

        max_chars = (
            self.settings.stock_max_chars if position == "stock" else self.settings.summary_max_chars
        )
        fallback = (
            _fallback_stock(content, code or "")
            if position == "stock"
            else _fallback_summary(content, market)
        )
        today = self._clock().date()
        used = self._calls.get(today, 0)
        if not self.settings.enabled or used >= self.settings.max_calls_per_day:
            reason = "disabled" if not self.settings.enabled else "budget_exhausted"
            return self._save_fallback(snapshot_id, position, code, fallback, reason)

        whitelist = build_whitelist(content, market=market, code=code)
        payload = json.dumps(whitelist, ensure_ascii=False, sort_keys=True)
        prompt = f"结构化快照（只读）：\n{payload}\n\n请按系统规则输出一段中文文案。"
        self._calls[today] = used + 1

        try:
            result = self._generate(prompt, whitelist, position)
            raw = strip_markdown(str(getattr(result, "text", "") or "")).strip()
            ok, reason = validate_text(raw, whitelist, position=position, max_chars=max_chars)
            if not ok:
                return self._save_fallback(snapshot_id, position, code, fallback, reason)
            model = str(getattr(result, "model", "") or "")
            outcome = TextOutcome(
                text=f"{raw}\n（AI 生成摘要，以结构化字段为准）",
                degraded=False,
                source="llm",
                model=model,
                channel=str(getattr(result, "provider", "") or getattr(result, "backend", "") or ""),
                consistency="passed",
                usage=dict(getattr(result, "usage", {}) or {}),
                fingerprint=self.settings.fingerprint(model),
            )
            self.store.save_llm_text(
                snapshot_id, position, outcome.text, code=code, model=model,
                channel=outcome.channel, consistency="passed",
                fingerprint=outcome.fingerprint, usage=outcome.usage,
            )
            return outcome
        except Exception as exc:  # noqa: BLE001 - 任何失败都降级，不阻塞推送
            reason = "timeout" if isinstance(exc, FutureTimeout) else f"error:{type(exc).__name__}"
            logger.warning("LLM 文案生成失败，已降级为规则模板：%s", reason)
            return self._save_fallback(snapshot_id, position, code, fallback, reason)

    def _save_fallback(
        self, snapshot_id: str, position: str, code: str | None, text: str, reason: str
    ) -> TextOutcome:
        outcome = TextOutcome(
            text=text,
            degraded=True,
            source="template",
            consistency=f"fallback:{reason}",
            fingerprint=self.settings.fingerprint("template"),
        )
        self.store.save_llm_text(
            snapshot_id, position, text, code=code, consistency=outcome.consistency,
            fingerprint=outcome.fingerprint,
        )
        return outcome
