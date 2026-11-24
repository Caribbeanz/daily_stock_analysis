# -*- coding: utf-8 -*-
"""取数层真实联网冒烟（不进入 pytest 默认套件，避免 CI 依赖外网）。

用法::

    python scripts/assistant_fetch_smoke.py --codes 600000 000001 --years 3
    python scripts/assistant_fetch_smoke.py --codes 600000 --no-tencent --json

输出：每个标的实际使用的数据源、行数、耗时、丢弃行、降级原因，以及熔断状态。
只读，不写数据库（``--save`` 才落到临时 SQLite）。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.assistant.data.fetcher import (  # noqa: E402
    CircuitBreaker,
    DailyFetcher,
    EastmoneySource,
    FetchError,
    SinaSource,
    TencentSource,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="assistant 取数层联网冒烟")
    parser.add_argument("--codes", nargs="+", default=["600000"], help="A 股代码")
    parser.add_argument("--years", type=int, default=3, help="回看年数")
    parser.add_argument("--no-eastmoney", action="store_true")
    parser.add_argument("--no-sina", action="store_true")
    parser.add_argument("--no-tencent", action="store_true")
    parser.add_argument("--save", action="store_true", help="写入临时 SQLite 验证 L1 落库")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    sources = []
    if not args.no_eastmoney:
        sources.append(EastmoneySource())
    if not args.no_sina:
        sources.append(SinaSource())
    if not args.no_tencent:
        sources.append(TencentSource())
    if not sources:
        print("至少启用一个数据源", file=sys.stderr)
        return 2

    store = None
    if args.save:
        from src.assistant.data.storage import AssistantStore

        store = AssistantStore(Path(tempfile.mkdtemp()) / "smoke.db")
        store.create_all()

    breaker = CircuitBreaker(threshold=3, cooldown_seconds=300)
    fetcher = DailyFetcher(sources, store=store, breaker=breaker, retries=2, min_interval_seconds=0.2)

    end = date.today()
    start = end - timedelta(days=365 * args.years)
    out: dict[str, object] = {"window": [start.isoformat(), end.isoformat()], "results": [], "breaker": {}}

    failures = 0
    for code in args.codes:
        try:
            result = fetcher.fetch_daily(code, start, end)
        except FetchError as exc:
            failures += 1
            record = {"code": code, "ok": False, "error": str(exc)}
        else:
            record = {"code": code, "ok": True, **result.as_dict()}
            if result.bars:
                record["first"] = result.bars[0].trade_date.isoformat()
                record["last"] = result.bars[-1].trade_date.isoformat()
                record["last_close"] = result.bars[-1].close
            if store is not None and result.bars:
                store.save_bars(code, result.bars)
                record["saved_rows"] = store.bar_count(code)
        out["results"].append(record)
        if not args.json:
            if record.get("ok"):
                print(
                    f"[{code}] {record['source_tag']} via {record['source_name']} "
                    f"rows={record['rows']} dropped={record['dropped']} "
                    f"{record['elapsed_ms']}ms first={record.get('first')} last={record.get('last')}"
                )
                for attempt in record["attempts"]:
                    print(f"      · {attempt}")
                if record.get("note"):
                    print(f"      ! {record['note']}")
            else:
                print(f"[{code}] FAILED {record['error']}")

    out["breaker"] = breaker.snapshot()
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print("breaker:", breaker.snapshot() or "无失败")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
