import type React from 'react';
import { useCallback, useEffect, useState } from 'react';
import { RefreshCw, Undo2, Wifi, WifiOff } from 'lucide-react';
import { actionsApi, auditApi } from '../api/actions';
import type { ActionAuditItem, ActionAuditStats, ActionPreview } from '../api/actions';
import type { ParsedApiError } from '../api/error';
import { getParsedApiError } from '../api/error';
import {
  ApiErrorAlert,
  AppPage,
  Badge,
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  Loading,
  PageHeader,
} from '../components/common';
import { useActionStream } from '../hooks/useActionStream';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import type { UiTextKey } from '../i18n/uiText';
import { formatDateTime } from '../utils/format';

const STATUS_VARIANT: Record<string, 'success' | 'warning' | 'danger' | 'default'> = {
  ok: 'success',
  rejected: 'warning',
  failed: 'danger',
};

const STATUS_LABEL_KEY: Record<string, UiTextKey> = {
  ok: 'audit.status.ok',
  rejected: 'audit.status.rejected',
  failed: 'audit.status.failed',
};

const SOURCE_LABEL_KEY: Record<string, UiTextKey> = {
  h5: 'audit.source.h5',
  feishu: 'audit.source.feishu',
  api: 'audit.source.api',
  system: 'audit.source.system',
};

const STATUS_FILTERS: Array<{ value: string; labelKey: UiTextKey }> = [
  { value: 'all', labelKey: 'audit.filterAllStatus' },
  { value: 'ok', labelKey: 'audit.status.ok' },
  { value: 'rejected', labelKey: 'audit.status.rejected' },
  { value: 'failed', labelKey: 'audit.status.failed' },
];

const SOURCE_FILTERS: Array<{ value: string; labelKey: UiTextKey }> = [
  { value: 'all', labelKey: 'audit.filterAllSource' },
  { value: 'h5', labelKey: 'audit.source.h5' },
  { value: 'feishu', labelKey: 'audit.source.feishu' },
];

const renderValue = (value: unknown): string => {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }
  return String(value);
};

/**
 * 操作记录页（P2.4 · NFR-11 / FR-14.0.5）。
 *
 * 展示写操作的可追溯留痕（改前 / 改后），支持一键撤销（同样二次确认）；
 * 通过 SSE 订阅端间 `invalidate` 事件，任一端写操作后**自动刷新**本页。
 */
