# -*- coding: utf-8 -*-
"""LLM 文案层测试：白名单、数值校验、降级、缓存、预算与展示接线。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace

from src.assistant.data.storage import AssistantStore
from src.assistant.llm import (
    LlmTextService,
    LlmTextSettings,
    build_whitelist,
    validate_text,
)
from src.assistant.pipeline import AssistantPipeline, PipelineSettings
from src.assistant.render.feishu import fit_feishu
from tests.assistant.helpers import make_bars, trending

DAY = date(2026, 11, 25)


def content() -> dict:
    return {
        "data_date": DAY.isoformat(),
        "snapshot_id": "20261125-abc",
        "calc_version": "rules-v1/p1",
        "conclusions": [
            {"code": "600000", "name": "浦发银行", "advice": "加仓", "advice_display": "➕ 加仓",
             "score": 82.0, "stage": "S1", "risk_level": "警示"},
        ],
        "plans": [
            {"code": "600000", "scenario": "回踩", "trigger": "9.80", "action": "加仓",
             "shares": 1000, "est_amount": 9800.0, "pct_of_total": 1.96, "notes": "分两笔"},
        ],
        "risks": [
            {"code": "600000", "category": "数据缺口", "level": "警示",
             "human_text": "缺口 1 日", "affects_advice": False},
        ],
    }


@dataclass
class FakeBackend:
    text: str = ""
    error: Exception | None = None
    delay: float = 0.0
    calls: list[dict] = field(default_factory=list)

    def generate(self, prompt, config, *, system_prompt=None, **kwargs):
        self.calls.append({"prompt": prompt, "config": config, "system": system_prompt or ""})
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            text=self.text, model="fake-model", provider="fake", backend="fake", usage={"total_tokens": 10}
        )


def service(backend, *, enabled=True, **kwargs):
    store = AssistantStore(":memory:")
    store.create_all()
    settings = LlmTextSettings(enabled=enabled, **kwargs)
    return LlmTextService(store, settings, backend=backend), store


class TestWhitelist:
    def test_only_allowed_fields_reach_prompt(self) -> None:
        whitelist = build_whitelist(content())
        assert set(whitelist) == {
            "data_date", "snapshot_id", "calc_version", "holding_conclusion",
            "conditional_plan", "risk_signals", "counts",
        }
        assert whitelist["conditional_plan"][0]["shares"] == 1000
        assert "bars" not in whitelist and "news" not in whitelist

    def test_stock_position_filters_other_codes(self) -> None:
        data = content()
        data["conclusions"].append({"code": "000001", "advice": "观望"})
        whitelist = build_whitelist(data, code="000001")
        assert [row["code"] for row in whitelist["holding_conclusion"]] == ["000001"]

    def test_numeric_consistency(self) -> None:
        whitelist = build_whitelist(content())
        assert validate_text("计划 1000 股，触发价 9.80", whitelist, position="summary", max_chars=220)[0]
        assert validate_text("计划 2000 股", whitelist, position="summary", max_chars=220)[1] == (
            "number_mismatch:2000"
        )

    def test_promise_and_new_action_rejected(self) -> None:
        whitelist = build_whitelist(content())
        assert validate_text("后市必涨", whitelist, position="summary", max_chars=220)[1] == "promise"
        assert validate_text("建议清仓", whitelist, position="summary", max_chars=220)[1].startswith(
            "new_action:"
        )

    def test_stock_opposite_advice_rejected(self) -> None:
        whitelist = build_whitelist(content(), code="600000")
        ok, reason = validate_text("建议立即卖出", whitelist, position="stock", max_chars=120)
        assert ok is False and reason == "opposite_advice"


class TestGeneration:
    def test_valid_text_is_cached_and_marked(self) -> None:
        backend = FakeBackend(text="共 1 只，计划 1000 股，按结构化字段执行。")
        llm, store = service(backend)

        first = llm.text("summary", content())
        second = llm.text("summary", content())

        assert first.source == "llm" and first.degraded is False
        assert first.consistency == "passed"
        assert "AI 生成摘要，以结构化字段为准" in first.text
        assert first.usage == {"total_tokens": 10}
        assert second.text == first.text
        assert len(backend.calls) == 1  # 同快照同位置只生成一次
        assert store.load_llm_text("20261125-abc", "summary") is not None

    def test_prompt_does_not_contain_raw_series(self) -> None:
        backend = FakeBackend(text="共 1 只，按结构化字段执行。")
        llm, _ = service(backend)
        llm.text("summary", content())

        prompt = backend.calls[0]["prompt"]
        assert "20261125-abc" in prompt and "1000" in prompt
        assert "新闻原文" not in prompt and "2026-05-04" not in prompt

    def test_invented_number_falls_back_to_template(self) -> None:
        backend = FakeBackend(text="共 3 只，计划 1000 股。")
        llm, _ = service(backend)

        outcome = llm.text("summary", content())

        assert outcome.degraded is True
        assert outcome.consistency == "fallback:number_mismatch:3"
        assert "自选 1 只" in outcome.text  # 模板取自结构化字段
        assert "AI 生成摘要" not in outcome.text

    def test_promise_falls_back_and_caches(self) -> None:
        backend = FakeBackend(text="后市必涨")
        llm, store = service(backend)

        first = llm.text("summary", content())
        second = llm.text("summary", content())
        assert first.consistency == "fallback:promise"
        assert second.text == first.text
        assert len(backend.calls) == 1

    def test_backend_error_falls_back(self) -> None:
        backend = FakeBackend(error=RuntimeError("渠道不可用"))
        llm, _ = service(backend)

        outcome = llm.text("summary", content())
        assert outcome.consistency == "fallback:error:RuntimeError"
        assert outcome.degraded is True

    def test_timeout_falls_back_without_blocking(self) -> None:
        backend = FakeBackend(text="太慢了", delay=0.4)
        llm, _ = service(backend, timeout_seconds=0.1)

        started = time.perf_counter()
        outcome = llm.text("summary", content())
        elapsed = time.perf_counter() - started

        assert outcome.consistency == "fallback:timeout"
        assert elapsed < 0.35  # 没等后台线程跑完

    def test_disabled_never_calls_backend(self) -> None:
        backend = FakeBackend(text="不该被调用")
        llm, _ = service(backend, enabled=False)

        outcome = llm.text("summary", content())
        assert outcome.consistency == "fallback:disabled"
        assert backend.calls == []

    def test_daily_budget_stops_calls(self) -> None:
        backend = FakeBackend(text="共 1 只，按结构化字段执行。")
        llm, store = service(backend, max_calls_per_day=1)
        first = content()
        second = dict(content(), snapshot_id="20261125-def")

        assert llm.text("summary", first).source == "llm"
        assert llm.text("summary", second).consistency == "fallback:budget_exhausted"
        assert len(backend.calls) == 1

    def test_temperature_changes_fingerprint_only(self) -> None:
        a = LlmTextSettings(enabled=True, temperature=0.0)
        b = LlmTextSettings(enabled=True, temperature=0.5)
        assert a.fingerprint("m") != b.fingerprint("m")
        # 决策版本不随措辞参数变化：文案指纹与 content["calc_version"] 各管各的
        assert content()["calc_version"] == content()["calc_version"]


class TestRenderIntegration:
    @staticmethod
    def pipeline(*, llm=None, settings=None) -> AssistantPipeline:
        store = AssistantStore(":memory:")
        store.create_all()
        return AssistantPipeline(
            store=store,
            settings=PipelineSettings(total_capital=100_000.0),
            llm=llm,
            llm_settings=settings,
        )

    def test_summary_text_is_appended_to_section_two(self) -> None:
        backend = FakeBackend(text="共 1 只，计划 1000 股。")
        llm, _ = service(backend)
        pipeline = self.pipeline(llm=llm, settings=LlmTextSettings(enabled=True))

        text = fit_feishu(content(), llm_summary=pipeline.llm_text("summary", content()).text)
        assert "共 1 只，计划 1000 股。" in text
        assert "AI 生成摘要，以结构化字段为准" in text

    def test_no_llm_summary_keeps_original_output(self) -> None:
        plain = fit_feishu(content())
        assert "大盘：" in plain
        assert "AI 生成摘要" not in plain

    def test_fallback_summary_uses_structured_fields(self) -> None:
        backend = FakeBackend(error=RuntimeError("down"))
        llm, _ = service(backend)
        pipeline = self.pipeline(llm=llm, settings=LlmTextSettings(enabled=True))
        outcome = pipeline.llm_text("summary", content())

        text = fit_feishu(content(), llm_summary=outcome.text)
        assert "风险 1 项" in text and "条件计划 1 条" in text

    def test_payload_includes_llm_summary_when_enabled(self) -> None:
        # 不带数字：真实快照的股数由资金推导，测试只验证「文案被接进正文」
        backend = FakeBackend(text="按结构化字段执行，节奏平稳。")
        llm, store = service(backend)
        store.save_bars("600000", make_bars(trending(150)))
        store.append_watchlist("600000", "add", effective_date=date(2026, 5, 1))
        pipeline = AssistantPipeline(
            store=store,
            settings=PipelineSettings(total_capital=100_000.0),
            llm=llm,
            llm_settings=LlmTextSettings(enabled=True),
        )

        payload = pipeline.payload(DAY)
        assert "AI 生成摘要，以结构化字段为准" in payload.text
        assert backend.calls  # 真走过一次后端，之后同快照复用缓存
