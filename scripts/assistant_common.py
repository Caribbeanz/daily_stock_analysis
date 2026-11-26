# -*- coding: utf-8 -*-
"""助手回放/回看脚本的共用取数层（只读，脚本专用）。

放在 ``scripts/`` 而不是 ``src/assistant/``：这里要读**主项目**的 ``stock_daily`` 表，
属于脚本级桥接；助手包内保持不 import 主项目（D1 模块隔离），也不把主库表结构带进包里。
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from src.assistant.data.storage import AssistantStore
from src.assistant.domain import Bar


def parse_day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:  # pragma: no cover - argparse 层
        raise argparse.ArgumentTypeError(f"日期须为 YYYY-MM-DD：{text}") from exc


def parse_holdings(items: list[str] | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items or []:
        code, _, shares = item.partition(":")
        if not code or not shares.isdigit():
            raise SystemExit(f"--holding 需要 代码:股数 形式，收到 {item!r}")
        out[code] = int(shares)
    return out


def import_from_main_db(
    source: Path,
    store: AssistantStore,
    codes: Iterable[str],
    *,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, int]:
    """把主项目 ``stock_daily`` 的日线复制进助手 L1（只读源库，只写 assistant 表）。"""
    code_list = list(codes)
    if not source.is_file():
        raise SystemExit(f"主库不存在：{source}")
    placeholders = ",".join("?" for _ in code_list)
    sql = (
        "SELECT code, date, open, high, low, close, volume FROM stock_daily "
        f"WHERE code IN ({placeholders})"
    )
    params: list[object] = list(code_list)
    if start is not None:
        sql += " AND date >= ?"
        params.append(start.isoformat())
    if end is not None:
        sql += " AND date <= ?"
        params.append(end.isoformat())
    sql += " ORDER BY code, date"

    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    grouped: dict[str, list[Bar]] = {}
    for code, day, open_, high, low, close, volume in rows:
        if not all(isinstance(value, (int, float)) for value in (open_, high, low, close)):
            continue
        grouped.setdefault(str(code), []).append(
            Bar(
                trade_date=date.fromisoformat(str(day)),
                open=float(open_),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=float(volume or 0.0),
            )
        )
    return {code: store.save_bars(code, bars) for code, bars in grouped.items()}


def warmup_start(start: date | None, lookback_bars: int) -> date | None:
    """回看窗口的起点：留足自然日，保证序列长度够 ``lookback_bars``。"""
    if start is None:
        return None
    return start - timedelta(days=max(400, lookback_bars * 2))


def load_bars_bundle(
    store: AssistantStore,
    codes: Iterable[str],
    *,
    start: date | None,
    end: date | None,
    lookback_bars: int = 260,
) -> dict[str, list[Bar]]:
    """从 L1 读每只标的的日线（含预热区），空序列不进结果。"""
    begin = warmup_start(start, lookback_bars)
    bundle: dict[str, list[Bar]] = {}
    for code in codes:
        bars = store.load_bars(code, start=begin, end=end)
        if bars:
            bundle[code] = bars
    return bundle
