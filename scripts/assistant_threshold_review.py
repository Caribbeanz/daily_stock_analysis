# -*- coding: utf-8 -*-
"""阈值回看（D-5 分数带 / D-6 占位参数）：把「分布」摆出来，而不是凭感觉调数。

用法::

    # 1) 分数带与阶段/意见分布（研发区间 IS）
    python scripts/assistant_threshold_review.py --codes 002202 601818 601919 601169 000725 \\
        --start 2025-04-03 --end 2026-02-27 --import-main-db data/stock_analysis.db

    # 2) 看候选分数带会怎样改变档位占比（不写库、只打印）
    python scripts/assistant_threshold_review.py ... --bands 68,56,42,28

    # 3) 单参数一维扫描（禁止组合爆炸：一次只扫一个参数）
    python scripts/assistant_threshold_review.py ... --sweep advice_margin=0.05,0.10,0.15

只读现有 K 线：不生成快照、不写结论；只有 `--import-main-db` 时才把日线复制进 L1。

两条纪律：

- **只看分布，不拿收益反推阈值**（§9.4.4 明确禁止「以回测结果反推阈值并在同一区间验证」）。
  分数带是「评分刻度」，用分数自身分布标定；收益相关的结论另用 OOS 区间复核。
- **IS / OOS 必须分开**：``--end`` 定 IS，改动后再用另一段跑 OOS 看是否翻车。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.assistant.config.overrides import ParamSet  # noqa: E402
from src.assistant.data.storage import AssistantStore  # noqa: E402
from src.assistant.domain import PortfolioView, PositionView, StockView  # noqa: E402
from src.assistant.engine.guard_chain import GuardChainInput, run_guard_chain  # noqa: E402
from src.assistant.engine.scoring import SCORE_BANDS, decide  # noqa: E402
from src.assistant.engine.stage import StageDebounceState  # noqa: E402
from src.assistant.enums import Action, Preset, SourceTag  # noqa: E402
from src.assistant.versioning import RuleFingerprint  # noqa: E402

from assistant_common import (  # noqa: E402 - 同目录脚本
    import_from_main_db,
    load_bars_bundle,
    parse_day,
    parse_holdings,
    warmup_start,
)

#: 阶段顺序，打印时固定，便于横向对比
_STAGES = ("S0", "S1", "S2", "S3", "S4", "S5")


def _band_of(score: float, thresholds: Sequence[tuple[float, str]]) -> str:
    for threshold, name in thresholds:
        if score >= threshold:
            return name
    return thresholds[-1][1]


def _parse_bands(text: str | None) -> tuple[tuple[float, str], ...] | None:
    if not text:
        return None
    parts = [item.strip() for item in text.split(",") if item.strip()]
    if len(parts) != 4:
        raise SystemExit("--bands 需要 4 个边界，且从高到低，如 68,56,42,28")
    values = [float(item) for item in parts]
    if values != sorted(values, reverse=True):
        raise SystemExit("--bands 必须从高到低")
    names = [name for _, name in SCORE_BANDS]
    return tuple(zip(values, names[:4])) + ((0.0, names[-1]),)


def _percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def _parse_overrides(items: Sequence[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items or []:
        key, _, raw = item.partition("=")
        if not key or not raw:
            raise SystemExit(f"--override 需要 key=value，收到 {item!r}")
        try:
            out[key] = float(raw)
        except ValueError:
            out[key] = raw
    return out


def review_pass(
    *,
    bars_by_code: Mapping[str, Sequence[Any]],
    days: Sequence[date],
    params: ParamSet,
    bands: Sequence[tuple[float, str]] | None = None,
    holdings: Mapping[str, int] | None = None,
    min_bars: int = 60,
    lookback_bars: int = 260,
) -> dict[str, Any]:
    """跑一遍分布回看：分数分位、档位占比、阶段/意见分布、切换与改写次数。"""
    thresholds = bands or SCORE_BANDS
    calc_version = RuleFingerprint(param_hash=params.param_hash).calc_version()
    held = dict(holdings or {})
    scores: list[float] = []
    bands_count: Counter[str] = Counter()
    stages: Counter[str] = Counter()
    advices: Counter[str] = Counter()
    rewritten = 0
    switches = 0
    plans = 0
    observations = 0
    prev_advice: dict[str, Action | None] = {code: None for code in bars_by_code}
    stages_state: dict[str, StageDebounceState] = {}

    for position, day in enumerate(days):
        for code, bars in bars_by_code.items():
            upto = [bar for bar in bars if bar.trade_date <= day]
            if len(upto) < min_bars or upto[-1].trade_date != day:
                continue
            view = StockView(
                code=code,
                name=code,
                bars=upto[-lookback_bars:],
                position=PositionView(shares=held.get(code, 0)),
                source_tag=SourceTag.D1,
            )
            outcome = run_guard_chain(
                GuardChainInput(
                    view=view,
                    portfolio=PortfolioView(total_capital=500_000.0, cash_available=400_000.0),
                    params=params,
                    calc_version=calc_version,
                    generated_at=datetime.combine(day, time(20, 0)),
                    expected_data_date=day,
                    trading_days=list(days[: position + 1]),
                    stage_state=stages_state.get(code, StageDebounceState()),
                    previous_advice=prev_advice.get(code),
                )
            )
            stages_state[code] = outcome.stage_state
            result = outcome.result
            score = float(result.conclusion.score)
            band = _band_of(score, thresholds)
            proposed = decide(score, is_holding=code in held, params=params).action
            final = result.conclusion.advice

            observations += 1
            scores.append(score)
            bands_count[band] += 1
            stages[result.stage_result.stage.value] += 1
            advices[result.conclusion.advice_display] += 1
            plans += len(result.plans)
            if final is not None and final is not proposed:
                rewritten += 1
            if final is not None and prev_advice.get(code) is not None and final is not prev_advice[code]:
                switches += 1
            prev_advice[code] = final

    return {
        "observations": observations,
        "scores": scores,
        "quantiles": {q: _percentile(scores, q) for q in (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)},
        "bands": dict(bands_count),
        "band_order": [name for _, name in SCORE_BANDS],
        "stages": {stage: stages.get(stage, 0) for stage in _STAGES},
        "advices": dict(advices),
        "plans": plans,
        "rewritten": rewritten,
        "switches": switches,
        "calc_version": calc_version,
    }


def _pct(n: int, total: int) -> str:
    return f"{n / total * 100:.1f}%" if total else "—"


def _print_report(title: str, metrics: Mapping[str, Any]) -> None:
    total = int(metrics["observations"])
    print(f"\n== {title} ==  观测 {total}（日 × 标的）· 规则 {metrics['calc_version']}")
    if not total:
        print("区间内没有可用观测（K 线不足或日期错位）")
        return
    q = metrics["quantiles"]
    mean = sum(metrics["scores"]) / total
    print(
        "分数分位："
        + " ".join(f"p{int(k * 100):02d} {v:.0f}" for k, v in q.items())
        + f" | 均值 {mean:.1f}"
    )
    order = metrics["band_order"]
    print(
        "档位占比："
        + " | ".join(f"{name} {_pct(metrics['bands'].get(name, 0), total)}" for name in order)
    )
    print(
        "阶段分布："
        + " | ".join(f"{stage} {_pct(count, total)}" for stage, count in metrics["stages"].items())
    )
    top = sorted(metrics["advices"].items(), key=lambda kv: kv[1], reverse=True)
    print("意见分布：" + " | ".join(f"{name} {_pct(count, total)}" for name, count in top))
    print(
        f"意见切换 {metrics['switches']} 次（{_pct(metrics['switches'], total)}）· "
        f"非决策表直出（档位缓冲/阶段门改写）{metrics['rewritten']} 次"
        f"（{_pct(metrics['rewritten'], total)}）· 计划 {metrics['plans']} 条"
        f"（{metrics['plans'] / total:.2f} 条/观测）"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="助手阈值回看（分数带/占位参数）")
    parser.add_argument("--db", default=None, help="助手 SQLite；缺省用默认库")
    parser.add_argument("--codes", nargs="+", required=True)
    parser.add_argument("--start", type=parse_day, default=None)
    parser.add_argument("--end", type=parse_day, default=None)
    parser.add_argument("--preset", choices=[p.value for p in Preset], default=Preset.STANDARD.value)
    parser.add_argument("--lookback", type=int, default=260)
    parser.add_argument("--min-bars", type=int, default=60)
    parser.add_argument("--holding", action="append", help="按持仓口径评估，形如 601919:1000")
    parser.add_argument("--import-main-db", default=None, help="把主库 stock_daily 复制进 L1")
    parser.add_argument("--bands", default=None, help="候选分数带，从高到低 4 个边界")
    parser.add_argument("--override", action="append", help="参数覆盖 key=value，可多次")
    parser.add_argument("--sweep", default=None, help="一维扫描，形如 advice_margin=0.05,0.10,0.15")
    args = parser.parse_args(argv)

    store = AssistantStore(args.db)
    if args.import_main_db:
        store.create_all()
        imported = import_from_main_db(
            Path(args.import_main_db), store, args.codes,
            start=warmup_start(args.start, args.lookback), end=args.end,
        )
        if not imported:
            raise SystemExit("主库里这些标的没有日线；检查代码或区间")
        print("已导入：" + "，".join(f"{code} {rows} 行" for code, rows in sorted(imported.items())))

    bundle = load_bars_bundle(
        store, args.codes, start=args.start, end=args.end, lookback_bars=args.lookback
    )
    if not bundle:
        raise SystemExit("L1 没有这些标的的日线")
    all_days = sorted({bar.trade_date for bars in bundle.values() for bar in bars})
    days = [day for day in all_days
            if (args.start is None or day >= args.start) and (args.end is None or day <= args.end)]
    if not days:
        raise SystemExit("区间内没有交易日")

    base = ParamSet.build(Preset(args.preset), overrides=_parse_overrides(args.override))
    bands = _parse_bands(args.bands)
    title = f"{days[0]} ~ {days[-1]}（{args.preset}）"
    metrics = review_pass(
        bars_by_code=bundle, days=days, params=base, bands=bands,
        holdings=parse_holdings(args.holding), min_bars=args.min_bars,
        lookback_bars=args.lookback,
    )
    _print_report(title, metrics)

    if args.sweep:
        key, _, raw = args.sweep.partition("=")
        values = [item.strip() for item in raw.split(",") if item.strip()]
        if not key or not values:
            raise SystemExit("--sweep 需要 参数名=v1,v2,...")
        print(f"\n== 单参数扫描 {key} ==")
        print("取值 | 强/偏强/中性/偏弱/差 | S1/S5 | 切换率 | 计划/观测")
        for value in values:
            try:
                numeric = float(value)
            except ValueError:
                numeric = value
            params = ParamSet.build(Preset(args.preset), overrides={**(_parse_overrides(args.override)), key: numeric})
            row = review_pass(
                bars_by_code=bundle, days=days, params=params, bands=bands,
                holdings=parse_holdings(args.holding), min_bars=args.min_bars,
                lookback_bars=args.lookback,
            )
            total = row["observations"] or 1
            shares = "/".join(f"{row['bands'].get(name, 0) / total * 100:.0f}%" for name in row["band_order"])
            print(
                f"{value} | {shares} | "
                f"{row['stages']['S1'] / total * 100:.0f}%/{row['stages']['S5'] / total * 100:.0f}% | "
                f"{row['switches'] / total * 100:.1f}% | {row['plans'] / total:.2f}"
            )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
