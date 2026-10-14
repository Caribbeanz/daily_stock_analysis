import type React from 'react';
import { useCallback, useEffect, useState } from 'react';
import { RefreshCw, Trash2, Undo2 } from 'lucide-react';
import { memoryApi } from '../api/memory';
import type { MemoryDetail, MemoryItem, MemoryStats, MemoryType } from '../api/memory';
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
  InlineAlert,
  Loading,
  PageHeader,
} from '../components/common';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import type { UiTextKey } from '../i18n/uiText';
import { formatDateTime } from '../utils/format';

const TYPE_LABEL_KEY: Record<string, UiTextKey> = {
  position_change: 'memory.type.positionChange',
  preference: 'memory.type.preference',
  confirmed_action: 'memory.type.confirmedAction',
  analysis_conclusion: 'memory.type.analysisConclusion',
};

const TYPE_BADGE_VARIANT: Record<string, 'default' | 'info' | 'warning' | 'success'> = {
  position_change: 'warning',
  preference: 'info',
  confirmed_action: 'success',
  analysis_conclusion: 'default',
};

const FILTERS: Array<{ value: MemoryType | 'all'; labelKey: UiTextKey }> = [
  { value: 'all', labelKey: 'memory.filterAll' },
  { value: 'position_change', labelKey: 'memory.type.positionChange' },
  { value: 'preference', labelKey: 'memory.type.preference' },
  { value: 'confirmed_action', labelKey: 'memory.type.confirmedAction' },
  { value: 'analysis_conclusion', labelKey: 'memory.type.analysisConclusion' },
];

/**
 * 记忆管理页（P2.3 · FR-15.5）。
 *
 * 查看 / 回链来源 / 软删 / 硬删 / 恢复 / 一键清空（强确认）。
 * 记忆由对话压缩产生，**只增不删**（决议 D5）；删除为用户显式操作。
 */
