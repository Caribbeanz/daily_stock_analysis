import { useEffect, useRef, useState } from 'react';
import { auditApi, type ActionInvalidatePayload } from '../api/actions';

/**
 * 写操作端间刷新 SSE hook（P2.4 · FR-14.0.5）。
 *
 * 订阅 `/api/v1/actions/stream`：任一端（H5 / 飞书 / API）完成写操作后，
 * 后端广播一条 `invalidate` 事件，本 hook 解析后回调 `onInvalidate`，
 * 由页面据 `scopes` 做**增量刷新**，无需重开页面。
 *
 * 轻量自愈：连接断开后按 `reconnectDelay` 自动重连；`EventSource` 不可用时静默降级。
 */

export interface UseActionStreamOptions {
  /** 收到端间刷新事件 */
  onInvalidate?: (payload: ActionInvalidatePayload) => void;
  /** 连接建立 */
  onConnected?: () => void;
  /** 连接错误 */
  onError?: (error: Event) => void;
  /** 是否启用 */
  enabled?: boolean;
  /** 是否自动重连 */
  autoReconnect?: boolean;
  /** 重连间隔（毫秒） */
  reconnectDelay?: number;
}

export interface UseActionStreamResult {
  isConnected: boolean;
}

const toScopes = (value: unknown): string[] =>
  Array.isArray(value) ? value.map(String) : [];

const toNumberOrNull = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) ? value : null;

const parseInvalidate = (raw: string): ActionInvalidatePayload | null => {
  try {
    const data = JSON.parse(raw) as Record<string, unknown>;
    return {
      scopes: toScopes(data.scopes),
      action: String(data.action ?? ''),
      label: String(data.label ?? ''),
      target: String(data.target ?? ''),
      source: String(data.source ?? ''),
      auditId: toNumberOrNull(data.audit_id),
      undoOf: toNumberOrNull(data.undo_of),
    };
  } catch (error) {
    console.error('Failed to parse action invalidate event:', error);
    return null;
  }
};

export function useActionStream(options: UseActionStreamOptions = {}): UseActionStreamResult {
  const { onInvalidate, onConnected, onError, enabled = true, autoReconnect = true, reconnectDelay = 3000 } = options;

  const [isConnected, setIsConnected] = useState(false);
  const callbacksRef = useRef({ onInvalidate, onConnected, onError });

  useEffect(() => {
    callbacksRef.current = { onInvalidate, onConnected, onError };
  });

  useEffect(() => {
    if (!enabled || typeof window === 'undefined' || typeof window.EventSource !== 'function') {
      return undefined;
    }

    let source: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let closed = false;

    const connect = () => {
      if (closed) return;
      source = new window.EventSource(auditApi.getStreamUrl(), { withCredentials: true });

      source.addEventListener('connected', () => {
        setIsConnected(true);
        callbacksRef.current.onConnected?.();
      });

      source.addEventListener('invalidate', (event) => {
        const payload = parseInvalidate((event as MessageEvent<string>).data);
        if (payload) {
          callbacksRef.current.onInvalidate?.(payload);
        }
      });

      source.onerror = (error) => {
        setIsConnected(false);
        callbacksRef.current.onError?.(error);
        if (source) {
          source.close();
          source = null;
        }
        if (autoReconnect && !closed) {
          reconnectTimer = setTimeout(connect, reconnectDelay);
        }
      };
    };

    connect();

    return () => {
      closed = true;
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
      }
      if (source) {
        source.close();
        source = null;
      }
      setIsConnected(false);
    };
  }, [autoReconnect, enabled, reconnectDelay]);

  return { isConnected };
}

export default useActionStream;
