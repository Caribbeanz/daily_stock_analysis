# -*- coding: utf-8 -*-
"""纸面回看（需求 §9.2 第一步）：把某只标的某一天的决策摊开成一张可逐字段核对的表。

用途不是「再跑一遍回测」，而是**把决策链的输入与产出摆在一起**，让人肉眼核对口径：

- 五个维度**各自有没有数据、依据是什么**（技术面/大盘/板块/基本面/估值与分红）；
- 评分**每一项加减分各是多少、理由是什么**（S5 trace 里的 ``score`` 拆解）；
- 阶段、关键价位、条件计划、风险、仓位建议各自的值与来源；
- 最后一节是人工核对清单，逐条打勾。

输入构造与**回放**走同一条路径（`market_views_from_index` / `SectorSeries` +
`sector_view_for_day` / `valuation_view`），决策本身走**同一个守卫链**
（`run_guard_chain`），所以这份表反映的就是引擎的真实产出，不是另写一套逻辑。

用法::

    python scripts/assistant_paper_review.py --db data/cache/pool20.db \\
        --code 600188 --day 2026-11-26 --index sh000001 \\
        --out deliverables/paper-review-600188-20261126.md
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.assistant.backtest_replay import (  # noqa: E402
    SectorSeries,
    market_views_from_index,
    sector_view_for_day,
)
from src.assistant.config.overrides import ParamSet  # noqa: E402
from src.assistant.data.storage import AssistantStore  # noqa: E402
from src.assistant.data.valuation import valuation_view  # noqa: E402
from src.assistant.domain import PortfolioView, PositionView, SectorView, StockView  # noqa: E402
from src.assistant.engine.guard_chain import GuardChainInput, run_guard_chain  # noqa: E402
from src.assistant.enums import Preset  # noqa: E402
from src.assistant.versioning import RuleFingerprint  # noqa: E402
from assistant_common import parse_day  # noqa: E402 - 同目录脚本

DEFAULT_SECTOR_MAP = "data/cache/assistant_sector_map.json"


def _pct_by_day(bars: Sequence[Any]) -> dict[date, float]:
    out: dict[date, float] = {}
    for prev, cur in zip(bars, bars[1:]):
        if prev.close > 0:
            out[cur.trade_date] = (cur.close / prev.close - 1) * 100
    return out


def _sector_inputs(
    store: AssistantStore, code: str, day: date, index_pct: dict[date, float], map_path: Path
) -> tuple[SectorView | None, dict[str, str]]:
    """按映射 JSON + L1 板块日线构造板块视图，返回 ``(视图, 供展示的依据说明)``。"""
    info: dict[str, str] = {}
    if not map_path.is_file():
        info["依据"] = f"没有映射文件 {map_path}"
        return None, info
    payload = json.loads(map_path.read_text(encoding="utf-8"))
    row = (payload.get("map") or {}).get(code)
    if not row:
        info["依据"] = "映射文件里没有这只标的（未映射）"
        return None, info
    name = str(row.get("sector_name") or "")
    bars_by_name = store.load_sector_bars_by_name(end=day)
    bars = bars_by_name.get(name)
    info["东财行业"] = f"{row.get('raw_industry')}（{row.get('matched_level')} 级）"
    info["同花顺板块"] = f"{name}（{row.get('sector_code')}）"
    if not bars:
        info["依据"] = f"L1 没有板块 {name} 的指数日线"
        return None, info
    series = SectorSeries.build(name, str(row.get("sector_code") or ""), bars)
    view = sector_view_for_day(series, day, index_pct)
    info["序列"] = f"{len(bars)} 根（{bars[0].trade_date} ~ {bars[-1].trade_date}）"
    return view, info


def _plan_rows(branches: Sequence[Any]) -> list[list[str]]:
    rows: list[list[str]] = []
    for branch in branches:
        trigger = "—"
        if branch.trigger_price is not None:
            trigger = f"{branch.trigger_price:.2f}"
        elif branch.pct_change is not None:
            trigger = f"{branch.pct_change:+.2%}"
        rows.append(
            [
                branch.branch_id,
                branch.scenario,
                branch.action.value,
                branch.trigger_type.value,
                trigger,
                str(branch.shares),
                f"{branch.est_amount:,.0f}",
                f"{branch.pct_of_total:.1%}",
                str(branch.valid_days),
                "；".join(branch.invalidate_if) or "—",
            ]
        )
    return rows


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    if not rows:
        return "_（空）_\n"
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines) + "\n"


def build_report(
    *,
    store: AssistantStore,
    code: str,
    day: date,
    index_symbol: str,
    sector_map: Path,
    lookback: int,
    capital: float,
    params: ParamSet,
) -> tuple[str, dict[str, Any]]:
    bars_all = store.load_bars(code, end=day)
    if not bars_all:
        raise SystemExit(f"库里没有 {code} 的日线，先跑 assistant_ingest_history.py")
    if bars_all[-1].trade_date != day:
        raise SystemExit(
            f"{code} 最后一根 K 线是 {bars_all[-1].trade_date}，不是 {day}；"
            "纸面回看要求当日有真实收盘数据（不拿旧数据冒充）"
        )
    bars = bars_all[-lookback:]
    last_close = bars[-1].close

    index_bars = store.load_bars(index_symbol, end=day)
    market = market_views_from_index(index_bars, [day], symbol=index_symbol).get(day)
    index_pct = _pct_by_day(index_bars)

    sector, sector_info = _sector_inputs(store, code, day, index_pct, sector_map)
    rows = store.load_valuation_rows(code, end=day)
    events = store.load_dividend_events([code]).get(code, [])
    valuation = valuation_view(rows, events, day, last_close)
    from src.assistant.data.dividend import dividend_facts  # noqa: PLC0415

    facts = dividend_facts(events, day, last_close)

    view = StockView(
        code=code,
        name=code,
        bars=bars,
        position=PositionView(),
        valuation=valuation,
    )
    calc_version = RuleFingerprint(param_hash=params.param_hash).calc_version()
    outcome = run_guard_chain(
        GuardChainInput(
            view=view,
            portfolio=PortfolioView(
                total_capital=capital, cash_available=capital, holding_market_value=0.0
            ),
            params=params,
            calc_version=calc_version,
            generated_at=datetime.combine(day, time(20, 0)),
            market=market,
            sector=sector,
            # 板块区整体没接入（没有映射文件）保持老行为；有映射但这只没接上才标「映射失败」
            sector_mapped=True if "没有映射文件" in sector_info.get("依据", "") else sector is not None,
            expected_data_date=day,
            trading_days=tuple(bar.trade_date for bar in bars_all),
            previous_advice=None,
        )
    )
    result = outcome.result
    score = next(
        (trace.payload.get("score", {}) for trace in result.trace if trace.payload.get("score")),
        {},
    )

    def ok(flag: bool) -> str:
        return "有" if flag else "**缺失**"

    lines: list[str] = []
    lines.append(f"# 纸面回看 · {code} · {day.isoformat()}\n")
    lines.append(
        f"规则 `{calc_version}` · 参数 `{params.param_hash[:10]}` · "
        f"数据截止 {bars[-1].trade_date}（{len(bars)} 根，回看窗口 {lookback}）\n"
    )

    lines.append("## 1. 五维度输入\n")
    lines.append(
        _table(
            ["维度", "状态", "依据"],
            [
                [
                    "技术面",
                    ok(len(bars) >= 60),
                    f"{len(bars)} 根日线（{bars[0].trade_date} ~ {bars[-1].trade_date}），收盘 {last_close:.2f}",
                ],
                [
                    "大盘",
                    ok(market is not None),
                    (
                        f"{index_symbol} 档位 {market.regime}、涨跌 {market.pct_change:+.2f}%、"
                        f"宽度缺失(degraded)" if market else f"L1 没有 {index_symbol} 当日指数或档位算不出"
                    ),
                ],
                [
                    "板块",
                    ok(sector is not None),
                    (
                        f"{sector_info.get('同花顺板块', '')} · 东财 {sector_info.get('东财行业', '')} · "
                        f"三档 {sector.strength} · rs_t "
                        f"{'—' if sector.rs_t is None else f'{sector.rs_t:+.2f}'} · "
                        f"近 10 日 rs {'/'.join(f'{v:+.2f}' for v in sector.history_rs_t) or '—'}"
                        f"{' · degraded' if sector.degraded else ''}"
                        if sector
                        else sector_info.get("依据", "未接入")
                    ),
                ],
                [
                    "估值",
                    ok(valuation.pe_percentile is not None or valuation.pb_percentile is not None),
                    (
                        f"PE 分位 {valuation.pe_percentile:.2f} / PB 分位 {valuation.pb_percentile:.2f}"
                        f"（样本 {len(rows)} 行，{rows[0].trade_date} ~ {rows[-1].trade_date}）"
                        if rows and (valuation.pe_percentile is not None or valuation.pb_percentile is not None)
                        else f"L1 估值行 {len(rows)} 条，分位算不出（样本不足 500 根）"
                    ),
                ],
                [
                    "分红",
                    ok(facts.has_data),
                    (
                        f"连续 {facts.continuity_years} 年"
                        f"{'（**中断**）' if facts.interrupted else ''} · "
                        f"TTM 股息率 {'—' if facts.dividend_yield is None else f'{facts.dividend_yield:.2f}%'} · "
                        f"下次除权 {facts.ex_dividend_date or '—'} · {facts.note}"
                    ),
                ],
                [
                    "基本面",
                    "**缺失**",
                    "FundamentalView 未接线（w_fundamental 恒 0，见实现笔记「没有做的部分」）",
                ],
            ],
        )
    )

    lines.append("\n## 2. 结论与评分拆解\n")
    lines.append(
        f"- 阶段 **{result.stage_result.stage.value}**（置信度 {result.stage_result.confidence.value}）"
        f"{'，阶段门命中' if result.conclusion.stage_gate_hit else ''}\n"
        f"- 趋势 {result.conclusion.trend} · 位置 {result.conclusion.position_tag}\n"
        f"- 建议 **{result.conclusion.advice.value}**"
        f"（状态 {result.conclusion.advice_state.value}）· 风险等级 {result.conclusion.risk_level.value}\n"
        f"- 总分 **{result.conclusion.score:.2f}**\n"
    )
    if score:
        lines.append(
            _table(
                ["项", "分值"],
                [
                    ["技术面", f"{score.get('tech', 0):+.2f}"],
                    ["大盘/板块", f"{score.get('market_adj', 0):+.2f}"],
                    ["基本面", f"{score.get('fundamental_adj', 0):+.2f}"],
                    ["分红", f"{score.get('dividend_adj', 0):+.2f}"],
                    ["估值", f"{score.get('valuation_adj', 0):+.2f}"],
                    ["合计", f"**{score.get('total', 0):+.2f}**"],
                ],
            )
        )
        notes = score.get("notes") or []
        if notes:
            lines.append("\n评分说明：\n")
            lines.extend(f"- {note}\n" for note in notes)
        parts = score.get("parts") or {}
        if parts:
            lines.append("\n技术面细分：" + "、".join(f"{k} {v:+.1f}" for k, v in parts.items()) + "\n")

    lines.append("\n## 3. 关键价位\n")
    lines.append(
        _table(
            ["类型", "价格", "来源"],
            [[lvl.kind, f"{lvl.price:.2f}", lvl.source] for lvl in result.key_levels],
        )
    )

    lines.append("\n## 4. 条件计划\n")
    lines.append(
        _table(
            ["分支", "场景", "动作", "触发类型", "触发价", "股数", "金额", "占总资金", "有效期",
             "失效条件"],
            _plan_rows(result.plans),
        )
    )

    lines.append("\n## 5. 风险\n")
    lines.append(
        _table(
            ["等级", "代码", "描述", "动作", "影响建议"],
            [
                [
                    signal.level.value,
                    signal.code.value,
                    signal.trigger_basis,
                    signal.action,
                    "是" if signal.affects_advice else "否",
                ]
                for signal in result.risks
            ],
        )
    )

    lines.append("\n## 6. 仓位建议\n")
    if result.position_advice is not None:
        pa = result.position_advice
        lines.append(
            f"- 档位 **{pa.tier}** · 目标占比 {pa.target_pct:.1%} · "
            f"目标金额 {pa.target_amount:,.0f} · 上限裁剪后 {pa.capped_pct:.1%}\n"
        )
        if pa.cap_reasons:
            lines.append("- 约束：" + "；".join(pa.cap_reasons) + "\n")
    else:
        lines.append("_（未给出仓位建议）_\n")

    lines.append("\n## 7. 人工核对清单（§9.2）\n")
    lines.append("- [ ] 阶段与肉眼判断一致（S1–S5）\n")
    lines.append("- [ ] 趋势三档、位置四类与图上一致\n")
    lines.append("- [ ] 关键价位能对上真实支撑/阻力\n")
    lines.append("- [ ] 条件计划的触发价、股数、有效期可照抄进券商 App\n")
    lines.append("- [ ] 风险等级与描述没有夸大/漏项\n")
    lines.append("- [ ] 评分拆解各项与「输入依据」能对上（缺哪个维度、扣在哪里）\n")
    lines.append("- [ ] 分红连续性/股息率与公告一致\n")
    lines.append("- [ ] 估值分位的样本区间可接受（当前为全部可用历史）\n")

    context = {
        "code": code,
        "day": day.isoformat(),
        "calc_version": calc_version,
        "score": score,
        "sector": sector_info,
        "dividend_note": facts.note,
        "valuation_rows": len(rows),
    }
    return "".join(lines), context


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="纸面回看（单只标的逐字段核对）")
    parser.add_argument("--db", required=True, help="助手 SQLite")
    parser.add_argument("--code", required=True)
    parser.add_argument("--day", type=parse_day, required=True)
    parser.add_argument("--index", default="sh000001")
    parser.add_argument("--sector-map", default=DEFAULT_SECTOR_MAP)
    parser.add_argument("--lookback", type=int, default=260)
    parser.add_argument("--capital", type=float, default=500_000.0)
    parser.add_argument("--preset", choices=[p.value for p in Preset], default=Preset.STANDARD.value)
    parser.add_argument("--out", default=None, help="Markdown 输出路径")
    args = parser.parse_args(argv)

    store = AssistantStore(args.db)
    store.create_all()
    text, context = build_report(
        store=store,
        code=args.code,
        day=args.day,
        index_symbol=args.index,
        sector_map=Path(args.sector_map),
        lookback=args.lookback,
        capital=args.capital,
        params=ParamSet.build(Preset(args.preset)),
    )
    print(text)
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        print(f"已写入 {target}")
    else:
        print(json.dumps(context, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
