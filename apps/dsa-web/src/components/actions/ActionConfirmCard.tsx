import type React from 'react';
import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, CheckCircle2, Loader2, ShieldAlert } from 'lucide-react';
import {
  actionsApi,
  type ActionExecution,
  type ActionPreview,
} from '../../api/actions';
import { cn } from '../../utils/cn';
import { useUiLanguage } from '../../contexts/UiLanguageContext';

/**
 * 写操作二次确认卡（P2.2 · FR-14.2.6）。
 *
 * 调用方只需给出**自然语言指令**（如「把 601919 止损设为 15」），组件负责：
 * `preview`（列出"将要做什么"）→ 用户点「确认执行」→ `execute` → 展示逐条回执。
 * 「取消」会作废令牌，**不做任何修改**。
 */
export type ActionConfirmCardProps = {
  /** 待执行的写指令原话（支持一句话多指令）。 */
  message: string;
  /** 上下文标的（详情页传入）。 */
  symbol?: string;
  /** 执行成功后的回调（例如刷新详情数据）。 */
  onExecuted?: () => void;
  className?: string;
};

export const ActionConfirmCard: React.FC<ActionConfirmCardProps> = ({
  message,
  symbol,
  onExecuted,
  className,
}) => {
  const { t } = useUiLanguage();
  const [preview, setPreview] = useState<ActionPreview | null>(null);
  const [execution, setExecution] = useState<ActionExecution | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const loadPreview = useCallback(async () => {
    setBusy(true);
    setError('');
    setExecution(null);
    try {
      setPreview(await actionsApi.preview(message, { symbol }));
    } catch {
      setError(t('actionCard.previewFailed'));
    } finally {
      setBusy(false);
    }
  }, [message, symbol, t]);

  useEffect(() => {
    void loadPreview();
  }, [loadPreview]);

  const handleConfirm = useCallback(async () => {
    if (!preview?.token) return;
    setBusy(true);
    setError('');
    try {
      const result = await actionsApi.execute(preview.token);
      setExecution(result);
      setPreview(null);
      if (result.ok) onExecuted?.();
    } catch {
      setError(t('actionCard.executeFailed'));
    } finally {
      setBusy(false);
    }
  }, [preview, onExecuted, t]);

  const handleCancel = useCallback(async () => {
    if (preview?.token) {
      try {
        await actionsApi.cancel(preview.token);
      } catch {
        /* 取消失败不阻塞界面：令牌会自然过期 */
      }
    }
    setPreview(null);
  }, [preview]);

  if (busy && !preview && !execution) {
    return (
      <div className={cn('flex items-center gap-2 rounded-xl border border-border/60 bg-card/70 px-4 py-3 text-xs text-secondary-text', className)}>
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        {t('actionCard.loading')}
      </div>
    );
  }

  if (execution) {
    return (
      <div className={cn('rounded-xl border border-border/60 bg-card/80 px-4 py-3', className)}>
        <p className="mb-2 flex items-center gap-1.5 text-xs font-medium text-foreground">
          <CheckCircle2 className={cn('h-3.5 w-3.5', execution.ok ? 'text-price-up' : 'text-warning')} />
          {execution.ok ? t('actionCard.executed') : t('actionCard.partial')}
        </p>
        <pre className="whitespace-pre-wrap break-words font-sans text-xs leading-relaxed text-secondary-text">
          {execution.receipt}
        </pre>
      </div>
    );
  }

  if (error) {
    return (
      <div className={cn('flex items-start gap-2 rounded-xl border border-warning/40 bg-warning/10 px-4 py-3 text-xs text-foreground', className)}>
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        <span>{error}</span>
      </div>
    );
  }

  if (!preview) return null;

  if (preview.status !== 'pending_confirmation') {
    return (
      <div className={cn('flex items-start gap-2 rounded-xl border border-border/60 bg-elevated/40 px-4 py-3 text-xs text-secondary-text', className)}>
        <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        <span className="whitespace-pre-wrap break-words">{preview.message || preview.summary}</span>
      </div>
    );
  }

  return (
    <div
      className={cn(
        'rounded-xl border border-primary/40 bg-primary/5 px-4 py-3',
        className,
      )}
    >
      <p className="mb-2 text-xs font-medium text-foreground">
        {t('actionCard.title', { count: String(preview.items.length) })}
      </p>
      <ol className="mb-3 space-y-1 text-xs leading-relaxed text-secondary-text">
        {preview.items.map((item, index) => (
          <li key={`${item.op}-${index}`} className="flex gap-1.5">
            <span className="text-primary">{index + 1}.</span>
            <span className="break-words">{item.description}</span>
          </li>
        ))}
      </ol>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => void handleConfirm()}
          disabled={busy}
          className="inline-flex items-center gap-1 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-opacity disabled:opacity-60"
        >
          {busy && <Loader2 className="h-3 w-3 animate-spin" />}
          {t('actionCard.confirm')}
        </button>
        <button
          type="button"
          onClick={() => void handleCancel()}
          disabled={busy}
          className="rounded-lg border border-border/70 bg-card/80 px-3 py-1.5 text-xs font-medium text-secondary-text transition-colors hover:text-foreground disabled:opacity-60"
        >
          {t('actionCard.cancel')}
        </button>
        <span className="ml-auto text-[10px] text-secondary-text">{t('actionCard.hint')}</span>
      </div>
    </div>
  );
};
