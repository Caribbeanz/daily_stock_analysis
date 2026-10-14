import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { BarChart3, RefreshCw } from 'lucide-react';
import { reviewApi, ruleBacktestApi } from '../api/review';
import type { ExecutionStatus, RuleBacktestReport, WeeklyReview } from '../api/review';
import type { ParsedApiError } from '../api/error';
import { getParsedApiError } from '../api/error';
import {
  ApiErrorAlert,
  AppPage,
  Badge,
  Button,
  Card,
  EmptyState,
  InlineAlert,
  Loading,
  PageHeader,
  SectionCard,
} from '../components/common';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import type { UiTextKey } from '../i18n/uiText';
import { formatDateTime } from '../utils/format';

const EXECUTION_OPTIONS: Array<{ value: ExecutionStatus; labelKey: UiTextKey }> = [
  { value: 'executed', labelKey: 'review.execution.executed' },
  { value: 'ignored', labelKey: 'review.execution.ignored' },
  { value: 'later', labelKey: 'review.execution.later' },
];

function toneClass(value?: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) return 'text-price-flat';
  if (value > 0) return 'text-price-up';
  if (value < 0) return 'text-price-down';
  return 'text-price-flat';
}

function pct(value?: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
}

function pctPlain(value?: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `${value.toFixed(1)}%`;
}

function price(value?: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return value.toFixed(3);
}

function amount(value?: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 0 });
}

function pf(value?: number | null): string {
  if (value === null || value === undefined) return '—';
  return value.toFixed(2);
}

const TH = 'px-2 py-2 text-xs font-medium text-secondary-text whitespace-nowrap';
const TD = 'px-2 py-2 text-sm whitespace-nowrap';

/**
 * 复盘与回测页（P3）。
 *
 * - 「周度复盘」（FR-11）：本周信号与胜率 / 持仓盈亏与执行回填 / 大盘板块 / 下周关注；
 * - 「策略回测」（FR-12）：规则有效性报告（胜率 / 盈亏比 / 回撤 / 样本 + 场景分档 + 局限）。
 */
