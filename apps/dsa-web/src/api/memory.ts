import apiClient from './index';
import { toCamelCase } from './utils';

/**
 * 结构化对话记忆（P2.3 · FR-15.5）。
 *
 * 记忆由对话压缩产生，**只增不删**（原文长期保留）；删除 / 清空是用户显式的
 * 数据管理操作。一键清空需 `confirm=true`（强确认，决议 D5）。
 */

export type MemoryType =
  | 'position_change'
  | 'preference'
  | 'confirmed_action'
  | 'analysis_conclusion';

export type MemoryItem = {
  id: number;
  memoryType: string;
  content: string;
  sourceMessageIds: number[];
  sessionId: string | null;
  status: string;
  createdAt: string | null;
  updatedAt: string | null;
};

export type MemorySource = {
  id: number;
  role: string;
  content: string;
  createdAt: string | null;
};

export type MemoryDetail = MemoryItem & { sources: MemorySource[] };

export type MemoryListResult = {
  items: MemoryItem[];
  total: number;
  limit: number;
  offset: number;
};

export type MemoryStats = {
  active: number;
  deleted: number;
  total: number;
  byType: Record<string, number>;
};

export type MemoryDeleteResult = { deleted: boolean; mode: string; id: number };
export type MemoryRestoreResult = { restored: boolean; id: number };
export type MemoryClearResult = { cleared: number; mode: string; sessionId: string | null };
export type MemoryCompressResult = {
  compressed: number;
  scanned: number;
  lastMessageId: number | null;
  skipped: string;
  types: Record<string, number>;
};

export const memoryApi = {
  async list(params?: {
    memoryType?: MemoryType;
    sessionId?: string;
    includeDeleted?: boolean;
    limit?: number;
    offset?: number;
  }): Promise<MemoryListResult> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/memory', {
      params: {
        memory_type: params?.memoryType,
        session_id: params?.sessionId,
        include_deleted: params?.includeDeleted,
        limit: params?.limit,
        offset: params?.offset,
      },
    });
    const payload = toCamelCase<MemoryListResult>(response.data);
    payload.items = payload.items ?? [];
    return payload;
  },

  async stats(): Promise<MemoryStats> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/memory/stats');
    const payload = toCamelCase<MemoryStats>(response.data);
    payload.byType = payload.byType ?? {};
    return payload;
  },

  async detail(id: number): Promise<MemoryDetail> {
    const response = await apiClient.get<Record<string, unknown>>(`/api/v1/memory/${id}`);
    const payload = toCamelCase<MemoryDetail>(response.data);
    payload.sources = payload.sources ?? [];
    return payload;
  },

  /** 删除记忆：默认软删（保留可审计），`hard=true` 物理删除。 */
  async remove(id: number, hard = false): Promise<MemoryDeleteResult> {
    const response = await apiClient.delete<Record<string, unknown>>(`/api/v1/memory/${id}`, {
      params: { hard },
    });
    return toCamelCase<MemoryDeleteResult>(response.data);
  },

  async restore(id: number): Promise<MemoryRestoreResult> {
    const response = await apiClient.post<Record<string, unknown>>(`/api/v1/memory/${id}/restore`);
    return toCamelCase<MemoryRestoreResult>(response.data);
  },

  /** 一键清空（**强确认**）：需显式传 `confirm=true`。 */
  async clear(options?: {
    sessionId?: string;
    hard?: boolean;
  }): Promise<MemoryClearResult> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/memory/clear', {
      confirm: true,
      session_id: options?.sessionId,
      hard: options?.hard ?? true,
    });
    return toCamelCase<MemoryClearResult>(response.data);
  },

  async compress(sessionId: string, force = false): Promise<MemoryCompressResult> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/memory/compress', {
      session_id: sessionId,
      force,
    });
    return toCamelCase<MemoryCompressResult>(response.data);
  },
};
