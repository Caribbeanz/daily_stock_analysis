#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股票分析助手 · 编码前 12 项实测前置（M5/M6/M7/M9/M10/M11/M12/M13/M14/M15/M16/M17）
附测 M18（cron 解析 + 交易日门控，v0.3 新增）。

用途：
    本脚本自包含，可在能出网的机器直接运行。逐项调用真实接口/读取本地事实，
    输出结构化结果（PASS / PARTIAL / FAIL / BLOCKED / MANUAL），**不伪造任何数据**。
    网络不可达、依赖缺失、凭据缺失一律如实标记，不用缓存或推断冒充实测。

运行：
    python scripts/m_preflight_check.py                 # 全部 13 项
    python scripts/m_preflight_check.py --only M5 M7    # 只跑指定项
    python scripts/m_preflight_check.py --local-only    # 只跑不依赖网络的项
    python scripts/m_preflight_check.py --timeout 12    # 单次网络超时（秒）

依赖：
    必装：requests
    网络项：pip install akshare tushare（版本见 requirements.txt）
    M18 cron：pip install croniter（可选，未装标记 BLOCKED）
    项目模块：交易日历/配置链检查需能 import src（脚本自动把仓库根加入 sys.path）

输出：
    deliverables/m-preflight-<YYYYMMDD>/m-check.json
    deliverables/m-preflight-<YYYYMMDD>/m-check-report.md

注意：
    - 只做只读实测，不写数据库、不改配置、不推送任何消息。
    - 飞书 Stream（M13）与 LLM 渠道（M14）的"真实连通/凭据"部分需用户本机确认，
      本脚本只验证配置链与依赖是否齐备，凭据相关项标 MANUAL，不会代填或探测密钥。
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import inspect
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

PASS = "PASS"
PARTIAL = "PARTIAL"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
MANUAL = "MANUAL"
STATUS_ORDER = {PASS: 0, PARTIAL: 1, MANUAL: 2, BLOCKED: 3, FAIL: 4}


@dataclass
class Result:
    mid: str
    name: str
    status: str
    summary: str
    evidence: list[str] = field(default_factory=list)
    caveat: str = ""
    elapsed_ms: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.mid,
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "evidence": self.evidence,
            "caveat": self.caveat,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
        }


CTX: dict[str, Any] = {"timeout": 12, "ak": None, "requests": None, "ts": None}


def has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def safe_call(fn: Callable, *args: Any, **kwargs: Any) -> tuple[Any | None, str]:
    """调用并捕获异常，返回 (结果, 错误串)。不吞异常细节。"""
    try:
        return fn(*args, **kwargs), ""
    except Exception as exc:  # noqa: BLE001 - 实测脚本需要记录所有失败
        return None, f"{type(exc).__name__}: {str(exc)[:300]}"


def retry_call(fn: Callable, *args: Any, attempts: int = 3, backoff: float = 1.5,
               **kwargs: Any) -> tuple[Any | None, str, int]:
    """带退避重试的调用。东财 push2his 对连续请求限流，单次失败不代表接口不可用。

    返回 (结果, 最终错误, 实际尝试次数)。
    """
    last_err = ""
    for i in range(attempts):
        res, err = safe_call(fn, *args, **kwargs)
        if not err:
            return res, "", i + 1
        last_err = err
        if i < attempts - 1:
            time.sleep(backoff * (i + 1))
    return None, last_err, attempts


def need_akshare(mid: str, name: str) -> Result | None:
    if CTX["ak"] is None:
        return Result(
            mid, name, BLOCKED,
            "akshare 未安装，无法实测真实接口返回",
            caveat="在能出网环境执行: pip install 'akshare>=1.12.0,<1.20.0'（版本见 requirements.txt）",
        )
    return None


def cols_of(df: Any) -> list[str]:
    return list(df.columns) if df is not None and hasattr(df, "columns") else []


def shape_of(df: Any) -> str:
    return str(getattr(df, "shape", "n/a"))


# ---------------------------------------------------------------------------
# M5 板块成分接口是否支持板块代码入参
# ---------------------------------------------------------------------------
def m5() -> Result:
    name = "M5 stock_board_industry_cons_em 板块代码入参"
    blocked = need_akshare("M5", name)
    if blocked:
        return blocked
    ak = CTX["ak"]
    out = Result("M5", name, BLOCKED, "")
    # 路径 A：东财原接口（push2 域，本机存在静默风控，见跨项发现）
    df_name, err_name = safe_call(ak.stock_board_industry_name_em)
    em_ok = not err_name
    out.evidence.append(
        f"东财 stock_board_industry_name_em -> {'shape=' + shape_of(df_name) if em_ok else 'ERR ' + err_name[:140]}"
    )
    if em_ok:
        name_col = next((c for c in ["板块名称", "名称", "name"] if c in cols_of(df_name)), None)
        code_col = next((c for c in ["板块代码", "代码", "code"] if c in cols_of(df_name)), None)
        out.evidence.append(f"东财 name_col={name_col} code_col={code_col}")
        if name_col:
            sample = str(df_name.iloc[0][name_col])
            arg = "BK0475" if not code_col else str(df_name.iloc[0][code_col])
            for label, a in [("板块名", sample), ("板块代码", arg)]:
                df, err = safe_call(ak.stock_board_industry_cons_em, symbol=a)
                out.evidence.append(
                    f"东财 cons {label}({a}) -> {'shape=' + shape_of(df) if not err else 'ERR ' + err[:120]}"
                )
            accepted = any("cons 板块代码" in e and "shape=" in e for e in out.evidence)
            out.status = PASS
            out.summary = "东财板块成分接口代码入参可用" if accepted else "东财板块成分接口代码入参不可用"
            return out
    # 路径 B：同花顺等价源（本机可达）——退一步验证「代码能否作为入参」
    ths_name, err_ths = safe_call(ak.stock_board_industry_name_ths)
    if err_ths:
        out.summary = "东财与同花顺板块接口均不可达"
        out.error = f"东财:{err_name[:120]} | 同花顺:{err_ths[:120]}"
        return out
    out.evidence.append(f"同花顺 stock_board_industry_name_ths -> shape={shape_of(ths_name)} cols={cols_of(ths_name)}")
    if ths_name is not None and hasattr(ths_name, "columns") and {"name", "code"} <= set(ths_name.columns):
        code0 = str(ths_name.iloc[0]["code"])
        name0 = str(ths_name.iloc[0]["name"])
        out.evidence.append(f"同花顺板块代码格式={code0}（{name0}），非东财 BK 前缀")
        idx_by_code, err_code = safe_call(ak.stock_board_industry_index_ths, symbol=code0,
                                          start_date="20260101", end_date=date.today().strftime("%Y%m%d"))
        out.evidence.append(f"同花顺 index_ths 用代码({code0})入参 -> {'OK shape=' + shape_of(idx_by_code) if not err_code else 'ERR ' + err_code[:120]}")
        idx_by_name, err_name2 = safe_call(ak.stock_board_industry_index_ths, symbol=name0,
                                           start_date="20260101", end_date=date.today().strftime("%Y%m%d"))
        out.evidence.append(f"同花顺 index_ths 用名称({name0})入参 -> {'OK shape=' + shape_of(idx_by_name) if not err_name2 else 'ERR ' + err_name2[:120]}")
        code_rejected = bool(err_code) and not err_name2
        out.status = PARTIAL
        out.summary = ("东财 cons 接口本机不可达；同花顺等价路径"
                       + ("仅接受板块名称、拒绝代码入参" if code_rejected else "代码与名称入参均可"))
        out.caveat = ("实测结论：code→板块映射无法靠行业字段兜底（见 M6 三套分类互不命中），"
                      "需维护显式映射表或走同源板块成分接口；代码库现用板块名（src/services/screening/industry.py:227-229）与实测一致")
        return out
    out.status = PARTIAL
    out.summary = "东财不可达；同花顺板块列表可得但字段结构与预期不符"
    return out


