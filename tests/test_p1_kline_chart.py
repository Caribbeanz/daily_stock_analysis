# -*- coding: utf-8 -*-
"""P1 · K 线图渲染单测（绘图数据准备 + 优雅降级）。"""

from __future__ import annotations

import pandas as pd
import pytest

from src.services import kline_chart
from src.services.kline_chart import build_chart_payload, render_png


def _df(n=80):
    closes = []
    price = 10.0
    for i in range(n):
        price *= 1.005 if i % 6 < 4 else 0.99
        closes.append(round(price, 3))
    return pd.DataFrame(
        {
            "date": [f"2026-0{(i // 28) + 1}-{(i % 28) + 1:02d}" for i in range(n)],
            "open": [c * 0.998 for c in closes],
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1_000_000 + i * 1000 for i in range(n)],
        }
    )


def test_build_payload_basic_shape():
    payload = build_chart_payload(_df(80), symbol="601919", name="中远海控", lookback=60, stop_loss=9.0, target=12.0)
    assert payload["symbol"] == "601919"
    assert payload["name"] == "中远海控"
    assert len(payload["close"]) == 60
    assert len(payload["high"]) == 60
    assert len(payload["volume"]) == 60
    assert payload["lookback"] == 60
    assert payload["last_close"] is not None
    assert payload["stop_loss"] == 9.0
    assert payload["target"] == 12.0


def test_build_payload_includes_ma_and_macd():
    payload = build_chart_payload(_df(120), lookback=60)
    assert set(payload["ma"].keys()) >= {"ma5", "ma10", "ma20"}
    for values in payload["ma"].values():
        assert len(values) == 60
    assert len(payload["macd"]["dif"]) == 60
    assert len(payload["macd"]["hist"]) == 60


def test_build_payload_change_pct():
    payload = build_chart_payload(_df(60), lookback=60)
    # 涨跌幅 = (今收 - 昨收)/昨收，字段存在即可（符号取决于合成数据）
    assert payload["change_pct"] is None or isinstance(payload["change_pct"], float)


def test_build_payload_empty_raises():
    with pytest.raises(ValueError):
        build_chart_payload(pd.DataFrame(), symbol="X")


def test_build_payload_handles_missing_volume():
    frame = _df(30).drop(columns=["volume"])
    payload = build_chart_payload(frame, lookback=30)
    assert payload["volume"] == [None] * 30


def test_render_png_graceful_when_pillow_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(kline_chart, "PIL_AVAILABLE", False)
    payload = build_chart_payload(_df(40), symbol="X")
    assert render_png(payload, str(tmp_path / "x.png")) is None


@pytest.mark.skipif(not kline_chart.PIL_AVAILABLE, reason="Pillow 未安装")
def test_render_png_produces_file(tmp_path):
    payload = build_chart_payload(_df(60), symbol="601919", name="中远海控", stop_loss=9.5, target=12.5)
    out = render_png(payload, str(tmp_path / "601919.png"))
    assert out is not None
    assert (tmp_path / "601919.png").exists()
    assert (tmp_path / "601919.png").stat().st_size > 0
