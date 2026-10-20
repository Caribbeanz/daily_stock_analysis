import type React from 'react';
import type { H5MarketBlock } from '../../api/h5';
import { formatPct, formatPrice, priceTone } from './format';
import { cn } from '../../utils/cn';

const INDICATOR_TONE: Record<string, string> = {
  up: 'text-price-up',
  down: 'text-price-down',
  warning: 'text-warning',
  neutral: 'text-foreground',
};

const REGIME_TONE: Record<string, string> = {
  up: 'border-price-up text-price-up',
  down: 'border-price-down text-price-down',
  warning: 'border-warning text-warning',
  neutral: 'border-border text-secondary-text',
};

/**
 * 首页大盘卡片（M2-A）。
 *
 * 层级：点位 / 涨跌 → 6 格指标 → 环境评级 + 节奏建议 → 一段总结。
 * 全部数据来自后端 `market` 字段，前端不做任何计算或推断；
 * 缺失项显示 `-`，大盘不可用时整卡降级但不影响列表。
 */
export const H5MarketCard: React.FC<{ market: H5MarketBlock }> = ({ market }) => {
  const index = market.index;
  const available = index?.close !== null && index?.close !== undefined;

  return (
    <section className="rounded-xl border border-border bg-card px-4 py-3">
      <div className="flex items-start justify-between gap-2">
        <p className="text-[11px] text-muted-text">大盘 · {index?.name || '上证指数'}</p>
        {market.others?.length ? (
          <p className="shrink-0 text-[10px] text-muted-text">
            {market.others
              .map((item) => `${item.name.replace('指数', '').replace('成指', '')} ${formatPct(item.changePct)}`)
              .join(' · ')}
          </p>
        ) : null}
      </div>

      <div className="mt-1 flex items-end gap-2">
        <span className={cn('text-[28px] font-medium leading-none tabular-nums', priceTone(index?.changePct))}>
          {formatPrice(index?.close)}
        </span>
        <span className={cn('pb-0.5 text-[13px] tabular-nums', priceTone(index?.changePct))}>
          {formatPct(index?.changePct)}
        </span>
      </div>

      {market.indicators?.length ? (
        <dl className="mt-3 grid grid-cols-3 gap-x-2 gap-y-2 border-t border-border pt-3">
          {market.indicators.map((item) => (
            <div key={item.key} className="min-w-0">
              <dt className="truncate text-[10px] text-muted-text">{item.label}</dt>
              <dd className={cn('mt-0.5 truncate text-[12px] tabular-nums', INDICATOR_TONE[item.tone] ?? INDICATOR_TONE.neutral)}>
                {item.value}
              </dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="mt-3 border-t border-border pt-3 text-[11px] text-muted-text">
          {available ? '指标暂不可用' : '大盘数据暂不可用'}
        </p>
      )}

      {market.regime?.label ? (
        <div className="mt-3 border-t border-border pt-3">
          <span
            className={cn(
              'inline-flex rounded-md border px-2 py-0.5 text-[11px]',
              REGIME_TONE[market.regime.tone] ?? REGIME_TONE.neutral,
            )}
          >
            环境：{market.regime.label}
          </span>
          {market.advice ? (
            <p className="mt-1.5 text-[12px] leading-5 text-foreground">{market.advice}</p>
          ) : null}
          {market.alerts?.length ? (
            <p className="mt-1 text-[11px] leading-4 text-warning">{market.alerts.join(' · ')}</p>
          ) : null}
        </div>
      ) : null}

      {market.summary ? (
        <p className="mt-2 text-[11px] leading-5 text-secondary-text">{market.summary}</p>
      ) : null}
    </section>
  );
};
