# -*- coding: utf-8 -*-
"""把历史日线灌进助手 L1（``assistant_stock_daily_raw``），供回放/回看使用。

用法::

    # 自选清单直接从主项目 watchlist_items 读（--watchlist），补齐 3 年
    python scripts/assistant_ingest_history.py --db data/cache/replay.db \\
        --watchlist data/stock_analysis.db --years 3

    # 指定标的与区间
    python scripts/assistant_ingest_history.py --db data/cache/replay.db \\
        --codes 601919 601818 --start 2023-01-01 --end 2026-10-22

口径：
- **只补缺**：默认只补 L1 里没有的标的，全量重取用 ``--refill``。
- **两种取数通道**：``--source fetcher``（默认，走产品同一条 ``DailyFetcher``，需要 akshare）
  或 ``--source tencent``（直连腾讯行情接口，不依赖 akshare，**前复权**）。
  回放建议用专用库（``data/cache/replay.db``），别把前复权序列混进实盘用的 L1。
- **不写结论**：只写 L1 日线，不生成快照、不碰 L2/L3。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.assistant.data.fetcher import (  # noqa: E402
    CircuitBreaker,
    DailyFetcher,
    ReferenceFetcher,
)
from src.assistant.domain import Bar  # noqa: E402
from src.assistant.data.storage import AssistantStore  # noqa: E402

from assistant_common import parse_day  # noqa: E402 - 同目录脚本


#: 腾讯行情接口一次最多返回的 K 线根数（3 年约 730 根，留余量）
_TENCENT_PAGE = 800


def _tencent_symbol(code: str) -> str:
    """A 股/ETF 代码 → 腾讯行情前缀（sh / sz / bj）。ETF 按号段判断，不猜。"""
    text = code.strip()
    head = text[:1]
    if head in {"6", "5", "9"}:
        return f"sh{text}"
    if head in {"0", "2", "3", "1"}:
        return f"sz{text}"
    if head in {"4", "8"}:
        return f"bj{text}"
    return f"sh{text}"


def fetch_tencent(code: str, start: date, end: date) -> list[Bar]:
    """直连腾讯日线（**前复权**），返回按日期升序的 Bar 列表。

    为什么允许这条路：这台机器的项目环境不一定装了 akshare，而回放需要历史序列；
    腾讯接口无需 token、字段稳定。前复权保证同一序列内收益连续（跨除权日不跳空），
    代价是与实盘用的不复权价绝对值不同——所以只用于回放专用库。
    """
    import requests  # noqa: PLC0415 - 仅在走这条通道时需要

    symbol = _tencent_symbol(code)
    params = {"param": f"{symbol},day,{start.isoformat()},{end.isoformat()},{_TENCENT_PAGE},qfq"}
    response = requests.get(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        params=params,
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    payload = response.json()
    node = (payload.get("data") or {}).get(symbol) or {}
    rows = node.get("qfqday") or node.get("day") or []
    bars: list[Bar] = []
    for row in rows:
        if len(row) < 6:
            continue
        # 腾讯字段顺序：日期, 开, 收, 高, 低, 量（注意不是 开高低收）
        day_text, open_, close_, high_, low_, volume = row[:6]
        bars.append(
            Bar(
                trade_date=date.fromisoformat(str(day_text)),
                open=float(open_),
                high=float(high_),
                low=float(low_),
                close=float(close_),
                volume=float(volume or 0.0),
            )
        )
    return [bar for bar in bars if start <= bar.trade_date <= end]


def watchlist_codes(db: Path) -> list[str]:
    """主项目 ``watchlist_items`` 里 enabled 的标的（只读）。"""
    if not db.is_file():
        raise SystemExit(f"主库不存在：{db}")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT symbol FROM watchlist_items WHERE enabled = 1 ORDER BY sort_order, symbol"
        ).fetchall()
    except sqlite3.Error as exc:
        raise SystemExit(f"读 watchlist_items 失败：{exc}") from exc
    finally:
        conn.close()
    return [str(row[0]) for row in rows if row and row[0]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="助手 L1 历史日线补齐")
    parser.add_argument("--db", required=True, help="目标助手 SQLite（L1 落这里）")
    parser.add_argument("--codes", nargs="+", default=None, help="A 股/ETF 代码")
    parser.add_argument("--watchlist", default=None, help="主项目 SQLite 路径；从其 watchlist_items 取标的")
    parser.add_argument("--start", type=parse_day, default=None)
    parser.add_argument("--end", type=parse_day, default=None)
    parser.add_argument("--years", type=int, default=3, help="未给 --start 时向前取多少年")
    parser.add_argument("--refill", action="store_true", help="全量重取（默认只补缺）")
    parser.add_argument("--source", choices=["fetcher", "tencent"], default="fetcher",
                        help="取数通道：fetcher=产品同链路（需 akshare）；tencent=直连（前复权）")
    parser.add_argument("--dividends", action="store_true",
                        help="顺带取分红送转事件（除权日 / 每股派息 / 送转），供 --adjust total_return")
    parser.add_argument("--index", nargs="*", default=None,
                        help="顺带补指数日线（如 sh000001），按原符号存入 L1，供回放注入大盘")
    parser.add_argument("--sectors", action="store_true",
                        help="顺带补板块指数日线：对本次 --codes 做个股→板块映射，落涉及的板块")
    parser.add_argument("--sector-names", nargs="*", default=None,
                        help="直接指定板块名（如 银行 白酒），补这些板块的指数日线")
    parser.add_argument("--sector-map", default="data/cache/assistant_sector_map.json",
                        help="个股→板块映射的落盘路径（回放读它，保证不联网）")
    parser.add_argument("--sector-refill", action="store_true",
                        help="板块指数日线全量重取（默认已有就跳过）")
    args = parser.parse_args(argv)

    codes = list(args.codes or [])
    if args.watchlist:
        codes += watchlist_codes(Path(args.watchlist))
    codes = sorted({code.strip() for code in codes if code.strip()})
    if not codes and not (args.sectors or args.sector_names):
        raise SystemExit("没有标的：给 --codes 或 --watchlist")

    end = args.end or date.today()
    start = args.start or (end - timedelta(days=365 * args.years + 10))

    store = AssistantStore(args.db)
    store.create_all()

    print(f"目标库 {args.db} · 区间 {start} ~ {end} · {len(codes)} 只标的")
    filled = skipped = failed = 0
    for code in codes:
        existing = store.load_bars(code, start=start, end=end)
        if existing and not args.refill:
            have = {bar.trade_date for bar in existing}
            print(f"  {code}: 已有 {len(existing)} 根（{min(have)} ~ {max(have)}），跳过")
            skipped += 1
            continue
        # 每只标的独立熔断器：批量补历史时，某只标的连失败三次不该把后面的全挡住
        fetcher = DailyFetcher(
            store=store, breaker=CircuitBreaker(), min_interval_seconds=0.6
        )
        try:
            if args.source == "tencent":
                bars = fetch_tencent(code, start, end)
                tag = "tencent-http/qfq"
            else:
                result = fetcher.fetch_daily(code, start, end)
                bars = list(result.bars)
                tag = f"{result.source_tag.value}({result.source_name})"
        except Exception as exc:  # noqa: BLE001 - 单只失败不影响其它标的
            print(f"  {code}: 取数失败 {type(exc).__name__}: {exc}")
            failed += 1
            continue
        if not bars:
            print(f"  {code}: 接口返回空序列，跳过（不写空数据）")
            failed += 1
            continue
        rows = store.save_bars(code, bars)
        print(f"  {code}: {rows} 行 · 来源 {tag} · {bars[0].trade_date} ~ {bars[-1].trade_date}")
        filled += 1

    for symbol in args.index or []:
        existing = store.load_bars(symbol, start=start, end=end)
        if existing and not args.refill:
            print(f"  {symbol}: 已有 {len(existing)} 根，跳过")
            skipped += 1
            continue
        # 指数走 ReferenceFetcher（新浪 index_zh_daily_sina），与个股不是同一条链路
        index_fetcher = ReferenceFetcher(store=store, breaker=CircuitBreaker())
        try:
            result = index_fetcher.fetch_index(symbol, start, end)
        except Exception as exc:  # noqa: BLE001
            print(f"  {symbol}: 指数取数失败 {type(exc).__name__}: {exc}")
            failed += 1
            continue
        rows = store.save_bars(symbol, list(result.bars))
        print(f"  {symbol}: {rows} 行 · 来源 {result.source_tag.value}({result.source_name}) · "
              f"{result.bars[0].trade_date} ~ {result.bars[-1].trade_date}")
        filled += 1

    if args.sectors or args.sector_names:
        from src.assistant.data.sector_map import IndustryResolver  # noqa: PLC0415

        # 板块代码只能从同花顺一览拿（个股映射也是按「板块名」对齐），一次请求顺带取回
        ref_fetcher = ReferenceFetcher(store=store, breaker=CircuitBreaker())
        overview = ref_fetcher.fetch_sectors()
        if not overview.rows:
            print(f"板块一览取数失败（{overview.note or '无数据'}），板块日线本次不补")
            failed += 1
        else:
            boards = {row.sector_name: row.sector_code for row in overview.rows if row.sector_code}
            wanted: dict[str, str] = {}
            if args.sectors:
                resolved = IndustryResolver().resolve(codes, boards)
                print(f"个股→板块映射：{len(resolved)}/{len(codes)} 只成功")
                for code in codes:
                    ref = resolved.get(code)
                    print(f"  {code}: {ref.sector_name if ref else '未映射'}"
                          + (f"（{ref.matched_level} 级 {ref.raw_industry}）" if ref else ""))
                wanted.update({ref.sector_name: ref.sector_code for ref in resolved.values()})
            for name in args.sector_names or []:
                code = boards.get(name)
                if not code:
                    print(f"  板块 {name!r} 不在同花顺一览里，跳过（不猜代码）")
                    failed += 1
                    continue
                wanted[name] = code
            print(f"补板块指数日线：{len(wanted)} 个板块")
            for name, code in sorted(wanted.items()):
                existing = store.load_sector_bars(code, end=end)
                if existing and not (args.refill or args.sector_refill):
                    print(f"  {name}({code}): 已有 {len(existing)} 根，跳过")
                    skipped += 1
                    continue
                bars = ref_fetcher.fetch_sector_index(name, start, end)
                if not bars:
                    print(f"  {name}({code}): 板块指数取不到，跳过（不写空数据）")
                    failed += 1
                    continue
                rows = store.save_sector_bars(code, name, bars)
                print(f"  {name}({code}): {rows} 行 · {bars[0].trade_date} ~ {bars[-1].trade_date}")
                filled += 1
            if args.sectors:
                # 映射单独落一份 JSON：回放要求「不联网」，靠它按代码→板块取 L1 序列，
                # 不去现场打东财 F10（实测 27 只要 3 秒，但回放几百天不该依赖网络可用）
                map_path = Path(args.sector_map)
                map_path.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "source": "ths-board-list + em-f10",
                    "map": {
                        code: {
                            "sector_name": ref.sector_name,
                            "sector_code": ref.sector_code,
                            "matched_level": ref.matched_level,
                            "raw_industry": ref.raw_industry,
                        }
                        for code, ref in sorted(resolved.items())
                    },
                }
                map_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                print(f"  映射已写入 {map_path}（{len(resolved)} 只）")

    if args.dividends:
        from src.assistant.data.dividend import fetch_dividend_events  # noqa: PLC0415

        print("补分红送转事件：")
        got = 0
        for code in codes:
            try:
                events = fetch_dividend_events(code)
            except Exception as exc:  # noqa: BLE001 - 单只失败不影响其它标的
                print(f"  {code}: 分红取数失败 {type(exc).__name__}: {exc}")
                continue
            if not events:
                print(f"  {code}: 无实施分配记录")
                continue
            store.save_dividend_events(code, events)
            span = f"{events[0].ex_date} ~ {events[-1].ex_date}"
            cash = sum(e.cash_per_share for e in events)
            print(f"  {code}: {len(events)} 次除权（{span}）· 累计每股派息 {cash:.3f} 元")
            got += 1
        print(f"  分红事件写入 {got} 只")

    print(f"完成：新写入 {filled} 只，跳过 {skipped} 只，失败 {failed} 只")
    return 0 if filled or skipped else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
