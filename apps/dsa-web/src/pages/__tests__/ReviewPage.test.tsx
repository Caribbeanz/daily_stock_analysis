import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../contexts/UiLanguageContext';
import ReviewPage from '../ReviewPage';

const { weekly, setExecution, report } = vi.hoisted(() => ({
  weekly: vi.fn(),
  setExecution: vi.fn(),
  report: vi.fn(),
}));

vi.mock('../../api/review', () => ({
  reviewApi: {
    weekly: (options?: unknown) => weekly(options),
    executionList: () => Promise.resolve({ weekKey: '2026-10-12', items: [], total: 0 }),
    setExecution: (input: unknown) => setExecution(input),
  },
  ruleBacktestApi: {
    report: (options?: unknown) => report(options),
  },
}));

const sampleReview = {
  weekKey: '2026-10-12',
  weekStart: '2026-10-12',
  weekEnd: '2026-10-16',
  generatedAt: '2026-10-16T09:00:00',
  headline: '🗓 周度复盘',
  signalSummary: {
    total: 1,
    evaluated: 1,
    correct: 1,
    unevaluated: 0,
    winRate: 1,
    avgReturnPct: 3.2,
    byAction: { build: 1 },
  },
  signals: [
    {
      signalId: 1,
      symbol: '601919',
      name: '中远海控',
      action: 'build',
      actionLabel: '建仓',
      scenario: 'up',
      createdAt: '2026-10-13',
      entryPrice: 12,
      stopLoss: 11,
      targetPrice: 14,
      outcome: 'hit',
      directionCorrect: true,
      returnPct: 3.2,
      evalStatus: 'completed',
    },
  ],
  positions: [
    {
      symbol: '601919',
      name: '中远海控',
      quantity: 200,
      avgCost: 12.927,
      lastPrice: 11,
      weekOpenPrice: 10,
      weekReturnPct: 10,
      weekPnlAmount: 200,
      totalReturnPct: -14.9,
      executionStatus: '',
      executionLabel: '未回填',
      note: null,
    },
  ],
  positionWeekPnl: 200,
  executionCounts: { unknown: 1 },
  market: [{ kind: 'index', name: '上证指数', weekReturnPct: 1.2, value: 3800, note: '' }],
  nextWeek: [
    {
      symbol: '601919',
      name: '中远海控',
      actionLabel: '建仓',
      entryLow: 11.5,
      entryHigh: 12,
      entryZone: '11.50 ~ 12.00',
      stopLoss: 11,
      targetPrice: 14,
      note: '',
    },
  ],
  notes: [],
  detailUrl: '',
  markdown: '',
};

function renderPage() {
  return render(
    <UiLanguageProvider>
      <ReviewPage />
    </UiLanguageProvider>,
  );
}

describe('ReviewPage', () => {
  beforeEach(() => {
    weekly.mockReset();
    setExecution.mockReset();
    report.mockReset();
    weekly.mockResolvedValue(sampleReview);
  });

  it('renders the weekly review sections', async () => {
    renderPage();
    expect(await screen.findByText('① 本周信号回顾与胜率')).toBeInTheDocument();
    expect(await screen.findByText('② 持仓盈亏与执行情况')).toBeInTheDocument();
    expect(await screen.findByText('③ 大盘 / 板块周度总结')).toBeInTheDocument();
    expect(await screen.findByText('④ 下周关注与关键价位')).toBeInTheDocument();
  });

  it('submits an execution feedback when a status button is clicked', async () => {
    setExecution.mockResolvedValue({ symbol: '601919', executionStatus: 'executed' });
    renderPage();
    const executedButton = await screen.findByRole('button', { name: '已执行' });
    fireEvent.click(executedButton);
    await waitFor(() =>
      expect(setExecution).toHaveBeenCalledWith(
        expect.objectContaining({ symbol: '601919', executionStatus: 'executed' }),
      ),
    );
  });

  it('runs the rule backtest and shows the verdict', async () => {
    report.mockResolvedValue({
      passed: true,
      violated: [],
      gate: {},
      overall: {
        symbol: '',
        samples: 36,
        wins: 21,
        winRate: 0.583,
        avgWin: 5,
        avgLoss: -2,
        profitFactor: 2.69,
        avgReturn: 2.62,
        maxDrawdown: 0.154,
        benchmarkReturn: 155.5,
        bars: 793,
      },
      symbols: [],
      scenarios: { up: { symbol: '', samples: 20, wins: 12, winRate: 0.6, avgWin: 6, avgLoss: -2, profitFactor: 3, avgReturn: 3.1, maxDrawdown: 0.1, benchmarkReturn: 0, bars: 0 } },
      notes: [],
      limitations: ['历史表现不代表未来收益。'],
      markdown: '',
    });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: '策略回测' }));
    fireEvent.click(await screen.findByRole('button', { name: /运行回测/ }));
    expect(await screen.findByText('✅ 通过质量门')).toBeInTheDocument();
    expect(await screen.findByText('历史表现不代表未来收益。')).toBeInTheDocument();
  });
});