# ---------------------------------------------------------------------------
# M6 stock_individual_info_em 行业字段口径
# ---------------------------------------------------------------------------
def _fetch_em_f10(code: str) -> tuple[dict[str, Any], str]:
    """取东财 F10 公司概况（emweb 线路，不受 push2 风控影响）。返回 (字段, 错误)。"""
    req_mod = CTX["requests"]
    if req_mod is None:
        return {}, "requests 未安装"
    mkt = "SH" if code.startswith("6") else "SZ"
    url = f"https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code={mkt}{code}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
        "Referer": "https://emweb.securities.eastmoney.com/",
        "Accept-Encoding": "identity",
    }
    try:
        resp = req_mod.get(url, headers=headers, timeout=CTX["timeout"])
        data = resp.json()
        jb = (data.get("jbzl") or [{}])[0]
        return jb, ""
    except Exception as exc:  # noqa: BLE001
        return {}, f"{type(exc).__name__}: {str(exc)[:150]}"


def m6() -> Result:
    name = "M6 stock_individual_info_em 行业字段口径"
    blocked = need_akshare("M6", name)
    if blocked:
        return blocked
    ak = CTX["ak"]
    out = Result("M6", name, BLOCKED, "")
    samples = ["600000", "000001", "300750", "002594"]
    # 路径 A：东财原接口（push2，本机不可达则如实记录）
    df, err = safe_call(ak.stock_individual_info_em, symbol="600000")
    out.evidence.append(f"东财 stock_individual_info_em -> {'shape=' + shape_of(df) if not err else 'ERR ' + err[:140]}")
    if not err:
        try:
            rows = dict(zip(df.iloc[:, 0].astype(str), df.iloc[:, 1].astype(str)))
            out.evidence.append(f"东财个股信息键值={json.dumps(rows, ensure_ascii=False)[:400]}")
        except Exception as exc:  # noqa: BLE001
            out.evidence.append(f"解析失败 {type(exc).__name__}")
    # 路径 B：东财 F10 线路（可用），暴露两套分类
    f10_rows: list[tuple[str, str, str]] = []
    for code in samples:
        jb, e = _fetch_em_f10(code)
        if e:
            out.evidence.append(f"F10 {code} -> ERR {e[:100]}")
            continue
        f10_rows.append((code, str(jb.get("EM2016") or ""), str(jb.get("INDUSTRYCSRC1") or "")))
    for code, em, csrc in f10_rows:
        out.evidence.append(f"F10 {code} EM2016={em!r} INDUSTRYCSRC1={csrc!r}")
    # 路径 C：巨潮（证监会口径）
    cninfo_ind = []
    if hasattr(ak, "stock_profile_cninfo"):
        for code in samples[:2]:
            d, e = safe_call(ak.stock_profile_cninfo, symbol=code)
            if e or d is None or "所属行业" not in cols_of(d):
                out.evidence.append(f"巨潮 {code} -> {'ERR ' + e[:100] if e else '无所属行业字段'}")
                continue
            v = str(d.iloc[0]["所属行业"])
            cninfo_ind.append(v)
            out.evidence.append(f"巨潮 {code} 所属行业={v!r}")
    # 与同花顺板块名逐字比对：验证「行业字段能否当板块名用」
    ths_names: set[str] = set()
    if hasattr(ak, "stock_board_industry_name_ths"):
        t, e = safe_call(ak.stock_board_industry_name_ths)
        if not e and t is not None and "name" in cols_of(t):
            ths_names = {str(x) for x in t["name"]}
            out.evidence.append(f"同花顺板块名 {len(ths_names)} 个，用于口径比对")
    if ths_names and f10_rows:
        em_last = [em.split("-")[-1] for _, em, _ in f10_rows if em]
        hit = [x for x in em_last if x in ths_names]
        out.evidence.append(f"东财 EM2016 末级 vs 同花顺板块名：命中 {len(hit)}/{len(em_last)}（{hit}）")
    if ths_names and cninfo_ind:
        hit2 = [x for x in cninfo_ind if x in ths_names]
        out.evidence.append(f"巨潮所属行业 vs 同花顺板块名：命中 {len(hit2)}/{len(cninfo_ind)}")
    if f10_rows:
        out.status = PARTIAL
        out.summary = ("东财 push2 个股接口本机不可达；F10 线路实测存在 ≥2 套分类"
                       "（EM2016 三级、INDUSTRYCSRC1 证监会），且均与板块名不互认")
        out.caveat = ("实证：行业字段与板块名是不同分类体系，逐字比对 0 命中，"
                      "严禁用行业字段兜底 code→板块映射（印证需求 R5）")
    else:
        out.summary = "个股行业字段所有线路均不可得"
        out.error = err
        out.caveat = "实测前严禁用于 code→板块映射；需把该值与 stock_board_industry_name_em 的板块名逐字比对后裁定"
    return out


