import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../contexts/UiLanguageContext';
import type { StockOverview } from '../../api/stocks';
import StockDetailPage from '../StockDetailPage';

const { getOverview, getHistory, toggleWatchlist, refreshWatchlist } = vi.hoisted(() => ({
  getOverview: vi.fn(),
  getHistory: vi.fn(),
  toggleWatchlist: vi.fn(),
  refreshWatchlist: vi.fn(),
}));

vi.mock('../../api/stocks', () => ({
  stocksApi: {
    getOverview: (code: string) => getOverview(code),
    getHistory: (code: string) => getHistory(code),
  },
}));

vi.mock('../../hooks/useWatchlist', () => ({
  useWatchlist: () => ({
    watchlistCodes: [],
    isLoading: false,
    isActioning: false,
    actionMessage: null,
    isInWatchlist: () => false,
    addToWatchlist: vi.fn(),
    removeFromWatchlist: vi.fn(),
    toggleWatchlist,
    refresh: refreshWatchlist,
  }),
}));

const overview: StockOverview = {
  symbol: '601919',
  name: '中远海控',
  asOf: '2026-10-13',
  generatedAt: '2026-10-13T20:00:00',
  action: 'reduce',
  actionLabel: '减仓',
  scenario: 'sideways',
  scenarioLabel: '震荡趋势',
  conclusion: '减仓（震荡趋势）：接近箱体上沿，建议减仓',
  reason: '接近箱体上沿，建议减仓',
  score: 62,
  confidence: 0.8,
  isUrgent: false,
  entryLow: 12.1,
  entryHigh: 12.8,
  entryZone: '12.10~12.80',
  stopLoss: 11.5,
  targetPrice: 14.2,
  positionRatio: 0.3,
  positionRatioLabel: '30%',
  positionReason: '减半仓落袋',
  discipline: '仓位建议 30% ｜ 参考价位 12.10~12.80 ｜ 止损 11.50 ｜ 目标 14.20',
  watchConditions: '跌破 11.5 离场',
  riskSummary: '已持有 500 股，触及分批止盈第一档',
  factors: [
    { key: 'trend', label: '日线趋势', value: '向上', tone: 'positive' },
    { key: 'risk_reasons', label: '风控依据', value: '已浮盈 8%', tone: 'warning' },
  ],
  events: ['601919 发布三季报预告'],
  quote: { currentPrice: 12.6, changePercent: 1.23, updateTime: '2026-10-13T15:00:00' },
  distanceToStopPct: 9.57,
  distanceToTargetPct: -11.27,
  signalId: 42,
  signalCreatedAt: '2026-10-13T20:00:00',
  degraded: [],
  disclaimer: '本内容由程序基于历史数据自动生成，仅供学习参考。',
};

function renderPage(entry = '/m/stocks/601919') {
  return render(
    <UiLanguageProvider>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/m/stocks/:code" element={<StockDetailPage />} />
        </Routes>
      </MemoryRouter>
    </UiLanguageProvider>,
  );
}

describe('StockDetailPage', () => {
  beforeEach(() => {
    getOverview.mockReset();
    getHistory.mockReset();
    toggleWatchlist.mockReset();
    refreshWatchlist.mockReset();
    getOverview.mockResolvedValue(overview);
    getHistory.mockResolvedValue({ stockCode: '601919', stockName: '中远海控', period: 'daily', data: [] });
    refreshWatchlist.mockResolvedValue(undefined);
  });

  it('renders the plain-language conclusion and the four key levels', async () => {
    renderPage();
    expect(await screen.findByText(/减仓（震荡趋势）/)).toBeInTheDocument();
    expect(screen.getByText('12.10~12.80')).toBeInTheDocument();
    expect(screen.getByText('11.50')).toBeInTheDocument();
    expect(screen.getByText('14.20')).toBeInTheDocument();
    expect(screen.getByText('30%')).toBeInTheDocument();
    expect(screen.getByText('中远海控')).toBeInTheDocument();
  });

  it('shows the live quote with CN up/down coloring', async () => {
    renderPage();
    expect(await screen.findByText('12.60')).toBeInTheDocument();
    const change = screen.getByText('+1.23%');
    expect(change.className).toContain('text-price-up');
  });

  it('keeps factor details collapsed until expanded', async () => {
    renderPage();
    await screen.findByText(/减仓（震荡趋势）/);
    expect(screen.queryByText('日线趋势')).toBeNull();
    fireEvent.click(screen.getByRole('button', { expanded: false }));
    expect(screen.getByText('日线趋势')).toBeInTheDocument();
    expect(screen.getByText('已浮盈 8%')).toBeInTheDocument();
  });

  it('renders events from the overview', async () => {
    renderPage();
    expect(await screen.findByText(/601919 发布三季报预告/)).toBeInTheDocument();
  });

  it('falls back to the empty-events hint when no events exist', async () => {
    getOverview.mockResolvedValue({ ...overview, events: [] });
    renderPage();
    expect(await screen.findByText(/今日无重大事件提醒/)).toBeInTheDocument();
  });

  it('shows degraded notes instead of pretending data is complete', async () => {
    getOverview.mockResolvedValue({
      ...overview,
      quote: null,
      distanceToStopPct: null,
      distanceToTargetPct: null,
      degraded: ['行情暂不可用：现价与距止损/目标距离已隐藏'],
    });
    renderPage();
    expect(await screen.findByText(/数据说明/)).toBeInTheDocument();
    expect(screen.getByText(/行情暂不可用/)).toBeInTheDocument();
  });

  it('marks visits coming from the daily push', async () => {
    renderPage('/m/stocks/601919?from=push');
    expect(await screen.findByText('来自今日推送')).toBeInTheDocument();
  });

  it('toggles the watchlist from the detail page', async () => {
    renderPage();
    const button = await screen.findByRole('button', { name: /加入自选/ });
    fireEvent.click(button);
    await waitFor(() => expect(toggleWatchlist).toHaveBeenCalledWith('601919'));
  });
});
