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
- **大盘靠 ``--index`` 注入**：给了指数符号（如 ``sh000001``）就按日算 M1 环境档并喂给守卫链；
  没给则 ``market=None``，M1 与「大盘 ≤ 警示」都不触发，结果偏乐观，报告用
  ``counters.market_missing_days`` 明写。宽度（涨跌家数）回放无法还原，一律留空，
  对应的「市场宽度恶化」风险项不会触发——这一点也写进 ``not_modelled``。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.assistant.backtest import RoundConfig  # noqa: E402
from src.assistant.backtest_replay import (  # noqa: E402
    ReplayConfig,
    market_views_from_index,
    ReplayState,
    load_state,
    replay,
)
from src.assistant.config.overrides import ParamSet  # noqa: E402
from src.assistant.data.storage import AssistantStore  # noqa: E402
from src.assistant.enums import Preset  # noqa: E402
from assistant_common import (  # noqa: E402 - 同目录脚本，直接 import
    import_from_main_db,
    load_bars_bundle,
    parse_day,
    parse_holdings,
    warmup_start,
)


def _parse_overrides(items: list[str] | None) -> dict[str, object]:
    out: dict[str, object] = {}
    for item in items or []:
        key, _, raw = item.partition("=")
        if not key or not raw:
            raise SystemExit(f"--override 需要 key=value，收到 {item!r}")
        try:
            out[key] = float(raw)
        except ValueError:
            out[key] = raw
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="assistant 历史计划回放（只读 L1）")
    parser.add_argument("--db", default=None, help="SQLite 路径；缺省用助手默认库")
    parser.add_argument("--codes", nargs="+", required=True, help="A 股代码")
    parser.add_argument("--start", type=parse_day, default=None, help="回放起始交易日")
    parser.add_argument("--end", type=parse_day, default=None, help="回放结束交易日")
    parser.add_argument("--preset", choices=[p.value for p in Preset], default=Preset.STANDARD.value)
    parser.add_argument("--lookback", type=int, default=260, help="每天喂给守卫链的 K 线根数")
    parser.add_argument("--min-bars", type=int, default=60, help="少于该根数不判")
    parser.add_argument("--max-days", type=int, default=None, help="规模护栏：最多回放多少个交易日")
    parser.add_argument("--cost-bps", type=float, default=3.0, help="单边成本（基点）")
    parser.add_argument("--max-hold", type=int, default=60, help="最长持有交易日")
    parser.add_argument("--min-closed", type=int, default=30, help="样本门槛：不足只报计数")
    parser.add_argument("--holding", action="append", help="起点持仓，形如 600000:1000，可多次")
    parser.add_argument("--override", action="append", help="参数覆盖 key=value（只做对照实验用），可多次")
    parser.add_argument("--index", default=None,
                        help="大盘指数符号（如 sh000001）；给了才注入 M1 环境档，缺省不注入")
    parser.add_argument("--sectors", action="store_true",
                        help="把板块层接进回放：按映射读 L1 板块指数日线，逐日复算三档与 rs_t")
    parser.add_argument("--sector-map", default="data/cache/assistant_sector_map.json",
                        help="个股→板块映射 JSON（assistant_ingest_history.py --sectors 产出）")
    parser.add_argument("--adjust", choices=["raw", "total_return"], default="raw",
                        help="价格口径：raw=不复权（默认）；total_return=含分红再投（前复权）")
    parser.add_argument("--valuations", action="store_true",
                        help="把估值分位与分红事实接进评分（w_valuation/w_dividend 与 DIV-1/2/3）")
    parser.add_argument("--dividend-tax", type=float, default=0.0,
                        help="红利税率（0=税前；持股 1 月–1 年用 0.10），只影响 total_return")
    parser.add_argument("--checkpoint", default=None, help="检查点路径（可续跑）")
    parser.add_argument("--resume", action="store_true", help="从检查点续跑")
    parser.add_argument("--import-main-db", default=None,
                        help="主项目 SQLite 路径；把其中的 stock_daily 复制进助手 L1 再回放")
    parser.add_argument("--out", default=None, help="结果 JSON 输出路径")
    args = parser.parse_args(argv)

    store = AssistantStore(args.db)
    store.create_all()
    holdings = parse_holdings(args.holding)

    if args.import_main_db:
        imported = import_from_main_db(
            Path(args.import_main_db), store, args.codes,
            start=warmup_start(args.start, args.lookback),
            end=args.end,
        )
        if not imported:
            raise SystemExit("主库里这些标的没有日线；检查代码或 --start/--end")
        print("已从主库导入：" + "，".join(f"{code} {rows} 行" for code, rows in sorted(imported.items())))

    bars_by_code = load_bars_bundle(
        store, args.codes, start=args.start, end=args.end, lookback_bars=args.lookback
    )
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
        params=ParamSet.build(Preset(args.preset), overrides=_parse_overrides(args.override)),
        adjust=args.adjust,
        dividend_tax_rate=args.dividend_tax,
        round_config=RoundConfig(
            cost_bps=args.cost_bps,
            max_hold_days=args.max_hold,
            min_closed_rounds=args.min_closed,
        ),
        holdings=holdings,
        max_days=args.max_days,
    )
    state = load_state(args.checkpoint) if (args.resume and args.checkpoint) else ReplayState()

    market_by_day = None
    index_bars = None
    if args.index:
        index_bars = store.load_bars(args.index, end=args.end)
        if not index_bars:
            raise SystemExit(f"库里没有 {args.index} 的日线，先跑 assistant_ingest_history.py --index {args.index}")
        market_by_day = market_views_from_index(index_bars, days, symbol=args.index)
        print(f"大盘注入：{args.index} · 覆盖 {len(market_by_day)}/{len(days)} 个交易日")

    sector_by_code = None
    sector_bars_by_name = None
    sector_code_by_name = None
    if args.sectors:
        map_path = Path(args.sector_map)
        if not map_path.is_file():
            raise SystemExit(
                f"没有板块映射 {map_path}：先跑 assistant_ingest_history.py "
                f"--codes ... --sectors（会补板块指数日线并落这份映射）"
            )
        payload = json.loads(map_path.read_text(encoding="utf-8"))
        sector_by_code = {
            str(code): str(row.get("sector_name") or "")
            for code, row in (payload.get("map") or {}).items()
            if row.get("sector_name")
        }
        sector_code_by_name = {
            str(row.get("sector_name")): str(row.get("sector_code") or "")
            for row in (payload.get("map") or {}).values()
            if row.get("sector_name")
        }
        sector_bars_by_name = store.load_sector_bars_by_name(end=args.end)
        unmapped = sorted(set(bars_by_code) - set(sector_by_code))
        missing_bars = sorted(
            {
                sector_by_code[code]
                for code in bars_by_code
                if sector_by_code.get(code) and not sector_bars_by_name.get(sector_by_code[code])
            }
        )
        covered = sum(
            1 for code in bars_by_code if sector_by_code.get(code) in sector_bars_by_name
        )
        print(
            f"板块注入：映射 {len(set(bars_by_code) & set(sector_by_code))}/{len(bars_by_code)} 只 · "
            f"L1 板块日线 {len(sector_bars_by_name)} 个 · 真正有序列 {covered} 只"
        )
        if unmapped:
            print(f"  未映射（板块分恒 0）：{', '.join(unmapped)}")
        if missing_bars:
            print(f"  缺板块日线（板块分恒 0）：{', '.join(missing_bars)}")
        if not index_bars:
            print("  提示：没给 --index，rs_t 与 SEC-1 不会生效（三档加减分仍照算）")

    dividend_events = None
    if config.adjust == "total_return" or args.valuations:
        dividend_events = store.load_dividend_events(list(bars_by_code))
        covered = sum(1 for code in bars_by_code if dividend_events.get(code))
        if config.adjust == "total_return":
            print(f"分红口径：含分红再投 · 事件覆盖 {covered}/{len(bars_by_code)} 只 · "
                  f"税率 {args.dividend_tax:.0%}")
        else:
            # raw 口径下事件不参与复权，只用来算「连续分红年数 / 是否中断 / 股息率」
            print(f"分红事实：事件覆盖 {covered}/{len(bars_by_code)} 只（口径仍为不复权）")
        if not covered and config.adjust == "total_return":
            raise SystemExit(
                "库里没有分红事件，先跑 assistant_ingest_history.py --dividends；"
                "否则 total_return 与 raw 没有区别"
            )

    valuation_rows = None
    if args.valuations:
        valuation_rows = {
            code: store.load_valuation_rows(code, end=args.end) for code in bars_by_code
        }
        with_rows = sum(1 for rows in valuation_rows.values() if rows)
        print(f"估值注入：{with_rows}/{len(bars_by_code)} 只有估值行；分位为落库时按 ≤当日样本滚动算好")
        if not with_rows:
            raise SystemExit(
                "库里没有估值行，先跑 assistant_ingest_history.py --valuations"
            )

    result = replay(
        days=days,
        bars_by_code=bars_by_code,
        config=config,
        state=state,
        checkpoint_path=args.checkpoint,
        market_by_day=market_by_day,
        dividend_events=dividend_events,
        sector_by_code=sector_by_code,
        sector_bars_by_name=sector_bars_by_name,
        sector_code_by_name=sector_code_by_name,
        index_bars=index_bars,
        valuations_by_code=valuation_rows,
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
    sector = stats.get("sector") or {}
    if sector.get("enabled"):
        print(
            f"板块：{len(sector['sectors'])} 个板块 · 板块视图 {sector['view_code_days']} 个 code-day "
            f"（偏强 {sector['strong_code_days']} / 偏弱 {sector['weak_code_days']} / "
            f"降级 {sector['degraded_code_days']} / 缺失 {sector['missing_code_days']}）"
        )

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
