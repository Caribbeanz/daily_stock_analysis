import apiClient from './index';
import { toCamelCase } from './utils';

/** 对话·读意图（后端 /api/v1/chat/ask 返回，P2.1）。 */
export type ChatReadIntent =
  | 'glossary'
  | 'qa'
  | 'deep_analysis'
  | 'portfolio_review'
  | 'write_intent'
  | 'out_of_scope';

/** 对话·读返回体（P2.1 · FR-14.3）。 */
export type ChatReadAnswer = {
  intent: ChatReadIntent;
  answer: string;
  riskNote: string;
  symbols: string[];
  terms: string[];
  degraded: boolean;
  source: string;
  reason: string;
  sessionId: string;
};

/** 名词表条目（FR-14.3.2）。 */
export type GlossaryItem = {
  term: string;
  explanation: string;
  aliases: string[];
};

export const chatApi = {
  /**
   * 统一读对话入口。
   *
   * 写操作（增删自选 / 改持仓 / 调权重 / 设止损）只识别、不执行，
   * 返回 "P2.2 开放" 回执；越界（下单）明确拒绝。**没有写副作用。**
   */
  async ask(
    message: string,
    options?: { symbol?: string; sessionId?: string },
  ): Promise<ChatReadAnswer> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/chat/ask', {
      message,
      symbol: options?.symbol,
      session_id: options?.sessionId,
    });
    const payload = toCamelCase<ChatReadAnswer>(response.data);
    payload.symbols = payload.symbols ?? [];
    payload.terms = payload.terms ?? [];
    return payload;
  },

  /** 名词表：留空返回全部；传入名词/别名做查询。 */
  async glossary(query?: string, limit = 50): Promise<GlossaryItem[]> {
    const response = await apiClient.get<{ items?: GlossaryItem[] }>('/api/v1/chat/glossary', {
      params: { q: query, limit },
    });
    const payload = toCamelCase<{ items: GlossaryItem[] }>(response.data);
    return payload.items ?? [];
  },
};