const ReviewPage: React.FC = () => {
  const { t } = useUiLanguage();
  const [tab, setTab] = useState<'weekly' | 'backtest'>('weekly');

  const [review, setReview] = useState<WeeklyReview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [busySymbol, setBusySymbol] = useState<string | null>(null);
  const [notice, setNotice] = useState<string>('');

  const [report, setReport] = useState<RuleBacktestReport | null>(null);
  const [running, setRunning] = useState(false);
  const [btError, setBtError] = useState<ParsedApiError | null>(null);

  const loadWeekly = useCallback(async (refreshOutcomes = false) => {
    setLoading(true);
    setError(null);
    try {
      setReview(await reviewApi.weekly({ refreshOutcomes }));
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadWeekly(false);
  }, [loadWeekly]);

  const runBacktest = useCallback(async () => {
    setRunning(true);
    setBtError(null);
    try {
      setReport(await ruleBacktestApi.report());
    } catch (err) {
      setBtError(getParsedApiError(err));
    } finally {
      setRunning(false);
    }
  }, []);

  const setExecution = useCallback(
    async (symbol: string, status: ExecutionStatus) => {
      setBusySymbol(symbol);
      setNotice('');
      try {
        await reviewApi.setExecution({ symbol, executionStatus: status, weekKey: review?.weekKey });
        setNotice(t('review.execution.updated'));
        await loadWeekly(false);
      } catch (err) {
        setError(getParsedApiError(err));
      } finally {
        setBusySymbol(null);
      }
    },
    [review?.weekKey, loadWeekly, t],
  );

  const summary = review?.signalSummary;
  const execCounts = review?.executionCounts ?? {};
  const executedCount = execCounts.executed ?? 0;
  const pendingCount = execCounts.unknown ?? 0;

  const tabs = useMemo(
    () => (
      <div className="mt-4 flex flex-wrap items-center gap-2">
        {(
          [
            { key: 'weekly', labelKey: 'review.tab.weekly' },
            { key: 'backtest', labelKey: 'review.tab.backtest' },
          ] as const
        ).map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => setTab(item.key)}
            className={`rounded-full border px-4 py-1.5 text-sm transition-colors ${
              tab === item.key
                ? 'border-[hsl(var(--primary))] bg-[var(--nav-active-bg)] text-[hsl(var(--primary))]'
                : 'border-border/70 text-secondary-text hover:text-foreground'
            }`}
          >
            {t(item.labelKey)}
          </button>
        ))}
      </div>
    ),
    [tab, t],
  );

  return (
    <AppPage>
      <PageHeader
        title={t('review.title')}
        description={t('review.description')}
        actions={
          tab === 'weekly' ? (
            <>
              <Button variant="outline" onClick={() => void loadWeekly(true)} disabled={loading}>
                <RefreshCw className="mr-1 h-4 w-4" />
                {t('review.regenerate')}
              </Button>
              <Button variant="ghost" onClick={() => void loadWeekly(false)} disabled={loading}>
                {t('common.retry')}
              </Button>
            </>
          ) : (
            <Button onClick={() => void runBacktest()} disabled={running}>
              <BarChart3 className="mr-1 h-4 w-4" />
              {running ? t('review.backtest.running') : t('review.backtest.run')}
            </Button>
          )
        }
      />

      {tabs}

      {notice ? <InlineAlert className="mt-4" variant="success" message={notice} /> : null}
      {error ? <ApiErrorAlert error={error} /> : null}

      {tab === 'weekly' ? (
        <>
          {loading && !review ? <Loading label={t('common.loading')} /> : null}

          {review ? (
            <>
              <div className="mt-4 flex flex-wrap items-center gap-2">
                <Badge variant="info">
                  {t('review.weekRange')} {review.weekStart} ~ {review.weekEnd}
                </Badge>
                <Badge variant="default">
                  {t('review.signalTotal')} {summary?.total ?? 0}
                </Badge>
                <Badge variant="success">
                  {t('review.signalWinRate')}{' '}
                  {summary?.winRate === null || summary?.winRate === undefined
                    ? t('review.signalNoSample')
                    : pctPlain(summary.winRate * 100)}
                </Badge>
                <Badge variant={summary && (summary.avgReturnPct ?? 0) >= 0 ? 'danger' : 'success'}>
                  {t('review.signalAvgReturn')} {pct(summary?.avgReturnPct)}
                </Badge>
                {review.generatedAt ? (
                  <span className="text-xs text-secondary-text">{formatDateTime(review.generatedAt)}</span>
                ) : null}
              </div>

              {/* ① 信号 */}
              <SectionCard className="mt-4" title={t('review.section.signals')}>
                {review.signals.length === 0 ? (
                  <p className="text-sm text-secondary-text">{t('review.signalEmpty')}</p>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[720px] text-left">
                      <thead>
                        <tr className="border-b border-border/60">
                          <th className={TH}>{t('review.col.symbol')}</th>
                          <th className={TH}>{t('review.col.action')}</th>
                          <th className={TH}>{t('review.col.scenario')}</th>
                          <th className={TH}>{t('review.col.date')}</th>
                          <th className={TH}>{t('review.col.outcome')}</th>
                          <th className={TH}>{t('review.col.return')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {review.signals.map((s) => (
                          <tr key={`${s.symbol}-${s.signalId ?? s.createdAt}`} className="border-b border-border/40">
                            <td className={TD}>
                              {s.name ? `${s.name}（${s.symbol}）` : s.symbol}
                            </td>
                            <td className={TD}>{s.actionLabel || s.action || '—'}</td>
                            <td className={TD}>{s.scenario || '—'}</td>
                            <td className={TD}>{s.createdAt || '—'}</td>
                            <td className={TD}>
                              {s.directionCorrect === true
                                ? t('review.outcome.correct')
                                : s.directionCorrect === false
                                  ? t('review.outcome.wrong')
                                  : s.evalStatus === 'unable'
                                    ? t('review.outcome.unable')
                                    : t('review.outcome.pending')}
                            </td>
                            <td className={`${TD} ${toneClass(s.returnPct)}`}>{pct(s.returnPct)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </SectionCard>

              {/* ② 持仓 */}
              <SectionCard
                className="mt-4"
                title={t('review.section.positions')}
                subtitle={
                  review.positionWeekPnl === null
                    ? undefined
                    : `${t('review.positionWeekPnl')} ${amount(review.positionWeekPnl)} 元`
                }
              >
                {review.positions.length === 0 ? (
                  <p className="text-sm text-secondary-text">{t('review.positionEmpty')}</p>
                ) : (
                  <>
                    <div className="overflow-x-auto">
                      <table className="w-full min-w-[860px] text-left">
                        <thead>
                          <tr className="border-b border-border/60">
                            <th className={TH}>{t('review.col.symbol')}</th>
                            <th className={TH}>{t('review.col.quantity')}</th>
                            <th className={TH}>{t('review.col.cost')}</th>
                            <th className={TH}>{t('review.col.last')}</th>
                            <th className={TH}>{t('review.col.weekReturn')}</th>
                            <th className={TH}>{t('review.col.weekPnl')}</th>
                            <th className={TH}>{t('review.col.totalReturn')}</th>
                            <th className={TH}>{t('review.col.execution')}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {review.positions.map((p) => (
                            <tr key={p.symbol} className="border-b border-border/40">
                              <td className={TD}>{p.name ? `${p.name}（${p.symbol}）` : p.symbol}</td>
                              <td className={TD}>{p.quantity.toFixed(0)}</td>
                              <td className={TD}>{price(p.avgCost)}</td>
                              <td className={TD}>{price(p.lastPrice)}</td>
                              <td className={`${TD} ${toneClass(p.weekReturnPct)}`}>{pct(p.weekReturnPct)}</td>
                              <td className={`${TD} ${toneClass(p.weekPnlAmount)}`}>{amount(p.weekPnlAmount)}</td>
                              <td className={`${TD} ${toneClass(p.totalReturnPct)}`}>{pct(p.totalReturnPct)}</td>
                              <td className={TD}>
                                <div className="flex flex-wrap items-center gap-1">
                                  {EXECUTION_OPTIONS.map((option) => (
                                    <button
                                      key={option.value}
                                      type="button"
                                      disabled={busySymbol === p.symbol}
                                      onClick={() => void setExecution(p.symbol, option.value)}
                                      className={`rounded-full border px-2 py-0.5 text-xs transition-colors disabled:opacity-50 ${
                                        p.executionStatus === option.value
                                          ? 'border-[hsl(var(--primary))] bg-[var(--nav-active-bg)] text-[hsl(var(--primary))]'
                                          : 'border-border/70 text-secondary-text hover:text-foreground'
                                      }`}
                                    >
                                      {t(option.labelKey)}
                                    </button>
                                  ))}
                                </div>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <p className="mt-3 text-xs text-secondary-text">
                      {t('review.executionPrefill')
                        .replace('{executed}', String(executedCount))
                        .replace('{pending}', String(pendingCount))}
                    </p>
                  </>
                )}
              </SectionCard>

              {/* ③ 大盘板块 */}
              <SectionCard className="mt-4" title={t('review.section.market')}>
                {review.market.length === 0 ? (
                  <p className="text-sm text-secondary-text">{t('review.marketEmpty')}</p>
                ) : (
                  <ul className="space-y-1.5 text-sm">
                    {review.market.map((m, index) => (
                      <li key={`${m.kind}-${m.name}-${index}`} className="flex items-center gap-2">
                        <Badge variant={m.kind === 'sector' ? 'info' : 'default'}>
                          {m.kind === 'sector' ? '板块' : '指数'}
                        </Badge>
                        <span className="text-foreground">{m.name}</span>
                        {m.value !== null ? (
                          <span className="text-secondary-text">（{m.value.toFixed(2)}）</span>
                        ) : null}
                        <span className={toneClass(m.weekReturnPct)}>{pct(m.weekReturnPct)}</span>
                        {m.note ? <span className="text-xs text-secondary-text">{m.note}</span> : null}
                      </li>
                    ))}
                  </ul>
                )}
              </SectionCard>

              {/* ④ 下周关注 */}
              <SectionCard className="mt-4" title={t('review.section.nextWeek')}>
                {review.nextWeek.length === 0 ? (
                  <p className="text-sm text-secondary-text">{t('review.nextWeekEmpty')}</p>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[640px] text-left">
                      <thead>
                        <tr className="border-b border-border/60">
                          <th className={TH}>{t('review.col.symbol')}</th>
                          <th className={TH}>{t('review.col.action')}</th>
                          <th className={TH}>{t('review.col.entryZone')}</th>
                          <th className={TH}>{t('review.col.stopLoss')}</th>
                          <th className={TH}>{t('review.col.target')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {review.nextWeek.map((f) => (
                          <tr key={f.symbol} className="border-b border-border/40">
                            <td className={TD}>{f.name ? `${f.name}（${f.symbol}）` : f.symbol}</td>
                            <td className={TD}>{f.actionLabel || '—'}</td>
                            <td className={TD}>{f.entryZone || '—'}</td>
                            <td className={TD}>{price(f.stopLoss)}</td>
                            <td className={TD}>{price(f.targetPrice)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </SectionCard>

              {review.notes.length > 0 ? (
                <InlineAlert
                  className="mt-4"
                  variant="warning"
                  message={review.notes.join('；')}
                />
              ) : null}

              <p className="mt-4 text-xs text-secondary-text">{t('review.disclaimer')}</p>
            </>
          ) : null}
        </>
      ) : (
        <>
          <InlineAlert className="mt-4" variant="info" message={t('review.backtest.hint')} />
          {btError ? <ApiErrorAlert error={btError} /> : null}
          {running ? <Loading label={t('review.backtest.running')} /> : null}

          {!running && !report ? (
            <EmptyState title={t('review.backtest.empty')} description={t('review.backtest.emptyHint')} />
          ) : null}

          {report ? (
            <>
              <div className="mt-4 flex flex-wrap items-center gap-2">
                <Badge variant={report.passed ? 'success' : 'danger'}>
                  {report.passed ? t('review.backtest.verdictPass') : t('review.backtest.verdictFail')}
                </Badge>
                {report.violated.map((item) => (
                  <Badge key={item} variant="warning">
                    {item}
                  </Badge>
                ))}
              </div>

              <SectionCard className="mt-4" title={t('review.backtest.title')}>
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[820px] text-left">
                    <thead>
                      <tr className="border-b border-border/60">
                        <th className={TH}>{t('review.col.symbol')}</th>
                        <th className={TH}>{t('review.backtest.colsamples')}</th>
                        <th className={TH}>{t('review.backtest.colWinRate')}</th>
                        <th className={TH}>{t('review.backtest.colProfitFactor')}</th>
                        <th className={TH}>{t('review.backtest.colExpectation')}</th>
                        <th className={TH}>{t('review.backtest.colDrawdown')}</th>
                        <th className={TH}>{t('review.backtest.colBenchmark')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {[...report.symbols, ...(report.overall ? [report.overall] : [])].map((s, index) => (
                        <tr key={`${s.symbol || 'overall'}-${index}`} className="border-b border-border/40">
                          <td className={TD}>{s.symbol || t('review.backtest.overall')}</td>
                          <td className={TD}>{s.samples}</td>
                          <td className={TD}>{pctPlain(s.winRate * 100)}</td>
                          <td className={TD}>{pf(s.profitFactor)}</td>
                          <td className={`${TD} ${toneClass(s.avgReturn)}`}>{pct(s.avgReturn)}</td>
                          <td className={TD}>{pctPlain(s.maxDrawdown * 100)}</td>
                          <td className={`${TD} ${toneClass(s.benchmarkReturn)}`}>{pct(s.benchmarkReturn)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </SectionCard>

              {Object.keys(report.scenarios).length > 0 ? (
                <SectionCard className="mt-4" title={t('review.backtest.scenarios')}>
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[640px] text-left">
                      <thead>
                        <tr className="border-b border-border/60">
                          <th className={TH}>{t('review.col.scenario')}</th>
                          <th className={TH}>{t('review.backtest.colsamples')}</th>
                          <th className={TH}>{t('review.backtest.colWinRate')}</th>
                          <th className={TH}>{t('review.backtest.colProfitFactor')}</th>
                          <th className={TH}>{t('review.backtest.colExpectation')}</th>
                          <th className={TH}>{t('review.backtest.colDrawdown')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(report.scenarios).map(([key, s]) => (
                          <tr key={key} className="border-b border-border/40">
                            <td className={TD}>{key}</td>
                            <td className={TD}>{s.samples}</td>
                            <td className={TD}>{pctPlain(s.winRate * 100)}</td>
                            <td className={TD}>{pf(s.profitFactor)}</td>
                            <td className={`${TD} ${toneClass(s.avgReturn)}`}>{pct(s.avgReturn)}</td>
                            <td className={TD}>{pctPlain(s.maxDrawdown * 100)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </SectionCard>
              ) : null}

              {report.limitations.length > 0 ? (
                <Card className="mt-4" variant="bordered">
                  <p className="text-sm font-medium text-foreground">
                    {t('review.backtest.limitations')}
                  </p>
                  <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-secondary-text">
                    {report.limitations.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </Card>
              ) : null}
            </>
          ) : null}
        </>
      )}
    </AppPage>
  );
};

export default ReviewPage;