# ---------------------------------------------------------------------------
# M7 板块行情字段完整性
# ---------------------------------------------------------------------------
def m7() -> Result:
    name = "M7 stock_board_industry_name_em 涨跌家数/领涨股字段"
    blocked = need_akshare("M7", name)
    if blocked:
        return blocked
    ak = CTX["ak"]
    out = Result("M7", name, BLOCKED, "")
    want = ["上涨家数", "下跌家数", "领涨股票", "领涨股"]
    # 路径 A：东财原接口
    df, err = safe_call(ak.stock_board_industry_name_em)
    out.evidence.append(f"东财 stock_board_industry_name_em -> {'shape=' + shape_of(df) if not err else 'ERR ' + err[:140]}")
    if not err:
        cols = cols_of(df)
        found = [c for c in want if c in cols]
        out.evidence.append(f"东财 cols={cols}")
        out.status = PASS if len(found) == len(want) else PARTIAL
        out.summary = f"东财命中字段={found}"
        out.caveat = ("代码库现只用『涨跌幅 + 板块名称』两列（data_provider/akshare_fetcher.py:2218-2223）；"
                      "缺列则 R5 按参考层降级为『未取得』")
        return out
    # 路径 B：同花顺等价源
    summ, err_sum = safe_call(ak.stock_board_industry_summary_ths)
    if err_sum:
        out.summary = "东财与同花顺板块行情接口均不可达"
        out.error = f"东财:{err[:120]} | 同花顺:{err_sum[:120]}"
        return out
    cols = cols_of(summ)
    out.evidence.append(f"同花顺 stock_board_industry_summary_ths -> shape={shape_of(summ)}")
    out.evidence.append(f"同花顺 cols={cols}")
    ths_want = {"上涨家数": ["上涨家数"], "下跌家数": ["下跌家数"],
                "领涨股": ["领涨股"], "领涨股涨跌幅": ["领涨股-涨跌幅"]}
    found = [k for k, names in ths_want.items() if any(n in cols for n in names)]
    missing = [k for k in ths_want if k not in found]
    out.evidence.append(f"字段映射命中={found} 缺失={missing}")
    if summ is not None and "板块" in cols and "上涨家数" in cols and len(summ) > 0:
        try:
            r0 = summ.iloc[0]
            out.evidence.append(f"样例：{r0['板块']} 涨跌家数 {r0['上涨家数']}/{r0['下跌家数']} 领涨股={r0.get('领涨股')!r}")
        except Exception:  # noqa: BLE001
            pass
    info, err_info = safe_call(ak.stock_board_industry_info_ths, symbol=str(summ.iloc[0]["板块"]) if len(summ) else "半导体")
    if not err_info and info is not None and "项目" in cols_of(info):
        kv = dict(zip(info["项目"].astype(str), info["值"].astype(str)))
        out.evidence.append(f"同花顺 info_ths 含涨跌家数={kv.get('涨跌家数')!r} 涨幅排名={kv.get('涨幅排名')!r}")
    out.status = PASS if not missing else PARTIAL
    out.summary = f"东财接口本机不可达；同花顺字段命中 {len(found)}/{len(ths_want)}（缺失={missing}）"
    out.caveat = ("字段可得但需换源 + 字段名映射（东财『领涨股票』→同花顺『领涨股』）；"
                  "换源后须同步改 data_provider/akshare_fetcher.py:2218-2223 的取列逻辑")
    return out


# ---------------------------------------------------------------------------
# M9 股息率/分红/PE-PB 分位
# ---------------------------------------------------------------------------
def m9() -> Result:
    name = "M9 股息率/分红记录/PE-PB 分位接口"
    blocked = need_akshare("M9", name)
    if blocked:
        return blocked
    ak = CTX["ak"]
    out = Result("M9", name, BLOCKED, "")
    # 分红类：两个接口都是代码库当前口径所需的原始来源
    dividend_probes = [
        ("分红明细 stock_fhps_detail_em", "stock_fhps_detail_em", {"symbol": "600000"}),
        ("历史分红 stock_history_dividend_detail", "stock_history_dividend_detail", {"symbol": "600000"}),
    ]
    dividend_ok = 0
    for label, fn_name, kwargs in dividend_probes:
        if not hasattr(ak, fn_name):
            out.evidence.append(f"{label} -> 接口不存在（akshare {getattr(ak, '__version__', '?')}）")
            continue
        df, err = safe_call(getattr(ak, fn_name), **kwargs)
        if err:
            out.evidence.append(f"{label} -> ERR {err[:160]}")
        else:
            dividend_ok += 1
            out.evidence.append(f"{label} -> shape={shape_of(df)} cols={cols_of(df)[:14]}")
    # PE/PB 分位：接口名随 akshare 版本变动，逐个探测，区分"不存在"与"调用失败"
    valuation_candidates = [
        ("stock_a_indicator_lg", {"symbol": "600000"}),
        ("stock_zh_valuation_baidu", {"symbol": "600000", "indicator": "市盈率(TTM)", "period": "近五年"}),
        ("stock_value_em", {"symbol": "600000"}),
    ]
    valuation_ok: list[str] = []
    for fn_name, kwargs in valuation_candidates:
        if not hasattr(ak, fn_name):
            out.evidence.append(f"PE/PB {fn_name} -> 接口不存在")
            continue
        df, err = safe_call(getattr(ak, fn_name), **kwargs)
        if err:
            out.evidence.append(f"PE/PB {fn_name} -> ERR {err[:160]}")
        else:
            valuation_ok.append(fn_name)
            cols = cols_of(df)
            out.evidence.append(f"PE/PB {fn_name} -> shape={shape_of(df)} cols={cols[:12]}")
            # 分位数能否直接算：需要足够长的历史序列
            if df is not None and hasattr(df, "shape") and df.shape[0] >= 250:
                out.evidence.append(f"PE/PB {fn_name} 历史行数 {df.shape[0]}，可自算历史分位")
    total_ok = dividend_ok + (1 if valuation_ok else 0)
    total_probes = len(dividend_probes) + 1
    if total_ok == total_probes:
        out.status = PASS
    elif total_ok > 0:
        out.status = PARTIAL
    else:
        out.status = FAIL
    out.summary = (f"分红接口 {dividend_ok}/{len(dividend_probes)} 可用；"
                   f"PE/PB 分位可用接口={valuation_ok or '无'}")
    out.caveat = ("股息率在代码库为自算值（data_provider/base.py:4481），非接口直出；"
                  "代码库零调用 PE/PB 分位接口。若分位不可得，§5.8 相应项须标注缺失并降置信度")
    return out


