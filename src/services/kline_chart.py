# -*- coding: utf-8 -*-
"""高抛低吸助手 · K 线图渲染（P1 · FR-10.4 关键信号附图）

职责：
1. ``build_chart_payload``：**纯函数**，把 OHLCV 归一为绘图数据结构
   （含 MA5/10/20、MACD DIF/DEA/柱、最新价与涨跌幅、止损/目标位）。
2. ``render_png``：用 Pillow 画「蜡烛图 + 均线 + 成交量 + MACD」三联图。

设计约束：
- **优雅降级**：本机没有 Pillow 时 ``render_png`` 返回 ``None``，调用方跳过附图，
  绝不因缺少绘图库而阻断推送（NFR-8）。
- **配色遵循 A 股约定**：**涨红跌绿**（regional_conventions）。
- 指标计算复用 ``src.core.indicators``（sma / macd），不重复实现。

用法::

    from src.services.kline_chart import build_chart_payload, render_png

    payload = build_chart_payload(df, symbol="601919", name="中远海控")
    path = render_png(payload, "out/601919.png")   # 无 Pillow → None
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "PIL_AVAILABLE",
    "build_chart_payload",
    "render_png",
    "DEFAULT_LOOKBACK",
    "DEFAULT_MA_WINDOWS",
]

try:  # pragma: no cover - 取决于运行环境
    from PIL import Image, ImageDraw, ImageFont

    PIL_AVAILABLE = True
except Exception:  # pragma: no cover
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]
    PIL_AVAILABLE = False


DEFAULT_LOOKBACK = 60
DEFAULT_MA_WINDOWS: Tuple[int, ...] = (5, 10, 20)

# A 股配色：涨红跌绿
_BG = (255, 255, 255)
_PANEL_BG = (250, 250, 251)
_GRID = (231, 233, 237)
_TEXT = (31, 41, 55)
_MUTED = (120, 128, 140)
_UP = (222, 48, 48)
_DOWN = (33, 150, 83)
_MA_COLORS = {5: (245, 145, 30), 10: (33, 118, 210), 20: (146, 62, 200), 60: (120, 120, 120)}
_MACD_DIF = (33, 118, 210)
_MACD_DEA = (245, 145, 30)
_STOP_COLOR = (222, 48, 48)
_TARGET_COLOR = (0, 148, 133)

_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)


# --------------------------------------------------------------------------- #
# 数据准备（纯函数，可单测）
# --------------------------------------------------------------------------- #
def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _series_to_floats(series: Any) -> List[Optional[float]]:
    return [_finite(v) for v in list(series)]


def _resolve_dates(frame: Any, length: int) -> List[str]:
    """尽量给出可读的日期标签；缺失则用序号占位。"""
    labels: List[str] = []
    try:
        if "date" in frame.columns:
            raw = list(frame["date"])
        else:
            raw = list(frame.index)
        for value in raw:
            text = str(value)
            if " " in text:
                text = text.split(" ")[0]
            if len(text) >= 10 and text[4] == "-":
                text = text[5:]  # MM-DD，节省横向空间
            labels.append(text)
    except Exception:
        labels = [str(i) for i in range(length)]
    if len(labels) < length:
        labels += [""] * (length - len(labels))
    return labels[:length]


def _resolve_close_and_ohlc(frame: Any) -> Tuple[List[Optional[float]], ...]:
    def col(name: str) -> List[Optional[float]]:
        if name in frame.columns:
            return _series_to_floats(frame[name])
        return []

    return (
        col("open"),
        col("high"),
        col("low"),
        col("close"),
        col("volume"),
    )


def build_chart_payload(
    df: Any,
    *,
    symbol: str = "",
    name: str = "",
    lookback: int = DEFAULT_LOOKBACK,
    ma_windows: Sequence[int] = DEFAULT_MA_WINDOWS,
    stop_loss: Optional[float] = None,
    target: Optional[float] = None,
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    """把日线 DataFrame 归一为绘图数据（含均线与 MACD）。

    Args:
        df: 含 open/high/low/close/volume 列的日线数据（可含 date 列或日期索引）。
        lookback: 只画最近 N 根（默认 60，约一个季度）。
    """
    if df is None or len(df) == 0:
        raise ValueError("empty dataframe for chart")

    import pandas as pd  # 延迟导入，保持模块纯净

    frame = df.tail(int(lookback)) if lookback and lookback > 0 else df
    n = len(frame)
    open_, high, low, close, volume = _resolve_close_and_ohlc(frame)
    if not close:
        raise ValueError("dataframe has no 'close' column")

    ma: Dict[str, List[Optional[float]]] = {}
    try:
        from src.core.indicators import macd as _macd
        from src.core.indicators import sma as _sma

        close_series = pd.Series([c if c is not None else float("nan") for c in close], dtype="float64")
        for window in ma_windows:
            ma[f"ma{int(window)}"] = _series_to_floats(_sma(close_series, int(window)))
        dif, dea, hist = _macd(close_series)
        macd_block = {
            "dif": _series_to_floats(dif),
            "dea": _series_to_floats(dea),
            "hist": _series_to_floats(hist),
        }
    except Exception:
        macd_block = {"dif": [], "dea": [], "hist": []}

    last_close = close[-1] if close else None
    prev_close = close[-2] if len(close) >= 2 else None
    change_pct = None
    if last_close is not None and prev_close not in (None, 0):
        change_pct = (last_close - prev_close) / prev_close * 100.0

    return {
        "symbol": str(symbol or ""),
        "name": str(name or ""),
        "as_of": str(as_of) if as_of else _resolve_dates(frame, n)[-1] if n else "",
        "dates": _resolve_dates(frame, n),
        "open": open_,
        "high": high if high else close,
        "low": low if low else close,
        "close": close,
        "volume": volume if volume else [None] * n,
        "ma": ma,
        "macd": macd_block,
        "last_close": last_close,
        "change_pct": change_pct,
        "stop_loss": _finite(stop_loss),
        "target": _finite(target),
        "lookback": n,
    }


# --------------------------------------------------------------------------- #
# 渲染（Pillow；不可用时返回 None）
# --------------------------------------------------------------------------- #
def _load_font(size: int):
    if not PIL_AVAILABLE:
        return None
    for path in _FONT_CANDIDATES:
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:  # pragma: no cover
        return None


def _fmt(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def render_png(
    payload: Dict[str, Any],
    out_path: str,
    *,
    width: int = 900,
    height: int = 560,
) -> Optional[str]:
    """渲染为 PNG。无 Pillow 时返回 ``None``（调用方据此跳过附图）。"""
    if not PIL_AVAILABLE:
        return None

    close = payload.get("close") or []
    if not close:
        return None
    n = len(close)

    canvas = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(canvas)

    font_title = _load_font(18)
    font_sub = _load_font(13)
    font_axis = _load_font(12)
    font_small = _load_font(11)

    pad_l, pad_r = 10, 66
    plot_x0, plot_x1 = pad_l, width - pad_r
    plot_w = max(1, plot_x1 - plot_x0)

    # 三个面板的纵向区间
    header_h = 46
    price_top = header_h
    price_bottom = int(height * 0.58)
    vol_top = int(height * 0.62)
    vol_bottom = int(height * 0.73)
    macd_top = int(height * 0.77)
    macd_bottom = height - 26

    def x_of(index: int) -> float:
        step = plot_w / max(1, n)
        return plot_x0 + step * (index + 0.5)

    step = plot_w / max(1, n)
    candle_w = max(2.0, step * 0.62)

    # ---- 标题 ----
    title = f"{payload.get('name') or ''} {payload.get('symbol') or ''}".strip() or "K 线图"
    last_close = payload.get("last_close")
    change = payload.get("change_pct")
    draw.text((pad_l + 4, 8), title, fill=_TEXT, font=font_title)
    if last_close is not None:
        color = _UP if (change or 0) >= 0 else _DOWN
        suffix = f"  {_fmt(change)}%" if change is not None else ""
        draw.text((pad_l + 4, 30), f"最新 {_fmt(last_close)}{suffix}", fill=color, font=font_sub)
    as_of = payload.get("as_of")
    if as_of:
        draw.text((plot_x1 - 150, 30), f"截至 {as_of}", fill=_MUTED, font=font_sub)

    # ---- 价格面板 ----
    highs = [v for v in (payload.get("high") or []) if v is not None]
    lows = [v for v in (payload.get("low") or []) if v is not None]
    extra = [v for v in (payload.get("stop_loss"), payload.get("target")) if v is not None]
    if not highs or not lows:
        highs, lows = [c for c in close if c is not None], [c for c in close if c is not None]
    ymax = max(highs + extra) if (highs or extra) else 1.0
    ymin = min(lows + extra) if (lows or extra) else 0.0
    if ymax <= ymin:
        ymax, ymin = ymin + 1.0, ymin - 1.0
    span = ymax - ymin
    ymax += span * 0.04
    ymin -= span * 0.04
    span = ymax - ymin

    def y_price(value: float) -> float:
        return price_bottom - (value - ymin) / span * (price_bottom - price_top)

    draw.rectangle([plot_x0, price_top, plot_x1, price_bottom], fill=_PANEL_BG)
    # 水平网格与右轴价格
    for i in range(5):
        value = ymax - span * i / 4.0
        y = y_price(value)
        draw.line([plot_x0, y, plot_x1, y], fill=_GRID, width=1)
        draw.text((plot_x1 + 4, y - 7), _fmt(value), fill=_MUTED, font=font_axis)

    # 蜡烛
    opens = payload.get("open") or []
    for i in range(n):
        c = close[i]
        if c is None:
            continue
        o = opens[i] if i < len(opens) and opens[i] is not None else c
        hi = payload["high"][i] if i < len(payload["high"]) else c
        lo = payload["low"][i] if i < len(payload["low"]) else c
        hi = hi if hi is not None else c
        lo = lo if lo is not None else c
        color = _UP if c >= o else _DOWN
        x = x_of(i)
        draw.line([x, y_price(hi), x, y_price(lo)], fill=color, width=1)
        y_hi, y_lo = y_price(max(o, c)), y_price(min(o, c))
        if abs(y_hi - y_lo) < 1.0:
            draw.line([x - candle_w / 2, y_lo, x + candle_w / 2, y_lo], fill=color, width=1)
        else:
            draw.rectangle(
                [x - candle_w / 2, y_hi, x + candle_w / 2, y_lo],
                fill=color,
                outline=color,
            )

    # 均线
    for key, values in (payload.get("ma") or {}).items():
        try:
            window = int(str(key).replace("ma", ""))
        except ValueError:
            continue
        color = _MA_COLORS.get(window, _MUTED)
        points = [(x_of(i), y_price(v)) for i, v in enumerate(values) if v is not None]
        if len(points) >= 2:
            draw.line(points, fill=color, width=2, joint="curve")
        if values and values[-1] is not None:
            draw.text((plot_x1 + 4, y_price(values[-1]) - 6), f"MA{window}", fill=color, font=font_small)

    # 止损 / 目标位
    stop = payload.get("stop_loss")
    if stop is not None and ymin <= stop <= ymax:
        y = y_price(stop)
        for x in range(plot_x0, plot_x1, 12):
            draw.line([x, y, x + 6, y], fill=_STOP_COLOR, width=1)
        draw.text((plot_x0 + 4, y - 13), f"止损 {_fmt(stop)}", fill=_STOP_COLOR, font=font_small)
    target = payload.get("target")
    if target is not None and ymin <= target <= ymax:
        y = y_price(target)
        for x in range(plot_x0, plot_x1, 12):
            draw.line([x, y, x + 6, y], fill=_TARGET_COLOR, width=1)
        draw.text((plot_x0 + 4, y - 13), f"目标 {_fmt(target)}", fill=_TARGET_COLOR, font=font_small)

    # ---- 成交量面板 ----
    draw.rectangle([plot_x0, vol_top, plot_x1, vol_bottom], fill=_PANEL_BG)
    volumes = payload.get("volume") or []
    finite_vols = [v for v in volumes if v is not None]
    if finite_vols:
        vmax = max(finite_vols) or 1.0
        for i in range(n):
            v = volumes[i] if i < len(volumes) else None
            if v is None:
                continue
            c = close[i]
            o = opens[i] if i < len(opens) and opens[i] is not None else c
            color = _UP if (c or 0) >= (o or 0) else _DOWN
            h = (vol_bottom - vol_top) * (v / vmax)
            x = x_of(i)
            draw.rectangle([x - candle_w / 2, vol_bottom - h, x + candle_w / 2, vol_bottom], fill=color)
    draw.text((plot_x0 + 4, vol_top - 14), "成交量", fill=_MUTED, font=font_small)

    # ---- MACD 面板 ----
    draw.rectangle([plot_x0, macd_top, plot_x1, macd_bottom], fill=_PANEL_BG)
    macd_block = payload.get("macd") or {}
    dif, dea, hist = macd_block.get("dif") or [], macd_block.get("dea") or [], macd_block.get("hist") or []
    macd_values = [v for v in (dif + dea + hist) if v is not None]
    if macd_values:
        m = max(abs(min(macd_values)), abs(max(macd_values))) or 1e-6
        mid = (macd_top + macd_bottom) / 2.0
        half = (macd_bottom - macd_top) / 2.0

        def y_macd(value: float) -> float:
            return mid - (value / m) * half * 0.92

        draw.line([plot_x0, mid, plot_x1, mid], fill=_GRID, width=1)
        for i, value in enumerate(hist):
            if value is None:
                continue
            color = _UP if value >= 0 else _DOWN
            x = x_of(i)
            y_zero = y_macd(0.0)
            y_value = y_macd(value)
            draw.rectangle(
                [x - candle_w / 2, min(y_zero, y_value), x + candle_w / 2, max(y_zero, y_value)],
                fill=color,
            )
        for values, color in ((dif, _MACD_DIF), (dea, _MACD_DEA)):
            points = [(x_of(i), y_macd(v)) for i, v in enumerate(values) if v is not None]
            if len(points) >= 2:
                draw.line(points, fill=color, width=2, joint="curve")
        draw.text((plot_x0 + 4, macd_top - 14), "MACD (DIF/DEA/柱)", fill=_MUTED, font=font_small)
        draw.text((plot_x1 + 4, mid - 7), "0", fill=_MUTED, font=font_axis)
    else:
        draw.text((plot_x0 + 4, macd_top + 8), "MACD 数据不足", fill=_MUTED, font=font_small)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, format="PNG")
    return str(out)


def _main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover
    """自检入口：渲染一张示意图，验证 Pillow 与字体可用。"""
    import argparse

    parser = argparse.ArgumentParser(description="K 线图渲染自检")
    parser.add_argument("--out", default="out/kline_selftest.png")
    args = parser.parse_args(argv)

    if not PIL_AVAILABLE:
        print("Pillow 不可用：render_png 将返回 None（推送自动跳过附图）")
        return 1

    import math as _math

    rows = []
    price = 10.0
    for i in range(60):
        price *= 1 + (0.012 if i % 7 < 4 else -0.008)
        open_ = price * (1 - 0.003)
        close_ = price
        high = max(open_, close_) * 1.008
        low = min(open_, close_) * 0.992
        rows.append(
            {
                "date": f"2026-08-{(i % 28) + 1:02d}",
                "open": open_,
                "high": high,
                "low": low,
                "close": close_,
                "volume": 1_000_000 * (1 + 0.3 * _math.sin(i)),
            }
        )
    import pandas as pd

    payload = build_chart_payload(pd.DataFrame(rows), symbol="TEST", name="自检", stop_loss=price * 0.92, target=price * 1.12)
    path = render_png(payload, args.out)
    print(f"已生成：{path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