const MemoryPage: React.FC = () => {
  const { t } = useUiLanguage();
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [total, setTotal] = useState(0);
  const [stats, setStats] = useState<MemoryStats | null>(null);
  const [filter, setFilter] = useState<MemoryType | 'all'>('all');
  const [includeDeleted, setIncludeDeleted] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<MemoryDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const [pendingDelete, setPendingDelete] = useState<MemoryItem | null>(null);
  const [showClearConfirm, setShowClearConfirm] = useState(false);
  const [clearing, setClearing] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [list, statsResult] = await Promise.all([
        memoryApi.list({
          memoryType: filter === 'all' ? undefined : filter,
          includeDeleted,
        }),
        memoryApi.stats(),
      ]);
      setItems(list.items);
      setTotal(list.total);
      setStats(statsResult);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setLoading(false);
    }
  }, [filter, includeDeleted]);

  useEffect(() => {
    void load();
  }, [load]);

  const toggleSources = useCallback(
    async (item: MemoryItem) => {
      if (expandedId === item.id) {
        setExpandedId(null);
        setDetail(null);
        return;
      }
      setExpandedId(item.id);
      setDetail(null);
      setDetailLoading(true);
      try {
        setDetail(await memoryApi.detail(item.id));
      } catch (err) {
        setError(getParsedApiError(err));
        setExpandedId(null);
      } finally {
        setDetailLoading(false);
      }
    },
    [expandedId],
  );

  const confirmDelete = useCallback(
    async (hard: boolean) => {
      if (!pendingDelete) return;
      const target = pendingDelete;
      setPendingDelete(null);
      setBusyId(target.id);
      try {
        await memoryApi.remove(target.id, hard);
        if (expandedId === target.id) {
          setExpandedId(null);
          setDetail(null);
        }
        await load();
      } catch (err) {
        setError(getParsedApiError(err));
      } finally {
        setBusyId(null);
      }
    },
    [pendingDelete, expandedId, load],
  );

  const restore = useCallback(
    async (item: MemoryItem) => {
      setBusyId(item.id);
      try {
        await memoryApi.restore(item.id);
        await load();
      } catch (err) {
        setError(getParsedApiError(err));
      } finally {
        setBusyId(null);
      }
    },
    [load],
  );

  const clearAll = useCallback(async () => {
    setShowClearConfirm(false);
    setClearing(true);
    try {
      await memoryApi.clear({ hard: true });
      setExpandedId(null);
      setDetail(null);
      await load();
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setClearing(false);
    }
  }, [load]);

  const typeLabel = (item: MemoryItem): string => {
    const key = TYPE_LABEL_KEY[item.memoryType];
    return key ? t(key) : item.memoryType;
  };

  return (
    <AppPage>
      <PageHeader
        title={t('memory.title')}
        description={t('memory.description')}
        actions={
          <>
            <Button variant="outline" onClick={() => void load()} disabled={loading}>
              <RefreshCw className="mr-1 h-4 w-4" />
              {t('common.retry')}
            </Button>
            <Button
              variant="danger-subtle"
              onClick={() => setShowClearConfirm(true)}
              disabled={clearing || total === 0}
            >
              <Trash2 className="mr-1 h-4 w-4" />
              {t('memory.clearAll')}
            </Button>
          </>
        }
      />

      {error ? <ApiErrorAlert error={error} /> : null}

      {stats ? (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Badge variant="info">
            {t('memory.statsActive')} {stats.active}
          </Badge>
          <Badge variant="default">
            {t('memory.statsDeleted')} {stats.deleted}
          </Badge>
        </div>
      ) : null}

      <div className="mt-4 flex flex-wrap items-center gap-2">
        {FILTERS.map((option) => (
          <button
            key={option.value}
            type="button"
            onClick={() => setFilter(option.value)}
            className={`rounded-full border px-3 py-1 text-xs transition-colors ${
              filter === option.value
                ? 'border-[hsl(var(--primary))] bg-[var(--nav-active-bg)] text-[hsl(var(--primary))]'
                : 'border-border/70 text-secondary-text hover:text-foreground'
            }`}
          >
            {t(option.labelKey)}
          </button>
        ))}
        <label className="ml-1 flex items-center gap-1.5 text-xs text-secondary-text">
          <input
            type="checkbox"
            checked={includeDeleted}
            onChange={(event) => setIncludeDeleted(event.target.checked)}
          />
          {t('memory.showDeleted')}
        </label>
      </div>

      <div className="mt-4 space-y-3">
        {loading && items.length === 0 ? <Loading label={t('common.loading')} /> : null}

        {!loading && items.length === 0 ? (
          <EmptyState title={t('memory.empty')} description={t('memory.emptyHint')} />
        ) : null}

        {items.map((item) => {
          const isDeleted = item.status === 'deleted';
          const expanded = expandedId === item.id;
          return (
            <Card key={item.id} variant="bordered">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant={TYPE_BADGE_VARIANT[item.memoryType] ?? 'default'}>
                    {typeLabel(item)}
                  </Badge>
                  {isDeleted ? <Badge variant="danger">{t('memory.deleted')}</Badge> : null}
                  <span className="text-xs text-secondary-text">
                    {item.createdAt ? formatDateTime(item.createdAt) : ''}
                  </span>
                  {item.sessionId ? (
                    <span className="text-xs text-secondary-text">
                      {t('memory.session')} {item.sessionId}
                    </span>
                  ) : null}
                </div>
                <div className="flex items-center gap-2">
                  <Button variant="ghost" onClick={() => void toggleSources(item)}>
                    {expanded ? t('memory.hideSources') : t('memory.viewSources')}
                  </Button>
                  {isDeleted ? (
                    <Button
                      variant="outline"
                      onClick={() => void restore(item)}
                      disabled={busyId === item.id}
                    >
                      <Undo2 className="mr-1 h-4 w-4" />
                      {t('memory.restore')}
                    </Button>
                  ) : (
                    <Button
                      variant="danger-subtle"
                      onClick={() => setPendingDelete(item)}
                      disabled={busyId === item.id}
                    >
                      <Trash2 className="mr-1 h-4 w-4" />
                      {t('memory.delete')}
                    </Button>
                  )}
                </div>
              </div>

              <p className="mt-2 whitespace-pre-wrap break-words text-sm text-foreground">{item.content}</p>

              {expanded ? (
                <div className="mt-3 rounded-lg border border-border/60 bg-hover/40 p-3">
                  <p className="mb-2 text-xs font-medium text-secondary-text">
                    {t('memory.sourceCount').replace('{count}', String(item.sourceMessageIds.length))}
                  </p>
                  {detailLoading ? <Loading label={t('common.loading')} /> : null}
                  {!detailLoading && detail && detail.sources.length === 0 ? (
                    <p className="text-xs text-secondary-text">{t('memory.noSources')}</p>
                  ) : null}
                  <div className="space-y-2">
                    {(detail?.sources ?? []).map((source) => (
                      <div key={source.id} className="rounded-md bg-card/70 p-2 text-xs">
                        <span className="mr-2 font-medium text-secondary-text">
                          {source.role === 'user' ? t('memory.roleUser') : t('memory.roleAssistant')}
                        </span>
                        <span className="whitespace-pre-wrap break-words text-foreground">
                          {source.content}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}
            </Card>
          );
        })}
      </div>

      {includeDeleted && items.length > 0 ? (
        <InlineAlert className="mt-4" variant="info" message={t('memory.deletedHint')} />
      ) : null}

      <ConfirmDialog
        isOpen={pendingDelete !== null}
        title={t('memory.deleteTitle')}
        message={t('memory.deleteMessage')}
        confirmText={t('memory.delete')}
        cancelText={t('common.cancel')}
        isDanger
        onConfirm={() => void confirmDelete(false)}
        onCancel={() => setPendingDelete(null)}
      />

      <ConfirmDialog
        isOpen={showClearConfirm}
        title={t('memory.clearTitle')}
        message={t('memory.clearMessage')}
        confirmText={t('memory.clearConfirm')}
        cancelText={t('common.cancel')}
        isDanger
        onConfirm={() => void clearAll()}
        onCancel={() => setShowClearConfirm(false)}
      />
    </AppPage>
  );
};

export default MemoryPage;