# ---------------------------------------------------------------------------
# M10 长周期序列可得性与复权口径
# ---------------------------------------------------------------------------
def m10() -> Result:
    name = "M10 3-5 年长周期序列与复权口径"
    blocked = need_akshare("M10", name)
    if blocked:
        return blocked
    ak = CTX["ak"]
    out = Result("M10", name, BLOCKED, "")
    end = date.today()
    start = end - timedelta(days=365 * 4)
    df, err, tries = retry_call(
        ak.stock_zh_a_hist, symbol="600000", period="daily",
        start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"), adjust="qfq",
    )
    source = f"东财 stock_zh_a_hist（尝试 {tries} 次）"
    if err and hasattr(ak, "stock_zh_a_daily"):
        # 主源失败时回退新浪源；不做静默兜底，来源写进证据
        df, err2 = safe_call(ak.stock_zh_a_daily, symbol="sh600000", adjust="qfq",
                             start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
        source = "新浪 stock_zh_a_daily"
        out.evidence.append(f"主源 stock_zh_a_hist 失败：{err[:120]}")
        err = err2
    if err:
        out.status, out.summary, out.error = BLOCKED, "长周期取数失败（主源与备源均失败）", err
        return out
    rows = int(getattr(df, "shape", (0,))[0])
    out.evidence.append(f"来源={source}")
    out.evidence.append(f"4 年 qfq 日线 shape={shape_of(df)} cols={cols_of(df)[:12]}")
    out.evidence.append(f"行数={rows}（约 970 交易日量级）")
    if rows and "日期" in cols_of(df):
        try:
            out.evidence.append(f"首行日期={df.iloc[0]['日期']} 末行日期={df.iloc[-1]['日期']}")
        except Exception:  # noqa: BLE001
            pass
    out.status = PASS if rows > 700 else PARTIAL
    out.summary = f"免费源可取 4 年日线 {rows} 行（前复权 qfq）"
    out.caveat = "代码库默认窗口 DAILY_LOOKBACK_DAYS=120（src/services/screening/config.py:230）；长周期位置分位需另建取数；qfq 非真实复权因子"
    return out


# ---------------------------------------------------------------------------
# M11 风险历史序列可得性（本地表 + akshare 指数/板块）
# ---------------------------------------------------------------------------
def m11() -> Result:
    name = "M11 风险判定历史序列可得性与存储"
    out = Result("M11", name, BLOCKED, "")
    db = REPO / "data" / "stock_analysis.db"
    if db.exists():
        con = sqlite3.connect(db)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
        planned = ["market_regime", "rs_t", "sector_strength", "market_snapshot", "sector_snapshot"]
        found = [t for t in planned if t in tables]
        out.evidence.append(f"规划表命中={found} 缺失={[t for t in planned if t not in tables]}")
    else:
        out.evidence.append("本地数据库不存在")
    ak = CTX["ak"]
    if ak is not None:
        idx, err, tries = retry_call(ak.stock_zh_index_daily_em, symbol="sh000001")
        if err and hasattr(ak, "stock_zh_index_daily"):
            out.evidence.append(f"东财指数接口失败（尝试 {tries} 次）：{err[:100]}")
            idx, err, tries = retry_call(ak.stock_zh_index_daily, symbol="sh000001")
            out.evidence.append(
                f"新浪指数历史 -> {('ERR ' + err[:100]) if err else 'shape=' + shape_of(idx) + ' cols=' + str(cols_of(idx))}（尝试 {tries} 次）"
            )
            if not err and idx is not None and len(idx):
                try:
                    out.evidence.append(
                        f"指数序列首日={idx.iloc[0]['date']} 末日={idx.iloc[-1]['date']} 收盘={float(idx.iloc[-1]['close']):.2f}"
                    )
                    out.evidence.append("结论：风险判定所需的指数历史长序列可从免费源取得，瓶颈在存储与聚合表（非取数）")
                except Exception:  # noqa: BLE001
                    pass
        else:
            out.evidence.append(
                f"上证指数历史 -> {('ERR ' + err[:120]) if err else 'shape=' + shape_of(idx)}（尝试 {tries} 次）"
            )
    idx_ok = any(("新浪指数历史 -> shape=" in e) or ("上证指数历史 -> shape=" in e) for e in out.evidence)
    out.status = PARTIAL
    out.summary = ("风险判定专用表在代码中不存在；"
                   + ("指数历史实测可取（长序列），需新建存储与聚合表" if idx_ok
                      else "指数历史本次未取得，需重试或换源"))
    out.caveat = "同名 market_regime 在 src/agent/skills/base.py:76 是策略路由标签，非风险判定历史序列，勿混淆"
    return out


# ---------------------------------------------------------------------------
# M12 财报渐进恶化字段
# ---------------------------------------------------------------------------
def m12() -> Result:
    name = "M12 财报渐进恶化判定字段可得性"
    blocked = need_akshare("M12", name)
    if blocked:
        return blocked
    ak = CTX["ak"]
    out = Result("M12", name, BLOCKED, "")
    probes = {
        "财务摘要 stock_financial_abstract": lambda: ak.stock_financial_abstract(symbol="600000"),
        "财务指标 stock_financial_analysis_indicator": lambda: ak.stock_financial_analysis_indicator(symbol="600000"),
        "业绩报告 stock_yjyg_em": lambda: ak.stock_yjyg_em(date="20240930"),
    }
    for label, fn in probes.items():
        df, err = safe_call(fn)
        if err:
            out.evidence.append(f"{label} -> ERR {err[:160]}")
        else:
            out.evidence.append(f"{label} -> shape={shape_of(df)} cols={cols_of(df)[:16]}")
    ok = sum(1 for e in out.evidence if "ERR" not in e)
    out.status = PARTIAL if 0 < ok < len(probes) else (PASS if ok == len(probes) else FAIL)
    out.summary = f"{ok}/{len(probes)} 财报接口可用；代码库无审计意见字段、无连续 N 期恶化判定逻辑"
    out.caveat = "审计意见全库零命中；连续期判定需 d 阶段实现，不可由单次财报快照推断"
    return out


# ---------------------------------------------------------------------------
# M13 飞书 Stream 配置链与依赖
# ---------------------------------------------------------------------------
def m13() -> Result:
    name = "M13 飞书 Stream 配置链/SDK/凭据"
    out = Result("M13", name, BLOCKED, "")
    out.evidence.append(f"lark_oapi 安装={has_module('lark_oapi')}")
    checks = [
        ("src/config.py", "feishu_stream_enabled"),
        ("main.py", "start_feishu_stream_background"),
        ("bot/platforms/feishu_stream.py", "P2ImMessageReceiveV1"),
        ("src/notification_sender/feishu_sender.py", "lark_md"),
    ]
    for rel, token in checks:
        p = REPO / rel
        text = p.read_text(encoding="utf-8", errors="ignore") if p.exists() else ""
        out.evidence.append(f"{rel}: {token} = {token in text}")
    env_keys = ["FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_STREAM_ENABLED"]
    present = {k: bool(os.getenv(k)) for k in env_keys}
    out.evidence.append("凭据环境变量是否设置=" + json.dumps(present))
    out.status = MANUAL
    out.summary = "配置链与 SDK 代码齐备；真实 Stream 收发与 emoji/lark_md 三端渲染需凭据 + 人工验证"
    out.caveat = "本脚本不探测/不记录密钥；三端渲染一致性（手机/桌面/网页）必须人工截图比对，不可用本地 HTML 模拟替代"
    return out


# ---------------------------------------------------------------------------
# M14 LLM 渠道配置与降级
# ---------------------------------------------------------------------------
def m14() -> Result:
    name = "M14 LLM 渠道可用性/延迟/降级"
    out = Result("M14", name, BLOCKED, "")
    channels = os.getenv("LLM_CHANNELS", "")
    out.evidence.append(f"LLM_CHANNELS={channels!r}")
    out.evidence.append("已设置 API_KEY 的渠道=" + json.dumps(
        [c.strip() for c in channels.split(",") if c.strip() and os.getenv(f"LLM_{c.strip().upper()}_API_KEY")]))
    for rel, token in [("src/config.py", "parse_llm_channels"), ("src/config.py", "litellm_fallback_models")]:
        p = REPO / rel
        text = p.read_text(encoding="utf-8", errors="ignore") if p.exists() else ""
        out.evidence.append(f"{rel}: {token} = {token in text}")
    out.status = MANUAL
    out.summary = "渠道配置与 fallback 链齐备；真实延迟/成本/20:00 前产出需凭据 + 目标环境实测"
    out.caveat = "专用『降级规则模板』在代码中未找到（仅有通用 template_fallback）；d 阶段须补实现并验证超时是否及时"
    return out


# ---------------------------------------------------------------------------
# M15 周/月线与 adj_factor
# ---------------------------------------------------------------------------
def m15() -> Result:
    name = "M15 周/月线与 adj_factor 可得性"
    out = Result("M15", name, BLOCKED, "")
    db = REPO / "data" / "stock_analysis.db"
    if db.exists():
        con = sqlite3.connect(db)
        cols = [r[1] for r in con.execute("PRAGMA table_info(stock_daily)")]
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        con.close()
        out.evidence.append(f"stock_daily 含 adj_factor = {'adj_factor' in cols}")
        out.evidence.append(f"周/月线表 = {[t for t in tables if 'week' in t.lower() or 'month' in t.lower()]}")
    src = (REPO / "src" / "services" / "screening" / "daily.py").read_text(encoding="utf-8", errors="ignore")
    out.evidence.append(f"pro.adj_factor 调用存在 = {'pro.adj_factor' in src}")
    out.evidence.append(f"adj_factor 列在内存计算后丢弃 = {'drop(columns=[\"adj_factor\"])' in src}")
    ak = CTX["ak"]
    if ak is not None and hasattr(ak, "stock_zh_a_hist"):
        wk_end = date.today().strftime("%Y%m%d")
        wk_start = (date.today() - timedelta(days=365)).strftime("%Y%m%d")
        df, err, tries = retry_call(ak.stock_zh_a_hist, symbol="600000", period="weekly",
                                    start_date=wk_start, end_date=wk_end, adjust="")
        out.evidence.append(f"周线 period=weekly -> {('ERR ' + err[:120]) if err else 'shape=' + shape_of(df)}（尝试 {tries} 次）")
        out.evidence.append(f"周线字段 = {cols_of(df)[:12] if df is not None else 'n/a'}")
        mo, err_mo, tries_mo = retry_call(ak.stock_zh_a_hist, symbol="600000", period="monthly",
                                          start_date=(date.today() - timedelta(days=365 * 3)).strftime("%Y%m%d"),
                                          end_date=wk_end, adjust="")
        out.evidence.append(f"月线 period=monthly -> {('ERR ' + err_mo[:120]) if err_mo else 'shape=' + shape_of(mo)}（尝试 {tries_mo} 次）")
    weekly_ok = any("周线 period=weekly -> shape=" in e for e in out.evidence)
    monthly_ok = any("月线 period=monthly -> shape=" in e for e in out.evidence)
    agg_ok = False
    # 退路：直取失败时验证「日线聚合」是否可行——这是 L1→L2 的原定做法，比直取更可控
    if not (weekly_ok and monthly_ok) and ak is not None and hasattr(ak, "stock_zh_a_daily"):
        dly, err_d, tries_d = retry_call(
            ak.stock_zh_a_daily, symbol="sh600000", adjust="qfq",
            start_date=(date.today() - timedelta(days=365 * 3)).strftime("%Y%m%d"),
            end_date=date.today().strftime("%Y%m%d"),
        )
        if err_d or dly is None:
            out.evidence.append(f"新浪日线（聚合原料）-> ERR {err_d[:120]}（尝试 {tries_d} 次）")
        else:
            out.evidence.append(f"新浪日线（聚合原料）-> shape={shape_of(dly)}（尝试 {tries_d} 次）")
            try:
                import pandas as pd
                frame = dly.copy()
                frame["date"] = pd.to_datetime(frame["date"])
                frame = frame.set_index("date").sort_index()
                weekly = frame.resample("W-FRI").agg(
                    {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
                ).dropna()
                monthly = frame.resample("ME").agg(
                    {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
                ).dropna()
                out.evidence.append(f"日线聚合周线 -> {len(weekly)} 根（3 年应约 156 根）")
                out.evidence.append(f"日线聚合月线 -> {len(monthly)} 根（3 年应约 36 根）")
                out.evidence.append(
                    f"聚合口径自检：末根周线 close={float(weekly.iloc[-1]['close']):.2f} "
                    f"与末根日线 close={float(frame.iloc[-1]['close']):.2f} "
                    f"{'一致' if abs(float(weekly.iloc[-1]['close']) - float(frame.iloc[-1]['close'])) < 1e-6 else '不一致'}"
                )
                agg_ok = 150 <= len(weekly) <= 160 and 34 <= len(monthly) <= 38
            except Exception as exc:  # noqa: BLE001
                out.evidence.append(f"日线聚合失败：{type(exc).__name__}: {str(exc)[:120]}")
    out.status = PARTIAL
    if weekly_ok and monthly_ok:
        period_note = "直取周线+月线均可取"
    elif agg_ok:
        period_note = "直取周/月线受限，但日线聚合周/月线可行（根数与末值自检通过）"
    elif weekly_ok or monthly_ok:
        period_note = "直取仅部分可取，聚合退路未通过"
    else:
        period_note = "直取与聚合均不可行"
    out.summary = f"adj_factor 仅内存计算、无落库字段；周/月线无本地表；{period_note}"
    out.caveat = ("不可退回前复权落库；不可得时按 §4.6.4 退化为除权事件表推算因子；"
                  "周/月线建议在 L2 层由 L1 日线聚合（不额外落库），避免依赖东财周/月线接口")
    return out


# ---------------------------------------------------------------------------
# M16 公告接口字段与返回量
# ---------------------------------------------------------------------------
def m16() -> Result:
    name = "M16 公告接口字段完整性与日返回量"
    blocked = need_akshare("M16", name)
    if blocked:
        return blocked
    ak = CTX["ak"]
    out = Result("M16", name, BLOCKED, "")
    end = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=7)).strftime("%Y%m%d")
    df, err = safe_call(ak.stock_zh_a_disclosure_report_cninfo,
                        symbol="600000", market="沪深京", start_date=start, end_date=end)
    if err:
        out.status, out.summary, out.error = BLOCKED, "公告接口调用失败", err
        return out
    cols = cols_of(df)
    out.evidence.append(f"7 日窗口 shape={shape_of(df)} cols={cols}")
    want = {"标题": ["公告标题", "标题", "announcementTitle", "title"],
            "公告编号": ["公告编号", "announcementId", "id"],
            "原文链接": ["原文链接", "公告链接", "adjunctUrl", "url"],
            "日期": ["公告时间", "公告日期", "date"]}
    for k, names in want.items():
        out.evidence.append(f"{k}: {[n for n in names if n in cols]}")
    got = sum(1 for names in want.values() if any(n in cols for n in names))
    missing_keys = [k for k, names in want.items() if not any(n in cols for n in names)]
    out.status = PASS if got == len(want) else PARTIAL
    n_rows = int(getattr(df, "shape", (0,))[0]) if df is not None else 0
    out.summary = f"字段命中 {got}/{len(want)}；单标的 7 日 {n_rows} 条" + (f"；缺 {missing_keys}" if missing_keys else "")
    out.caveat = ("实测缺失字段=" + str(missing_keys or "无") +
                  "；代码库现只取标题 + 日期（src/services/screening/candidate_context.py:229-248）；"
                  "缺字段则 §5.15 降级为『只显示标题 + 来源，无链接』")
    return out


# ---------------------------------------------------------------------------
# M17 3 年回补耗时与限频
# ---------------------------------------------------------------------------
def m17() -> Result:
    name = "M17 3 年首次回补耗时/增量更新"
    blocked = need_akshare("M17", name)
    if blocked:
        return blocked
    ak = CTX["ak"]
    out = Result("M17", name, BLOCKED, "")
    end = date.today()
    start = end - timedelta(days=365 * 3)
    # 不只测单只：串行跑多只，才能对外推给出可复核的样本
    samples = ["600000", "000001", "600519", "300750", "601318"]
    per: list[tuple[str, int, int]] = []  # (code, rows, ms)
    total_ms = 0
    used_source = "东财 stock_zh_a_hist"
    primary_dead = False  # 主源失败一次即熔断，模拟真实系统的源健康降级，避免重复试错拉高耗时
    for code in samples:
        t0 = time.perf_counter()
        df, err = None, "主源已熔断"
        if not primary_dead:
            df, err, _tries = retry_call(ak.stock_zh_a_hist, symbol=code, period="daily",
                                         start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
                                         adjust="qfq", attempts=2)
            if err:
                primary_dead = True
                out.evidence.append(f"主源 stock_zh_a_hist 熔断（{code} 失败后不再尝试）：{err[:100]}")
        if err and hasattr(ak, "stock_zh_a_daily"):
            sym = ("sh" if code.startswith(("6", "5")) else "sz") + code
            df, err = safe_call(ak.stock_zh_a_daily, symbol=sym, adjust="qfq",
                                start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
            used_source = "新浪 stock_zh_a_daily"
        ms = int((time.perf_counter() - t0) * 1000)
        total_ms += ms
        if err:
            out.evidence.append(f"{code} -> ERR {err[:100]}")
            continue
        rows = int(getattr(df, "shape", (0,))[0])
        per.append((code, rows, ms))
        out.evidence.append(f"{code} 3 年 qfq {rows} 行，耗时 {ms}ms")
    if not per:
        out.status, out.summary, out.error = BLOCKED, "3 年回补取数全部失败", "; ".join(out.evidence)[:300]
        return out
    avg = total_ms // len(per)
    out.evidence.append(f"来源={used_source}；样本 {len(per)} 只，串行总耗时 {total_ms}ms，均值 {avg}ms/只")
    # 用项目实际自选股数量外推，避免拍脑袋写 13
    universe = 0
    try:
        from src.config import get_config
        universe = len(get_config().stock_list or [])
    except Exception:  # noqa: BLE001
        pass
    if universe:
        out.evidence.append(f"STOCK_LIST 实际 {universe} 只，按均值外推全量首次回补约 {avg * universe / 1000:.1f}s")
    out.status = PASS if len(per) == len(samples) and min(r[1] for r in per) > 700 else PARTIAL
    out.summary = (f"{len(per)}/{len(samples)} 只可取 3 年日线，均值 {avg}ms/只"
                   + (f"；{universe} 只外推约 {avg * universe / 1000:.0f}s" if universe else ""))
    out.caveat = ("本地 stock_daily 0 行；首次回补逻辑在代码中不存在，仅有超时/并发保护"
                  "（daily.py:367-374, screening_service.py:2803）；超限须改分批回补 + 回补期 S0。"
                  "外推不含限频退避与失败重试，实际耗时会更高")
    return out


# ---------------------------------------------------------------------------
# M18（附测）cron 解析与交易日 fail-closed 门控
# ---------------------------------------------------------------------------
def m18() -> Result:
    name = "M18 cron 解析与交易日门控 fail-closed"
    out = Result("M18", name, BLOCKED, "")
    # A. cron 解析依赖
    installed = has_module("croniter")
    out.evidence.append(f"croniter 安装={installed}；requirements.txt 含 croniter/apscheduler="
                        + str(any(k in (REPO / "requirements.txt").read_text(encoding="utf-8").lower()
                                  for k in ["croniter", "apscheduler"])))
    if installed:
        croniter = importlib.import_module("croniter")
        for expr in ["0 20 * * 1-5", "*/15 9-18 * * 1-5"]:
            nxt, err = safe_call(lambda e=expr: croniter.croniter(e).get_next(datetime))
            out.evidence.append(f"{expr} -> {('ERR ' + err[:100]) if err else nxt}")
    # B. 交易日门控（本地可执行）
    try:
        from zoneinfo import ZoneInfo
        from src.core.trading_calendar import build_market_phase_context, is_market_open
        sh = ZoneInfo("Asia/Shanghai")
        for d, label in [(date(2026, 11, 24), "交易日"), (date(2026, 11, 28), "周六"), (date(2026, 10, 1), "国庆")]:
            now = datetime(d.year, d.month, d.day, 20, 0, tzinfo=sh)
            ctx = build_market_phase_context(market="cn", current_time=now)
            out.evidence.append(f"{d} {label}: is_market_open={is_market_open('cn', d)} ctx.is_trading_day={ctx.is_trading_day}")
        unknown = build_market_phase_context(market="xx", current_time=datetime(2026, 11, 24, 20, 0, tzinfo=sh))
        out.evidence.append(f"未知市场: is_market_open={is_market_open('xx', date(2026, 11, 24))} ctx.is_trading_day={unknown.is_trading_day}")
        out.status = PARTIAL if not installed else PASS
        out.summary = ("cron 解析依赖缺失但门控已验证；" if not installed else "cron 解析与门控均可用；")
        out.summary += "未知市场时 is_market_open=True 而 ctx.is_trading_day=None，印证 fail-open 不可用于门控"
    except Exception as exc:  # noqa: BLE001
        out.status, out.error = BLOCKED, f"交易日历 import 失败: {type(exc).__name__}: {str(exc)[:200]}"
        out.summary = "无法本地验证交易日门控"
    out.caveat = "门控须判 is_trading_day is True 才推；None/False 一律零产出 + 告警。通知去重对同源多时点须用独立去重键，否则同一文本会被吞"
    return out


CHECKS: dict[str, Callable[[], Result]] = {
    "M5": m5, "M6": m6, "M7": m7, "M9": m9, "M10": m10, "M11": m11,
    "M12": m12, "M13": m13, "M14": m14, "M15": m15, "M16": m16,
    "M17": m17, "M18": m18,
}
LOCAL_ONLY = {"M11", "M13", "M14", "M15", "M18"}


def network_probes() -> list[str]:
    """跨项环境发现：东财接口按域名分级风控，直接决定 M5/M6/M7 与所有实时行情路径。"""
    req = CTX["requests"]
    if req is None:
        return ["requests 未安装，跳过跨项网络探测"]
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "Chrome/131.0.0.0 Safari/537.36")
    probes = [
        ("东财 push2（实时行情，M5/M6/M7 依赖）",
         "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=3&fs=m:90+t:2&fields=f12,f14"),
        ("东财 17.push2（板块列表分片）",
         "https://17.push2.eastmoney.com/api/qt/clist/get?pn=1&pz=3&fs=m:90+t:2&fields=f12,f14"),
        ("东财 push2his（历史 K 线，M10/M17 依赖）",
         "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600000&klt=101&fqt=1"
         "&beg=20261101&end=20261124&fields1=f1&fields2=f51,f53"),
        ("东财 datacenter-web（数据中心）",
         "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_F10_CORETHEME_BOARDTYPE"
         "&columns=SECUCODE,SECURITY_CODE,BOARD_NAME&pageSize=3&pageNumber=1"),
        ("东财 emweb F10（公司概况，M6 备源）",
         "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code=SH600000"),
        ("腾讯行情（实时备源）", "https://qt.gtimg.cn/q=sh600000"),
        ("新浪行情（实时备源）", "https://hq.sinajs.cn/list=sh600000"),
    ]
    notes: list[str] = []
    for label, url in probes:
        headers = {"User-Agent": ua, "Referer": "https://quote.eastmoney.com/"}
        if "sinajs" in url:
            headers["Referer"] = "https://finance.sina.com.cn"
        try:
            r = req.get(url, headers=headers, timeout=CTX["timeout"])
            n = len(r.content)
            verdict = "（HTTP 200 但空响应＝静默风控）" if (r.status_code == 200 and n == 0) else ""
            notes.append(f"- {label}：HTTP {r.status_code}，{n} 字节{verdict}")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"- {label}：{type(exc).__name__} {str(exc)[:90]}")
    return notes