const AuditPage: React.FC = () => {
  const { t } = useUiLanguage();
  const [items, setItems] = useState<ActionAuditItem[]>([]);
  const [stats, setStats] = useState<ActionAuditStats | null>(null);
  const [total, setTotal] = useState(0);
  const [source, setSource] = useState('all');
  const [status, setStatus] = useState('all');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [expandedId, setExpandedId] = useState<number | null>(null);

  const [undoTarget, setUndoTarget] = useState<ActionAuditItem | null>(null);
  const [undoPreview, setUndoPreview] = useState<ActionPreview | null>(null);
  const [undoBusy, setUndoBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [list, statsResult] = await Promise.all([
        auditApi.list({
          source: source === 'all' ? undefined : source,
          status: status === 'all' ? undefined : status,
        }),
        auditApi.stats(),
      ]);
      setItems(list.items);
      setTotal(list.total);
      setStats(statsResult);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setLoading(false);
    }
  }, [source, status]);

  useEffect(() => {
    void load();
  }, [load]);

  // 端间刷新：任一端写操作成功 → 后端广播 invalidate → 本页自动刷新
  const { isConnected } = useActionStream({
    onInvalidate: () => {
      void load();
    },
  });

  const openUndo = useCallback(async (item: ActionAuditItem) => {
    setUndoBusy(true);
    try {
      const preview = await auditApi.undo(item.id);
      setUndoPreview(preview);
      setUndoTarget(item);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setUndoBusy(false);
    }
  }, []);

  const confirmUndo = useCallback(async () => {
    if (!undoPreview?.token) return;
    const token = undoPreview.token;
    setUndoTarget(null);
    setUndoPreview(null);
    setUndoBusy(true);
    try {
      await actionsApi.execute(token);
      await load();
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setUndoBusy(false);
    }
  }, [undoPreview, load]);

  const statusLabel = (value: string): string => {
    const key = STATUS_LABEL_KEY[value];
    return key ? t(key) : value;
  };

  const sourceLabel = (value: string): string => {
    const key = SOURCE_LABEL_KEY[value];
    return key ? t(key) : value;
  };

  return (
    <AppPage>
      <PageHeader
        title={t('audit.title')}
        description={t('audit.description')}
        actions={
          <>
            <span className="mr-1 flex items-center gap-1 text-xs text-secondary-text">
              {isConnected ? <Wifi className="h-3.5 w-3.5" /> : <WifiOff className="h-3.5 w-3.5" />}
              {t('audit.live')}
            </span>
            <Button variant="outline" onClick={() => void load()} disabled={loading}>
              <RefreshCw className="mr-1 h-4 w-4" />
              {t('common.retry')}
            </Button>
          </>
        }
      />

      {error ? <ApiErrorAlert error={error} /> : null}

      {stats ? (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Badge variant="info">
            {t('audit.statsTotal')} {stats.total}
          </Badge>
          <Badge variant="success">
            {t('audit.statsOk')} {stats.ok}
          </Badge>
          <Badge variant="warning">
            {t('audit.statsRejected')} {stats.rejected}
          </Badge>
          <Badge variant="danger">
            {t('audit.statsFailed')} {stats.failed}
          </Badge>
          <Badge variant="default">
            {t('audit.statsUndo')} {stats.undoRecords}
          </Badge>
        </div>
      ) : null}

      <div className="mt-4 flex flex-wrap items-center gap-2">
        {SOURCE_FILTERS.map((option) => (
          <button
            key={`source-${option.value}`}
            type="button"
            onClick={() => setSource(option.value)}
            className={`rounded-full border px-3 py-1 text-xs transition-colors ${
              source === option.value
                ? 'border-[hsl(var(--primary))] bg-[var(--nav-active-bg)] text-[hsl(var(--primary))]'
                : 'border-border/70 text-secondary-text hover:text-foreground'
            }`}
          >
            {t(option.labelKey)}
          </button>
        ))}
        <span className="mx-1 h-4 w-px bg-border/70" />
        {STATUS_FILTERS.map((option) => (
          <button
            key={`status-${option.value}`}
            type="button"
            onClick={() => setStatus(option.value)}
            className={`rounded-full border px-3 py-1 text-xs transition-colors ${
              status === option.value
                ? 'border-[hsl(var(--primary))] bg-[var(--nav-active-bg)] text-[hsl(var(--primary))]'
                : 'border-border/70 text-secondary-text hover:text-foreground'
            }`}
          >
            {t(option.labelKey)}
          </button>
        ))}
        <span className="ml-auto text-xs text-secondary-text">
          {t('audit.statsTotal')} {total}
        </span>
      </div>

      <div className="mt-4 space-y-3">
        {loading && items.length === 0 ? <Loading label={t('common.loading')} /> : null}

        {!loading && items.length === 0 ? (
          <EmptyState title={t('audit.empty')} description={t('audit.emptyHint')} />
        ) : null}

        {items.map((item) => {
          const expanded = expandedId === item.id;
          const isUndoRecord = item.undoOf !== null;
          return (
            <Card key={item.id} variant="bordered">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant={STATUS_VARIANT[item.status] ?? 'default'}>
                    {statusLabel(item.status)}
                  </Badge>
                  <span className="text-sm font-medium text-foreground">
                    {item.actionLabel || item.action}
                  </span>
                  {item.target ? (
                    <span className="text-xs text-secondary-text">
                      {t('audit.target')} {item.target}
                    </span>
                  ) : null}
                  <Badge variant="default">{sourceLabel(item.source)}</Badge>
                  {isUndoRecord ? (
                    <Badge variant="info">
                      {t('audit.undoOf').replace('{id}', String(item.undoOf))}
                    </Badge>
                  ) : null}
                  {item.undoneBy !== null ? <Badge variant="warning">{t('audit.undone')}</Badge> : null}
                  <span className="text-xs text-secondary-text">
                    {item.createdAt ? formatDateTime(item.createdAt) : ''}
                  </span>
                </div>
                <div className="flex items-center gap-2">
                  <Button
                    variant="ghost"
                    onClick={() => setExpandedId(expanded ? null : item.id)}
                  >
                    {expanded ? t('audit.hideDiff') : t('audit.viewDiff')}
                  </Button>
                  {item.undoable ? (
                    <Button
                      variant="outline"
                      onClick={() => void openUndo(item)}
                      disabled={undoBusy}
                    >
                      <Undo2 className="mr-1 h-4 w-4" />
                      {t('audit.undo')}
                    </Button>
                  ) : null}
                </div>
              </div>

              {item.description ? (
                <p className="mt-2 whitespace-pre-wrap break-words text-sm text-foreground">
                  {item.description}
                </p>
              ) : null}

              {expanded ? (
                <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
                  <div className="rounded-lg border border-border/60 bg-hover/40 p-3">
                    <p className="mb-1 text-xs font-medium text-secondary-text">{t('audit.before')}</p>
                    <pre className="overflow-x-auto whitespace-pre-wrap break-words text-xs text-foreground">
                      {renderValue(item.before)}
                    </pre>
                  </div>
                  <div className="rounded-lg border border-border/60 bg-hover/40 p-3">
                    <p className="mb-1 text-xs font-medium text-secondary-text">{t('audit.after')}</p>
                    <pre className="overflow-x-auto whitespace-pre-wrap break-words text-xs text-foreground">
                      {renderValue(item.after)}
                    </pre>
                  </div>
                </div>
              ) : null}
            </Card>
          );
        })}
      </div>

      <ConfirmDialog
        isOpen={undoTarget !== null}
        title={t('audit.undoTitle')}
        message={undoPreview?.summary ?? ''}
        confirmText={t('audit.undoConfirm')}
        cancelText={t('common.cancel')}
        isDanger
        onConfirm={() => void confirmUndo()}
        onCancel={() => {
          setUndoTarget(null);
          setUndoPreview(null);
        }}
      />
    </AppPage>
  );
};

export default AuditPage;
