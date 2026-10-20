import apiClient from './index';
import { toCamelCase } from './utils';

/**
 * 高抛低吸助手 · 移动 H5（M1）。
 *
 * 只读接口，供独立页面 `/h5`（列表）与 `/h5/stocks/:code`（详情）使用。
 * 结论与四要素同源于后端已落库的 `decision_signals`，前端不做任何计算或推断。
 */

export type H5WatchlistItem = {
  symbol: string;
  name: string;
  assetType: string;
  price: number | null;
  changePercent: number | null;
  action: string;
  actionLabel: string;
  /** 页面展示动作：建仓 / 加仓 / 持有 / 减仓 / 清仓 / 观望 / 暂无信号 */
  displayAction: string;
  /** 色调：up / down / flat / muted */
  actionTone: string;
  hasSignal: boolean;
  isUrgent: boolean;
  scenarioLabel: string;
  signalCreatedAt: string | null;
};

export type H5MarketIndex = {
  code: string;
  name: string;
  close: number | null;
  changePct: number | null;
};

export type H5MarketOtherIndex = {
  code: string;
  name: string;
  changePct: number | null;
};

export type H5MarketIndicator = {
  key: string;
  label: string;
  value: string;
  tone: string;
};

export type H5MarketRegime = {
  level: string;
  label: string;
  tone: string;
  advice: string;
};

export type H5MarketBlock = {
  index: H5MarketIndex;
  others: H5MarketOtherIndex[];
  indicators: H5MarketIndicator[];
  regime: H5MarketRegime;
  advice: string;
  alerts: string[];
  summary: string;
  counts: Record<string, number>;
  asOf: string;
  degraded: string[];
};

export type H5WatchlistResponse = {
  asOf: string;
  market?: H5MarketBlock | null;
  items: H5WatchlistItem[];
  count: number;
  degraded: string[];
  disclaimer: string;
};

export type H5Quote = {
  open: number | null;
  high: number | null;
  low: number | null;
  /** 收盘价；盘中为最新价 */
  close: number | null;
  prevClose: number | null;
  change: number | null;
  changePercent: number | null;
  volume: number | null;
  updateTime: string | null;
};

export type H5AnalysisDetail = {
  label: string;
  value: string;
};

export type H5SectorDetail = {
  industry: string;
  changePct: number | null;
  rank: number | null;
  total: number | null;
  relativePct: number | null;
  advancers: number | null;
  decliners: number | null;
  rankingAvailable: boolean;
};

export type H5AnalysisSection = {
  key: string;
  label: string;
  verdict: string;
  tone: string;
  available: boolean;
  details: H5AnalysisDetail[];
  sectorDetail?: H5SectorDetail | null;
};

export type H5IndexItem = {
  code: string;
  name: string;
  value: number | null;
  changePct: number | null;
};

export type H5Market = {
  indices: H5IndexItem[];
  asOf: string;
};

export type H5Detail = {
  symbol: string;
  name: string;
  asOf: string;
  generatedAt: string;
  action: string;
  actionLabel: string;
  displayAction: string;
  actionTone: string;
  hasSignal: boolean;
  isUrgent: boolean;
  scenario: string;
  scenarioLabel: string;
  conclusion: string;
  reason: string;
  score: number | null;
  confidence: number | null;
  quote: H5Quote | null;
  entryLow: number | null;
  entryHigh: number | null;
  entryZone: string;
  stopLoss: number | null;
  targetPrice: number | null;
  positionRatio: number | null;
  positionRatioLabel: string;
  positionReason: string;
  discipline: string;
  watchConditions: string;
  riskSummary: string;
  distanceToStopPct: number | null;
  distanceToTargetPct: number | null;
  market: H5Market;
  analysis: H5AnalysisSection[];
  signalId: number | null;
  signalCreatedAt: string | null;
  degraded: string[];
  disclaimer: string;
};

export const h5Api = {
  async watchlist(refresh = false): Promise<H5WatchlistResponse> {
    const response = await apiClient.get('/api/v1/h5/watchlist', {
      params: refresh ? { refresh: true } : undefined,
    });
    const payload = toCamelCase<H5WatchlistResponse>(response.data);
    payload.items = payload.items ?? [];
    payload.degraded = payload.degraded ?? [];
    return payload;
  },

  async detail(code: string): Promise<H5Detail> {
    const response = await apiClient.get(`/api/v1/h5/stocks/${encodeURIComponent(code)}`);
    const payload = toCamelCase<H5Detail>(response.data);
    payload.analysis = payload.analysis ?? [];
    payload.degraded = payload.degraded ?? [];
    return payload;
  },
};
