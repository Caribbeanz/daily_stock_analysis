import apiClient from './index';
import { toCamelCase } from './utils';

/**
 * 大屏看板（P4 · FR-14.1）。
 *
 * 收盘后数据与报告的**只读**一屏快照：指数 / 自选股卡片 / 持仓汇总 / 事件 / 复盘摘要。
 * 快照自带 `marketDate`（数据截止交易日）与 `quoteAsOf`（行情时间），前端需显著标注，
 * 避免"看起来实时"；同时返回 `refreshSeconds` 作为建议的自动刷新节奏。
 *
 * 不做盘中实时监控与分析（FR-14.1.1）：大屏只是同一份结论的远距离展示形态。
 */

export type ScreenIndex = {
  code: string;
  name: string;
  value: number | null;
  changePct: number | null;
};

export type ScreenCard = {
  symbol: string;
  name: string;
  price: number | null;
  changePct: number | null;
  quoteTime: string | null;
  action: string;
  actionLabel: string;
  scenarioLabel: string;
  conclusion: string;
  isUrgent: boolean;
  score: number | null;
  entryZone: string;
  stopLoss: number | null;
  targetPrice: number | null;
  positionRatioLabel: string;
  distanceToStopPct: number | null;
  distanceToTargetPct: number | null;
  signalCreatedAt: string | null;
  hasPosition: boolean;
  quantity: number;
  avgCost: number | null;
  pnlPct: number | null;
  positionValue: number | null;
  degraded: string[];
};

export type ScreenPortfolio = {
  positionCount: number;
  pricedCount: number;
  totalCost: number | null;
  marketValue: number | null;
  pnlAmount: number | null;
  pnlPct: number | null;
  winners: number;
  losers: number;
  complete: boolean;
};

export type ScreenWeekly = {
  weekKey?: string;
  headline?: string;
  evaluated?: number | null;
  correct?: number | null;
  unevaluated?: number | null;
  winRate?: number | null;
  avgReturnPct?: number | null;
  positionWeekPnl?: number | null;
  executionCounts?: Record<string, number>;
  detailUrl?: string;
};

export type ScreenSnapshot = {
  generatedAt: string;
  marketDate: string;
  quoteAsOf: string;
  refreshSeconds: number;
  indices: ScreenIndex[];
  cards: ScreenCard[];
  portfolio: ScreenPortfolio;
  events: string[];
  weekly: ScreenWeekly;
  actionCounts: Record<string, number>;
  urgentCount: number;
  degraded: string[];
  disclaimer: string;
};

export const screenApi = {
  /** 拉取一屏快照（只读）。`refresh` 为 true 时绕过服务端短暂缓存。 */
  async snapshot(options?: {
    symbols?: string[];
    withQuote?: boolean;
    withWeekly?: boolean;
    limitEvents?: number;
    refresh?: boolean;
  }): Promise<ScreenSnapshot> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/screen/snapshot', {
      params: {
        symbols: options?.symbols,
        with_quote: options?.withQuote,
        with_weekly: options?.withWeekly,
        limit_events: options?.limitEvents,
        refresh: options?.refresh,
      },
      paramsSerializer: { indexes: null },
    });
    const payload = toCamelCase<ScreenSnapshot>(response.data);
    payload.indices = payload.indices ?? [];
    payload.cards = payload.cards ?? [];
    payload.events = payload.events ?? [];
    payload.degraded = payload.degraded ?? [];
    payload.actionCounts = payload.actionCounts ?? {};
    payload.weekly = payload.weekly ?? {};
    payload.portfolio = payload.portfolio ?? {
      positionCount: 0,
      pricedCount: 0,
      totalCost: null,
      marketValue: null,
      pnlAmount: null,
      pnlPct: null,
      winners: 0,
      losers: 0,
      complete: true,
    };
    return payload;
  },
};
