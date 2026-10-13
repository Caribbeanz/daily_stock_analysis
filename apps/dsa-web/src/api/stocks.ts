import apiClient from './index';
import { toCamelCase } from './utils';

export type ExtractItem = {
  code?: string | null;
  name?: string | null;
  confidence: string;
};

export type ExtractFromImageResponse = {
  codes: string[];
  items?: ExtractItem[];
  rawText?: string;
};

/** 多因子明细一行（P2 · FR-14.2.1）。 */
export type StockOverviewFactor = {
  key: string;
  label: string;
  value: string;
  tone: 'positive' | 'negative' | 'warning' | 'neutral';
};

export type StockOverviewQuote = {
  currentPrice?: number | null;
  changePercent?: number | null;
  updateTime?: string | null;
};

/** 个股详情聚合视图（GET /api/v1/stocks/{code}/overview）。 */
export type StockOverview = {
  symbol: string;
  name: string;
  asOf: string;
  generatedAt: string;
  action: string;
  actionLabel: string;
  scenario: string;
  scenarioLabel: string;
  conclusion: string;
  reason: string;
  score?: number | null;
  confidence?: number | null;
  isUrgent: boolean;
  entryLow?: number | null;
  entryHigh?: number | null;
  entryZone: string;
  stopLoss?: number | null;
  targetPrice?: number | null;
  positionRatio?: number | null;
  positionRatioLabel: string;
  positionReason: string;
  discipline: string;
  watchConditions: string;
  riskSummary: string;
  factors: StockOverviewFactor[];
  events: string[];
  quote?: StockOverviewQuote | null;
  distanceToStopPct?: number | null;
  distanceToTargetPct?: number | null;
  signalId?: number | null;
  signalCreatedAt?: string | null;
  degraded: string[];
  disclaimer: string;
};

/** K 线数据点（GET /api/v1/stocks/{code}/history）。 */
export type StockKLinePoint = {
  date: string;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  close?: number | null;
  volume?: number | null;
  changePercent?: number | null;
};

export type StockHistoryResponse = {
  stockCode: string;
  stockName?: string | null;
  period: string;
  data: StockKLinePoint[];
};

export const stocksApi = {
  async extractFromImage(file: File): Promise<ExtractFromImageResponse> {
    const formData = new FormData();
    formData.append('file', file);

    const headers: { [key: string]: string | undefined } = { 'Content-Type': undefined };
    const response = await apiClient.post(
      '/api/v1/stocks/extract-from-image',
      formData,
      {
        headers,
        timeout: 60000, // Vision API can be slow; 60s
      },
    );

    const data = response.data as { codes?: string[]; items?: ExtractItem[]; raw_text?: string };
    return {
      codes: data.codes ?? [],
      items: data.items,
      rawText: data.raw_text,
    };
  },

  async parseImport(file?: File, text?: string): Promise<ExtractFromImageResponse> {
    if (file) {
      const formData = new FormData();
      formData.append('file', file);
      const headers: { [key: string]: string | undefined } = { 'Content-Type': undefined };
      const response = await apiClient.post('/api/v1/stocks/parse-import', formData, { headers });
      const data = response.data as { codes?: string[]; items?: ExtractItem[] };
      return { codes: data.codes ?? [], items: data.items };
    }
    if (text) {
      const response = await apiClient.post('/api/v1/stocks/parse-import', { text });
      const data = response.data as { codes?: string[]; items?: ExtractItem[] };
      return { codes: data.codes ?? [], items: data.items };
    }
    throw new Error('请提供文件或粘贴文本');
  },

  /**
   * 个股详情聚合视图（H5 主入口 · FR-14.2.1）。
   * 结论与四要素同源于 decision_signals；行情缺失时由 `degraded` 说明。
   */
  async getOverview(stockCode: string, options?: { withQuote?: boolean }): Promise<StockOverview> {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/stocks/${encodeURIComponent(stockCode)}/overview`,
      { params: options?.withQuote === false ? { with_quote: false } : undefined },
    );
    const payload = toCamelCase<StockOverview>(response.data);
    payload.factors = payload.factors ?? [];
    payload.events = payload.events ?? [];
    payload.degraded = payload.degraded ?? [];
    return payload;
  },

  /** 个股历史 K 线（详情页迷你图使用）。 */
  async getHistory(
    stockCode: string,
    options?: { period?: 'daily' | 'weekly' | 'monthly'; days?: number },
  ): Promise<StockHistoryResponse> {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/stocks/${encodeURIComponent(stockCode)}/history`,
      { params: { period: options?.period ?? 'daily', days: options?.days ?? 60 } },
    );
    const payload = toCamelCase<StockHistoryResponse>(response.data);
    payload.data = payload.data ?? [];
    return payload;
  },
};
