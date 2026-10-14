import apiClient from './index';
import { toCamelCase } from './utils';

/**
 * 写操作（P2.2 · FR-14.3.3 / FR-14.2.6）。
 *
 * 三段式：`preview`（生成确认令牌）→ `execute`（凭令牌确认执行）→ `cancel`。
 * **所有写操作一律二次确认**；前端只负责展示"将要做什么"并收集确认，
 * 真正的落库由后端 `action_service` 单一写点完成。
 */

/** 单条写操作的预览 / 执行结果。 */
export type ActionItem = {
  op: string;
  label: string;
  description: string;
  params: Record<string, unknown>;
  valid: boolean;
  error: string;
  before: unknown;
  after: unknown;
  status: '' | 'ok' | 'rejected' | 'failed';
  receipt: string;
};

export type ActionPreviewStatus = 'pending_confirmation' | 'not_write' | 'rejected' | 'nothing_to_do';

export type ActionPreview = {
  status: ActionPreviewStatus;
  token: string;
  message: string;
  summary: string;
  intent: string;
  expiresAt: number | null;
  items: ActionItem[];
};

export type ActionExecuteStatus = 'executed' | 'partial' | 'failed' | 'nothing_pending';

export type ActionExecution = {
  status: ActionExecuteStatus;
  ok: boolean;
  receipt: string;
  items: ActionItem[];
};

export type ActionCancellation = {
  status: 'cancelled' | 'nothing_pending';
  message: string;
};

/** 一条写操作审计记录（P2.4 · NFR-11）。 */
export type ActionAuditItem = {
  id: number;
  source: string;
  actor: string;
  action: string;
  actionLabel: string;
  target: string;
  before: unknown;
  after: unknown;
  confirmed: boolean;
  status: string;
  description: string;
  sessionId: string | null;
  undoOf: number | null;
  undoneBy: number | null;
  createdAt: string | null;
  undoable: boolean;
  undoReason: string;
};

export type ActionAuditDetail = ActionAuditItem & {
  undoOps: Array<Record<string, unknown>>;
};

export type ActionAuditListResult = {
  items: ActionAuditItem[];
  total: number;
  limit: number;
  offset: number;
};

export type ActionAuditStats = {
  total: number;
  ok: number;
  rejected: number;
  failed: number;
  undoRecords: number;
  byAction: Record<string, number>;
};

/** 端间刷新事件载荷（SSE `invalidate`）。 */
export type ActionInvalidatePayload = {
  scopes: string[];
  action: string;
  label: string;
  target: string;
  source: string;
  auditId: number | null;
  undoOf: number | null;
};

export const actionsApi = {
  /** 解析写指令 → 返回"将要做什么"清单 + 确认令牌（**不落库**）。 */
  async preview(
    message: string,
    options?: { symbol?: string; sessionId?: string },
  ): Promise<ActionPreview> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/actions/preview', {
      message,
      symbol: options?.symbol,
      session_id: options?.sessionId,
    });
    const payload = toCamelCase<ActionPreview>(response.data);
    payload.items = payload.items ?? [];
    return payload;
  },

  /** 凭令牌确认执行（二次确认后调用）。 */
  async execute(token: string, sessionId?: string): Promise<ActionExecution> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/actions/execute', {
      token,
      session_id: sessionId,
    });
    const payload = toCamelCase<ActionExecution>(response.data);
    payload.items = payload.items ?? [];
    return payload;
  },

  /** 放弃待确认操作（不做任何修改）。 */
  async cancel(token: string): Promise<ActionCancellation> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/actions/cancel', { token });
    return toCamelCase<ActionCancellation>(response.data);
  },
};

/**
 * 操作记录（审计）与端间刷新（P2.4 · NFR-11 / FR-14.0.5）。
 */
export const auditApi = {
  async list(params?: {
    source?: string;
    action?: string;
    status?: string;
    actor?: string;
    undoing?: boolean;
    limit?: number;
    offset?: number;
  }): Promise<ActionAuditListResult> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/actions/audit', {
      params: {
        source: params?.source,
        action: params?.action,
        status: params?.status,
        actor: params?.actor,
        undoing: params?.undoing,
        limit: params?.limit,
        offset: params?.offset,
      },
    });
    const payload = toCamelCase<ActionAuditListResult>(response.data);
    payload.items = payload.items ?? [];
    return payload;
  },

  async stats(): Promise<ActionAuditStats> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/actions/audit/stats');
    const payload = toCamelCase<ActionAuditStats>(response.data);
    payload.byAction = payload.byAction ?? {};
    return payload;
  },

  async detail(id: number): Promise<ActionAuditDetail> {
    const response = await apiClient.get<Record<string, unknown>>(`/api/v1/actions/audit/${id}`);
    const payload = toCamelCase<ActionAuditDetail>(response.data);
    payload.undoOps = payload.undoOps ?? [];
    return payload;
  },

  /** 撤销预览：返回"将要做什么"清单 + 确认令牌（**不直接落库**）。 */
  async undo(id: number, options?: { actor?: string; sessionId?: string }): Promise<ActionPreview> {
    const response = await apiClient.post<Record<string, unknown>>(`/api/v1/actions/audit/${id}/undo`, {
      actor: options?.actor ?? 'h5',
      session_id: options?.sessionId,
    });
    const payload = toCamelCase<ActionPreview>(response.data);
    payload.items = payload.items ?? [];
    return payload;
  },

  /** SSE 流地址：写操作成功 → `invalidate` 端间刷新。 */
  getStreamUrl(): string {
    const baseUrl = apiClient.defaults.baseURL || '';
    return `${baseUrl}/api/v1/actions/stream`;
  },
};
