import apiClient from './index';
import { toCamelCase } from './utils';

/**
 * 周度复盘 + 执行回填（P3 · FR-11）与规则回测报告（P3 · FR-12）。
 *
 * 复盘是**只读聚合**；执行回填（已执行 / 忽略 / 稍后）是「是否按建议执行」的
 * 人工登记（FR-11.2），不改动持仓 / 权重 / 止损等业务数据。
 */

export type WeeklySignal = {
  signalId: number | null;
  symbol: string;
  name: string;
  action: string;
  actionLabel: string;
  scenario: string;
  createdAt: string;
  entryPrice: number | null;
  stopLoss: number | null;
  targetPrice: number | null;
  outcome: string;
  directionCorrect: boolean | null;
  returnPct: number | null;
  evalStatus: string;
};

export type WeeklySignalSummary = {
  total: number;
  evaluated: number;
  correct: number;
  unevaluated: number;
  winRate: number | null;
  avgReturnPct: number | null;
  byAction: Record<string, number>;
};

export type WeeklyPosition = {
  symbol: string;
  name: string;
  quantity: number;
  avgCost: number;
  lastPrice: number | null;
  weekOpenPrice: number | null;
  weekReturnPct: number | null;
  weekPnlAmount: number | null;
  totalReturnPct: number | null;
  executionStatus: string;
  executionLabel: string;
  note: string | null;
};

export type WeeklyMarket = {
  kind: string;
  name: string;
  weekReturnPct: number | null;
  value: number | null;
  note: string;
};

export type WeeklyFocus = {
  symbol: string;
  name: string;
  actionLabel: string;
  entryLow: number | null;
  entryHigh: number | null;
  entryZone: string;
  stopLoss: number | null;
  targetPrice: number | null;
  note: string;
};

export type WeeklyReview = {
  weekKey: string;
  weekStart: string;
  weekEnd: string;
  generatedAt: string;
  headline: string;
  signalSummary: WeeklySignalSummary;
  signals: WeeklySignal[];
  positions: WeeklyPosition[];
  positionWeekPnl: number | null;
  executionCounts: Record<string, number>;
  market: WeeklyMarket[];
  nextWeek: WeeklyFocus[];
  notes: string[];
  detailUrl: string;
  markdown: string;
};

export type ExecutionStatus = 'executed' | 'ignored' | 'later';

export type ExecutionItem = {
  id: number;
  symbol: string;
  weekKey: string;
  signalId: number | null;
  executionStatus: string;
  note: string | null;
  source: string;
  createdAt: string | null;
  updatedAt: string | null;
};

export type ExecutionListResult = {
  weekKey: string;
  items: ExecutionItem[];
  total: number;
};

export type RuleBacktestStat = {
  symbol: string;
  samples: number;
  wins: number;
  winRate: number;
  avgWin: number;
  avgLoss: number;
  profitFactor: number | null;
  avgReturn: number;
  maxDrawdown: number;
  benchmarkReturn: number;
  bars: number;
};

export type RuleBacktestReport = {
  passed: boolean;
  violated: string[];
  gate: Record<string, number>;
  overall: RuleBacktestStat | null;
  symbols: RuleBacktestStat[];
  scenarios: Record<string, RuleBacktestStat>;
  notes: string[];
  limitations: string[];
  markdown: string;
};

export const reviewApi = {
  /** 生成 / 读取本周复盘（FR-11.1~11.4）。 */
  async weekly(options?: {
    refreshOutcomes?: boolean;
    limitNextWeek?: number;
    symbols?: string[];
  }): Promise<WeeklyReview> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/review/weekly', {
      params: {
        refresh_outcomes: options?.refreshOutcomes,
        limit_next_week: options?.limitNextWeek,
        symbols: options?.symbols,
      },
      paramsSerializer: { indexes: null },
    });
    const payload = toCamelCase<WeeklyReview>(response.data);
    payload.signals = payload.signals ?? [];
    payload.positions = payload.positions ?? [];
    payload.market = payload.market ?? [];
    payload.nextWeek = payload.nextWeek ?? [];
    payload.notes = payload.notes ?? [];
    return payload;
  },

  async executionList(weekKey?: string): Promise<ExecutionListResult> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/review/execution', {
      params: { week_key: weekKey },
    });
    const payload = toCamelCase<ExecutionListResult>(response.data);
    payload.items = payload.items ?? [];
    return payload;
  },

  async setExecution(input: {
    symbol: string;
    executionStatus: ExecutionStatus;
    weekKey?: string;
    signalId?: number;
    note?: string;
  }): Promise<ExecutionItem> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/review/execution', {
      symbol: input.symbol,
      execution_status: input.executionStatus,
      week_key: input.weekKey,
      signal_id: input.signalId,
      note: input.note,
      source: 'web',
    });
    const payload = toCamelCase<{ item: ExecutionItem }>(response.data);
    return payload.item;
  },
};

export const ruleBacktestApi = {
  /** 规则回测报告（FR-12：胜率 / 盈亏比 / 最大回撤 / 样本数 + 场景分档 + 局限）。 */
  async report(options?: {
    symbols?: string[];
    fetchDays?: number;
    horizonDays?: number;
  }): Promise<RuleBacktestReport> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/backtest/rule-report', {
      symbols: options?.symbols,
      fetch_days: options?.fetchDays,
      horizon_days: options?.horizonDays,
    });
    const payload = toCamelCase<RuleBacktestReport>(response.data);
    payload.symbols = payload.symbols ?? [];
    payload.scenarios = payload.scenarios ?? {};
    payload.notes = payload.notes ?? [];
    payload.limitations = payload.limitations ?? [];
    payload.violated = payload.violated ?? [];
    return payload;
  },
};
