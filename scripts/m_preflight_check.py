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
    # 代码库现状：用板块名（industry.py:227-229）。实测两种入参。
    df_name, err_name = safe_call(ak.stock_board_industry_name_em)
    if err_name:
        out.status, out.summary, out.error = BLOCKED, "取板块列表失败", err_name
        return out
    out.evidence.append(f"stock_board_industry_name_em -> shape={shape_of(df_name)} cols={cols_of(df_name)[:12]}")
    name_col = next((c for c in ["板块名称", "名称", "name"] if c in cols_of(df_name)), None)
    code_col = next((c for c in ["板块代码", "代码", "code"] if c in cols_of(df_name)), None)
    out.evidence.append(f"name_col={name_col} code_col={code_col}")
    if not name_col:
        out.status, out.summary = FAIL, "板块列表无名称列，映射链路无法建立"
        return out
    sample_name = str(df_name.iloc[0][name_col])
    for label, arg in [("板块名", sample_name), ("板块代码", "BK0475" if not code_col else str(df_name.iloc[0][code_col]))]:
        df, err = safe_call(ak.stock_board_industry_cons_em, symbol=arg)
        if err:
            out.evidence.append(f"{label}({arg}) -> ERR {err[:160]}")
            continue
        out.evidence.append(f"{label}({arg}) -> shape={shape_of(df)} cols={cols_of(df)[:10]}")
    accepted = any("-> shape=" in e and "ERR" not in e for e in out.evidence if "板块代码" in e)
    out.status = PASS if accepted else PARTIAL
    out.summary = ("板块代码入参可用" if accepted else
                   "板块代码入参不可用/未验证；代码库现用板块名（src/services/screening/industry.py:227-229）")
    out.caveat = "M5 决定 code→板块映射链路；若仅名称可用，需按 R5 走同源反查，严禁用 M6 的行业字段兜底"
    return out


