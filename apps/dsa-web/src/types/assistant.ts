/**
 * 股票分析助手只读快照 / 参数配置的类型定义。
 *
 * 字段名沿用后端 `snake_case`：这些接口是**快照原样回读**，中间不做字段改名，
 * 少一层转换就少一处「页面显示的和大盘/飞书不是同一个值」的机会。
 */

export type AssistantTriple = {
  data_date: string;
  snapshot_id: string;
  calc_version: string;
  bar_date?: string | null;
};

export type AssistantSnapshot = {
  snapshot_id: string;
  data_date: string;
  calc_version: string;
  param_hash: string;
  param_preset: string;
  generated_at: string;
  bars_used: number;
  gap_days: number;
  source_tag: string;
};

export type AssistantKeyLevel = {
  price: number;
  kind: 'support' | 'resistance' | 'invalid' | string;
  source: string;
  detail?: Record<string, unknown>;
};

export type AssistantConclusion = {
  code: string;
  name: string;
  advice: string | null;
  advice_display: string | null;
  advice_state: string;
  stage: string;
  stage_confidence: string;
  trend: string;
  position_tag: string;
  score: number;
  is_holding: boolean;
  est_level: string | null;
  div_yield: number | null;
  pnl_pct: number | null;
  last_close: number | null;
  pct_change: number | null;
  key_levels: AssistantKeyLevel[];
  position: { tier: string; pct: number | null; reasons: string[] };
  risk_level: string;
  human_text: string;
  degraded: boolean;
};

export type AssistantPlan = {
  code: string;
  branch_id: string;
  scenario: string;
  trigger_type: string;
  trigger_price: number | null;
  pct_change: number | null;
  action: string;
  shares: number | null;
  est_amount: number | null;
  pct_of_total: number | null;
  valid_days: number | null;
  settable: boolean;
  notes: string | null;
  technical_basis: string | null;
  is_probe: boolean;
};

export type AssistantRisk = {
  code: string;
  risk_id: string;
  risk_code: string;
  category: string;
  level: string;
  trigger_basis: string | null;
  data_date: string | null;
  affects_advice: boolean;
  action: string | null;
  human_text: string;
  source_tag: string;
  degraded: boolean;
};

export type AssistantStageDetail = {
  code: string;
  name: string;
  confidence: string | null;
  playbook: string;
  switch: string;
  allowed: string[];
  forbidden: string[];
};

export type AssistantLlmText = {
  available: boolean;
  text: string;
  model?: string;
  channel?: string;
  generated_at?: string;
};

export type AssistantHoldingDetail = AssistantConclusion & {
  stage_detail: AssistantStageDetail;
  conditional_plan: AssistantPlan[];
  risk_signals: AssistantRisk[];
  llm_text: AssistantLlmText;
};

export type AssistantMarketSnapshot = {
  available: boolean;
  reason?: string;
  index_code?: string;
  index_name?: string;
  pct_change?: number | null;
  market_regime?: string;
  regime_cells?: string | null;
  width?: number | null;
  degraded?: boolean;
};

export type AssistantSectorSnapshot = {
  sector_code: string;
  sector_name: string;
  pct_change: number | null;
  sector_strength: string | null;
  rs_label: string | null;
  rs_t: number | null;
  up_count: number | null;
  down_count: number | null;
  lead_stock: string | null;
};

export type AssistantParamSpec = {
  key: string;
  group: string;
  group_label: string;
  label: string;
  kind: 'float' | 'int' | 'ratio' | 'bool' | 'enum' | 'str' | string;
  unit: string | null;
  minimum: number | null;
  maximum: number | null;
  choices: string[];
  standard: unknown;
  loose: unknown;
  strict: unknown;
  preset_scoped: boolean;
  requires_confirm: boolean;
  provisional: boolean;
  description: string;
};

export type AssistantParamOverride = {
  key: string;
  value: unknown;
  label: string;
};

export type AssistantParams = {
  baseline_preset: 'loose' | 'standard' | 'strict' | string;
  overrides: AssistantParamOverride[];
  param_hash: string;
  values: Record<string, unknown>;
  total_capital: number | null;
  cash_available: number | null;
  config_source: 'env' | 'l4' | string;
  updated_at: string;
  describe: Record<string, string>;
  registry: AssistantParamSpec[];
  groups: { key: string; label: string }[];
};

export type AssistantWriteResponse = {
  ok: boolean;
  key?: string;
  value?: unknown;
  baseline_preset?: string;
  param_hash?: string;
  total_capital?: number;
  cash_available?: number | null;
  effective_date?: string;
  effective_snapshot_date?: string;
  requires_confirm?: boolean;
};

export type AssistantHoldingsResponse = AssistantTriple & {
  holding_conclusion: AssistantConclusion[];
};

export type AssistantMarketResponse = AssistantTriple & {
  market_snapshot: AssistantMarketSnapshot;
};

export type AssistantSectorsResponse = AssistantTriple & {
  sector_snapshot: AssistantSectorSnapshot[];
};

export type AssistantHoldingResponse = AssistantTriple & {
  holding_conclusion: AssistantHoldingDetail;
};

export type AssistantLlmTextResponse = AssistantTriple & {
  llm_text: AssistantLlmText;
};
