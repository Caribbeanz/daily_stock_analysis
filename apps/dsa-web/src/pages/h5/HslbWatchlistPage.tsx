import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ChevronRight, RefreshCw } from 'lucide-react';
import { h5Api, type H5WatchlistItem, type H5WatchlistResponse } from '../../api/h5';
import { H5ActionBadge } from '../../components/h5/H5ActionBadge';
import { H5MarketCard } from '../../components/h5/H5MarketCard';
import { formatPct, formatPrice, priceTone } from '../../components/h5/format';
import { cn } from '../../utils/cn';

/**
 * 高抛低吸 · 移动 H5 列表（`/h5`）。
 *
 * 只做一件事：一屏看清「哪只该动」。
 * 每行 = 名称 + 代码 + 当日价格 / 涨跌幅 + 操作建议；悬空信息（场景、紧急）作为次要线索。
 *
 * 约束：
 * - 独立渲染（App.tsx 在鉴权判断前早返回），不套站点 Shell；
 * - 「暂无信号」与「观望」严格区分，前者不代表建议按兵不动；
 * - 下拉刷新保留旧数据，刷新期间不清空列表。
 */
const PULL_THRESHOLD = 48;

const SkeletonRow: React.FC = () => (
  <div className="flex items-center gap-3 rounded-xl border border-border bg-card px-3 py-3">
    <div className="min-w-0 flex-1 space-y-2">
      <div className="h-3.5 w-24 animate-pulse rounded bg-muted" />
      <div className="h-2.5 w-16 animate-pulse rounded bg-muted" />
    </div>
    <div className="h-3.5 w-14 animate-pulse rounded bg-muted" />
    <div className="h-6 w-12 animate-pulse rounded bg-muted" />
  </div>
);

const WatchlistRow: React.FC<{ item: H5WatchlistItem; onOpen: (symbol: string) => void }> = ({ item, onOpen }) => (
  <button
    type="button"
    onClick={() => onOpen(item.symbol)}
    className={cn(
      'flex w-full items-center gap-3 rounded-xl border border-border bg-card px-3 py-3 text-left',
      'transition-colors active:bg-muted',
      item.isUrgent && 'border-price-down',
    )}
  >
    <div className="min-w-0 flex-1">
      <div className="flex items-center gap-1.5">
        <span className="truncate text-[15px] font-medium text-foreground">{item.name || item.symbol}</span>
        {item.isUrgent ? (
          <span className="shrink-0 rounded bg-muted px-1 text-[10px] leading-4 text-price-down">紧急</span>
        ) : null}
      </div>
      <div className="mt-0.5 flex items-center gap-2 text-[11px] text-muted-text">
        <span className="font-mono tracking-tight">{item.symbol}</span>
        {item.scenarioLabel ? <span className="truncate">{item.scenarioLabel}</span> : null}
      </div>
    </div>

    <div className="shrink-0 text-right">
      <div className={cn('text-[16px] font-medium tabular-nums', priceTone(item.changePercent))}>
        {formatPrice(item.price)}
      </div>
      <div className={cn('text-[11px] tabular-nums', priceTone(item.changePercent))}>
        {formatPct(item.changePercent)}
      </div>
    </div>

    <H5ActionBadge label={item.displayAction} tone={item.actionTone} urgent={item.isUrgent} />
    <ChevronRight className="h-4 w-4 shrink-0 text-muted-text" aria-hidden />
  </button>
);

