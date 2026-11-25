import type React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import assistantApi from '../../../api/assistant';
import { Detail } from '../Detail';
import Index from '../Index';
import { Settings } from '../Settings';

vi.mock('../../../api/assistant', () => ({
  default: {
    snapshot: vi.fn(),
    holdings: vi.fn(),
    holding: vi.fn(),
    market: vi.fn(),
    sectors: vi.fn(),
    llmText: vi.fn(),
    params: vi.fn(),
    setPreset: vi.fn(),
    setOverride: vi.fn(),
    rollbackOverride: vi.fn(),
    setFunds: vi.fn(),
  },
}));

const SNAPSHOT = {
  snapshot_id: 'sid-1',
  data_date: '2026-11-25',
  calc_version: 'rules-v1',
  param_hash: 'abcdef1234567890',
  param_preset: 'standard',
  generated_at: '2026-11-25T20:00:00',
  bars_used: 150,
  gap_days: 0,
  source_tag: 'D1',
};

const CONCLUSION = {
  code: '600000',
  name: '浦发银行',
  advice: '加仓',
  advice_display: '加仓',
  advice_state: '有效',
  stage: 'S1',
  stage_confidence: '高',
  trend: '偏强',
  position_tag: '高位',
  score: 86,
  is_holding: true,
  est_level: '高位',
  div_yield: 0.03,
  pnl_pct: 5.2,
  last_close: 21.15,
  pct_change: 0.38,
  key_levels: [{ price: 20.19, kind: 'support', source: 'MA20 折算', detail: {} }],
  position: { tier: '轻', pct: 0.08, reasons: ['单只上限裁剪'] },
  risk_level: '警示',
  human_text: '趋势偏强，等待回踩买点。',
  degraded: false,
};

const STAGE = {
  code: 'S1',
  name: '上升趋势',
  confidence: '高',
  playbook: '回踩不破再买',
  switch: '跌破 MA20 且连续 2 日不收回 → 转 S3',
  allowed: ['买入', '加仓'],
  forbidden: ['清仓'],
};

const DETAIL = {
  ...CONCLUSION,
  stage_detail: STAGE,
  conditional_plan: [
    {
      code: '600000',
      branch_id: 'b1',
      scenario: '回踩买点',
      trigger_type: 'price_lte',
      trigger_price: 20.5,
      pct_change: null,
      action: '加仓',
      shares: 1000,
      est_amount: 20500,
      pct_of_total: 0.2,
      valid_days: 3,
      settable: true,
      notes: '分两笔',
      technical_basis: 'MA20',
      is_probe: false,
    },
  ],
  risk_signals: [
    {
      code: '600000',
      risk_id: 'r1',
      risk_code: 'R3',
      category: '数据缺口',
      level: '警示',
      trigger_basis: '缺口 1 日',
      data_date: '2026-11-25',
      affects_advice: false,
      action: null,
      human_text: '缺口 1 日，已降级',
      source_tag: 'D2',
      degraded: true,
    },
  ],
  llm_text: { available: true, text: '今天可小幅加仓。\n（AI 生成摘要，以结构化字段为准）', model: 'fake' },
};

function stubListApi() {
  vi.mocked(assistantApi.snapshot).mockResolvedValue(SNAPSHOT);
  vi.mocked(assistantApi.holdings).mockResolvedValue({
    data_date: SNAPSHOT.data_date,
    snapshot_id: SNAPSHOT.snapshot_id,
    calc_version: SNAPSHOT.calc_version,
    bar_date: SNAPSHOT.data_date,
    holding_conclusion: [CONCLUSION],
  });
  vi.mocked(assistantApi.market).mockResolvedValue({
    data_date: SNAPSHOT.data_date,
    snapshot_id: SNAPSHOT.snapshot_id,
    calc_version: SNAPSHOT.calc_version,
    market_snapshot: {
      available: true,
      index_code: 'sh000001',
      index_name: '上证指数',
      pct_change: 0.27,
      market_regime: '偏弱',
      regime_cells: 'below+not_up',
      degraded: false,
    },
  });
  vi.mocked(assistantApi.sectors).mockResolvedValue({
    data_date: SNAPSHOT.data_date,
    snapshot_id: SNAPSHOT.snapshot_id,
    calc_version: SNAPSHOT.calc_version,
    sector_snapshot: [],
  });
  vi.mocked(assistantApi.llmText).mockResolvedValue({
    data_date: SNAPSHOT.data_date,
    snapshot_id: SNAPSHOT.snapshot_id,
    calc_version: SNAPSHOT.calc_version,
    llm_text: { available: true, text: '大盘偏弱，自选 1 只。' },
  });
  vi.mocked(assistantApi.holding).mockResolvedValue({
    data_date: SNAPSHOT.data_date,
    snapshot_id: SNAPSHOT.snapshot_id,
    calc_version: SNAPSHOT.calc_version,
    bar_date: SNAPSHOT.data_date,
    holding_conclusion: DETAIL,
  });
}