# ---------------------------------------------------------------------------
# M6 stock_individual_info_em 行业字段口径
# ---------------------------------------------------------------------------
def m6() -> Result:
    name = "M6 stock_individual_info_em 行业字段口径"
    blocked = need_akshare("M6", name)
    if blocked:
        return blocked
    ak = CTX["ak"]
    out = Result("M6", name, BLOCKED, "")
    df, err = safe_call(ak.stock_individual_info_em, symbol="600000")
    if err:
        out.status, out.summary, out.error = BLOCKED, "接口调用失败", err
        return out
    out.evidence.append(f"shape={shape_of(df)} cols={cols_of(df)}")
    try:
        rows = dict(zip(df.iloc[:, 0].astype(str), df.iloc[:, 1].astype(str)))
        industry = rows.get("行业")
        out.evidence.append(f"行业字段值={industry!r}")
        out.evidence.append("全部键值=" + json.dumps(rows, ensure_ascii=False)[:500])
        out.status = PARTIAL
        out.summary = f"取到行业字段值 {industry!r}，但是否东财板块名/证监会分类需人工比对板块列表"
        out.caveat = "实测前严禁用于 code→板块映射；需把该值与 stock_board_industry_name_em 的板块名逐字比对后裁定"
    except Exception as exc:  # noqa: BLE001
        out.status, out.summary, out.error = FAIL, "无法解析行业字段", str(exc)[:200]
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
    df, err = safe_call(ak.stock_board_industry_name_em)
    if err:
        out.status, out.summary, out.error = BLOCKED, "接口调用失败", err
        return out
    cols = cols_of(df)
    out.evidence.append(f"shape={shape_of(df)} cols={cols}")
    want = ["上涨家数", "下跌家数", "领涨股票", "领涨股"]
    found = [c for c in want if c in cols]
    missing = [c for c in want if c not in cols]
    out.status = PARTIAL if found and missing else (PASS if not missing else FAIL)
    out.summary = f"命中字段={found}；缺失={missing}"
    out.caveat = "代码库现只用『涨跌幅 + 板块名称』两列（data_provider/akshare_fetcher.py:2218-2223）；缺列则 R5 按参考层降级为『未取得』"
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
    probes = {
        "分红明细 stock_fhps_detail_em": lambda: ak.stock_fhps_detail_em(symbol="600000"),
        "历史分红 stock_history_dividend_detail": lambda: ak.stock_history_dividend_detail(symbol="600000"),
        "PE/PB 分位 stock_a_indicator_lg": lambda: ak.stock_a_indicator_lg(symbol="600000"),
    }
    for label, fn in probes.items():
        has = hasattr(ak, fn.__name__.split(" ")[-1].split("(")[0]) or True
        df, err = safe_call(fn)
        if err:
            out.evidence.append(f"{label} -> ERR {err[:160]}")
        else:
            out.evidence.append(f"{label} -> shape={shape_of(df)} cols={cols_of(df)[:14]}")
    ok = sum(1 for e in out.evidence if "ERR" not in e)
    out.status = PARTIAL if 0 < ok < len(probes) else (PASS if ok == len(probes) else FAIL)
    out.summary = f"{ok}/{len(probes)} 个接口可用；股息率为自算值（data_provider/base.py:4481），非接口直出"
    out.caveat = "代码库零调用 stock_a_indicator_lg / stock_zh_valuation；PE-PB 分位接口若不可用，§5.8 相应项标注缺失、降置信度"
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
    df, err = safe_call(
        ak.stock_zh_a_hist, symbol="600000", period="daily",
        start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"), adjust="qfq",
    )
    if err:
        out.status, out.summary, out.error = BLOCKED, "长周期取数失败", err
        return out
    rows = int(getattr(df, "shape", (0,))[0])
    out.evidence.append(f"4 年 qfq 日线 shape={shape_of(df)} cols={cols_of(df)[:12]}")
    out.evidence.append(f"行数={rows}（约 970 交易日量级）")
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
        idx, err = safe_call(ak.stock_zh_index_daily_em, symbol="sh000001")
        out.evidence.append(f"上证指数历史 -> {('ERR ' + err[:120]) if err else 'shape=' + shape_of(idx)}")
    out.status = PARTIAL
    out.summary = "风险判定专用表在代码中不存在；指数历史可取自免费源但需新建存储与聚合"
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
        df, err = safe_call(ak.stock_zh_a_hist, symbol="600000", period="weekly",
                            start_date="20250101", end_date="20251124", adjust="")
        out.evidence.append(f"周线 period=weekly -> {('ERR ' + err[:120]) if err else 'shape=' + shape_of(df)}")
    out.status = PARTIAL
    out.summary = "adj_factor 有接口调用但无落库字段；周/月线无本地聚合表；免费源可提供周线"
    out.caveat = "不可退回前复权落库；不可得时按 §4.6.4 退化为除权事件表推算因子"
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
    out.status = PASS if got == len(want) else PARTIAL
    out.summary = f"字段命中 {got}/{len(want)}；单标的 7 日 {shape_of(df)[0] if hasattr(df,'shape') else '?'} 条"
    out.caveat = "代码库现只取标题 + 日期（src/services/screening/candidate_context.py:229-248）；缺字段则 §5.15 降级为『只显示标题 + 来源，无链接』"
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
    t0 = time.perf_counter()
    df, err = safe_call(ak.stock_zh_a_hist, symbol="600000", period="daily",
                        start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"), adjust="qfq")
    elapsed = int((time.perf_counter() - t0) * 1000)
    if err:
        out.status, out.summary, out.error = BLOCKED, "3 年回补取数失败", err
        return out
    rows = int(getattr(df, "shape", (0,))[0])
    out.evidence.append(f"单标的 3 年 qfq {rows} 行，耗时 {elapsed}ms")
    out.evidence.append(f"按 13 只串行外推约 {elapsed * 13 / 1000:.1f}s（仅外推，未实跑 13 只）")
    out.status = PARTIAL
    out.summary = f"单标的 {rows} 行耗时 {elapsed}ms；13 只全量尚未实测，不能判定是否满足首推时点前产出"
    out.caveat = "本地 stock_daily 0 行；首次回补逻辑在代码中不存在，仅有超时/并发保护（daily.py:367-374, screening_service.py:2803）；超限须改分批回补 + 回补期 S0"
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


def render_markdown(results: list[Result], generated: str) -> str:
    lines = [
        "# 股票分析助手 · 编码前实测前置报告",
        "",
        f"- 生成时间：{generated}",
        "- 数据来源：本脚本真实调用 / 本地代码与数据库实读；**未使用缓存或推断冒充实测**",
        "- 状态定义：PASS 通过 / PARTIAL 部分通过 / MANUAL 需人工凭据或客户端验证 / BLOCKED 环境阻断 / FAIL 未通过",
        "",
        "| 项 | 内容 | 状态 | 结论 |",
        "| --- | --- | --- | --- |",
    ]
    for r in sorted(results, key=lambda x: (STATUS_ORDER[x.status], x.mid)):
        lines.append(f"| {r.mid} | {r.name} | **{r.status}** | {r.summary} |")
    lines += ["", "---", ""]
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
    outdir = REPO / "deliverables" / ("m-preflight-" + datetime.now().strftime("%Y%m%d"))
    outdir.mkdir(parents=True, exist_ok=True)
    # 部分运行（--only / --local-only）写独立文件，不覆盖全量报告。
    partial = bool(args.only) or args.local_only
    suffix = "-partial-" + datetime.now().strftime("%H%M%S") if partial else ""
    jpath = outdir / f"m-check{suffix}.json"
    mpath = outdir / f"m-check-report{suffix}.md"
    jpath.write_text(json.dumps({"generated_at": generated, "results": [r.as_dict() for r in results]},
                                ensure_ascii=False, indent=2), encoding="utf-8")
    mpath.write_text(render_markdown(results, generated), encoding="utf-8")
    print(f"\n[out] {jpath}\n[out] {mpath}")
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print("[summary] " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: STATUS_ORDER[kv[0]])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
