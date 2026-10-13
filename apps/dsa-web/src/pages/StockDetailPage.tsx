import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { ArrowLeft, ChevronDown, RefreshCw, Star } from 'lucide-react';
import { stocksApi, type StockKLinePoint, type StockOverview } from '../api/stocks';
import { createParsedApiError, parseApiError, type ParsedApiError } from '../api/error';
import { ApiErrorAlert } from '../components/common/ApiErrorAlert';
import { MiniKLineChart } from '../components/mobile/MiniKLineChart';
import { useWatchlist } from '../hooks/useWatchlist';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import { cn } from '../utils/cn';

const KLINE_DAYS = 60;

/** 涨跌配色（中国市场惯例：涨红跌绿 · FR-14.0.3）。 */
const toneClass = (value: number | null | undefined): string => {
  if (value === null || value === undefined || Number.isNaN(value)) return 'text-price-flat';
  if (value > 0) return 'text-price-up';
  if (value < 0) return 'text-price-down';
  return 'text-price-flat';
};

const FACTOR_TONE_CLASS: Record<string, string> = {
  positive: 'text-price-up',
  negative: 'text-price-down',
  warning: 'text-warning',
  neutral: 'text-foreground',
};

const formatPrice = (value?: number | null): string => {
  if (value === null || value === undefined || !Number.isFinite(value)) return '-';
  return Math.abs(value) >= 1 ? value.toFixed(2) : value.toFixed(3);
};

