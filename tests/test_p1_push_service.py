# -*- coding: utf-8 -*-
"""P1 · 推送编排离线端到端单测（stub 取数 / 渠道 / 渲染，不联网）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.services.p1_push_service import P1PushService


# --------------------------- 测试替身 --------------------------- #
class _StubBuilder:
    def __init__(self, results):
        self._results = results
        self.called_with = None

    def run(self, *, symbols=None, **kwargs):
        self.called_with = symbols
        return SimpleNamespace(total=len(self._results), succeeded=0, failed=0, results=self._results)


class _StubSignalService:
    """signal_id 存在时返回完整信号 dict。"""

    def __init__(self, payloads):
        self._payloads = payloads

    def get_signal(self, signal_id):
        return self._payloads[signal_id]


class _RecordingNotifier:
    def __init__(self, available=True):
        self.texts = []
        self.images = []
        self._available = available

    def is_available(self):
        return self._available

    def send_text(self, text):
        self.texts.append(text)
        return True

    def send_image(self, path):
        self.images.append(path)
        return True


def _result(symbol, action, **kw):
    base = dict(
        symbol=symbol,
        name=kw.pop("name", symbol),
        action=action,
        scenario=kw.pop("scenario", "up"),
        sub_action=None,
        signal_id=kw.pop("signal_id", None),
        stop_loss=kw.pop("stop_loss", None),
        is_urgent=kw.pop("is_urgent", False),
        error=kw.pop("error", None),
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------- dry-run --------------------------- #
def test_dry_run_generates_without_sending(tmp_path):
    notifier = _RecordingNotifier()
    service = P1PushService(
        builder=_StubBuilder([_result("601919", "buy")]),
        notifier=notifier,
        state_path=str(tmp_path / "state.json"),
        out_dir=str(tmp_path / "charts"),
    )
    result = service.run(dry_run=True, with_charts=False)
    assert result.sent_text is False
    assert notifier.texts == []
    assert result.skipped_reason == "dry_run"
    assert "每日概览" in result.markdown


# --------------------------- 正常推送 --------------------------- #
def test_send_path_pushes_text(tmp_path):
    notifier = _RecordingNotifier()
    service = P1PushService(
        builder=_StubBuilder([_result("601919", "sell", is_urgent=True)]),
        notifier=notifier,
        state_path=str(tmp_path / "state.json"),
        out_dir=str(tmp_path / "charts"),
    )
    result = service.run(with_charts=False)
    assert result.sent_text is True
    assert len(notifier.texts) == 1
    assert "紧急" in notifier.texts[0]
    assert (tmp_path / "state.json").exists()


def test_no_channel_configured_is_graceful(tmp_path):
    notifier = _RecordingNotifier(available=False)
    service = P1PushService(
        builder=_StubBuilder([_result("601919", "buy")]),
        notifier=notifier,
        state_path=str(tmp_path / "state.json"),
        out_dir=str(tmp_path / "charts"),
    )
    result = service.run(with_charts=False)
    assert result.skipped_reason == "no_channel_configured"
    assert result.sent_text is False
    assert result.errors  # 有提示但不抛异常


def test_state_is_not_advanced_when_send_fails(tmp_path):
    class _FailNotifier(_RecordingNotifier):
        def send_text(self, text):
            return False

    service = P1PushService(
        builder=_StubBuilder([_result("601919", "buy")]),
        notifier=_FailNotifier(),
        state_path=str(tmp_path / "state.json"),
        out_dir=str(tmp_path / "charts"),
    )
    service.run(with_charts=False)
    # 发送失败 → 不写降噪状态，避免"没发也折叠"
    assert not (tmp_path / "state.json").exists()


# --------------------------- 信号读回 --------------------------- #
def test_collect_signals_prefers_signal_service(tmp_path):
    payloads = {
        1: {
            "stock_code": "601919",
            "stock_name": "中远海控",
            "action": "buy",
            "score": 80,
            "metadata": {"scenario": "up", "position_advice": {"ratio": 0.3}},
        }
    }
    service = P1PushService(
        builder=_StubBuilder([_result("601919", "buy", signal_id=1)]),
        signal_service=_StubSignalService(payloads),
        notifier=_RecordingNotifier(),
        state_path=str(tmp_path / "state.json"),
        out_dir=str(tmp_path / "charts"),
    )
    signals = service._collect_signals(service.builder.run())
    assert signals[0]["stock_code"] == "601919"
    assert signals[0]["metadata"]["position_advice"]["ratio"] == 0.3


def test_collect_signals_falls_back_on_service_error(tmp_path):
    class _Boom:
        def get_signal(self, signal_id):
            raise RuntimeError("db down")

    service = P1PushService(
        builder=_StubBuilder([_result("601919", "buy", signal_id=1)]),
        signal_service=_Boom(),
        notifier=_RecordingNotifier(),
        state_path=str(tmp_path / "state.json"),
        out_dir=str(tmp_path / "charts"),
    )
    signals = service._collect_signals(service.builder.run())
    assert signals[0]["stock_code"] == "601919"
    assert signals[0]["action"] == "buy"


def test_failed_symbol_appears_in_error_section(tmp_path):
    service = P1PushService(
        builder=_StubBuilder([_result("513100", "watch", error="no data")]),
        notifier=_RecordingNotifier(),
        state_path=str(tmp_path / "state.json"),
        out_dir=str(tmp_path / "charts"),
    )
    result = service.run(with_charts=False)
    assert result.counts["failed"] == 1
    assert "数据异常" in result.markdown


# --------------------------- 附图 --------------------------- #
def test_charts_rendered_for_key_signals(tmp_path):
    rendered = []

    def fake_builder(df, **kwargs):
        return {"symbol": kwargs.get("symbol")}

    def fake_renderer(payload, out_path):
        rendered.append(payload["symbol"])
        from pathlib import Path

        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"png")
        return out_path

    service = P1PushService(
        builder=_StubBuilder([_result("601919", "buy"), _result("513100", "sell", is_urgent=True)]),
        notifier=_RecordingNotifier(),
        fetch=lambda symbol: [1, 2, 3],  # 非空即可（需支持 len()）
        chart_builder=fake_builder,
        chart_renderer=fake_renderer,
        state_path=str(tmp_path / "state.json"),
        out_dir=str(tmp_path / "charts"),
    )
    result = service.run(with_charts=True, max_charts=2)
    assert len(result.charts) == 2
    assert set(rendered) == {"601919", "513100"}
    assert result.sent_images == 2


def test_charts_skip_when_renderer_returns_none(tmp_path):
    service = P1PushService(
        builder=_StubBuilder([_result("601919", "buy")]),
        notifier=_RecordingNotifier(),
        fetch=lambda symbol: [1, 2, 3],
        chart_builder=lambda df, **kw: {"symbol": kw.get("symbol")},
        chart_renderer=lambda payload, out_path: None,
        state_path=str(tmp_path / "state.json"),
        out_dir=str(tmp_path / "charts"),
    )
    result = service.run(with_charts=True)
    assert result.charts == []
    assert result.sent_images == 0


# --------------------------- 质量门 --------------------------- #
def test_require_gate_blocks_when_failed(tmp_path):
    class _FailBacktester:
        def run(self, symbols, gate=None):
            from src.services.rule_backtest import BacktestReport, SymbolBacktest, _apply_gate

            stat = SymbolBacktest(symbol="")
            stat.samples = 1
            stat.win_rate = 0.1
            stat.profit_factor = 0.5
            stat.max_drawdown = 0.5
            report = BacktestReport(overall=stat, gate={"min_win_rate": 0.5, "min_profit_factor": 1.5, "max_drawdown": 0.25, "min_samples": 30})
            _apply_gate(report)
            return report

    notifier = _RecordingNotifier()
    builder = _StubBuilder([_result("601919", "buy")])
    service = P1PushService(
        builder=builder,
        notifier=notifier,
        backtester=_FailBacktester(),
        state_path=str(tmp_path / "state.json"),
        out_dir=str(tmp_path / "charts"),
    )
    result = service.run(require_gate=True, with_charts=False)
    assert result.skipped_reason == "gate_failed"
    assert result.gate_passed is False
    assert notifier.texts == []
    assert builder.called_with is None  # 未进入信号生成