const renderWithRouter = (ui: React.ReactElement, initialEntry = '/assistant') =>
  render(<MemoryRouter initialEntries={[initialEntry]}>{ui}</MemoryRouter>);

describe('助手 Web 页面', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    stubListApi();
  });

  it('大盘页展示环境档与总结段', async () => {
    renderWithRouter(<Index />);

    await waitFor(() => expect(screen.getByText('上证指数')).toBeTruthy());
    expect(screen.getByText('偏弱')).toBeTruthy();
    expect(screen.getByText('+0.27%')).toBeTruthy();
    expect(screen.getByText('大盘偏弱，自选 1 只。')).toBeTruthy();
    expect(screen.getByText(/板块数据未接入/)).toBeTruthy();
  });

  it('个股列表红涨绿跌、点行进入详情', async () => {
    renderWithRouter(<Index />);
    await waitFor(() => expect(screen.getByText('大盘与板块')).toBeTruthy());

    fireEvent.click(screen.getByText('个股列表（1）'));

    const pct = await screen.findByText('+0.38%');
    // A 股口径：涨为红（danger），不是美股的绿
    expect(pct.className).toContain('text-danger');
    expect(screen.getByText('浦发银行')).toBeTruthy();

    fireEvent.click(screen.getByText('浦发银行'));
    await waitFor(() => expect(screen.getByText('趋势与位置')).toBeTruthy());
    expect(vi.mocked(assistantApi.holding)).toHaveBeenCalledWith('600000', 'sid-1');
  });

  it('空快照给出人话提示而不是错误堆栈', async () => {
    vi.mocked(assistantApi.snapshot).mockRejectedValue({
      parsedError: {
        title: '未找到',
        message: '该交易日没有可用快照',
        rawMessage: '',
        category: 'http_error',
        status: 404,
        code: 'snapshot_not_found',
      },
    });
    renderWithRouter(<Index />);

    await waitFor(() => expect(screen.getByText('今日快照尚未生成')).toBeTruthy());
  });

  it('详情页按固定顺序给区块，并带价位来源与阶段打法', async () => {
    renderWithRouter(<Detail code="600000" onBack={() => undefined} />);

    await waitFor(() => expect(screen.getByText('趋势与位置')).toBeTruthy());
    for (const label of ['趋势结论', '当前位置', '关键价位', '风险信号', '条件计划', '阶段定位']) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(screen.getAllByText('MA20 折算').length).toBeGreaterThan(0);
    expect(screen.getAllByText(/回踩不破再买/).length).toBeGreaterThan(0);
    expect(screen.getByText(/AI 生成摘要/)).toBeTruthy();
    expect(screen.getByText('缺口 1 日，已降级')).toBeTruthy();
  });

  it('配置页按注册表渲染，切档提示生效时点', async () => {
    vi.mocked(assistantApi.params).mockResolvedValue({
      baseline_preset: 'standard',
      overrides: [],
      param_hash: 'abcdef1234567890',
      values: { box_touch_tol: 0.02, probe_enabled: true },
      total_capital: 100000,
      cash_available: null,
      config_source: 'env',
      updated_at: '',
      describe: {},
      groups: [{ key: 'stage', label: '阶段判定' }],
      registry: [
        {
          key: 'box_touch_tol',
          group: 'stage',
          group_label: '阶段判定',
          label: '箱体触碰容差',
          kind: 'ratio',
          unit: null,
          minimum: 0,
          maximum: 0.1,
          choices: [],
          standard: 0.02,
          loose: 0.01,
          strict: 0.03,
          preset_scoped: true,
          requires_confirm: false,
          provisional: true,
          description: '',
        },
      ],
    });
    vi.mocked(assistantApi.setPreset).mockResolvedValue({
      ok: true,
      baseline_preset: 'strict',
      effective_snapshot_date: '2026-11-26',
    });

    renderWithRouter(<Settings onBack={() => undefined} />);

    await waitFor(() => expect(screen.getByText('箱体触碰容差')).toBeTruthy());
    expect(screen.getByText('环境变量（初始值）')).toBeTruthy();

    fireEvent.click(screen.getByText('严格').closest('button') as HTMLElement);
    await waitFor(() => expect(vi.mocked(assistantApi.setPreset)).toHaveBeenCalledWith('strict'));
    await waitFor(() => expect(screen.getByText(/档位已切换为「strict」/)).toBeTruthy());
  });
});
