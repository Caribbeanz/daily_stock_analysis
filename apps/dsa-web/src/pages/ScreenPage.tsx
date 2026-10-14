import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  Clock,
  Maximize2,
  Minimize2,
  RefreshCw,
  TrendingDown,
  TrendingUp,
  Wifi,
  WifiOff,
} from 'lucide-react';
import { screenApi, type ScreenCard, type ScreenSnapshot } from '../api/screen';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import type { UiTextKey, UiTextParams } from '../i18n/uiText';
import { useActionStream } from '../hooks';
import { cn } from '../utils/cn';

type TFunc = (key: UiTextKey, params?: UiTextParams) => string;

/**
 * 大屏看板（P4 · FR-14.1）。
 *
 * - **收盘后展示**：只呈现收盘后数据与报告，**不做盘中实时监控**（FR-14.1.1）；
 * - **一屏铺开**：指数 / 持仓汇总 / 自选股卡片 / 事件 / 复盘摘要，字号放大、远距离可读
 *   （FR-14.1.2 / NFR-10）；
 * - **数据截止显著标注**：顶部固定显示"数据截止 / 行情时间 / 生成时间"（FR-14.1.3）；
 * - **免登录常驻**：路由 `/screen` 在鉴权判断之前渲染，浏览器全屏即可长期无人值守；
 *   **关键信息不需要任何交互即可看到**（FR-14.1.4）；
 * - **手动 + 定时刷新**：间隔由后端 `refreshSeconds` 给出（可用 `?refresh=秒` 覆盖，
 *   `?auto=0` 关闭自动刷新）；
 * - **端间实时同步**：订阅写操作广播（FR-14.0.5），持仓 / 自选股变更后自动重取（NFR-9）。
 *
 * 可用 URL 参数：`symbols=601169,513100`、`refresh=120`、`auto=0`、`weekly=0`、`events=8`。
 */

const MIN_REFRESH_SECONDS = 30;

type Tone = 'up' | 'down' | 'flat' | 'none';

const toneOf = (value: number | null | undefined): Tone => {
  if (typeof value !== 'number' || Number.isNaN(value)) return 'none';
  if (value > 0) return 'up';
  if (value < 0) return 'down';
  return 'flat';
};

const toneClass = (tone: Tone): string =>
  tone === 'up'
    ? 'text-price-up'
    : tone === 'down'
      ? 'text-price-down'
      : tone === 'flat'
        ? 'text-price-flat'
        : 'text-secondary-text';

const signed = (value: number | null, digits = 2): string =>
  typeof value === 'number' && Number.isFinite(value)
    ? `${value > 0 ? '+' : ''}${value.toFixed(digits)}%`
    : '—';

const price = (value: number | null): string => {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  return Math.abs(value) >= 1 ? value.toFixed(2) : value.toFixed(3);
};

const amount = (value: number | null): string => {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  if (Math.abs(value) >= 10000) return `${(value / 10000).toFixed(2)}万`;
  return value.toFixed(2);
};

const signedAmount = (value: number | null): string => {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  const sign = value > 0 ? '+' : '';
  return `${sign}${amount(value)}`;
};

const readNumber = (raw: string | null, fallback: number): number => {
  const parsed = Number(raw);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
};