const HslbWatchlistPage: React.FC = () => {
  const navigate = useNavigate();
  const [data, setData] = useState<H5WatchlistResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [error, setError] = useState('');
  const [pullDistance, setPullDistance] = useState(0);
  const pullStartY = useRef<number | null>(null);

  const load = useCallback(async (options?: { refresh?: boolean }) => {
    if (options?.refresh) {
      setIsRefreshing(true);
    } else {
      setIsLoading(true);
    }
    try {
      const payload = await h5Api.watchlist(Boolean(options?.refresh));
      setData(payload);
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败，请稍后重试');
    } finally {
      setIsLoading(false);
      setIsRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const items = data?.items ?? [];
  const hasItems = items.length > 0;

  const emptyHint = useMemo(() => {
    if (isLoading) return '';
    if (error) return '';
    return '暂无自选标的。可在「设置」或对话中添加后，交易日 20:00 自动生成建议。';
  }, [error, isLoading]);

  const onTouchStart = (event: React.TouchEvent) => {
    if (window.scrollY > 0 || isRefreshing) return;
    pullStartY.current = event.touches[0]?.clientY ?? null;
  };

  const onTouchMove = (event: React.TouchEvent) => {
    if (pullStartY.current === null) return;
    const delta = (event.touches[0]?.clientY ?? 0) - pullStartY.current;
    if (delta <= 0) {
      setPullDistance(0);
      return;
    }
    setPullDistance(Math.min(72, delta * 0.45));
  };

  const onTouchEnd = () => {
    if (pullDistance >= PULL_THRESHOLD) {
      void load({ refresh: true });
    }
    pullStartY.current = null;
    setPullDistance(0);
  };

  return (
    <div
      className="min-h-screen bg-background text-foreground"
      onTouchStart={onTouchStart}
      onTouchMove={onTouchMove}
      onTouchEnd={onTouchEnd}
    >
      <header className="sticky top-0 z-20 border-b border-border bg-background/95 px-4 pb-2 pt-3 backdrop-blur">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="text-[17px] font-medium leading-6">高抛低吸</h1>
            <p className="mt-0.5 text-[11px] text-muted-text">
              {data?.asOf ? `数据截至 ${data.asOf} 收盘 · 收盘后自动更新` : '收盘后自动更新'}
            </p>
          </div>
          <button
            type="button"
            aria-label="刷新"
            onClick={() => void load({ refresh: true })}
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-border text-secondary-text active:bg-muted"
          >
            <RefreshCw className={cn('h-4 w-4', isRefreshing && 'animate-spin')} aria-hidden />
          </button>
        </div>
      </header>

      <div
        className="flex items-center justify-center overflow-hidden text-[11px] text-muted-text transition-all"
        style={{ height: pullDistance }}
      >
        {isRefreshing ? '正在刷新…' : pullDistance >= PULL_THRESHOLD ? '松开刷新' : '下拉刷新'}
      </div>

      <main className="space-y-2 px-3 pb-10 pt-2">
        {data?.market ? <H5MarketCard market={data.market} /> : null}

        {error ? (
          <div className="rounded-xl border border-border bg-card p-4">
            <p className="text-[13px] text-foreground">列表加载失败</p>
            <p className="mt-1 text-[12px] text-muted-text">{error}</p>
            <button
              type="button"
              onClick={() => void load()}
              className="mt-3 rounded-lg border border-border px-3 py-1.5 text-[12px] text-secondary-text active:bg-muted"
            >
              重试
            </button>
          </div>
        ) : null}

        {isLoading && !hasItems
          ? Array.from({ length: 5 }).map((_, index) => <SkeletonRow key={`skeleton-${index}`} />)
          : null}

        {!isLoading && !error && !hasItems ? (
          <div className="rounded-xl border border-dashed border-border p-6 text-center">
            <p className="text-[13px] text-secondary-text">还没有自选标的</p>
            <p className="mt-1 text-[12px] leading-5 text-muted-text">{emptyHint}</p>
          </div>
        ) : null}

        {hasItems ? (
          <p className="px-1 pb-0.5 pt-1 text-[12px] text-muted-text">关注标的（{items.length}）</p>
        ) : null}

        {items.map((item) => (
          <WatchlistRow key={item.symbol} item={item} onOpen={(symbol) => navigate(`/h5/stocks/${symbol}`)} />
        ))}

        {data?.degraded?.length ? (
          <div className="rounded-xl border border-warning bg-card p-3">
            <p className="text-[11px] font-medium text-warning">部分数据不可用</p>
            <ul className="mt-1 space-y-0.5">
              {data.degraded.map((note) => (
                <li key={note} className="text-[11px] leading-5 text-muted-text">
                  · {note}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {data?.disclaimer ? (
          <p className="px-1 pt-2 text-[10px] leading-4 text-muted-text">{data.disclaimer}</p>
        ) : null}
      </main>
    </div>
  );
};

export default HslbWatchlistPage;