const formatSignedPct = (value?: number | null): string => {
  if (value === null || value === undefined || !Number.isFinite(value)) return '-';
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}%`;
};

/**
 * 个股详情页（H5 主入口 · P2 · FR-14.2.1 / FR-14.0.2）。
 *
 * 一屏呈现：**人话结论（顶部大字号）** + **四要素**（建仓区间 / 止损 / 目标 / 仓位建议）
 * + 迷你 K 线 + **多因子明细（可展开）** + **事件与风险** + 数据降级说明 + 免责声明。
 *
 * 高频操作 ≤3 步（FR-14.2.5）：推送链接直达本页（1 步）→ 一键加/移自选（2 步）。
 * 持仓编辑属写操作，按决议 D3 统一走 `action_service` + 二次确认，排在 P2.2。
 */
const StockDetailPage: React.FC = () => {
  const { code = '' } = useParams<{ code: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { t } = useUiLanguage();
  const fromPush = searchParams.get('from') === 'push';

  const [overview, setOverview] = useState<StockOverview | null>(null);
  const [kline, setKline] = useState<StockKLinePoint[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [showFactors, setShowFactors] = useState(false);

  const { isLoading: watchlistLoading, isInWatchlist, toggleWatchlist, refresh: refreshWatchlist } = useWatchlist();

  const symbol = useMemo(() => code.trim().toUpperCase(), [code]);

  const load = useCallback(async () => {
    if (!symbol) {
      setError(
        createParsedApiError({
          title: t('stockDetail.loadFailed'),
          message: t('stockDetail.notFound'),
          category: 'missing_params',
        }),
      );
      setIsLoading(false);
      return;
    }
    setIsLoading(true);
    setError(null);
    const [overviewResult, klineResult] = await Promise.allSettled([
      stocksApi.getOverview(symbol),
      stocksApi.getHistory(symbol, { period: 'daily', days: KLINE_DAYS }),
    ]);

    if (overviewResult.status === 'fulfilled') {
      setOverview(overviewResult.value);
    } else {
      setOverview(null);
      setError(parseApiError(overviewResult.reason));
    }
    setKline(klineResult.status === 'fulfilled' ? klineResult.value.data : []);
    setIsLoading(false);
  }, [symbol, t]);

  useEffect(() => {
    void load();
    void refreshWatchlist();
  }, [load, refreshWatchlist]);

  const quote = overview?.quote ?? null;
  const inWatchlist = isInWatchlist(symbol);

  const handleWatchlistToggle = useCallback(async () => {
    if (!symbol) return;
    await toggleWatchlist(symbol);
  }, [symbol, toggleWatchlist]);

  if (isLoading) {
    return (
      <div className="flex min-h-[50vh] flex-col items-center justify-center gap-3 text-sm text-secondary-text">
        <RefreshCw className="h-5 w-5 animate-spin" />
        {t('stockDetail.loading')}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3" data-testid="stock-detail-page">
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => navigate(-1)}
          className="inline-flex h-9 w-9 items-center justify-center rounded-xl border border-border/70 bg-card/80 text-secondary-text transition-colors hover:text-foreground"
          aria-label={t('common.close')}
        >
          <ArrowLeft className="h-4 w-4" />
        </button>
        <div className="min-w-0 flex-1">
          <p className="truncate text-base font-semibold text-foreground">
            {overview?.name || symbol}
          </p>
          <p className="truncate text-[11px] text-secondary-text">
            {symbol}
            {overview?.signalCreatedAt
              ? ` · ${t('stockDetail.signalTime', { time: overview.signalCreatedAt.slice(0, 16).replace('T', ' ') })}`
              : ''}
          </p>
        </div>
        <button
          type="button"
          onClick={() => void handleWatchlistToggle()}
          disabled={watchlistLoading}
          className={cn(
            'inline-flex items-center gap-1 rounded-xl border px-3 py-2 text-xs font-medium transition-colors',
            inWatchlist
              ? 'border-primary/60 bg-primary/10 text-primary'
              : 'border-border/70 bg-card/80 text-secondary-text hover:text-foreground',
          )}
        >
          <Star className={cn('h-3.5 w-3.5', inWatchlist && 'fill-current')} />
          {inWatchlist ? t('stockDetail.watchlistRemove') : t('stockDetail.watchlistAdd')}
        </button>
      </div>

      {fromPush ? (
        <p className="rounded-xl border border-primary/30 bg-primary/5 px-3 py-1.5 text-[11px] text-primary">
          {t('stockDetail.fromPush')}
        </p>
      ) : null}

      {error ? <ApiErrorAlert error={error} /> : null}

      {overview ? (
        <>
          {/* 结论区：顶部大字号人话结论 */}
          <section className="glass-panel-lg px-4 py-4">
            <p className="label-uppercase">{t('stockDetail.section.conclusion')}</p>
            <h1 className="mt-2 text-xl font-semibold leading-snug text-foreground">
              {overview.conclusion}
            </h1>
            {quote?.currentPrice !== null && quote?.currentPrice !== undefined ? (
              <div className="mt-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span className="text-2xl font-semibold text-foreground">
                  {formatPrice(quote.currentPrice)}
                </span>
                <span className={cn('text-sm font-medium', toneClass(quote.changePercent))}>
                  {formatSignedPct(quote.changePercent)}
                </span>
                {quote.updateTime ? (
                  <span className="text-[11px] text-secondary-text">{quote.updateTime}</span>
                ) : null}
              </div>
            ) : null}
            {overview.riskSummary ? (
              <p className="mt-3 text-xs leading-relaxed text-secondary-text">
                {t('stockDetail.riskSummary')}：{overview.riskSummary}
              </p>
            ) : null}
          </section>

          {/* 四要素 */}
          <section className="grid grid-cols-2 gap-2">
            <div className="rounded-2xl border border-border/70 bg-card/70 px-3 py-3">
              <p className="text-[11px] text-secondary-text">{t('stockDetail.entryZone')}</p>
              <p className="mt-1 text-base font-semibold text-foreground">{overview.entryZone}</p>
            </div>
            <div className="rounded-2xl border border-border/70 bg-card/70 px-3 py-3">
              <p className="text-[11px] text-secondary-text">{t('stockDetail.stopLoss')}</p>
              <p className="mt-1 text-base font-semibold text-foreground">
                {formatPrice(overview.stopLoss)}
              </p>
              {overview.distanceToStopPct !== null && overview.distanceToStopPct !== undefined ? (
                <p className={cn('text-[11px]', toneClass(overview.distanceToStopPct))}>
                  {t('stockDetail.distanceToStop')} {formatSignedPct(overview.distanceToStopPct)}
                </p>
              ) : null}
            </div>
            <div className="rounded-2xl border border-border/70 bg-card/70 px-3 py-3">
              <p className="text-[11px] text-secondary-text">{t('stockDetail.target')}</p>
              <p className="mt-1 text-base font-semibold text-foreground">
                {formatPrice(overview.targetPrice)}
              </p>
              {overview.distanceToTargetPct !== null && overview.distanceToTargetPct !== undefined ? (
                <p className={cn('text-[11px]', toneClass(overview.distanceToTargetPct))}>
                  {t('stockDetail.distanceToTarget')} {formatSignedPct(overview.distanceToTargetPct)}
                </p>
              ) : null}
            </div>
            <div className="rounded-2xl border border-border/70 bg-card/70 px-3 py-3">
              <p className="text-[11px] text-secondary-text">{t('stockDetail.position')}</p>
              <p className="mt-1 text-base font-semibold text-foreground">
                {overview.positionRatioLabel}
              </p>
              {overview.positionReason ? (
                <p className="text-[11px] text-secondary-text">{overview.positionReason}</p>
              ) : null}
            </div>
          </section>

          {overview.discipline ? (
            <p className="rounded-xl border border-border/60 bg-elevated/40 px-3 py-2 text-xs text-secondary-text">
              {overview.discipline}
            </p>
          ) : null}

          {/* K 线 */}
          <section className="rounded-2xl border border-border/70 bg-card/70 px-3 py-3">
            <p className="mb-2 text-xs font-medium text-secondary-text">
              {t('stockDetail.section.kline', { days: KLINE_DAYS })}
            </p>
            <MiniKLineChart points={kline} limit={KLINE_DAYS} />
          </section>

          {/* 多因子明细（可展开） */}
          {overview.factors.length > 0 ? (
            <section className="rounded-2xl border border-border/70 bg-card/70">
              <button
                type="button"
                onClick={() => setShowFactors((prev) => !prev)}
                className="flex w-full items-center justify-between px-3 py-3 text-left"
                aria-expanded={showFactors}
              >
                <span className="text-xs font-medium text-secondary-text">
                  {t('stockDetail.section.factors')}
                </span>
                <span className="inline-flex items-center gap-1 text-[11px] text-secondary-text">
                  {showFactors ? t('stockDetail.collapse') : t('stockDetail.expand')}
                  <ChevronDown className={cn('h-3.5 w-3.5 transition-transform', showFactors && 'rotate-180')} />
                </span>
              </button>
              {showFactors ? (
                <ul className="border-t border-border/60 px-3 py-2">
                  {overview.factors.map((factor) => (
                    <li
                      key={factor.key}
                      className="flex items-start justify-between gap-3 border-b border-border/40 py-2 last:border-b-0"
                    >
                      <span className="shrink-0 text-[11px] text-secondary-text">{factor.label}</span>
                      <span
                        className={cn(
                          'text-right text-xs font-medium',
                          FACTOR_TONE_CLASS[factor.tone] ?? 'text-foreground',
                        )}
                      >
                        {factor.value}
                      </span>
                    </li>
                  ))}
                </ul>
              ) : null}
            </section>
          ) : null}

          {/* 事件与风险 */}
          <section className="rounded-2xl border border-border/70 bg-card/70 px-3 py-3">
            <p className="mb-2 text-xs font-medium text-secondary-text">
              {t('stockDetail.section.events')}
            </p>
            <ul className="flex flex-col gap-1.5">
              {(overview.events.length > 0 ? overview.events : [t('stockDetail.eventsEmpty')]).map(
                (event) => (
                  <li key={event} className="text-xs leading-relaxed text-secondary-text">
                    · {event}
                  </li>
                ),
              )}
            </ul>
          </section>

          {/* 降级说明：数据缺失不伪装成正常（NFR-8） */}
          {overview.degraded.length > 0 ? (
            <section className="rounded-2xl border border-warning/40 bg-warning/5 px-3 py-3">
              <p className="mb-1 text-xs font-medium text-warning">
                {t('stockDetail.section.degraded')}
              </p>
              <ul className="flex flex-col gap-1">
                {overview.degraded.map((note) => (
                  <li key={note} className="text-[11px] leading-relaxed text-secondary-text">
                    ! {note}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          <p className="px-1 text-[11px] leading-relaxed text-secondary-text">
            {t('stockDetail.disclaimer')}：{overview.disclaimer}
          </p>
        </>
      ) : null}
    </div>
  );
};

export default StockDetailPage;
