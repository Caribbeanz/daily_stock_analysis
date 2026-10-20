import type React from 'react';
import { cn } from '../../utils/cn';

/** 建议标签色调 → 样式（与涨跌色同源，但语义是"建议方向"而非"价格涨跌"）。 */
const TONE_CLASS: Record<string, string> = {
  up: 'border-price-up text-price-up',
  down: 'border-price-down text-price-down',
  flat: 'border-border text-secondary-text',
  muted: 'border-dashed border-border text-muted-text',
};

type H5ActionBadgeProps = {
  label: string;
  tone?: string;
  urgent?: boolean;
  size?: 'sm' | 'md';
};

/**
 * 操作建议标签：建仓 / 加仓（红）、减仓 / 清仓（绿）、持有 / 观望（灰）、暂无信号（虚线灰）。
 *
 * 紧急（止损 / 清仓）带实心圆点，避免只靠颜色传达优先级。
 */
export const H5ActionBadge: React.FC<H5ActionBadgeProps> = ({ label, tone = 'flat', urgent = false, size = 'md' }) => (
  <span
    className={cn(
      'inline-flex shrink-0 items-center gap-1 rounded-md border font-medium',
      size === 'sm' ? 'px-1.5 py-0.5 text-[11px]' : 'px-2 py-1 text-[12px]',
      TONE_CLASS[tone] ?? TONE_CLASS.flat,
    )}
  >
    {urgent ? <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden /> : null}
    {label || '观望'}
  </span>
);