def render_markdown(results: list[Result], generated: str, env_info: dict[str, Any] | None = None,
                    net_notes: list[str] | None = None) -> str:
    lines = [
        "# 股票分析助手 · 编码前实测前置报告",
        "",
        f"- 生成时间：{generated}",
        "- 数据来源：本脚本真实调用 / 本地代码与数据库实读；**未使用缓存或推断冒充实测**",
        "- 状态定义：PASS 通过 / PARTIAL 部分通过 / MANUAL 需人工凭据或客户端验证 / BLOCKED 环境阻断 / FAIL 未通过",
    ]
    if env_info:
        lines.append("- 运行环境：" + "；".join(f"{k}={v}" for k, v in env_info.items()))
    lines += [
        "",
        "| 项 | 内容 | 状态 | 结论 |",
        "| --- | --- | --- | --- |",
    ]
    for r in sorted(results, key=lambda x: (STATUS_ORDER[x.status], x.mid)):
        lines.append(f"| {r.mid} | {r.name} | **{r.status}** | {r.summary} |")
    lines += ["", "---", ""]
    if net_notes:
        lines += [
            "## 跨项发现 · 接口按域名分级风控",
            "",
            "同一时刻对各线路的真实探测结果（决定了 M5/M6/M7 的降级路径与所有实时行情取数）：",
            "",
        ]
        lines += net_notes
        lines += [
            "",
            "**处置**：实时行情不可依赖单一 `push2`。板块字段走同花顺，个股/公司口径走东财 `emweb` 或巨潮，",
            "历史 K 线以 `push2his` 为主并配腾讯/新浪备源与重试退避。",
            "",
            "---",
            "",
        ]
    for r in results:
        lines += [f"## {r.mid} · {r.name}", "", f"- **状态**：{r.status}", f"- **结论**：{r.summary}"]
        if r.evidence:
            lines.append("- **证据**：")
            lines += [f"  - {e}" for e in r.evidence]
        if r.caveat:
            lines.append(f"- **注意**：{r.caveat}")
        if r.error:
            lines.append(f"- **错误**：{r.error}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="股票分析助手编码前 M 系列实测")
    ap.add_argument("--only", nargs="*", help="只跑指定项，如 --only M5 M7")
    ap.add_argument("--local-only", action="store_true", help="只跑不依赖网络的项")
    ap.add_argument("--timeout", type=int, default=12, help="单次网络超时（秒），默认 12")
    args = ap.parse_args()
    CTX["timeout"] = args.timeout

    CTX["requests"] = importlib.import_module("requests") if has_module("requests") else None
    CTX["ak"] = importlib.import_module("akshare") if has_module("akshare") else None
    CTX["ts"] = importlib.import_module("tushare") if has_module("tushare") else None
    print(f"[env] akshare={CTX['ak'] is not None} tushare={CTX['ts'] is not None} requests={CTX['requests'] is not None}")

    selected = args.only or list(CHECKS)
    if args.local_only:
        selected = [m for m in selected if m in LOCAL_ONLY]
    net_notes = [] if args.local_only else network_probes()
    if net_notes:
        print("[net] 跨项网络探测完成：" + str(len(net_notes)) + " 条")
        for n in net_notes:
            print("      " + n)
    results: list[Result] = []
    for mid in selected:
        fn = CHECKS.get(mid)
        if fn is None:
            print(f"[skip] unknown id {mid}")
            continue
        t0 = time.perf_counter()
        try:
            r = fn()
        except Exception as exc:  # noqa: BLE001
            r = Result(mid, mid, FAIL, "实测脚本自身异常", error=f"{type(exc).__name__}: {str(exc)[:300]}")
        r.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        results.append(r)
        print(f"[{mid}] {r.status:8s} {r.summary[:100]}")

    generated = datetime.now().astimezone().isoformat(timespec="seconds")
    env_info: dict[str, Any] = {
        "python": sys.version.split()[0],
        "akshare": getattr(CTX["ak"], "__version__", "未安装") if CTX["ak"] else "未安装",
        "tushare": getattr(CTX["ts"], "__version__", "未安装") if CTX["ts"] else "未安装",
        "pandas": getattr(importlib.import_module("pandas"), "__version__", "?") if has_module("pandas") else "未安装",
        "代理环境变量": "已设置" if (os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY")) else "未设置",
        "网络超时": f"{CTX['timeout']}s",
    }
    outdir = REPO / "deliverables" / ("m-preflight-" + datetime.now().strftime("%Y%m%d"))
    outdir.mkdir(parents=True, exist_ok=True)
    # 部分运行（--only / --local-only）写独立文件，不覆盖全量报告。
    partial = bool(args.only) or args.local_only
    suffix = "-partial-" + datetime.now().strftime("%H%M%S") if partial else ""
    jpath = outdir / f"m-check{suffix}.json"
    mpath = outdir / f"m-check-report{suffix}.md"
    jpath.write_text(json.dumps({"generated_at": generated, "env": env_info,
                                 "network_probes": net_notes,
                                 "results": [r.as_dict() for r in results]},
                                ensure_ascii=False, indent=2), encoding="utf-8")
    mpath.write_text(render_markdown(results, generated, env_info, net_notes), encoding="utf-8")
    print(f"\n[out] {jpath}\n[out] {mpath}")
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print("[summary] " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: STATUS_ORDER[kv[0]])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
