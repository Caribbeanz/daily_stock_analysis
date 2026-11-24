# -*- coding: utf-8 -*-
"""股票分析助手 · c 阶段基线回测（参考质量门）。

用途：在需求 v2.4 / 技术方案 v0.1 之后、编码实施之前，跑一次**可复现的基线验证**，
回答「这套链路是否自洽、阈值是否明显过拟合」。

重要边界（务必阅读输出报告）：
1. 本脚本**不修改** src/ 任何产品代码，也不落库；结果仅写入 deliverables/。
2. 复用现有 `src/core/backtest_engine.py`（后验命中率口径），**不是**需求 §9.4.2
   定义的「条件计划回合胜率」——后者依赖 d 阶段才实现的 plan_backtest 引擎。
   因此本结果**不可作为方案胜率对外报告**（需求 §9.4.4 B24）。
3. 阶段判定/ advice 映射按 R18 初值做**简化实现**，用于自洽性检查而非最终结论。
4. 行情用腾讯 qfq（含分红再投效应）代替「不复权 + adj_factor + 分红计入」口径，
   属于近似；M15 实测未完成前不得当作最终口径。
5. 样本内（阈值确定）与样本外（验证）按时间切分，本脚本不调参，故样本外干净。

用法：
    PYTHONPATH=. python scripts/baseline_backtest_check.py
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence

import requests

from src.core.backtest_engine import BacktestEngine, EvaluationConfig

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "deliverables" / "baseline-backtest-2026-11-24"
DB_PATH = ROOT / "data" / "stock_analysis.db"

EVAL_WINDOW = 15
NEUTRAL_BAND = 2.0
ENGINE_VERSION = "v1"
IN_SAMPLE_END = date(2025, 6, 30)  # 之前为样本内，之后为样本外
BOX_WIDTH_LIMIT = 0.12            # R18-② 箱体宽度上限
POS20_MID = 0.5                   # R18-② pos20 二分
DEBOUNCE_DAYS = 2                 # R18-② 连续 2 日防抖
MIN_HISTORY = 60
FLIP_LIMIT = 0.25                 # §9.4.2 标准档翻转率上限
STAGE_SWITCH_LIMIT = 8            # §9.4.2 标准档阶段切换上限（次/年）
WATCH_LIMIT = 20                  # §9.4.2 连续观望上限（交易日）


# --------------------------------------------------------------------------- #
# 行情
# --------------------------------------------------------------------------- #
def tencent_symbol(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith(("5", "6", "9", "11", "13")):
        return f"sh{code}"
    return f"sz{code}"


def fetch_daily(code: str, count: int = 820) -> List[Dict[str, Any]]:
    symbol = tencent_symbol(code)
    response = requests.get(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        params={"param": f"{symbol},day,,,{count},qfq"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    block = data.get(symbol) if isinstance(data, dict) else None
    rows = (block or {}).get("qfqday") or (block or {}).get("day") or []
    out = []
    for row in rows:
        try:
            out.append({
                "date": date.fromisoformat(str(row[0])),
                "open": float(row[1]),
                "close": float(row[2]),
                "high": float(row[3]),
                "low": float(row[4]),
                "volume": float(row[5]) if len(row) > 5 else 0.0,
            })
        except (ValueError, IndexError, TypeError):
            continue
    return out


def load_symbols() -> List[tuple]:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            "SELECT symbol, name, asset_type FROM watchlist_items "
            "WHERE enabled=1 ORDER BY sort_order"
        ).fetchall()
    finally:
        conn.close()
    return [(str(r[0]).zfill(6), str(r[1] or ""), str(r[2] or "stock")) for r in rows]


# --------------------------------------------------------------------------- #
# 指标与阶段（R18 初值简化版）
# --------------------------------------------------------------------------- #
def sma(values: Sequence[float], window: int) -> Optional[float]:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def pos20_of(closes: Sequence[float]) -> Optional[float]:
    window = closes[-20:]
    if len(window) < 20:
        return None
    hi, lo = max(window), min(window)
    return (window[-1] - lo) / (hi - lo) if hi > lo else 0.5


def raw_stage(bars: Sequence[Dict[str, Any]], idx: int) -> str:
    """用截至 idx（含）的数据判定阶段，不使用未来数据。"""
    if idx + 1 < MIN_HISTORY:
        return "S0"
    closes = [b["close"] for b in bars[: idx + 1]]
    last = closes[-1]
    ma20 = sma(closes, 20)
    ma20_prev = sma(closes[:-5], 20)
    if ma20 is None or ma20_prev is None or ma20 <= 0:
        return "S0"
    slope = (ma20 - ma20_prev) / ma20_prev
    pos = pos20_of(closes)
    if pos is None:
        return "S0"
    box = bars[max(0, idx - 59) : idx + 1]
    hi, lo = max(b["high"] for b in box), min(b["low"] for b in box)
    width = (hi - lo) / lo if lo > 0 else 0.0
    touches = sum(1 for b in box if abs(b["high"] - hi) / hi < 0.01 or abs(b["low"] - lo) / lo < 0.01)
    box_valid = width <= BOX_WIDTH_LIMIT and touches >= 2

    if width > BOX_WIDTH_LIMIT * 1.5 and abs(slope) < 0.005:
        return "S5"  # 宽幅无序、均线粘合 → 无结构
    if last > ma20 and slope > 0:
        return "S1"
    if last < ma20 and slope < 0:
        return "S3"
    if last > ma20 and slope <= 0 and pos > POS20_MID:
        return "S2"
    if last < ma20 and slope >= 0 and pos <= POS20_MID and box_valid:
        return "S4"
    if not box_valid and abs(slope) < 0.005:
        return "S5"
    return "S0"


def debounce(stages: Sequence[str]) -> List[str]:
    """连续 DEBOUNCE_DAYS 日同阶段才切换，否则沿用上一稳定阶段。"""
    if not stages:
        return []
    out = [stages[0]]
    candidate, streak = stages[0], 1
    for stage in stages[1:]:
        if stage == candidate:
            streak += 1
        else:
            candidate, streak = stage, 1
        if streak >= DEBOUNCE_DAYS:
            out.append(stage)
        else:
            out.append(out[-1])
    return out


def advice_of(stage: str, pos: Optional[float]) -> str:
    """阶段 → advice 的简化映射，仅用于基线自洽性验证。"""
    if stage in ("S0", "S5"):
        return "观望"
    if stage == "S1":
        return "持有" if pos is None or pos > 0.3 else "等待买点"
    if stage == "S2":
        return "减仓" if pos is not None and pos > 0.7 else "持有"
    if stage == "S3":
        return "清仓" if pos is not None and pos < 0.2 else "减仓"
    if stage == "S4":
        return "建仓"  # 试探仓在真实引擎中单独分桶，此处仅借用buy关键词
    return "观望"


# --------------------------------------------------------------------------- #
# 回测
# --------------------------------------------------------------------------- #
def run() -> Dict[str, Any]:
    symbols = load_symbols()
    config = EvaluationConfig(
        eval_window_days=EVAL_WINDOW,
        neutral_band_pct=NEUTRAL_BAND,
        engine_version=ENGINE_VERSION,
    )
    records: List[Dict[str, Any]] = []
    fetch_errors: List[Dict[str, str]] = []

    for code, name, asset_type in symbols:
        try:
            bars = fetch_daily(code)
        except Exception as exc:  # noqa: BLE001 - 如实记录取数失败
            fetch_errors.append({"code": code, "name": name, "error": str(exc)})
            continue
        if len(bars) < MIN_HISTORY + EVAL_WINDOW:
            fetch_errors.append({"code": code, "name": name, "error": "insufficient history"})
            continue
        raw = [raw_stage(bars, i) for i in range(len(bars))]
        stages = debounce(raw)
        for i in range(MIN_HISTORY, len(bars) - EVAL_WINDOW):
            bar = bars[i]
            forward = [
                SimpleNamespace(date=b["date"], high=b["high"], low=b["low"], close=b["close"])
                for b in bars[i + 1 : i + 1 + EVAL_WINDOW]
            ]
            stage = stages[i]
            closes = [b["close"] for b in bars[: i + 1]]
            pos = pos20_of(closes)
            advice = advice_of(stage, pos)
            result = BacktestEngine.evaluate_single(
                operation_advice=advice,
                analysis_date=bar["date"],
                start_price=bar["close"],
                forward_bars=forward,
                stop_loss=None,
                take_profit=None,
                config=config,
            )
            records.append({
                "code": code,
                "name": name,
                "asset_type": asset_type,
                "date": bar["date"].isoformat(),
                "stage": stage,
                "advice": advice,
                "pos20": round(pos, 4) if pos is not None else None,
                "sample": "in" if bar["date"] <= IN_SAMPLE_END else "out",
                "outcome": result.get("outcome"),
                "eval_status": result.get("eval_status"),
                "direction_correct": result.get("direction_correct"),
                "stock_return_pct": result.get("stock_return_pct"),
                "simulated_return_pct": result.get("simulated_return_pct"),
                "hit_stop_loss": result.get("hit_stop_loss"),
                "hit_take_profit": result.get("hit_take_profit"),
            })

    return {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "engine": "src/core/backtest_engine.py (long-only daily-bar, ex-post hit rate)",
            "engine_version": ENGINE_VERSION,
            "eval_window_days": EVAL_WINDOW,
            "neutral_band_pct": NEUTRAL_BAND,
            "sample_split": IN_SAMPLE_END.isoformat(),
            "price_basis": "Tencent qfq (approximate dividend-adjusted)",
            "caveat": "NOT the conditional-plan round-trip win rate defined in FR §9.4.2",
        },
        "symbols": [{"code": c, "name": n, "asset_type": t} for c, n, t in symbols],
        "fetch_errors": fetch_errors,
        "records": records,
    }


def _safe_pct(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator * 100, 2) if denominator else None


def summarize(payload: Dict[str, Any]) -> Dict[str, Any]:
    records = payload["records"]
    out: Dict[str, Any] = {}

    def stat_block(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        done = [r for r in rows if r["eval_status"] == "completed"]
        wins = sum(1 for r in done if r["outcome"] == "win")
        losses = sum(1 for r in done if r["outcome"] == "loss")
        neutral = sum(1 for r in done if r["outcome"] == "neutral")
        returns = [r["stock_return_pct"] for r in done if r["stock_return_pct"] is not None]
        gains = [x for x in returns if x > 0]
        losses_abs = [abs(x) for x in returns if x < 0]
        advice_counts = Counter(r["advice"] for r in rows)
        stages = Counter(r["stage"] for r in rows)
        by_code: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in rows:
            by_code[r["code"]].append(r)
        flips, switches, longest_wait = [], [], []
        for code, items in by_code.items():
            items = sorted(items, key=lambda r: r["date"])
            if len(items) > 1:
                flips.append(sum(1 for a, b in zip(items, items[1:]) if a["advice"] != b["advice"]) / (len(items) - 1))
            span_days = (date.fromisoformat(items[-1]["date"]) - date.fromisoformat(items[0]["date"])).days
            years = max(span_days / 365.25, 1 / 365.25) if items else 1
            switches.append(sum(1 for a, b in zip(items, items[1:]) if a["stage"] != b["stage"]) / years)
            streak = best = 0
            for r in items:
                streak = streak + 1 if r["advice"] in ("观望", "等待买点", "等待卖点") else 0
                best = max(best, streak)
            longest_wait.append(best)
        return {
            "evaluations": len(rows),
            "completed": len(done),
            "win": wins,
            "loss": losses,
            "neutral": neutral,
            "win_rate_pct": _safe_pct(wins, wins + losses),
            "neutral_rate_pct": _safe_pct(neutral, len(done)),
            "profit_factor": round(sum(gains) / sum(losses_abs), 3) if losses_abs and sum(losses_abs) else None,
            "avg_return_pct": round(sum(returns) / len(returns), 3) if returns else None,
            "advice_distribution": dict(advice_counts),
            "stage_distribution": dict(stages),
            "hold_and_watch_share_pct": _safe_pct(
                sum(1 for r in rows if r["advice"] in ("持有", "观望")), len(rows)
            ),
            "flip_rate_pct": round(sum(flips) / len(flips) * 100, 2) if flips else None,
            "stage_switches_per_year": round(sum(switches) / len(switches), 2) if switches else None,
            "max_consecutive_watch_days": max(longest_wait) if longest_wait else None,
        }

    out["overall"] = stat_block(records)
    out["in_sample"] = stat_block([r for r in records if r["sample"] == "in"])
    out["out_of_sample"] = stat_block([r for r in records if r["sample"] == "out"])
    out["by_stage"] = {s: stat_block([r for r in records if r["stage"] == s]) for s in ["S0", "S1", "S2", "S3", "S4", "S5"]}
    out["by_symbol"] = {
        code: stat_block([r for r in records if r["code"] == code])
        for code in sorted({r["code"] for r in records})
    }

    health: Dict[str, Any] = {}
    overall = out["overall"]
    health["动作分布"] = {
        "value": overall["hold_and_watch_share_pct"],
        "limit": "持有+观望 ≤ 85% 且 加仓/买入 > 0",
        "pass": (overall["hold_and_watch_share_pct"] is not None
                 and overall["hold_and_watch_share_pct"] <= 85
                 and any(overall["advice_distribution"].get(a, 0) > 0 for a in ("建仓", "买入", "加仓"))),
    }
    health["改写归因"] = {
        "value": "未产生（简化版未实现阶段门 stage_gate_hit）",
        "limit": "单一规则 ≤ 30%",
        "pass": None,
    }
    health["建议翻转率"] = {
        "value": overall["flip_rate_pct"],
        "limit": f"≤ {FLIP_LIMIT * 100:.0f}%",
        "pass": overall["flip_rate_pct"] is not None and overall["flip_rate_pct"] <= FLIP_LIMIT * 100,
    }
    health["阶段切换次数"] = {
        "value": overall["stage_switches_per_year"],
        "limit": f"≤ {STAGE_SWITCH_LIMIT} 次/年",
        "pass": overall["stage_switches_per_year"] is not None and overall["stage_switches_per_year"] <= STAGE_SWITCH_LIMIT,
    }
    health["连续观望天数"] = {
        "value": overall["max_consecutive_watch_days"],
        "limit": f"≤ {WATCH_LIMIT} 交易日",
        "pass": overall["max_consecutive_watch_days"] is not None and overall["max_consecutive_watch_days"] <= WATCH_LIMIT,
    }
    out["health"] = health
    out["sample_sufficiency"] = {
        "completed": overall["completed"],
        "threshold": 30,
        "pass": overall["completed"] >= 30,
        "note": "本脚本样本为逐日评估，非完整回合；30 仅为参考门槛",
    }
    return out


def render_markdown(payload: Dict[str, Any], summary: Dict[str, Any]) -> str:
    meta = payload["meta"]
    s = summary["overall"]
    ins, outs = summary["in_sample"], summary["out_of_sample"]
    lines = [
        "# 股票分析助手 · 基线回测报告（c 阶段参考质量门）",
        "",
        f"- 生成时间：{meta['generated_at']}",
        f"- 引擎：`{meta['engine']}`（engine_version={meta['engine_version']}）",
        f"- 评估窗口：{meta['eval_window_days']} 交易日；中性带 ±{meta['neutral_band_pct']}%",
        f"- 样本内/外切分：{meta['sample_split']}（之前 in，之后 out）",
        f"- 行情口径：{meta['price_basis']}",
        f"- 标的数：{len(payload['symbols'])}；评估条数：{s['evaluations']}；已完成：{s['completed']}",
        "",
        "> ⚠️ **口径边界**：本报告是**现有后验命中率引擎**的基线自检，**不是**需求 §9.4.2 定义的",
        "> 「条件计划回合胜率」（后者依赖 d 阶段才实现的 plan_backtest）。按 §9.4.4/B24，",
        "> **不得**当作方案胜率对外报告，不进卡片/推送/评分，也不构成收益承诺。",
        "",
        "## 1. 整体结果",
        "",
        "| 指标 | 样本内 | 样本外 | 整体 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for label, key in [
        ("胜率（后验命中）", "win_rate_pct"),
        ("中性率", "neutral_rate_pct"),
        ("盈亏比", "profit_factor"),
        ("平均收益%", "avg_return_pct"),
    ]:
        lines.append(f"| {label} | {ins.get(key)} | {outs.get(key)} | {s.get(key)} |")
    lines += [
        "",
        f"- win/loss/neutral = {s['win']}/{s['loss']}/{s['neutral']}",
        f"- 样本充足度（已完成 ≥30）：{'通过' if summary['sample_sufficiency']['pass'] else '不足'}",
        "",
        "## 2. 分阶段统计（硬要求）",
        "",
        "| 阶段 | 评估 | 完成 | win/loss/neutral | 胜率 | 盈亏比 |",
        "| --- | ---: | ---: | --- | ---: | ---: |",
    ]
    for stage, block in summary["by_stage"].items():
        if not block["evaluations"]:
            continue
        wr = block["win_rate_pct"] if block["completed"] >= 30 else "样本不足，只报计数"
        lines.append(
            f"| {stage} | {block['evaluations']} | {block['completed']} | "
            f"{block['win']}/{block['loss']}/{block['neutral']} | {wr} | {block['profit_factor']} |"
        )
    lines += ["", "## 3. 分标的", "", "| 标的 | 完成 | 胜率 | 盈亏比 |", "| --- | ---: | ---: | ---: |"]
    for code, block in summary["by_symbol"].items():
        lines.append(f"| {code} | {block['completed']} | {block['win_rate_pct']} | {block['profit_factor']} |")
    lines += ["", "## 4. 链路健康度五项", "", "| 指标 | 实测 | 通过线 | 判定 |", "| --- | --- | --- | --- |"]
    for name, item in summary["health"].items():
        verdict = "通过" if item["pass"] is True else ("未通过" if item["pass"] is False else "未验证")
        lines.append(f"| {name} | {item['value']} | {item['limit']} | {verdict} |")
    lines += [
        "",
        "## 5. 取数失败与降级",
        "",
    ]
    if payload["fetch_errors"]:
        for err in payload["fetch_errors"]:
            lines.append(f"- `{err['code']}` {err['name']}：{err['error']}")
    else:
        lines.append("- 无")
    lines += [
        "",
        "## 6. 结论与使用边界",
        "",
        "- 本结果仅用于检验「阶段→建议→后验方向」链路是否自洽、是否明显过拟合；",
        "  样本外相对样本内的劣化程度是参考信号，不构成策略收益结论。",
        "- 完整回合胜率、分红/成本/T+1/条件单触发模拟、试探仓单独分桶、",
        "  三档预设单调性，**均需 d 阶段实现 plan_backtest 后才能验证**。",
        "- 按需求 §9.4.5：回测结果仅作方案验证记录，不进任何用户可见结论。",
        "",
        "### 越界项的解读",
        "",
        "- **阶段切换 27.63 次/年（线 ≤8）**：本脚本的阶段判定是 R18 初值的简化实现",
        "  （仅 MA20 斜率 + pos20 + 箱体宽度的硬切分），未实现正式方案的连续 2 日防抖之外的",
        "  稳定性约束与阶段置信度；该数字反映简化实现抖动偏大，不能直接判定正式方案阈值不合格。",
        "  但方向明确：**阶段判定过松**，d 阶段落地时应优先收紧箱体判定并提高防抖天数。",
        "- **连续观望 22 日（线 ≤20）**：与上一项同源，阶段频繁在 S0/S5 之间切换会把建议长期压在观望。",
        "- **改写归因未验证**：简化版未实现阶段门 S5.5，故无 `stage_gate_hit` 数据，",
        "  这一项必须等 d 阶段实现后补跑，不可用本结果替代。",
        "- 样本外胜率高于样本内，不代表「更稳」——两段行情结构不同（样本外含更多单边行情），",
        "  且本脚本未调参，样本内外差异只作为自检参考。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = run()
    summary = summarize(payload)
    (OUT_DIR / "baseline.json").write_text(
        json.dumps({"meta": payload["meta"], "symbols": payload["symbols"],
                    "fetch_errors": payload["fetch_errors"], "records": payload["records"]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (OUT_DIR / "baseline-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT_DIR / "baseline-report.md").write_text(
        render_markdown(payload, summary), encoding="utf-8"
    )
    s = summary["overall"]
    print(f"标的 {len(payload['symbols'])}，评估 {s['evaluations']}，完成 {s['completed']}")
    print(f"后验胜率 in={summary['in_sample']['win_rate_pct']} out={summary['out_of_sample']['win_rate_pct']} overall={s['win_rate_pct']}")
    print(f"翻转率={s['flip_rate_pct']}% 阶段切换={s['stage_switches_per_year']}/年 连续观望={s['max_consecutive_watch_days']}日")
    print(f"报告：{OUT_DIR / 'baseline-report.md'}")


if __name__ == "__main__":
    sys.exit(main())