export default function ScreenPage() {
  const { t } = useUiLanguage();

  const query = useMemo(() => {
    if (typeof window === 'undefined') return new URLSearchParams();
    return new URLSearchParams(window.location.search);
  }, []);

  const symbols = useMemo(() => {
    const raw = query.get('symbols');
    if (!raw) return undefined;
    const list = raw
      .split(',')
      .map((item) => item.trim().toUpperCase())
      .filter(Boolean);
    return list.length ? list : undefined;
  }, [query]);

  const withWeekly = query.get('weekly') !== '0';
  const autoRefresh = query.get('auto') !== '0';
  const intervalOverride = readNumber(query.get('refresh'), 0);
  const limitEvents = Math.max(0, Math.min(20, Number(query.get('events')) || 6));

  const [snapshot, setSnapshot] = useState<ScreenSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [secondsLeft, setSecondsLeft] = useState(MIN_REFRESH_SECONDS);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [lastLoadedAt, setLastLoadedAt] = useState('');
  const inFlight = useRef(false);

  const load = useCallback(
    async (force = false) => {
      if (inFlight.current) return;
      inFlight.current = true;
      try {
        const payload = await screenApi.snapshot({
          symbols,
          withWeekly,
          limitEvents,
          refresh: force,
        });
        setSnapshot(payload);
        setFailed(false);
        setLastLoadedAt(new Date().toLocaleTimeString());
      } catch (error) {
        console.error('大屏快照获取失败', error);
        setFailed(true);
      } finally {
        inFlight.current = false;
        setLoading(false);
      }
    },
    [limitEvents, symbols, withWeekly]
  );

  // 首次加载
  useEffect(() => {
    void load(true);
  }, [load]);

  const refreshSeconds = useMemo(() => {
    const fromServer = snapshot?.refreshSeconds ?? 0;
    const resolved = intervalOverride || fromServer || MIN_REFRESH_SECONDS * 10;
    return Math.max(MIN_REFRESH_SECONDS, resolved);
  }, [intervalOverride, snapshot?.refreshSeconds]);

  // 倒计时 + 到点自动刷新
  useEffect(() => {
    setSecondsLeft(refreshSeconds);
  }, [refreshSeconds]);

  useEffect(() => {
    if (!autoRefresh) return undefined;
    const id = window.setInterval(() => {
      setSecondsLeft((prev) => Math.max(0, prev - 1));
    }, 1000);
    return () => window.clearInterval(id);
  }, [autoRefresh]);

  useEffect(() => {
    if (!autoRefresh || secondsLeft > 0) return;
    void load(false);
    setSecondsLeft(refreshSeconds);
  }, [autoRefresh, load, refreshSeconds, secondsLeft]);

  // 端间实时同步（FR-14.0.5）：任一端改持仓 / 自选股后自动重取
  const { isConnected } = useActionStream({
    onInvalidate: () => {
      void load(true);
    },
  });

  // 全屏
  useEffect(() => {
    const onChange = () => setIsFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener('fullscreenchange', onChange);
    return () => document.removeEventListener('fullscreenchange', onChange);
  }, []);

  const toggleFullscreen = useCallback(() => {
    if (typeof document === 'undefined') return;
    if (!document.fullscreenElement) {
      void document.documentElement.requestFullscreen?.();
    } else {
      void document.exitFullscreen?.();
    }
  }, []);

  const dataAsOf = snapshot?.marketDate || t('screen.noData');
  const quoteAsOf = snapshot?.quoteAsOf ? snapshot.quoteAsOf.slice(5) : '—';
  const degraded = snapshot?.degraded ?? [];
  const hasContent = Boolean(snapshot && (snapshot.cards.length || snapshot.indices.length));

  return (
    <div className="flex min-h-screen flex-col gap-4 bg-base p-4 text-foreground xl:p-6">
      {/* ① 顶部：数据截止 + 刷新 + 全屏 */}
      <header className="flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-border bg-card px-5 py-4">
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
          <h1 className="text-2xl font-semibold tracking-tight xl:text-3xl">{t('screen.title')}</h1>
          <div className="flex items-center gap-2 text-secondary-text">
            <Clock className="h-6 w-6" aria-hidden />
            <span className="text-base xl:text-lg">{t('screen.dataAsOf')}</span>
            <span className="text-2xl font-bold tabular-nums text-foreground xl:text-3xl">
              {dataAsOf}
            </span>
          </div>
          <div className="text-base text-secondary-text xl:text-lg">
            {t('screen.quoteAsOf')}
            <span className="ml-2 font-medium tabular-nums text-foreground">{quoteAsOf}</span>
          </div>
          <div className="text-base text-secondary-text xl:text-lg">
            {t('screen.generatedAt')}
            <span className="ml-2 font-medium tabular-nums text-foreground">
              {snapshot?.generatedAt || '—'}
            </span>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <span
            className={cn(
              'flex items-center gap-1.5 rounded-full border border-border px-3 py-1.5 text-sm',
              isConnected ? 'text-price-down' : 'text-secondary-text'
            )}
            title={isConnected ? t('screen.liveOn') : t('screen.liveOff')}
          >
            {isConnected ? <Wifi className="h-4 w-4" aria-hidden /> : <WifiOff className="h-4 w-4" aria-hidden />}
            {isConnected ? t('screen.liveOn') : t('screen.liveOff')}
          </span>
          {autoRefresh ? (
            <span className="rounded-full border border-border px-3 py-1.5 text-sm tabular-nums text-secondary-text">
              {t('screen.nextRefresh', { n: secondsLeft })}
            </span>
          ) : (
            <span className="rounded-full border border-border px-3 py-1.5 text-sm text-secondary-text">
              {t('screen.autoRefreshOff')}
            </span>
          )}
          <button
            type="button"
            onClick={() => void load(true)}
            className="flex items-center gap-2 rounded-xl border border-border px-4 py-2 text-base transition-colors hover:bg-elevated"
          >
            <RefreshCw className={cn('h-5 w-5', loading && 'animate-spin')} aria-hidden />
            {t('screen.refreshNow')}
          </button>
          <button
            type="button"
            onClick={toggleFullscreen}
            className="flex items-center gap-2 rounded-xl border border-border px-4 py-2 text-base transition-colors hover:bg-elevated"
          >
            {isFullscreen ? (
              <Minimize2 className="h-5 w-5" aria-hidden />
            ) : (
              <Maximize2 className="h-5 w-5" aria-hidden />
            )}
            {isFullscreen ? t('screen.exitFullscreen') : t('screen.fullscreen')}
          </button>
        </div>
      </header>

      {failed ? (
        <div className="flex items-center gap-3 rounded-2xl border border-border bg-card px-5 py-4 text-lg">
          <AlertTriangle className="h-6 w-6 text-price-up" aria-hidden />
          <span>{t('screen.loadFailed')}</span>
          <span className="text-secondary-text">
            {lastLoadedAt ? t('screen.lastSuccess', { t: lastLoadedAt }) : ''}
          </span>
        </div>
      ) : null}

      {/* ② 指数条 */}
      <section className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        {(snapshot?.indices ?? []).map((item) => {
          const tone = toneOf(item.changePct);
          return (
            <div
              key={item.code}
              className="flex items-baseline justify-between rounded-2xl border border-border bg-card px-5 py-4"
            >
              <span className="text-xl text-secondary-text xl:text-2xl">{item.name}</span>
              <span className="flex items-baseline gap-3">
                <span className={cn('text-3xl font-bold tabular-nums xl:text-4xl', toneClass(tone))}>
                  {price(item.value)}
                </span>
                <span className={cn('flex items-center gap-1 text-2xl font-semibold tabular-nums', toneClass(tone))}>
                  {tone === 'up' ? <TrendingUp className="h-5 w-5" aria-hidden /> : null}
                  {tone === 'down' ? <TrendingDown className="h-5 w-5" aria-hidden /> : null}
                  {signed(item.changePct)}
                </span>
              </span>
            </div>
          );
        })}
      </section>

      {/* ③ 持仓汇总 */}
      <section className="grid grid-cols-2 gap-4 rounded-2xl border border-border bg-card px-5 py-4 lg:grid-cols-5">
        <Metric label={t('screen.totalCost')} value={amount(snapshot?.portfolio.totalCost ?? null)} />
        <Metric label={t('screen.marketValue')} value={amount(snapshot?.portfolio.marketValue ?? null)} />
        <Metric
          label={t('screen.pnl')}
          value={signedAmount(snapshot?.portfolio.pnlAmount ?? null)}
          tone={toneOf(snapshot?.portfolio.pnlAmount ?? null)}
        />
        <Metric
          label={t('screen.pnlPct')}
          value={signed(snapshot?.portfolio.pnlPct ?? null)}
          tone={toneOf(snapshot?.portfolio.pnlPct ?? null)}
        />
        <Metric
          label={t('screen.winnersLosers')}
          value={`${snapshot?.portfolio.winners ?? 0} / ${snapshot?.portfolio.losers ?? 0}`}
          hint={t('screen.holdingsCount', { n: snapshot?.portfolio.positionCount ?? 0 })}
        />
      </section>

      {/* ④ 自选股卡片 */}
      <section className="flex flex-col gap-3">
        <SectionTitle title={t('screen.watchlist')} extra={`${snapshot?.cards.length ?? 0}`} />
        {!hasContent && !loading ? (
          <p className="rounded-2xl border border-border bg-card px-5 py-6 text-lg text-secondary-text">
            {t('screen.empty')}
          </p>
        ) : null}
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 2xl:grid-cols-3">
          {(snapshot?.cards ?? []).map((card) => (
            <CardTile key={card.symbol} card={card} t={t} />
          ))}
        </div>
      </section>

      {/* ⑤ 事件 + 复盘 */}
      <section className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="flex flex-col gap-3 rounded-2xl border border-border bg-card px-5 py-4">
          <SectionTitle title={t('screen.events')} extra={String(snapshot?.events.length ?? 0)} />
          {(snapshot?.events.length ?? 0) === 0 ? (
            <p className="text-lg text-secondary-text">{t('screen.noEvents')}</p>
          ) : (
            <ul className="flex flex-col gap-2">
              {(snapshot?.events ?? []).map((event) => (
                <li key={event} className="text-lg leading-relaxed xl:text-xl">
                  · {event}
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="flex flex-col gap-3 rounded-2xl border border-border bg-card px-5 py-4">
          <SectionTitle title={t('screen.weekly')} extra={snapshot?.weekly.weekKey ?? ''} />
          {snapshot?.weekly.headline ? (
            <>
              <p className="text-xl leading-relaxed xl:text-2xl">{snapshot.weekly.headline}</p>
              <div className="grid grid-cols-3 gap-3">
                <Metric
                  label={t('screen.winRate')}
                  value={
                    typeof snapshot.weekly.winRate === 'number'
                      ? `${snapshot.weekly.winRate.toFixed(1)}%`
                      : '—'
                  }
                />
                <Metric
                  label={t('screen.samples')}
                  value={`${snapshot.weekly.evaluated ?? 0} / ${snapshot.weekly.unevaluated ?? 0}`}
                />
                <Metric
                  label={t('screen.weekPnl')}
                  value={signedAmount(snapshot.weekly.positionWeekPnl ?? null)}
                  tone={toneOf(snapshot.weekly.positionWeekPnl ?? null)}
                />
              </div>
              <a className="text-base text-[hsl(var(--primary))] underline" href="/review">
                {t('screen.openReview')}
              </a>
            </>
          ) : (
            <p className="text-lg text-secondary-text">{t('screen.noWeekly')}</p>
          )}
        </div>
      </section>

      {/* ⑥ 降级提示 + 免责 */}
      <footer className="flex flex-col gap-2 rounded-2xl border border-border bg-card px-5 py-4">
        {degraded.length ? (
          <div className="flex flex-col gap-1">
            <span className="text-base font-medium text-secondary-text">{t('screen.degraded')}</span>
            <ul className="flex flex-col gap-1">
              {degraded.map((note) => (
                <li key={note} className="text-base text-secondary-text">
                  · {note}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        <p className="text-base text-secondary-text">
          {t('screen.disclaimer')}
          {snapshot?.disclaimer ? ` ${snapshot.disclaimer}` : ''}
        </p>
      </footer>
    </div>
  );
}

const Metric: React.FC<{ label: string; value: string; tone?: Tone; hint?: string }> = ({
  label,
  value,
  tone = 'none',
  hint,
}) => (
  <div className="flex flex-col gap-1">
    <span className="text-base text-secondary-text xl:text-lg">{label}</span>
    <span className={cn('text-2xl font-bold tabular-nums xl:text-3xl', toneClass(tone))}>{value}</span>
    {hint ? <span className="text-sm text-secondary-text">{hint}</span> : null}
  </div>
);

const SectionTitle: React.FC<{ title: string; extra?: string }> = ({ title, extra }) => (
  <div className="flex items-baseline justify-between">
    <h2 className="text-lg font-semibold text-secondary-text xl:text-xl">{title}</h2>
    {extra ? <span className="text-base tabular-nums text-secondary-text">{extra}</span> : null}
  </div>
);

const CardTile: React.FC<{ card: ScreenCard; t: TFunc }> = ({ card, t }) => {
  const tone = toneOf(card.changePct);
  const pnlTone = toneOf(card.pnlPct);
  return (
    <article
      data-testid={`screen-card-${card.symbol}`}
      className={cn(
        'flex flex-col gap-3 rounded-2xl border bg-card px-5 py-4',
        card.isUrgent ? 'border-price-up' : 'border-border'
      )}
    >
      <div className="flex items-baseline justify-between gap-3">
        <div className="flex items-baseline gap-3">
          <span className="text-2xl font-semibold xl:text-3xl">{card.name || card.symbol}</span>
          <span className="text-lg tabular-nums text-secondary-text">{card.symbol}</span>
        </div>
        {card.isUrgent ? (
          <span className="rounded-full border border-price-up px-3 py-1 text-sm font-semibold text-price-up">
            {t('screen.urgent')}
          </span>
        ) : null}
      </div>

      <div className="flex items-baseline gap-4">
        <span className={cn('text-4xl font-bold tabular-nums', toneClass(tone))}>{price(card.price)}</span>
        <span className={cn('text-2xl font-semibold tabular-nums', toneClass(tone))}>
          {signed(card.changePct)}
        </span>
        <span className="ml-auto text-base text-secondary-text">
          {card.score === null ? '' : `${t('screen.score')} ${card.score}`}
        </span>
      </div>

      <div className="rounded-xl bg-elevated px-3 py-2">
        <p className="text-xl font-semibold leading-relaxed xl:text-2xl">
          {card.actionLabel}
          <span className="ml-2 text-base font-normal text-secondary-text">{card.scenarioLabel}</span>
        </p>
        {card.conclusion ? (
          <p className="mt-1 text-base leading-relaxed text-secondary-text xl:text-lg">{card.conclusion}</p>
        ) : null}
      </div>

      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-base xl:text-lg">
        <Field label={t('screen.entryZone')} value={card.entryZone} />
        <Field label={t('screen.stopLoss')} value={price(card.stopLoss)} />
        <Field label={t('screen.targetPrice')} value={price(card.targetPrice)} />
        <Field label={t('screen.positionRatio')} value={card.positionRatioLabel} />
        <Field
          label={t('screen.toStop')}
          value={signed(card.distanceToStopPct)}
          tone={toneOf(card.distanceToStopPct)}
        />
        <Field
          label={t('screen.toTarget')}
          value={signed(card.distanceToTargetPct)}
          tone={toneOf(card.distanceToTargetPct)}
        />
      </div>

      {card.hasPosition ? (
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1 border-t border-border pt-2 text-base xl:text-lg">
          <span className="text-secondary-text">{t('screen.holding')}</span>
          <span className="tabular-nums">
            {t('screen.qty')} {card.quantity}
          </span>
          <span className="tabular-nums">
            {t('screen.cost')} {price(card.avgCost)}
          </span>
          <span className={cn('font-semibold tabular-nums', toneClass(pnlTone))}>
            {signed(card.pnlPct)}
          </span>
        </div>
      ) : null}
    </article>
  );
};

const Field: React.FC<{ label: string; value: string; tone?: Tone }> = ({ label, value, tone = 'none' }) => (
  <div className="flex items-baseline justify-between gap-2">
    <span className="text-secondary-text">{label}</span>
    <span className={cn('font-medium tabular-nums', toneClass(tone))}>{value}</span>
  </div>
);
