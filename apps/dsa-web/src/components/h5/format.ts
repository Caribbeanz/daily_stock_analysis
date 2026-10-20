/**
 * 移动 H5 展示格式化（列表 / 详情共用）。
 *
 * 口径与后端保持一致：价格 ≥1 保留两位，<1 保留三位；涨红跌绿（中国习惯）。
 */

export const formatPrice = (value?: number | null): string => {
  if (value === null || value === undefined || !Number.isFinite(value)) return '-';
  return Math.abs(value) >= 1 ? value.toFixed(2) : value.toFixed(3);
};

export const formatPct = (value?: number | null): string => {
  if (value === null || value === undefined || !Number.isFinite(value)) return '-';
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}%`;
};

/** 价格 / 涨跌文字色：涨红、跌绿、平灰。 */
export const priceTone = (value?: number | null): string => {
  if (value === null || value === undefined || !Number.isFinite(value) || value === 0) {
    return 'text-price-flat';
  }
  return value > 0 ? 'text-price-up' : 'text-price-down';
};

/** 分析区块色调（up/down/warning/neutral）。 */
export const sectionTone = (tone?: string): string => {
  switch (tone) {
    case 'up':
      return 'text-price-up';
    case 'down':
      return 'text-price-down';
    case 'warning':
      return 'text-warning';
    default:
      return 'text-secondary-text';
  }
};

/** ISO 时间 → `YYYY-MM-DD HH:mm`，解析失败原样返回。 */
export const formatMoment = (value?: string | null): string => {
  if (!value) return '';
  const text = String(value).replace('T', ' ');
  return text.length >= 16 ? text.slice(0, 16) : text;
};
