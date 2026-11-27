# -*- coding: utf-8 -*-
"""不复权行情取数（技术方案 §3.1，需求 §4.6.4 / §7.2）。

三条硬约束：
1. **只取不复权**（``adjust=""``）。前复权序列绝不落库（需求 §4.6.4 硬约束）。
2. **多源降级 + 留痕**：东财 → 新浪 → 腾讯，每次记录实际来源 ``source_tag``（D1/D2/D3）；
   东财 ``push2`` 系列存在**HTTP 200 但空响应**的静默风控（2026-11-24 实测），
   因此「返回空表」必须按失败处理并降级，不能当成「该标的没有行情」。
3. **失败不静默**：连续失败触发源熔断（跳过冷却期内该源），全源失败才回落本地缓存
   并标 ``D3`` + ``degraded``；无缓存则抛 :class:`FetchError`。

设计上把「数据源」与「调度/熔断/归一化」分开：数据源只负责取原始行，
归一化、重试、熔断、限速全部集中在 :class:`DailyFetcher`，便于离线单测（注入假源即可）。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from src.assistant.domain import Bar
from src.assistant.enums import SourceTag

logger = logging.getLogger(__name__)

#: 归一化使用的列名别名（不同源/不同 akshare 版本列名不一致）
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "日期", "trade_date", "时间"),
    "open": ("open", "开盘", "开盘价"),
    "high": ("high", "最高", "最高价"),
    "low": ("low", "最低", "最低价"),
    "close": ("close", "收盘", "收盘价"),
    "volume": ("volume", "成交量", "vol"),
    "amount": ("amount", "成交额", "turnover_amount"),
}

#: 交易日历容错：A 股日线最多回看 3 年（需求 §4.6.3 首次回补 ≥3 年）
DEFAULT_BACKFILL_YEARS = 3


class FetchError(RuntimeError):
    """所有数据源都失败且无可用缓存。"""

    def __init__(self, code: str, attempts: Sequence[str], cache_tried: bool = False) -> None:
        self.code = code
        self.attempts = tuple(attempts)
        detail = "；".join(self.attempts) or "无可用源"
        suffix = "；本地缓存不可用" if cache_tried else ""
        super().__init__(f"{code} 取数失败：{detail}{suffix}")


@dataclass(frozen=True)
class FetchResult:
    """一次取数的结果与审计信息。"""

    code: str
    bars: tuple[Bar, ...]
    source_tag: SourceTag
    source_name: str
    attempts: tuple[str, ...] = ()
    degraded: bool = False
    dropped: int = 0
    note: str = ""
    elapsed_ms: int = 0

    @property
    def rows(self) -> int:
        return len(self.bars)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "rows": self.rows,
            "source_tag": self.source_tag.value,
            "source_name": self.source_name,
            "attempts": list(self.attempts),
            "degraded": self.degraded,
            "dropped": self.dropped,
            "note": self.note,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass(frozen=True)
class SectorRow:
    """板块一览行（同花顺汇总口径）。"""

    sector_code: str
    sector_name: str
    pct_change: float
    up_count: int | None = None
    down_count: int | None = None
    lead_stock: str = ""
    lead_pct: float | None = None


@dataclass(frozen=True)
class SectorOverview:
    """板块一览结果 + 审计信息。"""

    rows: tuple[SectorRow, ...]
    source_tag: SourceTag
    source_name: str
    degraded: bool = False
    note: str = ""

    def by_name(self) -> dict[str, SectorRow]:
        return {row.sector_name: row for row in self.rows}

    def by_code(self) -> dict[str, SectorRow]:
        return {row.sector_code: row for row in self.rows if row.sector_code}


# ---------------------------------------------------------------------------
# 数据源协议与实现（真实实现懒加载 akshare，便于无网单测）
# ---------------------------------------------------------------------------
class DailySource(Protocol):
    """单标的日线数据源。"""

    name: str
    tag: SourceTag

    def fetch(self, code: str, start: date, end: date) -> Sequence[Mapping[str, Any]]:
        """返回原始行（列名见 :data:`_COLUMN_ALIASES`）。失败抛异常。"""


class _AkshareSource:
    """akshare 数据源基类：懒加载 + 只取不复权。"""

    name = "base"
    tag = SourceTag.D1

    def _ak(self):  # pragma: no cover - 真实依赖
        import akshare as ak  # noqa: PLC0415 - 懒加载，避免无网/无依赖时导入失败

        return ak

    def fetch(self, code: str, start: date, end: date) -> Sequence[Mapping[str, Any]]:  # pragma: no cover
        raise NotImplementedError


def _ak_fn(ak: Any, names: Sequence[str]) -> Any:
    """按顺序找一个可用的 akshare 接口：**新版名字优先，旧版兜底**。

    akshare 1.20.46 起把 ``stock_zh_a_hist`` 改名为 ``stock_zh_a_hist_em``、
    ``stock_zh_a_daily`` 改名为 ``stock_zh_a_hist_sina``、
    ``stock_zh_index_daily`` 改名为 ``index_zh_daily_sina``。
    不锁版本就必须按「名字在不在」探测，而不是 try/except 网络异常——
    否则限流/超时也会被当成「名字不对」而白换一轮。
    """
    for name in names:
        fn = getattr(ak, name, None)
        if callable(fn):
            return fn
    raise AttributeError(f"akshare 缺少接口：{' / '.join(names)}")


class EastmoneySource(_AkshareSource):
    """主源：东财日线（``stock_zh_a_hist_em``，``adjust=""``）；ETF 走 ``fund_etf_hist_em``。

    实测注意：``push2his`` 存在间歇限流与**空响应**，空表必须当失败。
    """

    name = "eastmoney"
    tag = SourceTag.D1

    def fetch(self, code: str, start: date, end: date) -> Sequence[Mapping[str, Any]]:  # pragma: no cover
        ak = self._ak()
        start_text, end_text = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
        if is_fund_code(code):
            fn = _ak_fn(ak, ("fund_etf_hist_em",))
            df = fn(
                symbol=to_plain_code(code),
                period="daily",
                start_date=start_text,
                end_date=end_text,
                adjust="",
            )
        else:
            fn = _ak_fn(ak, ("stock_zh_a_hist_em", "stock_zh_a_hist"))
            df = fn(
                symbol=to_plain_code(code),
                period="daily",
                start_date=start_text,
                end_date=end_text,
                adjust="",
            )
        return _records(df)


class SinaSource(_AkshareSource):
    """备源一：新浪日线（``stock_zh_a_hist_sina``，``adjust=""``）；ETF 走 ``fund_etf_hist_sina``。

    新浪的 ETF 接口**不吃区间参数**（返回全历史），区间由调用方统一过滤，这里不自己截。
    """

    name = "sina"
    tag = SourceTag.D2

    def fetch(self, code: str, start: date, end: date) -> Sequence[Mapping[str, Any]]:  # pragma: no cover
        ak = self._ak()
        if is_fund_code(code):
            fn = _ak_fn(ak, ("fund_etf_hist_sina",))
            df = fn(symbol=to_market_symbol(code))
        else:
            fn = _ak_fn(ak, ("stock_zh_a_hist_sina", "stock_zh_a_daily"))
            df = fn(
                symbol=to_market_symbol(code),
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust="",
            )
        return _records(df)


class TencentSource(_AkshareSource):
    """备源二：腾讯日线（``stock_zh_a_hist_tx``，无复权参数＝不复权）。

    实测这个接口**同时支持 ETF**（``sh512000``），只要代码前缀别猜错。
    """

    name = "tencent"
    tag = SourceTag.D2

    def fetch(self, code: str, start: date, end: date) -> Sequence[Mapping[str, Any]]:  # pragma: no cover
        df = self._ak().stock_zh_a_hist_tx(
            symbol=to_market_symbol(code),
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        return _records(df)


class SinaIndexSource(_AkshareSource):
    """指数历史（新浪 ``index_zh_daily_sina``，实测 8772 行可用）。"""

    name = "sina_index"
    tag = SourceTag.D1

    def fetch(self, code: str, start: date, end: date) -> Sequence[Mapping[str, Any]]:  # pragma: no cover
        # 指数必须带市场前缀，绝不能走个股代码归一化：
        # ``sh000001``（上证指数 3548 点）与 ``sz000001``（平安银行 12 元）是两回事。
        fn = _ak_fn(self._ak(), ("index_zh_daily_sina", "stock_zh_index_daily"))
        return _records(fn(symbol=to_index_symbol(code)))


def default_sources() -> tuple[DailySource, ...]:
    """默认降级链：东财（D1）→ 新浪（D2）→ 腾讯（D2）。"""
    return (EastmoneySource(), SinaSource(), TencentSource())


# ---------------------------------------------------------------------------
# 熔断与限速
# ---------------------------------------------------------------------------
@dataclass
class _BreakerState:
    failures: int = 0
    open_until: float = 0.0


class CircuitBreaker:
    """连续失败 N 次后跳过该源 ``cooldown`` 秒（技术方案 §3.1「连续 3 个交易日失败降级或排除」）。"""

    def __init__(self, threshold: int = 3, cooldown_seconds: float = 300.0, clock: Callable[[], float] | None = None) -> None:
        if threshold < 1:
            raise ValueError("threshold 必须 ≥1")
        self.threshold = threshold
        self.cooldown = cooldown_seconds
        self._clock = clock or time.monotonic
        self._states: dict[str, _BreakerState] = {}

    def allow(self, name: str) -> bool:
        state = self._states.get(name)
        if state is None:
            return True
        if state.failures < self.threshold:
            return True
        return self._clock() >= state.open_until

    def record_success(self, name: str) -> None:
        self._states.pop(name, None)

    def record_failure(self, name: str) -> None:
        state = self._states.setdefault(name, _BreakerState())
        state.failures += 1
        if state.failures >= self.threshold:
            state.open_until = self._clock() + self.cooldown

    def reset(self) -> None:
        self._states.clear()

    def snapshot(self) -> dict[str, dict[str, float]]:
        return {name: {"failures": st.failures, "open_until": st.open_until} for name, st in self._states.items()}


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------
def _records(obj: Any) -> list[dict[str, Any]]:
    """把 DataFrame / list[dict] 统一成 ``list[dict]``。"""
    if obj is None:
        return []
    if isinstance(obj, (list, tuple)):
        return [dict(row) for row in obj]
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        try:
            return list(to_dict("records"))
        except TypeError:  # pragma: no cover - 非 DataFrame 的 to_dict
            pass
    raise TypeError(f"无法识别的取数返回类型：{type(obj)!r}")


def _pick(row: Mapping[str, Any], key: str) -> Any:
    for alias in _COLUMN_ALIASES[key]:
        if alias in row:
            return row[alias]
    return None


def _to_float(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return None if math.isnan(val) else val
    text = str(raw).strip().replace(",", "").replace("%", "")
    if not text or text in ("-", "--", "nan", "None"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_date(raw: Any) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    to_pydatetime = getattr(raw, "date", None)  # pandas.Timestamp
    if callable(to_pydatetime):
        try:
            return to_pydatetime()
        except (TypeError, ValueError):  # pragma: no cover
            pass
    text = str(raw).strip().replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[: len(fmt) + 2], fmt).date()
        except ValueError:
            continue
    return None


def normalize_rows(rows: Iterable[Mapping[str, Any]], *, source_tag: SourceTag = SourceTag.D1) -> tuple[list[Bar], int]:
    """把原始行归一化为 :class:`Bar` 列表，返回 ``(bars, dropped)``。

    容错策略（**不编造数据**）：
    - 缺日期/缺收盘/价格非正 → 丢弃并计数；
    - ``high`` / ``low`` 与开收不自洽 → 夹紧到 ``[max(open,close), min(open,close)]`` 的外包区间；
    - 同日期重复 → 保留最后一条（后到的视为修正值）。
    """
    by_date: dict[date, Bar] = {}
    dropped = 0
    for row in rows:
        trade_date = _to_date(_pick(row, "date"))
        close = _to_float(_pick(row, "close"))
        if trade_date is None or close is None or close <= 0:
            dropped += 1
            continue
        open_ = _to_float(_pick(row, "open"))
        high = _to_float(_pick(row, "high"))
        low = _to_float(_pick(row, "low"))
        open_ = close if open_ is None or open_ <= 0 else open_
        high = close if high is None or high <= 0 else high
        low = close if low is None or low <= 0 else low
        high = max(high, open_, close)
        low = min(low, open_, close)
        if low <= 0:
            dropped += 1
            continue
        volume = _to_float(_pick(row, "volume")) or 0.0
        amount = _to_float(_pick(row, "amount")) or 0.0
        by_date[trade_date] = Bar(
            trade_date=trade_date,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=max(0.0, volume),
            amount=max(0.0, amount),
            source_tag=source_tag,
        )
    bars = [by_date[key] for key in sorted(by_date)]
    return bars, dropped


# ---------------------------------------------------------------------------
# 代码规范化
# ---------------------------------------------------------------------------
def to_plain_code(code: str) -> str:
    """``sh600000`` / ``600000.SH`` / ``600000`` → ``600000``。"""
    text = str(code).strip().upper().replace(".", "")
    if text.startswith(("SH", "SZ", "BJ")) and len(text) > 6:
        text = text[2:]
    elif text.endswith(("SH", "SZ", "BJ")) and len(text) > 6:
        text = text[:-2]
    return text


def is_fund_code(code: str) -> bool:
    """ETF / 场内基金：沪市 ``5xxxxx``，深市 ``15xxxx`` / ``16xxxx``。

    只做号段判断、不看名字：ETF 与个股的取数接口不同（``fund_etf_*`` vs ``stock_zh_a_*``），
    拿错接口会直接报错或返回空表，所以这一步必须确定。
    """
    plain = to_plain_code(code)
    return plain.startswith("5") or plain.startswith(("15", "16"))


def to_market_symbol(code: str) -> str:
    """``600000`` / ``512000`` → ``sh600000`` / ``sh512000``（新浪/腾讯口径）。"""
    plain = to_plain_code(code)
    if len(plain) != 6 or not plain.isdigit():
        raise ValueError(f"无法识别的 A 股代码：{code!r}")
    if plain.startswith(("6", "5", "9")):
        return f"sh{plain}"
    if plain.startswith(("0", "1", "2", "3")):
        return f"sz{plain}"
    if plain.startswith(("4", "8")):
        return f"bj{plain}"
    raise ValueError(f"无法识别的 A 股代码：{code!r}")


def to_index_symbol(code: str) -> str:
    """指数代码归一化：**必须带市场前缀**，否则拒绝。

    指数代码与个股代码会撞号：``sh000001`` 是上证指数（3000+ 点），而 ``000001``
    按个股规则会被判成 ``sz000001`` 平安银行（10 元级）。因此这里不做「猜市场」，
    缺前缀直接报错，避免静默取到另一只标的（2026-11-24 联网验证时真实踩到过）。

    接受：``sh000001`` / ``SH000001`` / ``000001.SH`` / ``000001sh``。
    """
    text = str(code).strip().lower()
    markets = ("sh", "sz", "bj")
    if "." in text:
        left, _, right = text.partition(".")
        if right in markets and len(left) == 6 and left.isdigit():
            return f"{right}{left}"
        if left in markets and len(right) == 6 and right.isdigit():
            return f"{left}{right}"
        raise ValueError(f"指数代码需要市场前缀（如 sh000001 / sz399001），收到：{code!r}")
    if len(text) == 8:
        if text[:2] in markets and text[2:].isdigit():
            return text
        if text[-2:] in markets and text[:6].isdigit():
            return f"{text[-2:]}{text[:6]}"
    raise ValueError(f"指数代码需要市场前缀（如 sh000001 / sz399001），收到：{code!r}")


# ---------------------------------------------------------------------------
# 取数编排
# ---------------------------------------------------------------------------
class DailyFetcher:
    """日线取数：多源降级 + 重试退避 + 熔断 + 限速 + 缓存兜底。"""

    def __init__(
        self,
        sources: Sequence[DailySource] | None = None,
        *,
        store: Any | None = None,
        breaker: CircuitBreaker | None = None,
        retries: int = 2,
        backoff_seconds: float = 0.4,
        min_interval_seconds: float = 0.0,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.sources = tuple(sources) if sources is not None else default_sources()
        self.store = store
        self.breaker = breaker or CircuitBreaker(clock=clock)
        self.retries = max(0, int(retries))
        self.backoff = max(0.0, float(backoff_seconds))
        self.min_interval = max(0.0, float(min_interval_seconds))
        self._clock = clock or time.monotonic
        self._sleep = sleeper or time.sleep
        self._last_call = 0.0

    # -- 限速 -----------------------------------------------------------
    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        gap = self._clock() - self._last_call
        if gap < self.min_interval:
            self._sleep(self.min_interval - gap)
        self._last_call = self._clock()

    # -- 单源取数（含重试）-----------------------------------------------
    def _fetch_one(self, source: DailySource, code: str, start: date, end: date, attempts: list[str]) -> list[Bar] | None:
        last_error = ""
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                raw = source.fetch(code, start, end)
            except Exception as exc:  # noqa: BLE001 - 取数失败必须降级，不能中断全链
                last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
                if attempt < self.retries:
                    self._sleep(self.backoff * (2**attempt))
                    continue
                break
            bars, _dropped = normalize_rows(raw, source_tag=source.tag)
            if bars:
                self.breaker.record_success(source.name)
                return bars
            # 空表 = 静默风控（东财 push2 实测），按失败处理并重试
            last_error = "空响应"
            if attempt < self.retries:
                self._sleep(self.backoff * (2**attempt))
        self.breaker.record_failure(source.name)
        attempts.append(f"{source.name}: {last_error or '空响应'}")
        logger.warning("数据源 %s 取 %s 失败：%s", source.name, code, last_error or "空响应")
        return None

    # -- 对外主入口 ------------------------------------------------------
    def fetch_daily(
        self,
        code: str,
        start: date,
        end: date,
        *,
        allow_cache: bool = True,
        now: date | None = None,
    ) -> FetchResult:
        """按降级链取不复权日线；全源失败时回落本地缓存（D3）。"""
        if start > end:
            raise ValueError(f"起始日 {start} 晚于结束日 {end}")
        attempts: list[str] = []
        began = self._clock()
        today = now or date.today()

        for source in self.sources:
            if not self.breaker.allow(source.name):
                attempts.append(f"{source.name}: 熔断跳过")
                continue
            bars = self._fetch_one(source, code, start, end, attempts)
            if not bars:
                continue
            # 只保留请求区间内的数据（新浪指数类接口会返回全量）
            bars = [bar for bar in bars if start <= bar.trade_date <= end]
            if not bars:
                attempts.append(f"{source.name}: 区间内无数据")
                continue
            degraded = source.tag is SourceTag.D2
            note = f"主源不可用，已降级到 {source.name}" if source.tag is SourceTag.D2 else ""
            return FetchResult(
                code=code,
                bars=tuple(bars),
                source_tag=source.tag,
                source_name=source.name,
                attempts=tuple(attempts) + (f"{source.name}: ok",),
                degraded=degraded,
                note=note,
                elapsed_ms=int((self._clock() - began) * 1000),
            )

        if allow_cache and self.store is not None:
            cached = self.store.load_bars(code, start=start, end=end)
            if cached:
                stale_days = (today - cached[-1].trade_date).days
                return FetchResult(
                    code=code,
                    bars=tuple(cached),
                    source_tag=SourceTag.D3,
                    source_name="cache",
                    attempts=tuple(attempts) + ("cache: ok（非最新）",),
                    degraded=True,
                    note=f"全部数据源不可用，使用本地缓存（末条 {cached[-1].trade_date}，距今 {stale_days} 天）",
                    elapsed_ms=int((self._clock() - began) * 1000),
                )

        raise FetchError(code, attempts, cache_tried=self.store is not None and allow_cache)

    # -- 回补 -----------------------------------------------------------
    def backfill(
        self,
        codes: Sequence[str],
        *,
        years: int = DEFAULT_BACKFILL_YEARS,
        end: date | None = None,
        progress: Callable[[str, FetchResult], None] | None = None,
        allow_cache: bool = False,
    ) -> list[FetchResult]:
        """批量回补日线并落 L1；已足够新的标的直接跳过（幂等，可断点续跑）。"""
        if self.store is None:
            raise ValueError("回补需要 store（L1 落库）")
        end_date = end or date.today()
        start_date = end_date - timedelta(days=365 * max(1, years))
        results: list[FetchResult] = []
        for code in codes:
            last = self.store.last_trade_date(code)
            if last is not None and (end_date - last).days <= 3:
                result = FetchResult(
                    code=code,
                    bars=tuple(self.store.load_bars(code, start=start_date, end=end_date)),
                    source_tag=SourceTag.D3,
                    source_name="cache",
                    note=f"本地已更新到 {last}，跳过回补",
                )
                results.append(result)
                if progress:
                    progress(code, result)
                continue
            since = start_date if last is None else max(start_date, last + timedelta(days=1))
            try:
                result = self.fetch_daily(code, since, end_date, allow_cache=allow_cache)
            except FetchError as exc:
                logger.error("回补失败：%s", exc)
                result = FetchResult(
                    code=code,
                    bars=(),
                    source_tag=SourceTag.D3,
                    source_name="none",
                    attempts=exc.attempts,
                    degraded=True,
                    note=str(exc),
                )
            else:
                if result.bars:
                    self.store.save_bars(code, result.bars)
                    self.store.rebuild_weekly_monthly(code)
            results.append(result)
            if progress:
                progress(code, result)
        return results


class ReferenceFetcher:
    """指数与板块一览取数（用于大盘环境与板块强弱）。"""

    def __init__(
        self,
        index_sources: Sequence[DailySource] | None = None,
        *,
        store: Any | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.index_sources = tuple(index_sources) if index_sources is not None else (SinaIndexSource(),)
        self.store = store
        self.breaker = breaker or CircuitBreaker()

    def fetch_index(self, code: str, start: date, end: date) -> FetchResult:
        """取指数日线（原样返回，不落库；落库由调用方决定）。

        入口即校验市场前缀——指数与个股撞号（``sh000001`` vs ``sz000001``）会取错标的。
        """
        symbol = to_index_symbol(code)
        if start > end:
            raise ValueError(f"起始日 {start} 晚于结束日 {end}")
        attempts: list[str] = []
        for source in self.index_sources:
            if not self.breaker.allow(source.name):
                attempts.append(f"{source.name}: 熔断跳过")
                continue
            try:
                raw = source.fetch(symbol, start, end)
            except Exception as exc:  # noqa: BLE001
                self.breaker.record_failure(source.name)
                attempts.append(f"{source.name}: {type(exc).__name__}: {str(exc)[:100]}")
                continue
            bars, dropped = normalize_rows(raw, source_tag=source.tag)
            bars = [bar for bar in bars if start <= bar.trade_date <= end]
            if not bars:
                self.breaker.record_failure(source.name)
                attempts.append(f"{source.name}: 区间内无数据")
                continue
            self.breaker.record_success(source.name)
            return FetchResult(code=symbol, bars=tuple(bars), source_tag=source.tag, source_name=source.name,
                               attempts=tuple(attempts) + (f"{source.name}: ok",), dropped=dropped)
        raise FetchError(symbol, attempts)

    def fetch_sector_index(self, name: str, start: date, end: date) -> tuple[Bar, ...]:
        """取板块指数日线（同花顺 ``stock_board_industry_index_ths``）。

        板块三档要求**多日序列**（需求 §4.6：单日涨跌幅不得判档），一览表只有当日值，
        因此这里按板块名单独取指数日线。

        **要防「整年静默缺失」**：该接口内部按 ``{year}.js`` 逐年下载，某年请求失败时
        它**自己 ``continue`` 掉、不抛异常**，于是返回一份看不出缺口的序列
        （2026-11-27 实测：2023–2026 一次性请求，银行少 2023 年 31 根、
        生物制品少 2024 年 242 根，接口却返回成功）。所以这里先整体请求一次，再按
        「结果里一根都没有的年份」逐年补一次，补不到就写日志并按缺失处理——
        宁可在报告里标 degraded，也不要一份自洽却缺一年的序列。

        返回空元组 = 「这次取不到」（网络失败 / 板块无指数 / 区间内无数据），三者对
        三档判定是同一件事——调用方按 ``degraded`` 处理，**不猜档位**。
        """
        if not name:
            return ()
        if start > end:
            raise ValueError(f"起始日 {start} 晚于结束日 {end}")
        bars = self._fetch_sector_index_year(name, start, end)
        collected: dict[date, Bar] = {bar.trade_date: bar for bar in bars}
        # 整年静默缺失才补：先按区间整体请求（正常情况只打一次），再按「结果里没有的年份」
        # 逐年重试。无条件分年会让每个板块打 4×4 次请求，反而容易被限流。
        covered = {bar.trade_date.year for bar in bars}
        for year in range(start.year, end.year + 1):
            if year in covered:
                continue
            seg_start = max(start, date(year, 1, 1))
            seg_end = min(end, date(year, 12, 31))
            for attempt in range(2):
                if attempt:
                    time.sleep(0.5)
                seg = self._fetch_sector_index_year(name, seg_start, seg_end)
                if seg:
                    for bar in seg:
                        collected[bar.trade_date] = bar
                    break
            else:
                logger.warning(
                    "板块指数 %s 的 %d 年取不到（接口按年分文件、失败会静默跳过），该段按缺失处理",
                    name,
                    year,
                )
        return tuple(sorted(collected.values(), key=lambda bar: bar.trade_date))

    def _fetch_sector_index_year(self, name: str, start: date, end: date) -> tuple[Bar, ...]:
        """单年区间取板块指数；失败返回空元组（由调用方重试/计缺失）。"""
        import akshare as ak  # noqa: PLC0415

        try:
            frame = ak.stock_board_industry_index_ths(
                symbol=name,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
            )
            raw = _records(frame)
        except Exception as exc:  # noqa: BLE001 - 单板块失败只降级该板块
            logger.warning("板块指数 %s 取数失败：%s: %s", name, type(exc).__name__, exc)
            return ()
        if not raw:
            return ()
        bars, dropped = normalize_rows(raw, source_tag=SourceTag.D1)
        kept = tuple(bar for bar in bars if start <= bar.trade_date <= end)
        if dropped:
            logger.debug("板块指数 %s 丢弃 %d 行无效数据", name, dropped)
        return kept

    def fetch_sectors(self) -> SectorOverview:  # pragma: no cover - 真实依赖
        """同花顺行业板块一览：汇总（涨跌幅/涨跌家数/领涨股）+ 名称→代码映射。

        实测口径（2026-11-24）：``stock_board_industry_summary_ths`` 提供
        ``板块/涨跌幅/上涨家数/下跌家数/领涨股/领涨股-涨跌幅``；代码需由
        ``stock_board_industry_name_ths`` 的 ``name→code`` 映射补齐。
        单一来源，无等价备源 → 失败即 ``degraded``，不做推测。
        """
        import akshare as ak  # noqa: PLC0415

        try:
            summary = _records(ak.stock_board_industry_summary_ths())
            name_map = _records(ak.stock_board_industry_name_ths())
        except Exception as exc:  # noqa: BLE001
            return SectorOverview(rows=(), source_tag=SourceTag.D3, source_name="ths", degraded=True,
                                  note=f"板块数据取数失败：{type(exc).__name__}")
        code_of = {str(row.get("name") or row.get("板块")): str(row.get("code") or "") for row in name_map}
        rows: list[SectorRow] = []
        for row in summary:
            name = str(row.get("板块") or "")
            if not name:
                continue
            rows.append(
                SectorRow(
                    sector_code=code_of.get(name, ""),
                    sector_name=name,
                    pct_change=_to_float(row.get("涨跌幅")) or 0.0,
                    up_count=_to_int(row.get("上涨家数")),
                    down_count=_to_int(row.get("下跌家数")),
                    lead_stock=str(row.get("领涨股") or ""),
                    lead_pct=_to_float(row.get("领涨股-涨跌幅")),
                )
            )
        missing_code = sum(1 for row in rows if not row.sector_code)
        note = f"{len(rows)} 个板块" + (f"，其中 {missing_code} 个缺代码（映射未命中）" if missing_code else "")
        return SectorOverview(rows=tuple(rows), source_tag=SourceTag.D1, source_name="ths",
                              degraded=missing_code > 0, note=note)


def _to_int(raw: Any) -> int | None:
    val = _to_float(raw)
    return None if val is None else int(val)


# ---------------------------------------------------------------------------
# 派生：后复权序列（L2 内存，不落库）
# ---------------------------------------------------------------------------
def post_adjusted_closes(bars: Sequence[Bar]) -> list[float]:
    """后复权收盘序列（需求 §4.6.4）：``raw_close × cum_adj_factor``，仅用于指标与回测。"""
    return [bar.close * (bar.adj_factor or 1.0) for bar in bars]


def derive_adj_factor_from_qfq(raw: Sequence[Bar], qfq_rows: Sequence[Mapping[str, Any]]) -> list[Bar]:
    """由「同源不复权 + 前复权」比值推算累积复权因子（M15 兜底路径）。

    ``factor[t] = qfq_close[t] / raw_close[t]``。**只是推算因子，不把前复权价落库**；
    调用方须把结果标为 D2 并在快照留痕。取不到对应日期的行时因子保持原值。
    """
    qfq_close: dict[date, float] = {}
    for row in qfq_rows:
        day = _to_date(_pick(row, "date"))
        val = _to_float(_pick(row, "close"))
        if day is not None and val and val > 0:
            qfq_close[day] = val
    out: list[Bar] = []
    for bar in raw:
        factor = bar.adj_factor
        target = qfq_close.get(bar.trade_date)
        if target and bar.close > 0:
            factor = round(target / bar.close, 6)
        out.append(
            Bar(
                trade_date=bar.trade_date,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                amount=bar.amount,
                adj_factor=factor,
                source_tag=SourceTag.D2,
            )
        )
    return out
