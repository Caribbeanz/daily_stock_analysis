import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../contexts/UiLanguageContext';
import AuditPage from '../AuditPage';

const { list, stats, undo, execute } = vi.hoisted(() => ({
  list: vi.fn(),
  stats: vi.fn(),
  undo: vi.fn(),
  execute: vi.fn(),
}));

vi.mock('../../api/actions', () => ({
  auditApi: {
    list: (params?: unknown) => list(params),
    stats: () => stats(),
    detail: vi.fn(),
    undo: (id: number) => undo(id),
    getStreamUrl: () => '/api/v1/actions/stream',
  },
  actionsApi: {
    execute: (token: string) => execute(token),
  },
}));

vi.mock('../../hooks/useActionStream', () => ({
  useActionStream: () => ({ isConnected: true }),
  default: () => ({ isConnected: true }),
}));

const sampleItem = {
  id: 1,
  source: 'h5',
  actor: 'h5',
  action: 'set_position',
  actionLabel: '设置持仓',
  target: '601919',
  before: null,
  after: { avg_cost: 12.9, quantity: 200 },
  confirmed: true,
  status: 'ok',
  description: '新增持仓：601919',
  sessionId: null,
  undoOf: null,
  undoneBy: null,
  createdAt: '2026-10-14T10:00:00',
  undoable: true,
  undoReason: '',
};

function renderPage() {
  return render(
    <UiLanguageProvider>
      <AuditPage />
    </UiLanguageProvider>,
  );
}

describe('AuditPage', () => {
  beforeEach(() => {
    list.mockReset();
    stats.mockReset();
    undo.mockReset();
    execute.mockReset();
    list.mockResolvedValue({ items: [sampleItem], total: 1, limit: 100, offset: 0 });
    stats.mockResolvedValue({ total: 1, ok: 1, rejected: 0, failed: 0, undoRecords: 0, byAction: {} });
  });

  it('renders audit records with action label', async () => {
    renderPage();
    expect(await screen.findByText('设置持仓')).toBeInTheDocument();
    expect(await screen.findByText(/601919/)).toBeInTheDocument();
  });

  it('undoes an operation only after confirmation', async () => {
    undo.mockResolvedValue({
      status: 'pending_confirmation',
      token: 'tok-1',
      message: '',
      summary: '1. 清空 601919 持仓',
      intent: 'undo',
      expiresAt: null,
      items: [],
    });
    execute.mockResolvedValue({ status: 'executed', ok: true, receipt: '✅', items: [] });

    renderPage();
    const undoButton = await screen.findByRole('button', { name: /撤销/ });
    fireEvent.click(undoButton);
    await waitFor(() => expect(undo).toHaveBeenCalledWith(1));

    const confirm = await screen.findByRole('button', { name: '确认撤销' });
    fireEvent.click(confirm);
    await waitFor(() => expect(execute).toHaveBeenCalledWith('tok-1'));
  });

  it('does not offer undo for already-undone records', async () => {
    list.mockResolvedValue({
      items: [{ ...sampleItem, undoable: false, undoneBy: 9 }],
      total: 1,
      limit: 100,
      offset: 0,
    });
    renderPage();
    expect(await screen.findByText('已被撤销')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^撤销$/ })).toBeNull();
  });
});
