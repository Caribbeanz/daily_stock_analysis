# -*- coding: utf-8 -*-
"""历史计划回放（D-24）批处理入口：读库内 K 线，逐日重跑守卫链并模拟回合。

用法::

    # 用库里已有的 K 线跑最近一段（只读，不写 assistant_* 表）
    python scripts/assistant_backtest_replay.py --codes 600000 000001 \\
        --start 2026-01-05 --end 2026-11-26 --preset standard

    # 断点续跑 + 落结果文件（分钟级任务中断后接着跑）
    python scripts/assistant_backtest_replay.py --codes 600000 --start 2026-01-05 \\
        --checkpoint data/cache/replay_600000.json --out deliverables/replay_600000.json

口径提醒：
- **不联网**：K 线必须先由取数层落进 L1（``main.py --assistant`` 或 ``scripts/assistant_fetch_smoke.py``），
  缺数据的日子如实跳过并在 ``counters`` 里计数，不补、不猜。
- 若手头只有主项目的 ``stock_daily`` 表，用 ``--import-main-db`` 把对应标的的日线**复制**进
  L1 再回放（只读源库、只写 assistant 自己的表；脚本层桥接，助手包内不 import 主项目）。
- **一次只跑一套参数**：参数网格请多次调用本脚本（避免组合爆炸），``--max-days`` 是规模护栏。
- **大盘默认缺失**：不注入指数序列时 M1 环境档与「大盘 ≤ 警示」不触发，结果偏乐观，
  报告里的 ``counters.market_missing_days`` 会写明。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.assistant.backtest import RoundConfig  # noqa: E402
from src.assistant.backtest_replay import (  # noqa: E402
    ReplayConfig,
    ReplayState,
    load_state,
    replay,
)
from src.assistant.config.overrides import ParamSet  # noqa: E402
from src.assistant.data.storage import AssistantStore  # noqa: E402
from src.assistant.domain import Bar  # noqa: E402
from src.assistant.enums import Preset  # noqa: E402


def _parse_day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:  # pragma: no cover - argparse 层
        raise argparse.ArgumentTypeError(f"日期须为 YYYY-MM-DD：{text}") from exc


def _parse_holdings(items: list[str] | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items or []:
        code, _, shares = item.partition(":")
        if not code or not shares.isdigit():
            raise SystemExit(f"--holding 需要 代码:股数 形式，收到 {item!r}")
        out[code] = int(shares)
    return out


def _import_from_main_db(source: Path, store: AssistantStore, codes: list[str], start: date | None,
                         end: date | None) -> dict[str, int]:
    """把主项目 ``stock_daily`` 的日线复制进助手 L1（只读源库，只写 assistant 表）。

    助手包内不 import 主项目；这里用一条 SQL 做脚本级桥接，字段直接对应
    ``Bar(trade_date, open, high, low, close, volume)``，不做任何价格加工。
    """
    if not source.is_file():
        raise SystemExit(f"主库不存在：{source}")
    imported: dict[str, int] = {}
    placeholders = ",".join("?" for _ in codes)
    sql = (
        "SELECT code, date, open, high, low, close, volume FROM stock_daily "
        f"WHERE code IN ({placeholders})"
    )
    params: list[object] = list(codes)
    if start is not None:
        sql += " AND date >= ?"
        params.append(start.isoformat())
    if end is not None:
        sql += " AND date <= ?"
        params.append(end.isoformat())
    sql += " ORDER BY code, date"

    source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        rows = source_conn.execute(sql, params).fetchall()
    finally:
        source_conn.close()

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
    for code, bars in grouped.items():
        imported[code] = store.save_bars(code, bars)
    return imported


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="assistant 历史计划回放（只读 L1）")
    parser.add_argument("--db", default=None, help="SQLite 路径；缺省用助手默认库")
    parser.add_argument("--codes", nargs="+", required=True, help="A 股代码")
    parser.add_argument("--start", type=_parse_day, default=None, help="回放起始交易日")
    parser.add_argument("--end", type=_parse_day, default=None, help="回放结束交易日")
    parser.add_argument("--preset", choices=[p.value for p in Preset], default=Preset.STANDARD.value)
    parser.add_argument("--lookback", type=int, default=260, help="每天喂给守卫链的 K 线根数")
    parser.add_argument("--min-bars", type=int, default=60, help="少于该根数不判")
    parser.add_argument("--max-days", type=int, default=None, help="规模护栏：最多回放多少个交易日")
    parser.add_argument("--cost-bps", type=float, default=3.0, help="单边成本（基点）")
    parser.add_argument("--max-hold", type=int, default=60, help="最长持有交易日")
    parser.add_argument("--min-closed", type=int, default=30, help="样本门槛：不足只报计数")
    parser.add_argument("--holding", action="append", help="起点持仓，形如 600000:1000，可多次")
    parser.add_argument("--checkpoint", default=None, help="检查点路径（可续跑）")
    parser.add_argument("--resume", action="store_true", help="从检查点续跑")
    parser.add_argument("--import-main-db", default=None,
                        help="主项目 SQLite 路径；把其中的 stock_daily 复制进助手 L1 再回放")
    parser.add_argument("--out", default=None, help="结果 JSON 输出路径")
    args = parser.parse_args(argv)

    store = AssistantStore(args.db)
    store.create_all()
    holdings = _parse_holdings(args.holding)

    warmup = timedelta(days=max(400, args.lookback * 2))
    if args.import_main_db:
        imported = _import_from_main_db(
            Path(args.import_main_db), store, args.codes,
            start=(args.start - warmup) if args.start else None,
            end=args.end,
        )
        if not imported:
            raise SystemExit("主库里这些标的没有日线；检查代码或 --start/--end")
        print("已从主库导入：" + "，".join(f"{code} {rows} 行" for code, rows in sorted(imported.items())))

    bars_by_code = {}
    for code in args.codes:
        bars = store.load_bars(
            code,
            start=(args.start - warmup) if args.start else None,
            end=args.end,
        )
        if bars:
            bars_by_code[code] = bars
    if not bars_by_code:
        raise SystemExit("库内没有这些标的的 K 线，先跑取数（main.py --assistant 或取数冒烟脚本）")

    all_days = sorted({bar.trade_date for bars in bars_by_code.values() for bar in bars})
    days = [day for day in all_days if (args.start is None or day >= args.start)
            and (args.end is None or day <= args.end)]
    if not days:
        raise SystemExit("给定的日期区间内没有交易日（检查 --start/--end）")

    config = ReplayConfig(
        lookback_bars=args.lookback,
        min_bars=args.min_bars,
        params=ParamSet.build(Preset(args.preset)),
        round_config=RoundConfig(
            cost_bps=args.cost_bps,
            max_hold_days=args.max_hold,
            min_closed_rounds=args.min_closed,
        ),
        holdings=holdings,
        max_days=args.max_days,
    )
    state = load_state(args.checkpoint) if (args.resume and args.checkpoint) else ReplayState()

    result = replay(
        days=days,
        bars_by_code=bars_by_code,
        config=config,
        state=state,
        checkpoint_path=args.checkpoint,
    )

    stats = result.stats
    print(
        f"回放完成：{result.days} 个交易日 / 计划 {result.plans} 条 / 回合 {len(result.rounds)} 个 "
        f"（闭合 {stats['closed']}）"
    )
    print(f"参数 {args.preset} · 规则 {stats['calc_version']} · 截断={result.truncated}")
    if stats.get("win_rate") is None:
        print(f"胜率：—（{stats.get('note', '样本不足')}）")
    else:
        print(f"胜率：{stats['win_rate'] * 100:.1f}%（闭合 {stats['closed']}）")
    if result.counters:
        print("计数：" + "，".join(f"{key}={value}" for key, value in sorted(result.counters.items())))

    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(result.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
        )
        print(f"结果已写入 {target}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
