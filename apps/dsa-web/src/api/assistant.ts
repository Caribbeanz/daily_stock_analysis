import apiClient from './index';
import type {
  AssistantHoldingResponse,
  AssistantHoldingsResponse,
  AssistantLlmTextResponse,
  AssistantMarketResponse,
  AssistantParams,
  AssistantSectorsResponse,
  AssistantSnapshot,
  AssistantWriteResponse,
} from '../types/assistant';

/** 助手快照只读接口（GET 一律不改数据，也不触发重算）。 */
const BASE = '/assistant';

export const assistantApi = {
  /** 最新快照摘要；`dataDate` 留空取最新。 */
  snapshot: async (dataDate?: string): Promise<AssistantSnapshot> => {
    const { data } = await apiClient.get<AssistantSnapshot>(`${BASE}/snapshot`, {
      params: dataDate ? { data_date: dataDate } : undefined,
    });
    return data;
  },

  holdings: async (snapshotId: string): Promise<AssistantHoldingsResponse> => {
    const { data } = await apiClient.get<AssistantHoldingsResponse>(`${BASE}/holdings`, {
      params: { snapshot_id: snapshotId },
    });
    return data;
  },

  holding: async (code: string, snapshotId: string): Promise<AssistantHoldingResponse> => {
    const { data } = await apiClient.get<AssistantHoldingResponse>(
      `${BASE}/holding/${encodeURIComponent(code)}`,
      { params: { snapshot_id: snapshotId } },
    );
    return data;
  },

  market: async (snapshotId: string): Promise<AssistantMarketResponse> => {
    const { data } = await apiClient.get<AssistantMarketResponse>(`${BASE}/market`, {
      params: { snapshot_id: snapshotId },
    });
    return data;
  },

  sectors: async (snapshotId: string): Promise<AssistantSectorsResponse> => {
    const { data } = await apiClient.get<AssistantSectorsResponse>(`${BASE}/sectors`, {
      params: { snapshot_id: snapshotId },
    });
    return data;
  },

  llmText: async (
    snapshotId: string,
    position: 'summary' | 'stock' | 'market',
    code?: string,
  ): Promise<AssistantLlmTextResponse> => {
    const { data } = await apiClient.get<AssistantLlmTextResponse>(`${BASE}/llm_text`, {
      params: { snapshot_id: snapshotId, position, ...(code ? { code } : {}) },
    });
    return data;
  },

  params: async (): Promise<AssistantParams> => {
    const { data } = await apiClient.get<AssistantParams>(`${BASE}/params`);
    return data;
  },

  setPreset: async (preset: string): Promise<AssistantWriteResponse> => {
    const { data } = await apiClient.put<AssistantWriteResponse>(`${BASE}/params/preset`, { preset });
    return data;
  },

  setOverride: async (key: string, value: unknown): Promise<AssistantWriteResponse> => {
    const { data } = await apiClient.put<AssistantWriteResponse>(`${BASE}/params/override`, {
      key,
      value,
    });
    return data;
  },

  rollbackOverride: async (key: string): Promise<AssistantWriteResponse> => {
    const { data } = await apiClient.delete<AssistantWriteResponse>(`${BASE}/params/override`, {
      params: { key },
    });
    return data;
  },

  setFunds: async (totalCapital: number, cashAvailable?: number | null): Promise<AssistantWriteResponse> => {
    const { data } = await apiClient.put<AssistantWriteResponse>(`${BASE}/params/funds`, {
      total_capital: totalCapital,
      cash_available: cashAvailable ?? null,
    });
    return data;
  },
};

export default assistantApi;
