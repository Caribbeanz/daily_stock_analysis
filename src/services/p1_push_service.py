# -*- coding: utf-8 -*-
"""高抛低吸助手 · P1 每日推送闭环（20:00）

编排（对应方案 §4 P1）::

    跑 P0 信号 → 读回信号（四端同源）→ 组装每日概览 → 渲染关键信号 K 线图 → 飞书推送

关键约束：
- **失败隔离**：单只标的失败只体现在"数据异常"板块，不中断整批（NFR-8）；
- **降噪**：与上一交易日结论一致的观望项自动折叠（NFR-3，状态落 ``var/``）；
- **优雅降级**：无绘图库跳过附图、未配置渠道则只产出文本（不阻断）；
- **可演练**：``--dry-run`` 只生成不发送。

用法::

    python -m src.services.p1_push_service --dry-run               # 演练：生成但不发送
    python -m src.services.p1_push_service                         # 生成并推送飞书
    python -m src.services.p1_push_service --symbols 601919 513100
    python -m src.services.p1_push_service --require-gate          # 先跑回测质量门，不达标则不发
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from src.services.daily_digest import (
    DailyDigest,
    build_digest,
    load_previous_state,
    save_state,
)

logger = logging.getLogger(__name__)

__all__ = ["P1PushService", "PushResult", "FeishuNotifier"]

_DEFAULT_STATE_PATH = "var/p1_push_state.json"
_DEFAULT_OUT_DIR = "var/p1_charts"
_DEFAULT_GATE_SYMBOLS = ["601919", "601288"]


@dataclass
class PushResult:
    as_of: str = ""
    counts: Dict[str, int] = field(default_factory=dict)
    headline: str = ""
    markdown: str = ""
    charts: List[str] = field(default_factory=list)
    sent_text: bool = False
    sent_images: int = 0
    skipped_reason: Optional[str] = None
    gate_passed: Optional[bool] = None
    gate_detail: Optional[Dict[str, Any]] = None
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "as_of": self.as_of,
            "counts": self.counts,
            "headline": self.headline,
            "charts": self.charts,
            "sent_text": self.sent_text,
            "sent_images": self.sent_images,
            "skipped_reason": self.skipped_reason,
            "gate_passed": self.gate_passed,
            "gate_detail": self.gate_detail,
            "errors": self.errors,
        }


# --------------------------------------------------------------------------- #
# 通知渠道（飞书；未配置时静默降级）
# --------------------------------------------------------------------------- #
class FeishuNotifier:
    """飞书通知薄封装：支持文本与文件（K 线图以文件附件发送）。"""

    def __init__(self, config: Any = None):
        self._config = config
        self._sender: Any = None

    def _ensure(self) -> Any:
        if self._sender is None:
            from src.config import get_config
            from src.notification_sender.feishu_sender import FeishuSender

            self._sender = FeishuSender(self._config or get_config())
        return self._sender

    def is_available(self) -> bool:
        try:
            from src.config import get_config

            config = self._config or get_config()
        except Exception:
            return False
        webhook = getattr(config, "feishu_webhook_url", None)
        app_id = getattr(config, "feishu_app_id", None)
        app_secret = getattr(config, "feishu_app_secret", None)
        chat_id = getattr(config, "feishu_chat_id", None)
        return bool(webhook) or bool(app_id and app_secret and chat_id)

    def send_text(self, text: str) -> bool:
        return bool(self._ensure().send_to_feishu(text))

    def send_image(self, path: str) -> bool:
        return bool(self._ensure().send_feishu_file(path))


# --------------------------------------------------------------------------- #
# 编排服务
# --------------------------------------------------------------------------- #
class P1PushService:
    """P1 每日推送编排器。依赖均可注入，便于离线测试。"""

    def __init__(
        self,
        *,
        builder: Any = None,
        signal_service: Any = None,
        notifier: Any = None,
        chart_builder: Callable[..., Dict[str, Any]] = None,
        chart_renderer: Callable[..., Optional[str]] = None,
        fetch: Callable[[str], Any] = None,
        event_provider: Callable[[], Sequence[str]] = None,
        use_default_events: bool = True,
        state_path: str = _DEFAULT_STATE_PATH,
        out_dir: str = _DEFAULT_OUT_DIR,
        history_days: int = 240,
        backtester: Any = None,
        h5_base_url: Optional[str] = None,
    ):
        self._builder = builder
        self._signal_service = signal_service
        self._notifier = notifier
        self._chart_builder = chart_builder
        self._chart_renderer = chart_renderer
        self._fetch = fetch
        self._event_provider = event_provider
        self._use_default_events = use_default_events
        self.state_path = state_path
        self.out_dir = out_dir
        self.history_days = history_days
        self._backtester = backtester
        self._h5_base_url = h5_base_url

    # ---------------- H5 深链（P2 · FR-14.0.2） ---------------- #
    def _resolve_h5_base_url(self) -> str:
        """解析 H5 基址：显式注入 > Config > 环境变量 > ''（不生成链接）。"""
        if self._h5_base_url is not None:
            return str(self._h5_base_url).strip()
        try:
            from src.config import get_config

            configured = getattr(get_config(), "hslb_h5_base_url", None)
            if configured:
                return str(configured).strip()
        except Exception:  # pragma: no cover - 配置不可用时不阻断推送
            pass
        import os

        return str(os.getenv("HSLB_H5_BASE_URL", "") or "").strip()

    # ---------------- 懒加载依赖 ---------------- #
    @property
    def builder(self):
        if self._builder is None:
            from src.services.p0_signal_builder import P0SignalBuilder

            self._builder = P0SignalBuilder(history_days=self.history_days)
        return self._builder

    @property
    def signal_service(self):
        if self._signal_service is None:
            from src.services.decision_signal_service import DecisionSignalService

            self._signal_service = DecisionSignalService()
        return self._signal_service

    @property
    def notifier(self):
        if self._notifier is None:
            self._notifier = FeishuNotifier()
        return self._notifier

    @property
    def backtester(self):
        if self._backtester is None:
            from src.services.rule_backtest import RuleBacktester

            self._backtester = RuleBacktester()
        return self._backtester

    # ---------------- 数据 ---------------- #
    def _fetch_daily(self, symbol: str):  # pragma: no cover - 依赖网络
        if self._fetch is not None:
            return self._fetch(symbol)
        from src.services.p0_signal_builder import P0SignalBuilder

        raw = P0SignalBuilder._fetch_daily(symbol, self.history_days)  # noqa: SLF001
        if raw is None:
            return None
        from src.services.alert_indicators import normalize_ohlcv

        return normalize_ohlcv(raw, required_columns=("open", "high", "low", "close", "volume"))

    def _resolve_event_provider(self) -> Optional[Callable[[], Sequence[str]]]:
        """返回事件源；未注入时懒加载默认 ``EventSource``（FR-20）。"""
        if self._event_provider is not None:
            return self._event_provider
        if not self._use_default_events:
            return None
        try:
            from src.services.event_source import EventSource

            self._event_provider = EventSource().as_provider()
        except Exception as exc:  # 事件源初始化失败不得阻断推送
            logger.warning("事件源初始化失败，事件板块降级为空：%s", exc)
            self._event_provider = lambda: []
        return self._event_provider

    def _collect_events(self) -> List[str]:
        provider = self._resolve_event_provider()
        if provider is None:
            return []
        try:
            return [str(e) for e in (provider() or []) if str(e).strip()]
        except Exception as exc:  # 事件源异常不得阻断推送
            logger.warning("事件源读取失败，已降级为空：%s", exc)
            return []

    # ---------------- 主流程 ---------------- #
    def run(
        self,
        *,
        symbols: Optional[Sequence[str]] = None,
        dry_run: bool = False,
        with_charts: bool = True,
        max_charts: int = 3,
        require_gate: bool = False,
        gate: Optional[Dict[str, float]] = None,
        now: Optional[datetime] = None,
    ) -> PushResult:
        now = now or datetime.now()
        result = PushResult(as_of=now.strftime("%Y-%m-%d"))
        symbol_list = [str(s).strip().upper() for s in (symbols or []) if str(s).strip()] or None

        # 1) 质量门（可选）
        if require_gate:
            gate_report = self._run_gate(symbol_list, gate)
            result.gate_passed = gate_report.passed
            result.gate_detail = gate_report.to_dict()
            if not gate_report.passed:
                result.skipped_reason = "gate_failed"
                logger.warning("规则体检未通过，按质量门策略中止推送：%s", gate_report.violated)
                return result

        # 2) 跑 P0 信号
        build = self.builder.run(symbols=symbol_list)

        # 3) 读回信号（四端同源：以 decision_signals 为准）
        signals = self._collect_signals(build)

        # 4) 组装概览（含降噪）
        previous_state = load_previous_state(self.state_path)
        digest = build_digest(
            signals,
            as_of=now.strftime("%Y-%m-%d"),
            events=self._collect_events(),
            previous_state=previous_state,
            now=now,
            h5_base_url=self._resolve_h5_base_url(),
        )
        result.counts = digest.counts
        result.headline = digest.headline()
        result.markdown = digest.to_markdown()

        # 5) 关键信号附图
        if with_charts:
            result.charts = self._render_charts(digest, max_charts=max_charts)

        # 6) 推送
        if dry_run:
            result.skipped_reason = result.skipped_reason or "dry_run"
            self._save_state(digest)
            return result

        if not getattr(self.notifier, "is_available", lambda: True)():
            result.skipped_reason = "no_channel_configured"
            result.errors.append("未配置飞书渠道（FEISHU_* 均为空），仅生成内容未发送")
            self._save_state(digest)
            return result

        try:
            result.sent_text = bool(self.notifier.send_text(result.markdown))
        except Exception as exc:
            result.errors.append(f"文本推送失败：{type(exc).__name__}: {exc}")

        for chart in result.charts:
            try:
                if self.notifier.send_image(chart):
                    result.sent_images += 1
            except Exception as exc:
                result.errors.append(f"附图推送失败（{chart}）：{type(exc).__name__}: {exc}")

        # 仅当确实推送给用户后才推进降噪状态（避免"没发也折叠"）
        if result.sent_text:
            self._save_state(digest)
        return result

    # ---------------- 内部 ---------------- #
    def _run_gate(self, symbols: Optional[Sequence[str]], gate: Optional[Dict[str, float]]):
        return self.backtester.run(symbols or _DEFAULT_GATE_SYMBOLS, gate=gate)

    def _collect_signals(self, build: Any) -> List[Dict[str, Any]]:
        signals: List[Dict[str, Any]] = []
        for item in getattr(build, "results", []) or []:
            signal_id = getattr(item, "signal_id", None)
            if signal_id:
                try:
                    signals.append(self.signal_service.get_signal(int(signal_id)))
                    continue
                except Exception as exc:  # 读回失败则退化为结果对象
                    logger.warning("读回信号 %s 失败，退化为构建结果：%s", signal_id, exc)
            signals.append(
                {
                    "stock_code": getattr(item, "symbol", ""),
                    "stock_name": getattr(item, "name", ""),
                    "action": getattr(item, "action", None) or "watch",
                    "scenario": getattr(item, "scenario", None),
                    "is_urgent": bool(getattr(item, "is_urgent", False)),
                    "stop_loss": getattr(item, "stop_loss", None),
                    "error": getattr(item, "error", None),
                }
            )
        return signals

    def _resolve_chart_deps(self):
        global _PIL_WARNED
        if self._chart_builder is None or self._chart_renderer is None:
            from src.services import kline_chart

            if not kline_chart.PIL_AVAILABLE and not _PIL_WARNED:
                logger.warning("未安装 Pillow，跳过关键信号附图（推送仍正常）")
                _PIL_WARNED = True
            if self._chart_builder is None:
                self._chart_builder = kline_chart.build_chart_payload
            if self._chart_renderer is None:
                self._chart_renderer = kline_chart.render_png
        return self._chart_builder, self._chart_renderer

    def _render_charts(self, digest: DailyDigest, *, max_charts: int) -> List[str]:
        chart_builder, chart_renderer = self._resolve_chart_deps()
        targets = list(digest.urgent) + list(digest.highlight)
        if max_charts > 0 and len(targets) > max_charts:
            targets = targets[:max_charts]

        paths: List[str] = []
        for item in targets:
            try:
                df = self._fetch_daily(item.symbol)
                if df is None or len(df) == 0:
                    continue
                payload = chart_builder(
                    df,
                    symbol=item.symbol,
                    name=item.name,
                    stop_loss=item.stop_loss,
                    target=item.target_price,
                )
                path = chart_renderer(payload, str(Path(self.out_dir) / f"{item.symbol}.png"))
                if path:
                    paths.append(path)
            except Exception as exc:
                logger.warning("K 线图渲染失败 %s：%s", item.symbol, exc)
        return paths

    def _save_state(self, digest: DailyDigest) -> None:
        try:
            save_state(self.state_path, digest.state)
        except Exception as exc:  # 状态落盘失败不影响主流程
            logger.warning("降噪状态写入失败：%s", exc)


_PIL_WARNED = False


def _main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="高抛低吸助手 · P1 每日推送（20:00）")
    parser.add_argument("--symbols", nargs="*", default=None, help="指定标的；缺省为全部启用自选股")
    parser.add_argument("--dry-run", action="store_true", help="只生成不发送")
    parser.add_argument("--no-charts", action="store_true", help="不渲染 K 线图")
    parser.add_argument("--max-charts", type=int, default=3, help="最多附图数量")
    parser.add_argument("--require-gate", action="store_true", help="先跑回测质量门，不达标不发")
    parser.add_argument("--no-events", action="store_true", help="关闭事件与风险板块（默认接情报源）")
    parser.add_argument("--state-path", default=_DEFAULT_STATE_PATH)
    parser.add_argument("--out-dir", default=_DEFAULT_OUT_DIR)
    parser.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    service = P1PushService(
        state_path=args.state_path,
        out_dir=args.out_dir,
        use_default_events=not args.no_events,
    )
    result = service.run(
        symbols=args.symbols,
        dry_run=args.dry_run,
        with_charts=not args.no_charts,
        max_charts=args.max_charts,
        require_gate=args.require_gate,
    )

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(result.markdown)
        print("")
        print(
            f"[发送情况] 文本={result.sent_text} 附图={result.sent_images} "
            f"跳过原因={result.skipped_reason or '-'} 错误={len(result.errors)}"
        )
        for err in result.errors:
            print(f"  · {err}")
    return 0 if not result.errors else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
