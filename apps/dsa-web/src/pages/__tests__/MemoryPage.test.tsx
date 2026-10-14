import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../contexts/UiLanguageContext';
import MemoryPage from '../MemoryPage';

const { list, stats, detail, remove, restore, clear } = vi.hoisted(() => ({
  list: vi.fn(),
  stats: vi.fn(),
  detail: vi.fn(),
  remove: vi.fn(),
  restore: vi.fn(),
  clear: vi.fn(),
}));

vi.mock('../../api/memory', () => ({
  memoryApi: {
    list: (params?: unknown) => list(params),
    stats: () => stats(),
    detail: (id: number) => detail(id),
    remove: (id: number, hard?: boolean) => remove(id, hard),
    restore: (id: number) => restore(id),
    clear: (options?: unknown) => clear(options),
  },
}));

const sampleItem = {
  id: 1,
  memoryType: 'preference',
  content: '偏好低波动',
  sourceMessageIds: [10],
  sessionId: 's1',
  status: 'active',
  createdAt: '2026-10-14T10:00:00',
  updatedAt: '2026-10-14T10:00:00',
};

function renderPage() {
  return render(
    <UiLanguageProvider>
      <MemoryPage />
    </UiLanguageProvider>,
  );
}

describe('MemoryPage', () => {
  beforeEach(() => {
    list.mockReset();
    stats.mockReset();
    detail.mockReset();
    remove.mockReset();
    restore.mockReset();
    clear.mockReset();
    list.mockResolvedValue({ items: [sampleItem], total: 1, limit: 100, offset: 0 });
    stats.mockResolvedValue({ active: 1, deleted: 0, total: 1, byType: { preference: 1 } });
  });

  it('renders memory entries', async () => {
    renderPage();
    expect(await screen.findByText('偏好低波动')).toBeInTheDocument();
    expect(await screen.findByText('偏好约束')).toBeInTheDocument();
  });

  it('soft-deletes a memory after confirmation', async () => {
    remove.mockResolvedValue({ deleted: true, mode: 'soft', id: 1 });
    renderPage();
    const deleteButton = await screen.findByRole('button', { name: /删除/ });
    fireEvent.click(deleteButton);
    const confirm = await screen.findByRole('button', { name: '删除' });
    fireEvent.click(confirm);
    await waitFor(() => expect(remove).toHaveBeenCalledWith(1, false));
  });

  it('clears all memory only after the strong confirmation', async () => {
    clear.mockResolvedValue({ cleared: 1, mode: 'hard', sessionId: null });
    renderPage();
    const clearAll = await screen.findByRole('button', { name: /一键清空/ });
    fireEvent.click(clearAll);
    expect(clear).not.toHaveBeenCalled();
    const confirm = await screen.findByRole('button', { name: '确认清空' });
    fireEvent.click(confirm);
    await waitFor(() => expect(clear).toHaveBeenCalled());
  });
});
