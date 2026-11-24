# -*- coding: utf-8 -*-
"""阶段判定 S0–S5 + 防抖（需求 §5.14.2 / 技术方案 §4.3）。

**判定顺序是硬约束（短路）**：``S0 数据校验 → S3 下行 → S1 上升 → S4/S2（箱体 + pos20 二分）→ S5 兜底``。
下行优先是为了不把「下跌中继」误判成箱体低吸机会；S5 放最后保证「有足够数据必有一档」。

> 这里的 ``S1–S5`` 是**阶段码**；``S0`` 表示「无法判定」，与守卫序 S0（数据校验步）同名不同义。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Sequence

from src.assistant.config.overrides import ParamSet
from src.assistant.domain import DataQuality, StockView, StageResult
from src.assistant.engine.indicators import (
    average,
    detect_box,
    ma_alignment_score,
    price_position,
    relative_slope,
    sma,
)
from src.assistant.enums import Stage, StageConfidence


def _confidence_by_evidence(evidence: int) -> StageConfidence:
    if evidence >= 3:
        return StageConfidence.HIGH
    if evidence == 2:
        return StageConfidence.MEDIUM
    return StageConfidence.LOW


def detect_stage(view: StockView, params: ParamSet, quality: DataQuality) -> StageResult:
    """确定性阶段判定（不含防抖；防抖由 :func:`apply_debounce` 负责）。"""
    if not quality.sufficient:
        return StageResult(
            stage=Stage.S0,
            confidence=StageConfidence.LOW,
            reasons=(quality.reason or "数据不足",) + tuple(quality.flags),
            metrics={"bars_used": quality.bars_used, "gap_days": quality.gap_days},
        )

    ma_window = int(params.get("ma_window"))
    slope_k = int(params.get("slope_k"))
    theta = float(params.get("stage_theta"))
    eps = float(params.get("stage_epsilon"))
    box_lookback = int(params.get("box_lookback"))
    box_width_max = float(params.get("box_width_max"))
    box_touch_min = int(params.get("box_touch_min"))
    box_touch_tol = float(params.get("box_touch_tol"))

    bars = list(view.bars)
    closes = [bar.close for bar in bars]
    close = closes[-1]

    ma_series = [val for val in sma(closes, ma_window) if val is not None]
    if not ma_series:
        return StageResult(
            stage=Stage.S0,
            confidence=StageConfidence.LOW,
            reasons=(f"MA{ma_window} 无法计算",),
            metrics={"bars_used": len(bars)},
        )
    ma = ma_series[-1]
    slope = relative_slope(ma_series, slope_k)
    pos20 = price_position(bars, 20 if len(bars) >= 20 else len(bars))
    alignment = ma_alignment_score(closes, ma_window)
    box = detect_box(
        bars,
        lookback=box_lookback,
        width_max=box_width_max,
        touch_min=box_touch_min,
        touch_tol=box_touch_tol,
    )
    vol_ma20 = average([bar.volume for bar in bars[-20:]]) if len(bars) >= 20 else average([b.volume for b in bars])

    metrics = {
        "close": close,
        "ma": round(ma, 4),
        "slope": round(slope, 6),
        "pos20": round(pos20, 4),
        "alignment": alignment,
        "theta": theta,
        "epsilon": eps,
        "box": box.as_dict() if box else None,
        "volume_ma20": round(vol_ma20, 2),
    }

    above = close > ma * (1 + eps)
    below = close < ma * (1 - eps)
    flat_slope = abs(slope) <= theta
    up_slope = slope > theta
    down_slope = slope < -theta

    # ---- S3 下行优先（避免把下跌中继当箱体低吸）--------------------------
    if below and down_slope:
        below_days = _consecutive_below_ma(closes, ma_series, eps)
        evidence = 2 + (1 if alignment <= 1 else 0) + (1 if below_days >= 2 else 0)
        reasons = [
            f"收盘 {close:.2f} 低于 MA{ma_window}×(1-ε)={ma * (1 - eps):.2f}",
            f"MA{ma_window} 斜率 {slope:+.4f} < -θ({-theta:.4f})",
        ]
        if alignment <= 1:
            reasons.append("均线空头排列")
        if below_days >= 2:
            reasons.append(f"连续 {below_days} 日收于 MA{ma_window} 下方")
        return StageResult(
            stage=Stage.S3,
            confidence=_confidence_by_evidence(evidence),
            reasons=tuple(reasons),
            metrics=metrics,
        )

    # ---- S1 上升趋势 -----------------------------------------------------
    if above and up_slope:
        # 冲突处理（需求 §5.14.2）：长期筑底后刚站上 MA20 → 归 S4 并标「右侧确认未完成」
        if box is not None and box.valid and pos20 < 0.5 and close <= box.high:
            return StageResult(
                stage=Stage.S4,
                confidence=StageConfidence.LOW,
                reasons=(
                    f"站上 MA{ma_window} 但未突破箱体上沿 {box.high:.2f}",
                    "右侧确认未完成（放量 + 突破上沿才算确认）",
                ),
                metrics=metrics,
            )
        evidence = 2 + (1 if alignment >= 2 else 0) + (1 if pos20 >= 0.5 else 0)
        if alignment < 2:
            # 价在 MA20 上方但均线缠绕 → 按 S5 处理并标「上升未确认」
            return StageResult(
                stage=Stage.S5,
                confidence=StageConfidence.LOW,
                reasons=(
                    f"收盘站上 MA{ma_window} 但均线排列仅 {alignment}/3 项成立",
                    "上升未确认（均线缠绕）",
                ),
                metrics=metrics,
            )
        reasons = [
            f"收盘 {close:.2f} 高于 MA{ma_window}×(1+ε)={ma * (1 + eps):.2f}",
            f"MA{ma_window} 斜率 {slope:+.4f} > θ({theta:.4f})",
            f"均线多头排列 {alignment}/3 项成立",
        ]
        return StageResult(
            stage=Stage.S1,
            confidence=_confidence_by_evidence(evidence),
            reasons=tuple(reasons),
            metrics=metrics,
        )

    # ---- S2 / S4：走平 + 箱体成立，按 pos20 二分 -------------------------
    if flat_slope and box is not None and box.valid:
        stage = Stage.S2 if pos20 >= 0.5 else Stage.S4
        evidence = 2 + (1 if box.up_touches >= 3 and box.down_touches >= 3 else 0)
        pos_label = "高位" if pos20 >= 0.5 else "低位"
        reasons = [
            f"MA{ma_window} 斜率 {slope:+.4f} 在 ±θ({theta:.4f}) 内",
            f"箱体成立：宽度 {box.width:.2%} ≤ {box_width_max:.0%}，上沿触碰 {box.up_touches} 次 / 下沿 {box.down_touches} 次",
            f"pos20={pos20:.2f} → {pos_label}（上沿 {box.high:.2f} / 下沿 {box.low:.2f}）",
        ]
        return StageResult(
            stage=stage,
            confidence=_confidence_by_evidence(evidence),
            reasons=tuple(reasons),
            metrics=metrics,
        )

    # ---- S5 兜底 --------------------------------------------------------
    reasons: list[str] = []
    if not flat_slope:
        reasons.append(f"MA{ma_window} 斜率 {slope:+.4f} 超出 ±θ，但方向与价格位置不一致")
    if box is None:
        reasons.append("箱体样本不足，无法定义上下沿")
    elif not box.valid:
        if box.width > box_width_max:
            reasons.append(f"箱体宽度 {box.width:.2%} > 上限 {box_width_max:.0%}，箱体不成立")
        else:
            reasons.append(
                f"上下沿触碰不足（上 {box.up_touches} 次 / 下 {box.down_touches} 次 < {box_touch_min}）"
            )
    if flat_slope and box is not None and not box.valid:
        reasons.append("窄幅无序或方向选择前收敛，策略为不动")
    return StageResult(
        stage=Stage.S5,
        # 需求 §5.14.2：S5 置信度最高只能到「中」（「没有结构」本身是弱结论）
        confidence=StageConfidence.MEDIUM if len(reasons) <= 1 else StageConfidence.LOW,
        reasons=tuple(reasons) or ("有足够数据但不满足 S1–S4 任一",),
        metrics=metrics,
    )


def _consecutive_below_ma(closes: Sequence[float], ma_series: Sequence[float], eps: float) -> int:
    """连续收于 MA 下方的天数（对齐到序列尾部）。"""
    count = 0
    for close, ma in zip(reversed(closes), reversed(ma_series)):
        if close < ma * (1 - eps):
            count += 1
        else:
            break
    return count


# ---------------------------------------------------------------------------
# 防抖（需求 §5.14.2：连续 N 日满足新阶段条件才切换）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StageDebounceState:
    """跨日阶段状态（落在调度层内存或快照读回，不进 L1）。"""

    stage: Stage | None = None
    pending_stage: Stage | None = None
    pending_days: int = 0
    as_of: date | None = None


def apply_debounce(
    raw: StageResult,
    state: StageDebounceState,
    *,
    days: int,
    trade_date: date,
) -> tuple[StageResult, StageDebounceState]:
    """把「原始判定」收敛为「确认阶段」。

    例外（立即生效，不需凑满天数）：
    - **首次判定**（无历史）→ 直接确认；
    - **S0**（数据不足）→ 立即生效（fail-safe，不能拿旧阶段顶替数据缺口）；
    - **从 S0 恢复**→ 立即生效（数据已修复，无需再等）。
    """
    if days < 1:
        raise ValueError(f"防抖天数必须 ≥1：{days}")

    previous = state.stage
    if previous is None:
        confirmed = raw.stage
        return (
            _finalize(raw, previous, confirmed, switched=False),
            StageDebounceState(stage=confirmed, pending_stage=None, pending_days=0, as_of=trade_date),
        )

    if raw.stage is Stage.S0 or previous is Stage.S0:
        return (
            _finalize(raw, previous, raw.stage, switched=raw.stage != previous),
            StageDebounceState(stage=raw.stage, pending_stage=None, pending_days=0, as_of=trade_date),
        )

    if raw.stage is previous:
        return (
            _finalize(raw, previous, previous, switched=False),
            StageDebounceState(stage=previous, pending_stage=None, pending_days=0, as_of=trade_date),
        )

    pending_stage = state.pending_stage
    pending_days = state.pending_days + 1 if pending_stage is raw.stage else 1
    if pending_days >= days:
        return (
            _finalize(raw, previous, raw.stage, switched=True),
            StageDebounceState(stage=raw.stage, pending_stage=None, pending_days=0, as_of=trade_date),
        )

    return (
        _finalize(raw, previous, previous, switched=False, pending_stage=raw.stage, pending_days=pending_days),
        StageDebounceState(
            stage=previous, pending_stage=raw.stage, pending_days=pending_days, as_of=trade_date
        ),
    )


def _finalize(
    raw: StageResult,
    previous: Stage | None,
    confirmed: Stage,
    *,
    switched: bool,
    pending_stage: Stage | None = None,
    pending_days: int = 0,
) -> StageResult:
    """把「确认阶段」写回结果；保留原始判定的指标与理由用于展示。"""
    reasons = list(raw.reasons)
    if switched and previous is not None:
        reasons.insert(0, f"阶段切换 {previous.value}→{confirmed.value}")
    elif pending_stage is not None:
        reasons.append(f"待确认切换 {confirmed.value}→{pending_stage.value}（{pending_days} 日）")
    return StageResult(
        stage=confirmed,
        confidence=raw.confidence if confirmed is raw.stage else _downgrade(raw.confidence),
        reasons=tuple(reasons),
        metrics=raw.metrics,
        previous_stage=previous,
        switched=switched,
        pending_stage=pending_stage,
        pending_days=pending_days,
    )


def _downgrade(confidence: StageConfidence) -> StageConfidence:
    """未确认的新阶段沿用旧阶段时，置信度降一档（不能拿旧阶段冒充高置信）。"""
    if confidence is StageConfidence.HIGH:
        return StageConfidence.MEDIUM
    return StageConfidence.LOW
