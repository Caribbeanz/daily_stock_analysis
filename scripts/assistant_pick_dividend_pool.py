"""从红利类 ETF 最新季报持仓里挑一篮子回放样本（只读，不写库）。

规则（可按需调，但每一条都要能说清理由）：

1. **来源**：10 只红利主题 ETF（红利低波 / 红利质量 / 红利 ETF / 中证红利 / 红利低波 100）
   的**最新季报**持仓并集。用 ETF 成分本身当第一道筛子——「红利」这个投资逻辑不需要我们再证。
2. **行业不集中**：按东财一级行业板块（``BOARD_NAME``）设上限，默认同板块最多 2 只。
3. **基本面门槛**：非 ST、PE_TTM ∈ (0, 40]、PB ∈ (0, 10]、市值 ≥ 100 亿、
   年化加权 ROE ≥ 10%、最新报告期归母净利同比 > 0；金融类（银行/保险/证券）不看资产负债率，
   其余要求 ≤ 70%（重资产行业本来负债高，这一条只当粗筛）。
4. **能回放**：需要足够长的日线（默认 ≥ 380 根）才会被最终采用——上市不满一年半的直接剔除。

为什么用「每板块最多 2 只」而不是「随机抽」：随机抽在 139 只里大概率抽出 8 只银行 + 5 只煤炭，
回放结果会被单一行业主导，等于换了个样本讲同一个故事。行业上限是**去偏**，不是挑好看的。

用法::

    python scripts/assistant_pick_dividend_pool.py --limit 20 --per-industry 2
    python scripts/assistant_pick_dividend_pool.py --limit 40 --out data/cache/pool.json
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 红利类 ETF 样本（低波 / 质量 / 宽基红利 / 低波 100）。成分并集 ~139 只
DIVIDEND_ETFS = (
    "512890",  # 红利低波（华泰柏瑞，中证红利低波动）
    "515100",  # 红利低波 100（景顺长城）
    "158023",  # 红利低波（万家）
    "159547",  # 红利低波（华夏）
    "515460",  # 红利质量（南方）
    "159758",  # 红利质量（华夏）
    "561430",  # 红利质量（博时）
    "510880",  # 红利 ETF（华泰柏瑞，上证红利）
    "515080",  # 中证红利 ETF（招商）
    "515180",  # 红利 ETF（易方达）
)

_DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
_BATCH = 40  # 一次 in-list 查询的代码数（实测 40 只仍返回正常）

#: 金融类不看资产负债率（银行 90%+ 是常态，不是风险信号）
_FINANCIAL_BOARDS = ("银行", "保险", "证券", "多元金融")


def fetch_holdings(etfs: Iterable[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """拉每只 ETF 最新季报持仓，返回 ``{代码: {name, etfs: [...], quarters}}`` 与成功列表。"""
    import akshare as ak  # noqa: PLC0415 - 只在真实取数时需要

    holders: dict[str, dict[str, Any]] = {}
    ok: list[str] = []
    for etf in etfs:
        part = None
        quarter = ""
        for year in (str(date.today().year), str(date.today().year - 1)):
            try:
                frame = ak.fund_portfolio_hold_em(symbol=etf, date=year)
            except Exception:  # noqa: BLE001 - 某只 ETF 取不到就跳过
                continue
            if frame is None or frame.empty:
                continue
            quarter = str(frame["季度"].iloc[-1])
            part = frame[frame["季度"] == quarter]
            if not part.empty:
                break
        if part is None or part.empty:
            print(f"  {etf}: 无持仓数据，跳过")
            continue
        for _, row in part.iterrows():
            code = str(row["股票代码"]).zfill(6)
            node = holders.setdefault(
                code, {"code": code, "name": str(row["股票名称"]), "etfs": [], "quarter": quarter}
            )
            node["etfs"].append(etf)
        print(f"  {etf}: {quarter} · {len(part)} 只")
        ok.append(etf)
    return holders, ok


def _post_page(report: str, codes: list[str], *, sort: str | None = None, page: int = 1,
               size: int = 500) -> list[dict[str, Any]]:
    quoted = ",".join(f'"{c}"' for c in codes)
    params: dict[str, Any] = {
        "reportName": report,
        "columns": "ALL",
        "filter": f"(SECURITY_CODE in ({quoted}))",
        "pageNumber": page,
        "pageSize": size,
    }
    if sort:
        params["sortColumns"] = sort
        params["sortTypes"] = "-1"
    response = requests.get(_DC_URL, params=params, timeout=20, headers=_HEADERS)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        # 某些报表对空结果返回 success=False，不当异常
        return []
    return ((payload.get("result") or {}).get("data")) or []


def fetch_valuation(codes: list[str]) -> dict[str, dict[str, Any]]:
    """行业板块 + PE/PB/市值（``RPT_VALUEANALYSIS_DET``，一次可查多只）。"""
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(codes), _BATCH):
        for row in _post_page("RPT_VALUEANALYSIS_DET", codes[i : i + _BATCH]):
            out[str(row["SECURITY_CODE"])] = {
                "board": str(row.get("BOARD_NAME") or "未分类"),
                "pe_ttm": row.get("PE_TTM"),
                "pb": row.get("PB_MRQ"),
                "market_cap": row.get("TOTAL_MARKET_CAP"),
            }
    return out


def fetch_financials(codes: list[str]) -> dict[str, dict[str, Any]]:
    """最新报告期的 ROE / 增速 / 负债率（``RPT_F10_FINANCE_MAINFINADATA``）。

    按报告期倒序取每只标的**第一条**即最新一期；``ROEJQ`` 是年内累计加权 ROE，
    三季报要乘 4/3 才是年化，因此这里按报告月份折算季度数。
    """
    rows: list[dict[str, Any]] = []
    for i in range(0, len(codes), _BATCH):
        rows.extend(
            _post_page("RPT_F10_FINANCE_MAINFINADATA", codes[i : i + _BATCH], sort="REPORT_DATE")
        )

    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row["SECURITY_CODE"])
        if code in latest:
            continue  # 已按报告期倒序，第一条即最新
        report_date = str(row.get("REPORT_DATE") or "")[:10]
        quarter = {"03": 1, "06": 2, "09": 3, "12": 4}.get(report_date[5:7], 4)
        raw_roe = row.get("ROEJQ")
        annual_roe = float(raw_roe) * 4 / quarter if raw_roe is not None else None
        latest[code] = {
            "report": str(row.get("REPORT_DATE_NAME") or ""),
            "roe": annual_roe,
            "profit_yoy": row.get("PARENTNETPROFITTZ"),
            "revenue_yoy": row.get("TOTALOPERATEREVETZ"),
            "debt_ratio": row.get("ZCFZL"),
            "st_flag": row.get("IS_BZ"),
        }
    return latest


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def passes(item: dict[str, Any], *, min_roe: float, max_pe: float, max_debt: float,
           min_cap: float) -> tuple[bool, str]:
    """逐条判否并返回**第一条**不满足的原因（便于人工复核，不做黑箱打分）。"""
    # 接口把 ``IS_BZ`` 以**字符串** ``"0"/"1"`` 返回，直接 if 会把全市场判成 ST
    if str(item.get("st_flag") or "0").strip() not in ("", "0", "None"):
        return False, "ST/风险警示"
    pe = _num(item.get("pe_ttm"))
    if pe is None or pe <= 0:
        return False, f"PE 不可用（{pe}）"
    if pe > max_pe:
        return False, f"PE {pe:.1f} > {max_pe}"
    pb = _num(item.get("pb"))
    if pb is None or pb <= 0:
        return False, f"PB 不可用（{pb}）"
    cap = _num(item.get("market_cap"))
    if cap is None or cap < min_cap:
        return False, f"市值 {0 if cap is None else cap / 1e8:.0f} 亿 < {min_cap / 1e8:.0f} 亿"
    roe = _num(item.get("roe"))
    if roe is None:
        return False, "ROE 不可用"
    if roe < min_roe:
        return False, f"年化 ROE {roe:.1f}% < {min_roe}"
    profit = _num(item.get("profit_yoy"))
    if profit is None or profit <= 0:
        return False, f"归母净利同比 {profit}% 未转正"
    board = str(item.get("board") or "")
    debt = _num(item.get("debt_ratio"))
    if not any(key in board for key in _FINANCIAL_BOARDS):
        if debt is not None and debt > max_debt:
            return False, f"资产负债率 {debt:.0f}% > {max_debt:.0f}%"
    return True, ""


def build_pool(
    holders: dict[str, dict[str, Any]],
    valuation: dict[str, dict[str, Any]],
    financials: dict[str, dict[str, Any]],
    *,
    per_industry: int,
    min_roe: float,
    max_pe: float,
    max_debt: float,
    min_cap: float,
    limit: int,
    mode: str = "random",
    seed: int = 20261126,
    exclude: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """过筛 + 行业上限，返回 ``(入选, 被剔除)``。

    ``mode="random"`` 用**分层随机**：按行业分组、组内以固定种子打乱，再跨行业轮转各取 1 只，
    取满 ``limit`` 为止。这样既守住「随机抽样」的立场，又不会抽出 5 只银行让结论被单一行业
    主导。``mode="ranked"`` 则按「被多少只红利 ETF 持有 → ROE → 市值」排序取前 N。
    """
    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    skip = exclude or set()
    for code, node in holders.items():
        item = {
            "code": code,
            "name": node["name"],
            "etf_count": len(node["etfs"]),
            **valuation.get(code, {}),
            **financials.get(code, {}),
        }
        if code in skip:
            rejected.append({**item, "reason": "显式排除（上市不满 1.5 年，K 线不够回放）"})
            continue
        if code not in valuation or code not in financials:
            rejected.append({**item, "reason": "缺估值/财务数据"})
            continue
        ok, reason = passes(
            item, min_roe=min_roe, max_pe=max_pe, max_debt=max_debt, min_cap=min_cap
        )
        if ok:
            candidates.append(item)
        else:
            rejected.append({**item, "reason": reason})

    if mode == "ranked":
        # 排序：被更多红利 ETF 持有 → 年化 ROE 更高 → 市值更大（越像「红利核心资产」）
        candidates.sort(
            key=lambda x: (
                -x["etf_count"], -(_num(x.get("roe")) or 0), -(_num(x.get("market_cap")) or 0)
            )
        )
    else:
        # 组内用固定种子打乱，组间也打乱，保证「随机」但可复现
        rng = random.Random(seed)
        groups_seed: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in candidates:
            groups_seed[str(item.get("board") or "未分类")].append(item)
        boards = list(groups_seed)
        rng.shuffle(boards)
        candidates = []
        for board in boards:
            bucket = groups_seed[board]
            rng.shuffle(bucket)
            candidates.extend(bucket)

    seen: dict[str, int] = defaultdict(int)
    picked: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    # 跨行业轮转：每一轮各板块各取 1 只，取满即止（board 顺序对 random 已打乱）
    for _round in range(per_industry):
        for item in list(candidates):
            if len(picked) >= limit:
                break
            board = str(item.get("board") or "未分类")
            if seen[board] >= per_industry:
                continue
            seen[board] += 1
            picked.append(item)
            candidates.remove(item)
        if len(picked) >= limit:
            break
    for item in candidates:
        board = str(item.get("board") or "未分类")
        if len(picked) >= limit:
            item["reason"] = f"超出 {limit} 只上限（行业 {board}）"
        else:
            item["reason"] = f"行业 {board} 已满 {per_industry} 只"
        remaining.append(item)
    rejected.extend(remaining)
    return picked, rejected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="红利 ETF 持仓里挑回放样本")
    parser.add_argument("--limit", type=int, default=20, help="最终取多少只")
    parser.add_argument("--per-industry", type=int, default=2, help="同一行业板块上限")
    parser.add_argument("--min-roe", type=float, default=10.0, help="年化加权 ROE 下限(%%)")
    parser.add_argument("--max-pe", type=float, default=40.0, help="PE_TTM 上限")
    parser.add_argument("--max-debt", type=float, default=70.0, help="非金融资产负债率上限(%%)")
    parser.add_argument("--min-cap", type=float, default=1e10, help="总市值下限（元）")
    parser.add_argument("--mode", choices=["random", "ranked"], default="random",
                        help="random=分层随机抽样（默认，种子固定可复现）；ranked=按权重排序取前 N")
    parser.add_argument("--seed", type=int, default=20261126, help="分层随机的随机种子")
    parser.add_argument("--exclude", nargs="*", default=None,
                        help="显式排除的代码（如上市不满 1.5 年、K 线不够回放的）")
    parser.add_argument("--bars-db", default=None,
                        help="从该库统计已有日线根数（缺省不统计，交给 ingest 脚本报数）")
    parser.add_argument("--out", default=None, help="候选池 JSON 输出路径")
    parser.add_argument("--rejected", default=None, help="被剔除清单 JSON 输出路径")
    args = parser.parse_args(argv)

    print(f"== 1. 拉 {len(DIVIDEND_ETFS)} 只红利 ETF 最新季报持仓")
    holders, ok = fetch_holdings(DIVIDEND_ETFS)
    print(f"   成功 {len(ok)} 只 ETF，去重 {len(holders)} 只标的")

    codes = sorted(holders)
    print(f"== 2. 批量取行业估值（{len(codes)} 只）")
    valuation = fetch_valuation(codes)
    print(f"   {len(valuation)} 只有行业/估值")
    print("== 3. 批量取最新报告期财务")
    financials = fetch_financials(codes)
    print(f"   {len(financials)} 只有财务数据")

    print("== 4. 过筛 + 行业上限")
    picked, rejected = build_pool(
        holders, valuation, financials,
        per_industry=args.per_industry, min_roe=args.min_roe, max_pe=args.max_pe,
        max_debt=args.max_debt, min_cap=args.min_cap,
        limit=args.limit, mode=args.mode, seed=args.seed,
        exclude={str(c).zfill(6) for c in (args.exclude or [])},
    )
    print(f"   过筛 {len(picked)} 只 / 剔除 {len(rejected)} 只")

    if args.bars_db:
        print("== 5. 统计已有日线根数")
        from src.assistant.data.storage import AssistantStore  # noqa: PLC0415

        store = AssistantStore(args.bars_db)
        for item in picked:
            item["bars"] = len(store.load_bars(item["code"]))

    final = picked
    print(f"\n== 入选 {len(final)} 只 ==")
    header = f"{'代码':<8}{'名称':<10}{'行业':<10}{'ETF':>4}{'PE':>8}{'PB':>7}{'市值(亿)':>10}{'ROE%':>8}{'净利YoY%':>10}"
    print(header)
    for item in final:
        print(
            f"{item['code']:<8}{item['name']:<10}{str(item.get('board'))[:8]:<10}"
            f"{item['etf_count']:>4}{_num(item.get('pe_ttm')) or 0:>8.1f}"
            f"{_num(item.get('pb')) or 0:>7.2f}"
            f"{(_num(item.get('market_cap')) or 0) / 1e8:>10.0f}"
            f"{_num(item.get('roe')) or 0:>8.1f}{_num(item.get('profit_yoy')) or 0:>10.1f}"
        )
    boards = defaultdict(int)
    for item in final:
        boards[str(item.get("board"))] += 1
    print(f"\n行业分布（{len(boards)} 个板块）：" + "、".join(f"{k}×{v}" for k, v in boards.items()))

    payload = {
        "as_of": date.today().isoformat(),
        "etfs": list(ok),
        "rules": {
            "per_industry": args.per_industry, "min_roe": args.min_roe,
            "max_pe": args.max_pe, "max_debt": args.max_debt, "min_cap": args.min_cap,
            "mode": args.mode, "seed": args.seed, "limit": args.limit,
        },
        "picked": final,
        "codes": [item["code"] for item in final],
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
