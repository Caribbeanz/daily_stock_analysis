/**
 * 助手页面的展示格式化。
 *
 * 涨跌配色走**A 股口径：红涨绿跌**（需求 P0-2 / 「红涨绿跌（>0 红 / <0 绿 / =0 灰）」）。
 * 这与仓库里回测页的美股口径相反，是刻意的：同一个数字在 A 股界面里红绿反了比不配色更糟。
 */

export const pctText = (value: number | null | undefined): string => {
  if (value === null || value === undefined || Number.isNaN(value)) return '--';
  const sign = value > 0 ? '+' : '';
  return `${sign}${value.toFixed(2)}%`;
};

export const pctClass = (value: number | null | undefined): string => {
  if (value === null || value === undefined || Number.isNaN(value) || value === 0) {
    return 'text-secondary-text';
  }
  return value > 0 ? 'text-danger' : 'text-success';
};

export const priceText = (value: number | null | undefined): string => {
  if (value === null || value === undefined || Number.isNaN(value)) return '--';
  return value.toFixed(2);
};

export const moneyText = (value: number | null | undefined): string => {
  if (value === null || value === undefined || Number.isNaN(value)) return '--';
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 0 });
};

/** 占比一律按百分数展示；库里存的是 0–1 小数。 */
export const ratioText = (value: number | null | undefined): string => {
  if (value === null || value === undefined || Number.isNaN(value)) return '--';
  return `${(value * 100).toFixed(2)}%`;
};

export const riskBadgeVariant = (level: string): 'danger' | 'warning' | 'info' | 'default' => {
  if (level === '严重') return 'danger';
  if (level === '警示') return 'warning';
  if (level === '观察') return 'info';
  return 'default';
};

export const stageText = (stage: string, name?: string): string => {
  if (!stage) return '--';
  return name ? `${stage} ${name}` : stage;
};

export const planTriggerText = (plan: {
  trigger_type: string;
  trigger_price: number | null;
  pct_change: number | null;
}): string => {
  if (plan.trigger_price !== null && plan.trigger_price !== undefined) {
    if (plan.trigger_type === 'price_gte') return `价格 ≥ ${priceText(plan.trigger_price)} 元`;
    if (plan.trigger_type === 'price_lte') return `价格 ≤ ${priceText(plan.trigger_price)} 元`;
    return `${priceText(plan.trigger_price)} 元`;
  }
  if (plan.pct_change !== null && plan.pct_change !== undefined) {
    return `涨跌幅 ${pctText(plan.pct_change)}`;
  }
  return '--';
};

/** 后端给的名字可能是代码兜底（需求 D-14）：展示时区分，别让代码冒充名称。 */
export const displayName = (name: string | null | undefined, code: string): string => {
  if (!name || name === code) return code;
  return name;
};
