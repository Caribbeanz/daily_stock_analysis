# -*- coding: utf-8 -*-
"""L1–L4 读写实现（技术方案 §3.2 / §3.3）。

三条硬约束：
1. **append-only**：``assistant_snapshot`` 及其子表只插入不更新；同日重跑生成新 ``snapshot_id``。
2. **L1 幂等**：``(code, trade_date)`` 冲突即重写该行（补洞/修正数据不产生重复）。
3. **L2 不落库**：周/月线是**聚合结果**（可重建），派生的后复权序列一律不写库。

存储与现有主流程共用同一个 SQLite 文件，但表名前缀 ``assistant_``、独立 metadata，
不改动 ``src/storage.py`` 的任何表与读写链。
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.assistant.config.overrides import ParamChange
from src.assistant.data.models import (
    AssistantBase,
    ConditionalPlanRow,
    DividendEventRow,
    DeliveryLogRow,
    HoldingConclusionRow,
    InboundLogRow,
    LlmTextCacheRow,
    MarketSnapshotRow,
    NoteRow,
    ParamOverrideRow,
    PortfolioSettingRow,
    RiskSignalRow,
    SectorDaily,
    SectorSnapshotRow,
    Snapshot,
    StageStateRow,
    StockDailyRaw,
    StockMonthly,
    StockWeekly,
    UserHolding,
    UserNote,
    WatchlistRuntime,
)
from src.assistant.data.dividend import DividendEvent
from src.assistant.domain import Bar, GuardChainResult
from src.assistant.enums import SourceTag

logger = logging.getLogger(__name__)


class AppendOnlyViolation(RuntimeError):
    """试图覆盖已存在的快照（违反 append-only）。"""


def default_db_path() -> Path:
    """与主流程一致的数据库文件（``data/stock_analysis.db``）。"""
    return Path(__file__).resolve().parents[3] / "data" / "stock_analysis.db"


class AssistantStore:
    """assistant 模块的存储入口。"""

    def __init__(self, db_path: str | Path | None = None, engine: Engine | None = None) -> None:
        if engine is not None:
            self._engine = engine
        else:
            path = Path(db_path) if db_path is not None else default_db_path()
            if path != Path(":memory:"):
                path.parent.mkdir(parents=True, exist_ok=True)
            self._engine = create_engine(f"sqlite:///{path}", future=True)
            if str(path) != ":memory:":
                from sqlalchemy import event

                @event.listens_for(self._engine, "connect")
                def _pragma(dbapi_conn, _record):  # pragma: no cover - 连接级设置
                    cur = dbapi_conn.cursor()
                    cur.execute("PRAGMA journal_mode=WAL")
                    cur.execute("PRAGMA foreign_keys=ON")
                    cur.close()

        self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False, future=True)

    # ------------------------------------------------------------------
    # 建表
    # ------------------------------------------------------------------
    def create_all(self) -> None:
        """仅创建本模块的表（不触碰主流程表）。

        ``create_all`` 只建表、**不给已有表补列**，而 ``assistant_dividend_event`` /
        ``assistant_sector_daily`` 都是早前预留的表（当时没有分红明细、也没有板块指数
        OHLC），老开发库直接写入会报「no column」。这里对这两张表做一次**追加列**迁移：
        只加缺的列，不删不改，非 SQLite 方言直接跳过。
        """
        AssistantBase.metadata.create_all(self._engine)
        self._ensure_dividend_columns()
        self._ensure_sector_columns()

    def _ensure_sector_columns(self) -> None:
        """给 ``assistant_sector_daily`` 补板块指数 OHLC（老库建表时只有当日涨跌）。"""
        from sqlalchemy import inspect, text

        inspector = inspect(self._engine)
        if "assistant_sector_daily" not in inspector.get_table_names():
            return
        existing = {col["name"] for col in inspector.get_columns("assistant_sector_daily")}
        wanted = {
            "open": "FLOAT",
            "high": "FLOAT",
            "low": "FLOAT",
            "close": "FLOAT",
            "volume": "FLOAT",
            "amount": "FLOAT",
        }
        missing = {name: ddl for name, ddl in wanted.items() if name not in existing}
        if not missing:
            return
        if self._engine.dialect.name != "sqlite":  # pragma: no cover - 当前只用 SQLite
            logger.warning("表 assistant_sector_daily 缺列 %s，非 SQLite 请手动迁移", list(missing))
            return
        with self._engine.begin() as conn:
            for name, ddl in missing.items():
                conn.execute(text(f"ALTER TABLE assistant_sector_daily ADD COLUMN {name} {ddl}"))
        logger.info("已为 assistant_sector_daily 补列：%s", list(missing))

    def _ensure_dividend_columns(self) -> None:
        from sqlalchemy import inspect, text

        inspector = inspect(self._engine)
        if "assistant_dividend_event" not in inspector.get_table_names():
            return
        existing = {col["name"] for col in inspector.get_columns("assistant_dividend_event")}
        wanted = {
            "cash_per_share": "FLOAT NOT NULL DEFAULT 0",
            "bonus_per_share": "FLOAT NOT NULL DEFAULT 0",
            "transfer_per_share": "FLOAT NOT NULL DEFAULT 0",
            "plan": "VARCHAR(128)",
            "updated_at": "DATETIME",
        }
        missing = {name: ddl for name, ddl in wanted.items() if name not in existing}
        if not missing:
            return
        if self._engine.dialect.name != "sqlite":  # pragma: no cover - 当前只用 SQLite
            logger.warning("表 assistant_dividend_event 缺列 %s，非 SQLite 请手动迁移", list(missing))
            return
        with self._engine.begin() as conn:
            for name, ddl in missing.items():
                conn.execute(text(f"ALTER TABLE assistant_dividend_event ADD COLUMN {name} {ddl}"))
        logger.info("已为 assistant_dividend_event 补列：%s", list(missing))

    @contextmanager
    def session(self) -> Iterator[Session]:
        sess = self._session_factory()
        try:
            yield sess
            sess.commit()
        except Exception:
            sess.rollback()
            raise
        finally:
            sess.close()

    # ------------------------------------------------------------------
    # L1 日线
    # ------------------------------------------------------------------
    def save_bars(self, code: str, bars: Sequence[Bar]) -> int:
        """幂等写入日线（不复权），返回写入行数。"""
        if not bars:
            return 0
        now = datetime.now()
        rows = [
            {
                "code": code,
                "trade_date": bar.trade_date,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "amount": bar.amount,
                "adj_factor": bar.adj_factor,
                "source_tag": bar.source_tag.value,
                "updated_at": now,
            }
            for bar in bars
        ]
        with self.session() as sess:
            stmt = sqlite_insert(StockDailyRaw).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["code", "trade_date"],
                set_={
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "volume": stmt.excluded.volume,
                    "amount": stmt.excluded.amount,
                    "adj_factor": stmt.excluded.adj_factor,
                    "source_tag": stmt.excluded.source_tag,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            sess.execute(stmt)
        return len(rows)

    def load_bars(
        self,
        code: str,
        *,
        start: date | None = None,
        end: date | None = None,
        limit: int | None = None,
    ) -> list[Bar]:
        """按日期升序读取日线；``limit`` 取**最后** N 根。"""
        with self.session() as sess:
            stmt = select(StockDailyRaw).where(StockDailyRaw.code == code)
            if start is not None:
                stmt = stmt.where(StockDailyRaw.trade_date >= start)
            if end is not None:
                stmt = stmt.where(StockDailyRaw.trade_date <= end)
            if limit is not None:
                stmt = stmt.order_by(StockDailyRaw.trade_date.desc()).limit(limit)
                rows = list(reversed(sess.execute(stmt).scalars().all()))
            else:
                rows = list(sess.execute(stmt.order_by(StockDailyRaw.trade_date.asc())).scalars().all())
        return [
            Bar(
                trade_date=row.trade_date,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                amount=row.amount or 0.0,
                adj_factor=row.adj_factor or 1.0,
                source_tag=SourceTag(row.source_tag) if row.source_tag else SourceTag.D1,
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # L1 板块指数日线（回放要按板块序列复算三档，光有当日涨跌幅不够）
    # ------------------------------------------------------------------
    def save_sector_bars(
        self,
        sector_code: str,
        sector_name: str,
        bars: Sequence[Bar],
        *,
        source_tag: str = "D1",
    ) -> int:
        """幂等写入板块指数日线，返回写入行数。

        **必须给板块代码**：表唯一键是 ``(sector_code, trade_date)``，没有代码的板块
        写进去会互相覆盖成同一个空代码——宁可不写并抛错，也不留一份无法区分的数据。
        """
        code = (sector_code or "").strip()
        if not code:
            raise ValueError(f"板块 {sector_name!r} 没有代码，无法写入板块日线（唯一键缺一半）")
        if not bars:
            return 0
        rows = [
            {
                "sector_code": code,
                "sector_name": sector_name or "",
                "trade_date": bar.trade_date,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "amount": bar.amount,
                "source_tag": source_tag,
            }
            for bar in bars
        ]
        with self.session() as sess:
            stmt = sqlite_insert(SectorDaily).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["sector_code", "trade_date"],
                set_={
                    "sector_name": stmt.excluded.sector_name,
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "volume": stmt.excluded.volume,
                    "amount": stmt.excluded.amount,
                    "source_tag": stmt.excluded.source_tag,
                },
            )
            sess.execute(stmt)
        return len(rows)

    def load_sector_bars(
        self,
        sector_code: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[Bar]:
        """按日期升序读取某板块的指数日线；没有 OHLC 的老行（只有当日涨跌）**不返回**。

        这不是丢数据：没有收盘价就算不出 20 日均线与 5 日斜率，硬塞 0 会让三档失真；
        调用方按「该板块序列缺失」处理，与取数失败同一路径。
        """
        with self.session() as sess:
            stmt = select(SectorDaily).where(
                SectorDaily.sector_code == sector_code, SectorDaily.close.is_not(None)
            )
            if start is not None:
                stmt = stmt.where(SectorDaily.trade_date >= start)
            if end is not None:
                stmt = stmt.where(SectorDaily.trade_date <= end)
            rows = list(sess.execute(stmt.order_by(SectorDaily.trade_date.asc())).scalars().all())
        return [
            Bar(
                trade_date=row.trade_date,
                open=row.open if row.open is not None else row.close,
                high=row.high if row.high is not None else row.close,
                low=row.low if row.low is not None else row.close,
                close=row.close,
                volume=row.volume or 0.0,
                amount=row.amount or 0.0,
                source_tag=SourceTag(row.source_tag) if row.source_tag else SourceTag.D1,
            )
            for row in rows
        ]

    def load_sector_bars_by_name(
        self, *, start: date | None = None, end: date | None = None
    ) -> dict[str, list[Bar]]:
        """``{板块名: 板块指数日线}``，一次读完供回放按名字取序列。

        同一板块名若对应多个代码（同花顺偶有重名），**取行数最多的那个**并留日志：
        回放的映射是按板块名给的，重名时猜错会让三档整体偏移。
        """
        with self.session() as sess:
            stmt = select(SectorDaily).where(SectorDaily.close.is_not(None))
            if start is not None:
                stmt = stmt.where(SectorDaily.trade_date >= start)
            if end is not None:
                stmt = stmt.where(SectorDaily.trade_date <= end)
            rows = list(sess.execute(stmt.order_by(SectorDaily.trade_date.asc())).scalars().all())

        grouped: dict[str, dict[str, list[SectorDaily]]] = {}
        for row in rows:
            name = row.sector_name or ""
            if not name:
                continue
            grouped.setdefault(name, {}).setdefault(row.sector_code, []).append(row)

        out: dict[str, list[Bar]] = {}
        for name, by_code in grouped.items():
            code = max(by_code, key=lambda key: len(by_code[key]))
            if len(by_code) > 1:
                logger.warning(
                    "板块名 %r 对应多个代码 %s，按行数较多的 %s 取序列",
                    name,
                    "/".join(sorted(by_code)),
                    code,
                )
            out[name] = [
                Bar(
                    trade_date=row.trade_date,
                    open=row.open if row.open is not None else row.close,
                    high=row.high if row.high is not None else row.close,
                    low=row.low if row.low is not None else row.close,
                    close=row.close,
                    volume=row.volume or 0.0,
                    amount=row.amount or 0.0,
                    source_tag=SourceTag(row.source_tag) if row.source_tag else SourceTag.D1,
                )
                for row in by_code[code]
            ]
        return out

    def sector_code_map(self) -> dict[str, str]:
        """``{板块名: 板块代码}``（只收有板块指数日线的板块），供离线映射用。"""
        with self.session() as sess:
            rows = sess.execute(
                select(SectorDaily.sector_name, SectorDaily.sector_code)
                .where(SectorDaily.close.is_not(None))
                .distinct()
            ).all()
        return {str(name): str(code) for name, code in rows if name and code}

    # ------------------------------------------------------------------
    # 除权除息事件（原始数据；复权序列按 §3.3 不落库）
    # ------------------------------------------------------------------
    def save_dividend_events(self, code: str, events: Sequence[DividendEvent]) -> int:
        """幂等写入某只标的的除权事件，返回写入行数。"""
        if not events:
            return 0
        now = datetime.now()
        rows = [
            {
                "code": code,
                "ex_date": event.ex_date,
                "cash_per_share": event.cash_per_share,
                "bonus_per_share": event.bonus_per_share,
                "transfer_per_share": event.transfer_per_share,
                "plan": event.plan,
                "record_date": event.record_date,
                "source_tag": "D1",
                "updated_at": now,
            }
            for event in events
        ]
        with self.session() as sess:
            stmt = sqlite_insert(DividendEventRow).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["code", "ex_date"],
                set_={
                    "cash_per_share": stmt.excluded.cash_per_share,
                    "bonus_per_share": stmt.excluded.bonus_per_share,
                    "transfer_per_share": stmt.excluded.transfer_per_share,
                    "plan": stmt.excluded.plan,
                    "record_date": stmt.excluded.record_date,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            sess.execute(stmt)
        return len(rows)

    def load_dividend_events(
        self, codes: Sequence[str] | None = None
    ) -> dict[str, list[DividendEvent]]:
        """读取除权事件，返回 ``{代码: [事件...]}``（按除权日升序）。"""
        with self.session() as sess:
            stmt = select(DividendEventRow)
            if codes is not None:
                wanted = list(codes)
                if not wanted:
                    return {}
                stmt = stmt.where(DividendEventRow.code.in_(wanted))
            rows = list(sess.execute(stmt.order_by(DividendEventRow.ex_date.asc())).scalars().all())
        out: dict[str, list[DividendEvent]] = {}
        for row in rows:
            out.setdefault(row.code, []).append(
                DividendEvent(
                    code=row.code,
                    ex_date=row.ex_date,
                    cash_per_share=row.cash_per_share or 0.0,
                    bonus_per_share=row.bonus_per_share or 0.0,
                    transfer_per_share=row.transfer_per_share or 0.0,
                    plan=row.plan or "",
                    record_date=row.record_date,
                )
            )
        return out

    def last_trade_date(self, code: str) -> date | None:
        with self.session() as sess:
            return sess.execute(select(func.max(StockDailyRaw.trade_date)).where(StockDailyRaw.code == code)).scalar()

    def bar_count(self, code: str) -> int:
        with self.session() as sess:
            return int(sess.execute(select(func.count()).select_from(StockDailyRaw).where(StockDailyRaw.code == code)).scalar() or 0)

    def gap_days(self, code: str, expected: date, trading_days: Sequence[date] | None = None) -> int:
        """缺口天数（需求 §4.6.3）：本地最后一日 → ``expected`` 之间缺失的交易日。"""
        last = self.last_trade_date(code)
        if last is None or last >= expected:
            return 0
        if trading_days:
            return len([d for d in trading_days if last < d <= expected])
        return (expected - last).days

    # ------------------------------------------------------------------
    # 周线 / 月线（L1 聚合，可重建）
    # ------------------------------------------------------------------
    def rebuild_weekly_monthly(self, code: str) -> tuple[int, int]:
        """按 ISO 周 / 自然月重建周线与月线，返回 ``(周线根数, 月线根数)``。"""
        bars = self.load_bars(code)
        if not bars:
            return (0, 0)
        weeks: dict[str, list[Bar]] = {}
        months: dict[str, list[Bar]] = {}
        for bar in bars:
            iso_year, iso_week, _ = bar.trade_date.isocalendar()
            weeks.setdefault(f"{iso_year}-W{iso_week:02d}", []).append(bar)
            months.setdefault(bar.trade_date.strftime("%Y-%m"), []).append(bar)

        with self.session() as sess:
            sess.execute(delete(StockWeekly).where(StockWeekly.code == code))
            sess.execute(delete(StockMonthly).where(StockMonthly.code == code))
            for key, group in weeks.items():
                sess.add(
                    StockWeekly(
                        code=code,
                        iso_week=key,
                        week_end_date=group[-1].trade_date,
                        open=group[0].open,
                        high=max(b.high for b in group),
                        low=min(b.low for b in group),
                        close=group[-1].close,
                        volume=sum(b.volume for b in group),
                    )
                )
            for key, group in months.items():
                sess.add(
                    StockMonthly(
                        code=code,
                        year_month=key,
                        month_end_date=group[-1].trade_date,
                        open=group[0].open,
                        high=max(b.high for b in group),
                        low=min(b.low for b in group),
                        close=group[-1].close,
                        volume=sum(b.volume for b in group),
                    )
                )
        return (len(weeks), len(months))

    # ------------------------------------------------------------------
    # L3 快照（append-only）
    # ------------------------------------------------------------------
    def write_snapshot(self, result: GuardChainResult, *, preset: str = "standard", notes: str = "") -> str:
        """写入**单标的**快照（兼容入口，等价 ``create_snapshot`` + ``write_conclusion``）。"""
        snapshot_id = self.create_snapshot(
            data_date=result.data_date,
            calc_version=result.calc_version,
            param_hash=result.param_hash,
            preset=preset,
            generated_at=result.generated_at,
            bars_used=result.data_quality.bars_used,
            gap_days=result.data_quality.gap_days,
            source_tag="D3" if result.conclusion.degraded else "D1",
            notes=notes,
            bar_date=result.data_date,
        )
        self.write_conclusion(snapshot_id, result)
        return snapshot_id

    def create_snapshot(
        self,
        *,
        data_date: date,
        calc_version: str,
        param_hash: str,
        preset: str,
        generated_at: datetime,
        bars_used: int,
        gap_days: int,
        source_tag: str,
        notes: str = "",
        bar_date: date | None = None,
    ) -> str:
        """只建快照主行，返回新的 ``snapshot_id``（append-only，同日重跑即新 id）。"""
        snapshot_id = f"{data_date:%Y%m%d}-{uuid.uuid4().hex[:12]}"
        with self.session() as sess:
            if sess.get(Snapshot, snapshot_id) is not None:  # pragma: no cover - uuid 碰撞
                raise AppendOnlyViolation(f"快照已存在：{snapshot_id}")
            sess.add(
                Snapshot(
                    snapshot_id=snapshot_id,
                    data_date=data_date,
                    bar_date=bar_date or data_date,
                    calc_version=calc_version,
                    param_hash=param_hash,
                    param_preset=preset,
                    generated_at=generated_at,
                    bars_used=bars_used,
                    gap_days=gap_days,
                    source_tag=source_tag,
                    notes=notes or None,
                )
            )
        return snapshot_id

    def write_conclusion(self, snapshot_id: str, result: GuardChainResult) -> None:
        """把一只标的的结论 + 条件计划 + 风险信号挂到已存在的快照上。"""
        with self.session() as sess:
            c = result.conclusion
            sess.add(
                HoldingConclusionRow(
                    snapshot_id=snapshot_id,
                    code=c.code,
                    name=c.name,
                    advice=c.advice.value if c.advice else None,
                    advice_display=c.advice_display,
                    advice_state=c.advice_state.value,
                    stage=c.stage.value,
                    stage_confidence=c.stage_confidence.value,
                    stage_gate_hit=json.dumps(c.stage_gate_hit.as_dict(), ensure_ascii=False) if c.stage_gate_hit else None,
                    trend=c.trend,
                    position_tag=c.position_tag,
                    score=c.score,
                    is_holding=c.is_holding,
                    est_level=c.est_level,
                    div_yield=c.div_yield,
                    pnl_pct=c.pnl_pct,
                    last_close=c.last_close,
                    pct_change=c.pct_change,
                    key_levels=json.dumps(
                        [
                            {"price": lv.price, "kind": lv.kind, "source": lv.source,
                             "detail": dict(lv.detail)}
                            for lv in (result.key_levels or ())
                        ],
                        ensure_ascii=False,
                        default=str,
                    )
                    if result.key_levels
                    else None,
                    position_tier=getattr(result.position_advice, "tier", None),
                    position_pct=getattr(result.position_advice, "capped_pct", None),
                    position_reasons=json.dumps(
                        list(getattr(result.position_advice, "cap_reasons", ())), ensure_ascii=False
                    ),
                    risk_level=c.risk_level.value,
                    human_text=c.human_text,
                    degraded=c.degraded,
                )
            )
            for branch in result.plans:
                sess.add(
                    ConditionalPlanRow(
                        snapshot_id=snapshot_id,
                        code=result.code,
                        branch_id=branch.branch_id,
                        scenario=branch.scenario,
                        trigger_type=branch.trigger_type.value,
                        trigger_price=branch.trigger_price,
                        pct_change=branch.pct_change,
                        action=branch.action.value,
                        shares=branch.shares,
                        est_amount=branch.est_amount,
                        pct_of_total=branch.pct_of_total,
                        valid_days=branch.valid_days,
                        settable=branch.settable,
                        notes=branch.notes,
                        technical_basis=branch.technical_basis,
                        position_check=json.dumps(dict(branch.position_check), ensure_ascii=False, default=str),
                        is_probe=branch.is_probe,
                    )
                )
            for risk in result.risks:
                sess.add(
                    RiskSignalRow(
                        snapshot_id=snapshot_id,
                        risk_id=risk.risk_id,
                        code=risk.stock_code or result.code,
                        risk_code=risk.code.value,
                        category=risk.category.value,
                        level=risk.level.value,
                        trigger_basis=risk.trigger_basis,
                        data_date=risk.data_date,
                        affects_advice=risk.affects_advice,
                        action=risk.action,
                        human_text=risk.human_text,
                        source_tag=risk.source_tag.value,
                        degraded=risk.degraded,
                    )
                )

    def write_day_snapshot(
        self,
        results: Sequence[GuardChainResult],
        *,
        preset: str = "standard",
        notes: str = "",
        generated_at: datetime | None = None,
        source_tag: str | None = None,
        data_date: date | None = None,
    ) -> str:
        """**一天一份**快照：多只标的的结论写进同一个 ``snapshot_id``。

        需求 §4.6 的「快照」是当日整体，不是「一只标的一份」。快照级字段取最差口径：
        最少 ``bars_used``、最大 ``gap_days``、任一降级即 ``D3``，避免个别标的数据不全
        被快照级数字掩盖。
        """
        items = list(results)
        if not items:
            raise ValueError("write_day_snapshot 需要至少一只标的的结论")
        first = items[0]
        for item in items:
            if item.param_hash != first.param_hash or item.calc_version != first.calc_version:
                raise AppendOnlyViolation(
                    "同一天快照内 param_hash / calc_version 必须一致："
                    f"{item.code} 为 {item.param_hash}/{item.calc_version}，"
                    f"基准为 {first.param_hash}/{first.calc_version}"
                )
            if item.data_date != first.data_date:
                raise AppendOnlyViolation(
                    f"同一天快照内 data_date 必须一致：{item.code} 为 {item.data_date}，基准为 {first.data_date}"
                )
        snapshot_id = self.create_snapshot(
            # data_date = 快照所属交易日（调用方给的生成日）；bar_date = 数据截止日
            data_date=data_date or first.data_date,
            bar_date=first.data_date,
            calc_version=first.calc_version,
            param_hash=first.param_hash,
            preset=preset,
            generated_at=generated_at or first.generated_at,
            bars_used=min(item.data_quality.bars_used for item in items),
            gap_days=max(item.data_quality.gap_days for item in items),
            source_tag=source_tag
            or ("D3" if any(item.conclusion.degraded for item in items) else "D1"),
            notes=notes,
        )
        for item in items:
            self.write_conclusion(snapshot_id, item)
        return snapshot_id

    def find_snapshot(
        self,
        data_date: date,
        *,
        param_hash: str | None = None,
        calc_version: str | None = None,
    ) -> dict | None:
        """按日 + 规则指纹找快照（幂等生成用：同参数同日不重复算）。"""
        with self.session() as sess:
            stmt = select(Snapshot).where(Snapshot.data_date == data_date)
            if param_hash is not None:
                stmt = stmt.where(Snapshot.param_hash == param_hash)
            if calc_version is not None:
                stmt = stmt.where(Snapshot.calc_version == calc_version)
            row = sess.execute(stmt.order_by(Snapshot.generated_at.desc()).limit(1)).scalars().first()
            return None if row is None else _snapshot_row(row)

    def previous_snapshot(self, before: date) -> dict | None:
        """``before`` 之前最近的一份快照（T 日复盘要对照 T-1 的 `snapshot_id`）。"""
        with self.session() as sess:
            row = sess.execute(
                select(Snapshot)
                .where(Snapshot.data_date < before)
                .order_by(Snapshot.data_date.desc(), Snapshot.generated_at.desc())
                .limit(1)
            ).scalars().first()
            return None if row is None else _snapshot_row(row)

    def snapshot_content(self, snapshot_id: str) -> dict | None:
        """读回一份快照的全部子行，供渲染与校验（**投递文本一律从库里回读后再渲染**）。"""
        with self.session() as sess:
            snap = sess.get(Snapshot, snapshot_id)
            if snap is None:
                return None
            conclusions = sess.execute(
                select(HoldingConclusionRow).where(HoldingConclusionRow.snapshot_id == snapshot_id)
                .order_by(HoldingConclusionRow.code.asc())
            ).scalars().all()
            plans = sess.execute(
                select(ConditionalPlanRow).where(ConditionalPlanRow.snapshot_id == snapshot_id)
                .order_by(ConditionalPlanRow.code.asc(), ConditionalPlanRow.branch_id.asc())
            ).scalars().all()
            risks = sess.execute(
                select(RiskSignalRow).where(RiskSignalRow.snapshot_id == snapshot_id)
                .order_by(RiskSignalRow.code.asc(), RiskSignalRow.risk_id.asc())
            ).scalars().all()
        return {
            "snapshot_id": snap.snapshot_id,
            "data_date": snap.data_date.isoformat(),
            "bar_date": (snap.bar_date or snap.data_date).isoformat(),
            "calc_version": snap.calc_version,
            "param_hash": snap.param_hash,
            "param_preset": snap.param_preset,
            "generated_at": snap.generated_at.isoformat(),
            "bars_used": snap.bars_used,
            "gap_days": snap.gap_days,
            "source_tag": snap.source_tag,
            "conclusions": [_conclusion_row(row) for row in conclusions],
            "plans": [_plan_row(row) for row in plans],
            "risks": [_risk_row(row) for row in risks],
        }

    def snapshot_market(self, snapshot_id: str) -> dict | None:
        """读大盘快照；未写入时返回 None（API 层如实标「不可用」，不编造）。"""
        with self.session() as sess:
            row = sess.execute(
                select(MarketSnapshotRow).where(MarketSnapshotRow.snapshot_id == snapshot_id)
                .order_by(MarketSnapshotRow.id.desc()).limit(1)
            ).scalars().first()
            if row is None:
                return None
            return {
                "index_code": row.index_code,
                "index_name": row.index_name,
                "pct_change": row.pct_change,
                "market_regime": row.market_regime,
                "regime_cells": row.regime_cells,
                "width": row.width,
                "degraded": bool(row.degraded),
            }

    def save_market_snapshot(
        self,
        snapshot_id: str,
        *,
        index_code: str,
        index_name: str = "",
        pct_change: float | None = None,
        market_regime: str = "中性",
        regime_cells: str | None = None,
        width: float | None = None,
        degraded: bool = False,
    ) -> bool:
        """写入大盘快照；同一 ``snapshot_id`` 已存在则跳过（幂等，不改写历史）。

        返回是否真的写入。**不可用时不写**——宁缺不造，读取方按「未写入」处理。
        """
        with self.session() as sess:
            exists = sess.execute(
                select(MarketSnapshotRow.id).where(MarketSnapshotRow.snapshot_id == snapshot_id).limit(1)
            ).first()
            if exists is not None:
                return False
            sess.add(
                MarketSnapshotRow(
                    snapshot_id=snapshot_id,
                    index_code=index_code,
                    index_name=index_name,
                    pct_change=pct_change,
                    market_regime=market_regime,
                    regime_cells=regime_cells,
                    width=width,
                    degraded=degraded,
                )
            )
            return True

    def snapshot_sectors(self, snapshot_id: str) -> list[dict]:
        """按**写入顺序**读回：调用方按「最强 → 最弱」写，页面就按这个顺序展示。

        不用 ``sector_code`` 排序——那会把最强和最弱按代码打散，看着像随机顺序。
        """
        with self.session() as sess:
            rows = sess.execute(
                select(SectorSnapshotRow).where(SectorSnapshotRow.snapshot_id == snapshot_id)
                .order_by(SectorSnapshotRow.id.asc())
            ).scalars().all()
            return [
                {
                    "sector_code": row.sector_code,
                    "sector_name": row.sector_name,
                    "pct_change": row.pct_change,
                    "sector_strength": row.sector_strength,
                    "rs_label": row.rs_label,
                    "rs_t": row.rs_t,
                    "up_count": row.up_count,
                    "down_count": row.down_count,
                    "lead_stock": row.lead_stock,
                    "as_of": row.as_of.isoformat() if row.as_of else None,
                    "degraded": bool(row.degraded),
                }
                for row in rows
            ]

    def save_sector_snapshots(self, snapshot_id: str, rows: Sequence[Mapping[str, Any]]) -> int:
        """批量写板块快照；同一 ``snapshot_id`` 已有行则跳过（幂等，不改写历史）。

        返回实际写入行数。``rows`` 为空不写——「宁缺不造」：取数失败时宁可让
        ``/sectors`` 返回空列表，也不覆盖成一批中性档。
        """
        if not rows:
            return 0
        with self.session() as sess:
            exists = sess.execute(
                select(SectorSnapshotRow.id).where(SectorSnapshotRow.snapshot_id == snapshot_id).limit(1)
            ).first()
            if exists is not None:
                return 0
            written = 0
            for row in rows:
                code = str(row.get("sector_code") or "").strip()
                if not code:
                    # 没有板块代码的行无法做锚点与历史聚合，跳过并计数（由调用方记 degraded）
                    continue
                sess.add(
                    SectorSnapshotRow(
                        snapshot_id=snapshot_id,
                        sector_code=code,
                        sector_name=str(row.get("sector_name") or ""),
                        pct_change=row.get("pct_change"),
                        sector_strength=row.get("sector_strength"),
                        rs_label=row.get("rs_label"),
                        rs_t=row.get("rs_t"),
                        up_count=row.get("up_count"),
                        down_count=row.get("down_count"),
                        lead_stock=str(row.get("lead_stock") or ""),
                        as_of=row.get("as_of"),
                        degraded=bool(row.get("degraded")),
                    )
                )
                written += 1
            return written

    # ------------------------------------------------------------------
    # 飞书入站：笔记与写操作审计（需求 §3.6.3 / §3.6.4）
    # ------------------------------------------------------------------
    def save_note(
        self,
        content: str,
        *,
        trade_date: date,
        user_id: str = "",
        code: str | None = None,
        source: str = "feishu",
    ) -> str:
        """存一条笔记（原文照存，不做任何解析或打分）；返回 ``note_id``。"""
        text = (content or "").strip()
        if not text:
            raise ValueError("笔记内容不能为空")
        note_id = f"N{uuid.uuid4().hex[:12]}"
        with self.session() as sess:
            sess.add(
                NoteRow(
                    note_id=note_id,
                    created_at=datetime.now(),
                    user_id=user_id,
                    trade_date=trade_date,
                    code=(code or None),
                    content=text,
                    source=source,
                )
            )
        return note_id

    def list_notes(
        self,
        *,
        trade_date: date | None = None,
        code: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """按日期/标的检索笔记（新→旧）。"""
        with self.session() as sess:
            stmt = select(NoteRow)
            if trade_date is not None:
                stmt = stmt.where(NoteRow.trade_date == trade_date)
            if code:
                stmt = stmt.where(NoteRow.code == code)
            rows = sess.execute(
                stmt.order_by(NoteRow.created_at.desc()).limit(max(1, limit))
            ).scalars().all()
            return [
                {
                    "note_id": row.note_id,
                    "created_at": row.created_at.isoformat(timespec="seconds"),
                    "user_id": row.user_id,
                    "trade_date": row.trade_date.isoformat(),
                    "code": row.code,
                    "content": row.content,
                    "source": row.source,
                }
                for row in rows
            ]

    def log_inbound(
        self,
        *,
        user_id: str,
        command: str,
        action: str | None = None,
        code: str | None = None,
        confirmed: bool = False,
        effective_date: date | None = None,
        before_state: Sequence[str] | None = None,
        after_state: Sequence[str] | None = None,
        raw_text: str | None = None,
    ) -> None:
        """写操作留痕（需求 §3.6.3）：确认与未确认都要留，便于回溯谁在什么时候改了什么。"""
        with self.session() as sess:
            sess.add(
                InboundLogRow(
                    created_at=datetime.now(),
                    user_id=user_id,
                    command=command,
                    code=code,
                    action=action,
                    confirmed=confirmed,
                    effective_date=effective_date,
                    before_state=json.dumps(sorted(before_state or []), ensure_ascii=False),
                    after_state=json.dumps(sorted(after_state or []), ensure_ascii=False),
                    raw_text=(raw_text or "")[:500] or None,
                )
            )

    def list_inbound_log(self, limit: int = 20) -> list[dict]:
        with self.session() as sess:
            rows = sess.execute(
                select(InboundLogRow).order_by(InboundLogRow.id.desc()).limit(max(1, limit))
            ).scalars().all()
            return [
                {
                    "id": row.id,
                    "created_at": row.created_at.isoformat(timespec="seconds"),
                    "user_id": row.user_id,
                    "command": row.command,
                    "code": row.code,
                    "action": row.action,
                    "confirmed": bool(row.confirmed),
                    "effective_date": row.effective_date.isoformat() if row.effective_date else None,
                    "before_state": json.loads(row.before_state) if row.before_state else [],
                    "after_state": json.loads(row.after_state) if row.after_state else [],
                    "raw_text": row.raw_text,
                }
                for row in rows
            ]

    # ------------------------------------------------------------------
    # 跨日阶段防抖状态（需求 §5.14.2「连续 N 日」）
    # ------------------------------------------------------------------
    def load_stage_state(self, code: str) -> dict | None:
        with self.session() as sess:
            row = sess.get(StageStateRow, code)
            if row is None:
                return None
            return {
                "stage": row.stage,
                "pending_stage": row.pending_stage,
                "pending_days": row.pending_days,
                "as_of": None if row.as_of is None else row.as_of.isoformat(),
            }

    def save_stage_state(
        self,
        code: str,
        *,
        stage: str | None,
        pending_stage: str | None,
        pending_days: int,
        as_of: date | None,
        now: datetime | None = None,
    ) -> None:
        """写入/更新防抖状态（这是**状态**不是审计，允许覆盖；历史由快照侧留痕）。"""
        stamp = now or datetime.now()
        with self.session() as sess:
            row = sess.get(StageStateRow, code)
            if row is None:
                sess.add(
                    StageStateRow(
                        code=code,
                        stage=stage,
                        pending_stage=pending_stage,
                        pending_days=pending_days,
                        as_of=as_of,
                        updated_at=stamp,
                    )
                )
            else:
                row.stage = stage
                row.pending_stage = pending_stage
                row.pending_days = pending_days
                row.as_of = as_of
                row.updated_at = stamp

    def snapshot_exists(self, snapshot_id: str) -> bool:
        with self.session() as sess:
            return sess.get(Snapshot, snapshot_id) is not None

    def latest_snapshot(self, data_date: date | None = None) -> dict | None:
        """读最新快照摘要（Web API 用）。"""
        with self.session() as sess:
            stmt = select(Snapshot)
            if data_date is not None:
                stmt = stmt.where(Snapshot.data_date == data_date)
            row = sess.execute(stmt.order_by(Snapshot.generated_at.desc()).limit(1)).scalars().first()
            if row is None:
                return None
            return {
                "snapshot_id": row.snapshot_id,
                "data_date": row.data_date.isoformat(),
                "calc_version": row.calc_version,
                "param_hash": row.param_hash,
                "param_preset": row.param_preset,
                "generated_at": row.generated_at.isoformat(),
                "bars_used": row.bars_used,
                "gap_days": row.gap_days,
                "source_tag": row.source_tag,
            }

    # ------------------------------------------------------------------
    # L4 用户输入
    # ------------------------------------------------------------------
    def save_portfolio_settings(
        self,
        *,
        baseline_preset: str,
        total_capital: float,
        cash_available: float | None = None,
        now: datetime | None = None,
    ) -> dict:
        """追加一条运行设置；当前生效值取最新一行（不覆盖历史）。"""
        if total_capital <= 0:
            raise ValueError("total_capital 必须为正数")
        if cash_available is not None and cash_available < 0:
            raise ValueError("cash_available 不能为负")
        stamp = now or datetime.now()
        with self.session() as sess:
            sess.add(
                PortfolioSettingRow(
                    baseline_preset=baseline_preset,
                    total_capital=float(total_capital),
                    cash_available=None if cash_available is None else float(cash_available),
                    updated_at=stamp,
                )
            )
        return {
            "baseline_preset": baseline_preset,
            "total_capital": float(total_capital),
            "cash_available": None if cash_available is None else float(cash_available),
            "updated_at": stamp.isoformat(),
        }

    def current_portfolio_settings(self) -> dict | None:
        """当前生效的运行设置；从未写入过返回 ``None``（由服务层回退 env）。"""
        with self.session() as sess:
            row = sess.execute(
                select(PortfolioSettingRow)
                .order_by(PortfolioSettingRow.updated_at.desc(), PortfolioSettingRow.id.desc())
                .limit(1)
            ).scalars().first()
            if row is None:
                return None
            return {
                "baseline_preset": row.baseline_preset,
                "total_capital": row.total_capital,
                "cash_available": row.cash_available,
                "updated_at": row.updated_at.isoformat(),
            }

    def record_param_change(self, change: ParamChange) -> None:
        """追加一条参数变更记录（不删除历史）。"""
        row = change.as_row()
        with self.session() as sess:
            sess.add(ParamOverrideRow(**row))

    def current_overrides(self) -> dict[str, object]:
        """按时间顺序重放变更记录，得到当前生效的单项覆盖集合。"""
        with self.session() as sess:
            rows = (
                sess.execute(select(ParamOverrideRow).order_by(ParamOverrideRow.op_time.asc(), ParamOverrideRow.id.asc()))
                .scalars()
                .all()
            )
        state: dict[str, object] = {}
        for row in rows:
            if row.op_type == "rollback":
                state.pop(row.param_key, None)
                continue
            state[row.param_key] = _decode(row.param_value)
        return state

    def param_history(self, key: str | None = None) -> list[dict]:
        with self.session() as sess:
            stmt = select(ParamOverrideRow)
            if key:
                stmt = stmt.where(ParamOverrideRow.param_key == key)
            rows = sess.execute(stmt.order_by(ParamOverrideRow.op_time.asc())).scalars().all()
        return [
            {
                "param_key": r.param_key,
                "param_value": r.param_value,
                "previous_value": r.previous_value,
                "baseline_preset": r.baseline_preset,
                "op_type": r.op_type,
                "op_time": r.op_time.isoformat(),
                "effective_date": r.effective_date.isoformat(),
                "user_id": r.user_id,
                "audit_id": r.audit_id,
            }
            for r in rows
        ]

    def upsert_holding(self, code: str, shares: int, cost_basis: float, buy_date: date | None = None) -> None:
        with self.session() as sess:
            existing = sess.execute(select(UserHolding).where(UserHolding.code == code)).scalars().first()
            if existing is None:
                sess.add(
                    UserHolding(code=code, shares=shares, cost_basis=cost_basis, buy_date=buy_date, recorded_at=datetime.now())
                )
            else:
                existing.shares = shares
                existing.cost_basis = cost_basis
                existing.buy_date = buy_date
                existing.recorded_at = datetime.now()

    def load_holdings(self) -> dict[str, dict]:
        with self.session() as sess:
            rows = sess.execute(select(UserHolding)).scalars().all()
        return {
            r.code: {"shares": r.shares, "cost_basis": r.cost_basis, "buy_date": r.buy_date.isoformat() if r.buy_date else None}
            for r in rows
        }

    def append_note(self, content: str, trade_date: date, code: str | None = None) -> str:
        note_id = f"N{uuid.uuid4().hex[:12]}"
        with self.session() as sess:
            sess.add(
                UserNote(
                    note_id=note_id,
                    trade_date=trade_date,
                    code=code,
                    content=content,
                    created_at=datetime.now(),
                )
            )
        return note_id

    def append_watchlist(self, code: str, action: str, effective_date: date | None = None, user_id: str = "") -> str:
        if action not in ("add", "remove"):
            raise ValueError(f"watchlist action 只能是 add/remove：{action}")
        audit_id = f"W{uuid.uuid4().hex[:12]}"
        with self.session() as sess:
            sess.add(
                WatchlistRuntime(
                    code=code,
                    action=action,
                    op_time=datetime.now(),
                    effective_date=effective_date,
                    user_id=user_id,
                    audit_id=audit_id,
                )
            )
        return audit_id

    def effective_watchlist(self) -> set[str]:
        """重放关注清单运行时写入：add 生效、remove 撤销。"""
        with self.session() as sess:
            rows = sess.execute(select(WatchlistRuntime).where(WatchlistRuntime.revoked.is_(False)).order_by(WatchlistRuntime.op_time.asc())).scalars().all()
        result: set[str] = set()
        for row in rows:
            if row.action == "add":
                result.add(row.code)
            else:
                result.discard(row.code)
        return result

    def load_llm_text(self, snapshot_id: str, position: str, code: str | None = None) -> dict | None:
        """读回一份已缓存文案（含降级文案）；没有返回 ``None``。"""
        with self.session() as sess:
            row = sess.execute(
                select(LlmTextCacheRow).where(
                    LlmTextCacheRow.snapshot_id == snapshot_id,
                    LlmTextCacheRow.position == position,
                    LlmTextCacheRow.code.is_(code) if code is None else LlmTextCacheRow.code == code,
                )
            ).scalars().first()
            if row is None:
                return None
            return {
                "snapshot_id": row.snapshot_id,
                "position": row.position,
                "code": row.code,
                "text": row.text,
                "model": row.model or "",
                "channel": row.channel or "",
                "consistency": row.consistency_check or "",
                "fingerprint": row.fingerprint or "",
                "usage": json.loads(row.usage_json) if row.usage_json else {},
                "generated_at": row.generated_at.isoformat() if row.generated_at else "",
            }

    def save_llm_text(self, snapshot_id: str, position: str, text: str, *, code: str | None = None,
                      model: str = "", channel: str = "", consistency: str = "",
                      fingerprint: str = "", usage: dict | None = None) -> None:
        """写 LLM 文案缓存；同快照同位置已存在则跳过（不覆盖）。"""
        with self.session() as sess:
            exists = sess.execute(
                select(LlmTextCacheRow).where(
                    LlmTextCacheRow.snapshot_id == snapshot_id,
                    LlmTextCacheRow.position == position,
                    LlmTextCacheRow.code.is_(code) if code is None else LlmTextCacheRow.code == code,
                )
            ).scalars().first()
            if exists is not None:
                return
            sess.add(
                LlmTextCacheRow(
                    snapshot_id=snapshot_id,
                    position=position,
                    code=code,
                    text=text,
                    model=model,
                    channel=channel,
                    generated_at=datetime.now(),
                    consistency_check=consistency or None,
                    fingerprint=fingerprint or None,
                    usage_json=json.dumps(usage, ensure_ascii=False) if usage else None,
                )
            )

    # ------------------------------------------------------------------
    # 投递台账（多时点去重 + 同源校验，需求 B50 ③）
    # ------------------------------------------------------------------
    def claim_delivery(
        self,
        slot_key: str,
        *,
        data_date: date,
        snapshot_id: str,
        slot_label: str,
        now: datetime | None = None,
    ) -> bool:
        """领取某时点的投递权。已发送过返回 ``False``；失败过的可再次领取。"""
        stamp = now or datetime.now()
        with self.session() as sess:
            stmt = (
                sqlite_insert(DeliveryLogRow)
                .values(
                    slot_key=slot_key,
                    slot_label=slot_label,
                    data_date=data_date,
                    snapshot_id=snapshot_id,
                    status="claimed",
                    attempts=1,
                    first_attempt_at=stamp,
                    last_attempt_at=stamp,
                )
                .on_conflict_do_nothing(index_elements=["slot_key"])
            )
            inserted = sess.execute(stmt).rowcount == 1
            if inserted:
                return True

            row = sess.execute(
                select(DeliveryLogRow).where(DeliveryLogRow.slot_key == slot_key)
            ).scalars().first()
            if row is None:  # pragma: no cover - 冲突必然对应已有行
                return False
            if row.status == "sent":
                return False
            row.attempts += 1
            row.status = "claimed"
            row.snapshot_id = snapshot_id
            row.last_attempt_at = stamp
            row.error = None
            return True

    def mark_delivery_sent(self, slot_key: str, *, now: datetime | None = None) -> None:
        stamp = now or datetime.now()
        with self.session() as sess:
            row = sess.execute(
                select(DeliveryLogRow).where(DeliveryLogRow.slot_key == slot_key)
            ).scalars().first()
            if row is None:  # pragma: no cover - 调用方必先 claim
                return
            row.status = "sent"
            row.sent_at = stamp
            row.last_attempt_at = stamp
            row.error = None

    def release_delivery(self, slot_key: str, error: str) -> None:
        """发送失败：标记 failed，供下一次触发重试（不删除记录）。"""
        with self.session() as sess:
            row = sess.execute(
                select(DeliveryLogRow).where(DeliveryLogRow.slot_key == slot_key)
            ).scalars().first()
            if row is None:  # pragma: no cover
                return
            row.status = "failed"
            row.error = error[:500]

    def delivery_done(self, slot_key: str) -> bool:
        with self.session() as sess:
            row = sess.execute(
                select(DeliveryLogRow).where(DeliveryLogRow.slot_key == slot_key)
            ).scalars().first()
            return row is not None and row.status == "sent"

    def delivered_snapshot_for(self, data_date: date) -> str | None:
        """当日已成功投递的快照 id（用于「当日快照只生成一次」的同源校验）。"""
        with self.session() as sess:
            row = sess.execute(
                select(DeliveryLogRow)
                .where(DeliveryLogRow.data_date == data_date, DeliveryLogRow.status == "sent")
                .order_by(DeliveryLogRow.sent_at.asc())
                .limit(1)
            ).scalars().first()
            return None if row is None else row.snapshot_id

    def delivery_log(self, data_date: date) -> list[dict]:
        """当日投递台账（排查用）。"""
        with self.session() as sess:
            rows = sess.execute(
                select(DeliveryLogRow)
                .where(DeliveryLogRow.data_date == data_date)
                .order_by(DeliveryLogRow.slot_key.asc())
            ).scalars().all()
        return [
            {
                "slot_key": row.slot_key,
                "slot_label": row.slot_label,
                "snapshot_id": row.snapshot_id,
                "status": row.status,
                "attempts": row.attempts,
                "sent_at": None if row.sent_at is None else row.sent_at.isoformat(),
                "error": row.error,
            }
            for row in rows
        ]


def _snapshot_row(row) -> dict:
    return {
        "snapshot_id": row.snapshot_id,
        "data_date": row.data_date.isoformat(),
        "bar_date": (row.bar_date or row.data_date).isoformat(),
        "calc_version": row.calc_version,
        "param_hash": row.param_hash,
        "param_preset": row.param_preset,
        "generated_at": row.generated_at.isoformat(),
        "bars_used": row.bars_used,
        "gap_days": row.gap_days,
        "source_tag": row.source_tag,
    }


def _conclusion_row(row) -> dict:
    return {
        "code": row.code,
        "name": row.name,
        "advice": row.advice,
        "advice_display": row.advice_display,
        "advice_state": row.advice_state,
        "stage": row.stage,
        "stage_confidence": row.stage_confidence,
        "stage_gate_hit": json.loads(row.stage_gate_hit) if row.stage_gate_hit else None,
        "trend": row.trend,
        "position_tag": row.position_tag,
        "score": row.score,
        "is_holding": row.is_holding,
        "est_level": row.est_level,
        "div_yield": row.div_yield,
        "pnl_pct": row.pnl_pct,
        "last_close": row.last_close,
        "pct_change": row.pct_change,
        "key_levels": json.loads(row.key_levels) if row.key_levels else [],
        "position": {
            "tier": row.position_tier or "",
            "pct": row.position_pct,
            "reasons": json.loads(row.position_reasons) if row.position_reasons else [],
        },
        "risk_level": row.risk_level,
        "human_text": row.human_text,
        "degraded": row.degraded,
    }


def _plan_row(row) -> dict:
    return {
        "code": row.code,
        "branch_id": row.branch_id,
        "scenario": row.scenario,
        "trigger_type": row.trigger_type,
        "trigger_price": row.trigger_price,
        "pct_change": row.pct_change,
        "action": row.action,
        "shares": row.shares,
        "est_amount": row.est_amount,
        "pct_of_total": row.pct_of_total,
        "valid_days": row.valid_days,
        "settable": row.settable,
        "notes": row.notes,
        "technical_basis": row.technical_basis,
        "position_check": json.loads(row.position_check) if row.position_check else {},
        "is_probe": row.is_probe,
    }


def _risk_row(row) -> dict:
    return {
        "code": row.code,
        "risk_id": row.risk_id,
        "risk_code": row.risk_code,
        "category": row.category,
        "level": row.level,
        "trigger_basis": row.trigger_basis,
        "data_date": None if row.data_date is None else row.data_date.isoformat(),
        "affects_advice": row.affects_advice,
        "action": row.action,
        "human_text": row.human_text,
        "source_tag": row.source_tag,
        "degraded": row.degraded,
    }


def _decode(raw: str) -> object:
    """把存储值还原为原始类型（与 ``ParamChange.as_row`` 的 JSON 编码对称）。"""
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw
