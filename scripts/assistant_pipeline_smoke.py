# -*- coding: utf-8 -*-
"""装配链真实联网冒烟：取数 → 守卫链 → 快照 → 投递载荷。

**不进入 pytest 套件**（CI 不应依赖外网）。用途是拿真实行情验证：

1. 缺数据的标的会被取数层补上，且 L1 落库；
2. 同一天跑两次得到同一个 ``snapshot_id``（幂等），不重算；
3. 投递文本从库里回读渲染，两次逐字相同（同源）；
4. 阶段防抖状态写入 ``assistant_stage_state``。

用法：
    python scripts/assistant_pipeline_smoke.py --codes 600000 000001 --total-capital 500000
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.assistant.data.fetcher import DailyFetcher  # noqa: E402
from src.assistant.data.storage import AssistantStore  # noqa: E402
from src.assistant.pipeline import AssistantPipeline, PipelineSettings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="装配链真实联网冒烟")
    parser.add_argument("--codes", nargs="+", default=["600000", "000001"])
    parser.add_argument("--day", default=date.today().isoformat())
    parser.add_argument("--total-capital", type=float, default=500_000.0)
    parser.add_argument("--cash", type=float, default=None)
    parser.add_argument("--holding", default="", help="持仓，形如 600000:1000:8.50")
    parser.add_argument("--db", default="", help="留空则用临时库")
    args = parser.parse_args()

    day = date.fromisoformat(args.day)
    db_path = args.db or os.path.join(tempfile.mkdtemp(), "assistant_smoke.db")
    store = AssistantStore(db_path)
    store.create_all()
    print(f"[env] db={db_path} day={day}")

    for code in args.codes:
        store.append_watchlist(code, "add")
    if args.holding:
        code, shares, cost = args.holding.split(":")
        store.upsert_holding(code, shares=int(shares), cost_basis=float(cost))
        print(f"[env] 持仓 {code} {shares} 股 @ {cost}")

    pipeline = AssistantPipeline(
        store=store,
        settings=PipelineSettings(total_capital=args.total_capital, cash_available=args.cash),
        fetcher=DailyFetcher(),
        clock=datetime.now,
    )

    print("\n=== 第 1 次 ensure_snapshot（真实取数）===")
    first = pipeline.ensure_snapshot(day)
    print(f"snapshot_id={first.snapshot_id} created={first.created}")
    print(f"data_date={first.content['data_date']} bars_used={first.content['bars_used']} "
          f"gap={first.content['gap_days']} source={first.content['source_tag']}")
    print(f"calc_version={first.content['calc_version']} param_hash={first.content['param_hash'][:12]}")
    for row in first.content["conclusions"]:
        print(f"  - {row['code']} {row['name']}: 阶段 {row['stage']}/{row['stage_confidence']} "
              f"评分 {row['score']:.0f} 建议 {row['advice_display']} 风险 {row['risk_level']}")
    print(f"  条件计划 {len(first.content['plans'])} 条，风险信号 {len(first.content['risks'])} 条")
    for warning in first.warnings:
        print(f"  ! {warning}")
    for code, reason in first.skipped:
        print(f"  skip {code}: {reason}")

    print("\n=== 第 2 次 ensure_snapshot（应复用，不重算）===")
    second = pipeline.ensure_snapshot(day)
    print(f"snapshot_id={second.snapshot_id} created={second.created} "
          f"same={second.snapshot_id == first.snapshot_id}")

    print("\n=== 投递载荷（同源校验）===")
    a = pipeline.payload(day)
    b = pipeline.payload(day)
    print(f"same_source={a.same_source_as(b)} stale={a.stale} bar_date={a.bar_date}")
    print("--- text ---")
    print(a.text)

    print("\n=== 阶段防抖状态 ===")
    for code in first.codes:
        print(f"  {code}: {store.load_stage_state(code)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
