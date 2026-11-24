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
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator, Sequence

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.assistant.config.overrides import ParamChange
from src.assistant.data.models import (
    AssistantBase,
    ConditionalPlanRow,
    DeliveryLogRow,
    HoldingConclusionRow,
    LlmTextCacheRow,
    ParamOverrideRow,
    RiskSignalRow,
    Snapshot,
    StockDailyRaw,
    StockMonthly,
    StockWeekly,
    UserHolding,
    UserNote,
    WatchlistRuntime,
)
from src.assistant.domain import Bar, GuardChainResult
from src.assistant.enums import SourceTag


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
        """仅创建本模块的表（不触碰主流程表）。"""
        AssistantBase.metadata.create_all(self._engine)

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
        """写入一份快照及其子行；``snapshot_id`` 已存在则抛 :class:`AppendOnlyViolation`。"""
        snapshot_id = f"{result.data_date:%Y%m%d}-{uuid.uuid4().hex[:12]}"
        with self.session() as sess:
            if sess.get(Snapshot, snapshot_id) is not None:  # pragma: no cover - uuid 碰撞
                raise AppendOnlyViolation(f"快照已存在：{snapshot_id}")
            sess.add(
                Snapshot(
                    snapshot_id=snapshot_id,
                    data_date=result.data_date,
                    calc_version=result.calc_version,
                    param_hash=result.param_hash,
                    param_preset=preset,
                    generated_at=result.generated_at,
                    bars_used=result.data_quality.bars_used,
                    gap_days=result.data_quality.gap_days,
                    source_tag="D3" if result.conclusion.degraded else "D1",
                    notes=notes or None,
                )
            )
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
        return snapshot_id

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

    def save_llm_text(self, snapshot_id: str, position: str, text: str, *, code: str | None = None,
                      model: str = "", channel: str = "", consistency: str = "") -> None:
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


def _decode(raw: str) -> object:
    """把存储值还原为原始类型（与 ``ParamChange.as_row`` 的 JSON 编码对称）。"""
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw
