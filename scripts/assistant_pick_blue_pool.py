"""从沪深 300 里挑一篮子**非红利**回放样本（蓝筹 / 白马），只读、不写库。

这是第二个独立验证样本（第一个是 D-30 的红利 ETF 成分 20 只）。做法与
``assistant_pick_dividend_pool.py`` 同构——同一套筛法骨架、同样的行业上限、同样固定种子，
只在**来源**与**门槛**两处不同：

============  =========================================  ===============================
项目          红利样本（D-30）                            蓝筹/白马样本（本脚本）
============  =========================================  ===============================
来源          10 只红利主题 ETF 最新季报持仓并集（139 只）  沪深 300 现成分（300 只）
市值下限      ≥ 100 亿                                    ≥ 500 亿
年化 ROE      ≥ 10%                                       ≥ 12%
PE_TTM 上限   40                                          60
行业上限      2 只/东财一级行业                            2 只/东财一级行业
抽样          分层随机（seed=20261126）                    分层随机（seed=20261126）
============  =========================================  ===============================

为什么门槛这样定：蓝筹/白马的常识口径就是「大市值 + 高 ROE + 盈利在增长」，所以市值与 ROE
都往上提；PE 放宽到 60 是因为成长型白马（新能源、医药、科技）的估值本来高于红利股，
用 40 去筛会系统性剔掉「白马」这一半。**净利同比 > 0 与 ROE ≥ 12% 是「白马」的定义，
不是挑好看的**——同一道门槛对 300 只股票一视同仁，且脚本会输出被剔除的每一条理由。

**排除红利池已用的 20 只**：用户要的是「非红利」的另一类样本，重复标的会让两个样本的差异
被稀释（同一只票在两边的结果不可能不同）。

已知偏差（与红利样本共有，所以两边可比）：成分股取的是**今天**的名单，回放窗口在 2025–2026。
被调进沪深 300 的股票往往正是前一段涨得好的，这会系统性偏向样本——两个样本同样受影响，
所以「两个样本谁更好」可比，但「这套策略的绝对收益」不可外推。

用法::

    python scripts/assistant_pick_blue_pool.py --limit 20 --per-industry 2
    python scripts/assistant_pick_blue_pool.py --limit 20 --out data/cache/blue_pool.json
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_pick_dividend_pool import (  # noqa: E402 - 同目录脚本，复用同一套取数与筛法
    _num,
    fetch_financials,
    fetch_valuation,
    passes,
)

#: 沪深 300 指数代码（中证官方口径）
HS300 = "000300"
#: 成分股名单很慢（一次 ~60s）且一天内不会变，落盘复用
CONS_CACHE = ROOT / "data/cache/hs300_cons.json"
#: 红利样本已用的名单（默认从这里读，保证「非红利」）
DIVIDEND_POOL = ROOT / "data/cache/dividend_pool.json"


def fetch_constituents(*, refresh: bool = False) -> dict[str, str]:
    """``{代码: 名称}``；沪深 300 现成分。落盘缓存，``refresh`` 强制重取。"""
    if CONS_CACHE.exists() and not refresh:
        payload = json.loads(CONS_CACHE.read_text(encoding="utf-8"))
        if payload.get("codes"):
            return dict(payload["codes"])
    import akshare as ak  # noqa: PLC0415 - 只在真实取数时需要

    frame = ak.index_stock_cons(symbol=HS300)
    if frame is None or frame.empty:
        raise SystemExit("沪深 300 成分取不到，接口返回空")
    codes: dict[str, str] = {}
    for _, row in frame.iterrows():
        code = str(row["品种代码"]).zfill(6)
        codes[code] = str(row["品种名称"])
    CONS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    CONS_CACHE.write_text(
        json.dumps({"as_of": date.today().isoformat(), "codes": codes}, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    return codes


def dividend_codes(path: Path = DIVIDEND_POOL) -> set[str]:
    """红利样本已用的代码（读不到就空集，并在调用处提示）。"""
    if not path.exists():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("codes") or [item.get("code") for item in payload.get("picked", [])]
    return {str(code).zfill(6) for code in raw if code}


def stratify(
    candidates: list[dict[str, Any]], *, limit: int, per_industry: int, seed: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按东财一级行业分组 → 组内固定种子打乱 → 跨行业轮转各取 1 只。

    与红利样本同一个抽样器：随机（可复现）但不让单一行业主导结论。
    """
    rng = random.Random(seed)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in candidates:
        groups[str(item.get("board") or "未分类")].append(item)
    boards = list(groups)
    rng.shuffle(boards)
    ordered: list[dict[str, Any]] = []
    for board in boards:
        bucket = groups[board]
        rng.shuffle(bucket)
        ordered.extend(bucket)

    seen: dict[str, int] = defaultdict(int)
    picked: list[dict[str, Any]] = []
    for _round in range(per_industry):
        for item in ordered:
            if len(picked) >= limit:
                break
            board = str(item.get("board") or "未分类")
            if seen[board] >= per_industry or item in picked:
                continue
            seen[board] += 1
            picked.append(item)
        if len(picked) >= limit:
            break

    rest = [item for item in ordered if item not in picked]
    for item in rest:
        board = str(item.get("board") or "未分类")
        item["reason"] = (
            f"超出 {limit} 只上限" if len(picked) >= limit else f"行业 {board} 已满 {per_industry} 只"
        )
    return picked, rest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="沪深 300 里挑非红利（蓝筹/白马）回放样本")
    parser.add_argument("--limit", type=int, default=20, help="最终取多少只")
    parser.add_argument("--per-industry", type=int, default=2, help="同一行业板块上限")
    parser.add_argument("--min-roe", type=float, default=12.0, help="年化加权 ROE 下限(%%)")
    parser.add_argument("--max-pe", type=float, default=60.0, help="PE_TTM 上限")
    parser.add_argument("--max-debt", type=float, default=70.0, help="非金融资产负债率上限(%%)")
    parser.add_argument("--min-cap", type=float, default=5e10, help="总市值下限（元），默认 500 亿")
    parser.add_argument("--seed", type=int, default=20261126, help="分层随机的随机种子")
    parser.add_argument("--refresh-cons", action="store_true", help="强制重取沪深 300 成分")
    parser.add_argument("--keep-dividend", action="store_true",
                        help="不排除红利样本已用的标的（默认排除）")
    parser.add_argument("--out", default=None, help="候选池 JSON 输出路径")
    parser.add_argument("--rejected", default=None, help="被剔除清单 JSON 输出路径")
    args = parser.parse_args(argv)

    print(f"== 1. 拉沪深 300 成分（{HS300}）")
    cons = fetch_constituents(refresh=args.refresh_cons)
    print(f"   {len(cons)} 只" + ("（缓存）" if CONS_CACHE.exists() and not args.refresh_cons else ""))

    exclude = set() if args.keep_dividend else dividend_codes()
    if exclude:
        print(f"   排除红利样本已用的 {len(exclude)} 只")
    codes = sorted(code for code in cons if code not in exclude)
    print(f"   可筛 {len(codes)} 只（已排除 {len(cons) - len(codes)} 只）")

    print(f"== 2. 批量取行业估值（{len(codes)} 只）")
    valuation = fetch_valuation(codes)
    print(f"   {len(valuation)} 只有行业/估值")
    print("== 3. 批量取最新报告期财务")
    financials = fetch_financials(codes)
    print(f"   {len(financials)} 只有财务数据")

    print("== 4. 过筛")
    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for code in codes:
        item: dict[str, Any] = {"code": code, "name": cons[code]}
        item.update(valuation.get(code, {}))
        item.update(financials.get(code, {}))
        if code not in valuation or code not in financials:
            rejected.append({**item, "reason": "缺估值/财务数据"})
            continue
        ok, reason = passes(
            item, min_roe=args.min_roe, max_pe=args.max_pe,
            max_debt=args.max_debt, min_cap=args.min_cap,
        )
        if ok:
            candidates.append(item)
        else:
            rejected.append({**item, "reason": reason})
    print(f"   过筛 {len(candidates)} 只 / 剔除 {len(rejected)} 只")

    print("== 5. 行业上限 + 分层随机")
    picked, rest = stratify(
        candidates, limit=args.limit, per_industry=args.per_industry, seed=args.seed
    )
    rejected.extend(rest)

    print(f"\n== 入选 {len(picked)} 只 ==")
    print(f"{'代码':<8}{'名称':<10}{'行业':<12}{'PE':>8}{'PB':>7}{'市值(亿)':>10}{'ROE%':>8}{'净利YoY%':>10}")
    for item in picked:
        print(
            f"{item['code']:<8}{item['name']:<10}{str(item.get('board'))[:10]:<12}"
            f"{_num(item.get('pe_ttm')) or 0:>8.1f}{_num(item.get('pb')) or 0:>7.2f}"
            f"{(_num(item.get('market_cap')) or 0) / 1e8:>10.0f}"
            f"{_num(item.get('roe')) or 0:>8.1f}{_num(item.get('profit_yoy')) or 0:>10.1f}"
        )
    boards = defaultdict(int)
    for item in picked:
        boards[str(item.get("board"))] += 1
    print(f"\n行业分布（{len(boards)} 个板块）：" + "、".join(f"{k}×{v}" for k, v in boards.items()))

    payload = {
        "as_of": date.today().isoformat(),
        "source": f"index_stock_cons({HS300})",
        "rules": {
            "per_industry": args.per_industry, "min_roe": args.min_roe,
            "max_pe": args.max_pe, "max_debt": args.max_debt, "min_cap": args.min_cap,
            "seed": args.seed, "limit": args.limit, "exclude_dividend": not args.keep_dividend,
        },
        "picked": picked,
        "codes": [item["code"] for item in picked],
    }
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        print(f"\n候选池已写入 {path}")
    if args.rejected:
        path = Path(args.rejected)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(rejected, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
        )
        print(f"剔除清单已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
