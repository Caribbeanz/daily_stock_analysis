import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../contexts/UiLanguageContext';
import ScreenPage from '../ScreenPage';

const { snapshot } = vi.hoisted(() => ({ snapshot: vi.fn() }));

vi.mock('../../api/screen', () => ({
  screenApi: {
    snapshot: (options?: unknown) => snapshot(options),
  },
}));

vi.mock('../../hooks', () => ({
  useActionStream: () => ({ isConnected: false }),
}));

const sampleSnapshot = {
  generatedAt: '2026-10-14 16:41:32',
  marketDate: '2026-10-14',
  quoteAsOf: '2026-10-14 15:00:00',
  refreshSeconds: 300,
  indices: [
    { code: 'sh000001', name: '上证指数', value: 3767.23, changePct: -0.885 },
    { code: 'sz399001', name: '深证成指', value: 12110.59, changePct: 1.234 },
    { code: 'sz399006', name: '创业板指', value: null, changePct: null },
  ],
  cards: [
    {
      symbol: '601169',
      name: '北京银行',
      price: 5.6,
      changePct: 1.86,
      quoteTime: '2026-10-14 15:00:00',
      action: 'reduce',
      actionLabel: '减仓',
      scenarioLabel: '上涨趋势',
      conclusion: '减仓（上涨趋势）：触及止盈位',
      isUrgent: true,
      score: 58,
      entryZone: '12.10~12.50',
      stopLoss: 5.1,
      targetPrice: 6.4,
      positionRatioLabel: '30%',
      distanceToStopPct: -8.5,
      distanceToTargetPct: 16.2,
      signalCreatedAt: '2026-10-13 20:05:00',
      hasPosition: true,
      quantity: 1900,
      avgCost: 6.418,
      pnlPct: -12.74,
      positionValue: 10640,
      degraded: [],
    },
  ],
  portfolio: {
    positionCount: 8,
    pricedCount: 4,
    totalCost: 92356.6,
    marketValue: 84349.2,
    pnlAmount: -8007.4,
    pnlPct: -8.67,
    winners: 1,
    losers: 3,
    complete: false,
  },
  events: ['601169：发布三季报'],
  weekly: {
    weekKey: '2026-10-12',
    headline: '本周胜率 60%（6/10）',
    evaluated: 10,
    correct: 6,
    unevaluated: 2,
    winRate: 60,
    avgReturnPct: 1.25,
    positionWeekPnl: 128.5,
    executionCounts: { executed: 3 },
    detailUrl: '/review',
  },
  actionCounts: { reduce: 1 },
  urgentCount: 1,
  degraded: ['部分持仓缺少行情（513100, 513180），盈亏暂不展示'],
  disclaimer: '本内容由程序基于历史数据自动生成，仅供学习参考。',
};

function renderPage() {
  return render(
    <UiLanguageProvider>
      <ScreenPage />
    </UiLanguageProvider>,
  );
}

describe('ScreenPage', () => {
  beforeEach(() => {
    snapshot.mockReset();
    snapshot.mockResolvedValue(sampleSnapshot);
    window.history.replaceState({}, '', '/screen');
  });

  afterEach(() => {
    window.history.replaceState({}, '', '/');
  });

  it('renders cutoff time, indices, cards, portfolio, events, weekly and disclaimer', async () => {
    renderPage();

    // 数据截止时间显著标注（FR-14.1.3）
    expect(await screen.findByText('数据截止')).toBeInTheDocument();
    expect(await screen.findByText('2026-10-14')).toBeInTheDocument();
    expect(await screen.findByText('上证指数')).toBeInTheDocument();
    expect(await screen.findByText('北京银行')).toBeInTheDocument();
    expect(await screen.findByText('减仓')).toBeInTheDocument();
    expect(await screen.findByText('总成本')).toBeInTheDocument();
    expect(await screen.findByText('9.24万')).toBeInTheDocument();
    expect(await screen.findByText('-0.80万')).toBeInTheDocument();
    expect(await screen.findByText('601169：发布三季报')).toBeInTheDocument();
    expect(await screen.findByText('本周胜率 60%（6/10）')).toBeInTheDocument();
    expect(
      await screen.findByText(/本内容由程序基于历史数据自动生成/),
    ).toBeInTheDocument();
    // 降级提示如实呈现，不伪造
    expect(await screen.findByText(/部分持仓缺少行情/)).toBeInTheDocument();
  });

  it('marks rising prices red and falling prices green (A-share convention)', async () => {
    renderPage();

    const risingCard = await screen.findByTestId('screen-card-601169');
    expect(risingCard.querySelector('.text-price-up')).not.toBeNull();

    const index = await screen.findByText('上证指数');
    const indexRow = index.closest('div');
    expect(indexRow?.querySelector('.text-price-down')).not.toBeNull();
  });

  it('flags urgent cards and renders the position block', async () => {
    renderPage();

    const card = await screen.findByTestId('screen-card-601169');
    expect(card.className).toContain('border-price-up');
    expect(await screen.findByText('紧急')).toBeInTheDocument();
    expect(await screen.findByText('持仓')).toBeInTheDocument();
    expect(await screen.findByText(/成本 6.42/)).toBeInTheDocument();
    expect(await screen.findByText('-12.74%')).toBeInTheDocument();
  });

  it('requests a forced refresh when the refresh button is clicked', async () => {
    renderPage();
    await screen.findByText('北京银行');

    fireEvent.click(screen.getByRole('button', { name: /立即刷新/ }));

    await waitFor(() =>
      expect(snapshot).toHaveBeenLastCalledWith(
        expect.objectContaining({ refresh: true, withWeekly: true }),
      ),
    );
  });

  it('honours url overrides for symbols, weekly and auto refresh', async () => {
    window.history.replaceState(
      {},
      '',
      '/screen?symbols=601169,513100&weekly=0&auto=0&events=3',
    );
    renderPage();

    await screen.findByText('北京银行');
    expect(snapshot).toHaveBeenCalledWith(
      expect.objectContaining({
        symbols: ['601169', '513100'],
        withWeekly: false,
        limitEvents: 3,
      }),
    );
    expect(await screen.findByText('自动刷新已关闭')).toBeInTheDocument();
  });

  it('shows a load failure notice without crashing', async () => {
    snapshot.mockRejectedValue(new Error('boom'));
    renderPage();

    expect(await screen.findByText(/大屏数据加载失败/)).toBeInTheDocument();
  });
});
