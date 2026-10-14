import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { ActionConfirmCard } from '../ActionConfirmCard';

const { preview, execute, cancel } = vi.hoisted(() => ({
  preview: vi.fn(),
  execute: vi.fn(),
  cancel: vi.fn(),
}));

vi.mock('../../../api/actions', () => ({
  actionsApi: {
    preview: (message: string, options?: unknown) => preview(message, options),
    execute: (token: string) => execute(token),
    cancel: (token: string) => cancel(token),
  },
}));

function renderCard(props: { message: string; symbol?: string; onExecuted?: () => void }) {
  return render(
    <UiLanguageProvider>
      <ActionConfirmCard {...props} />
    </UiLanguageProvider>,
  );
}

describe('ActionConfirmCard', () => {
  beforeEach(() => {
    preview.mockReset();
    execute.mockReset();
    cancel.mockReset();
  });

  it('lists what will be done and requires explicit confirmation', async () => {
    preview.mockResolvedValue({
      status: 'pending_confirmation',
      token: 'tok-1',
      message: '待确认',
      summary: '1. 把 601919 止损设为 15.00',
      intent: 'write_intent',
      expiresAt: null,
      items: [
        {
          op: 'set_stop_loss',
          label: '设置止损',
          description: '把 601919 止损设为 15.00',
          params: {},
          valid: true,
          error: '',
          before: null,
          after: 15,
          status: '',
          receipt: '',
        },
      ],
    });

    renderCard({ message: '把 601919 止损设为 15 元', symbol: '601919' });
    expect(await screen.findByText(/把 601919 止损设为 15.00/)).toBeInTheDocument();
    // 未点击确认前不应执行
    expect(execute).not.toHaveBeenCalled();
  });

  it('executes only after the confirm button is pressed', async () => {
    preview.mockResolvedValue({
      status: 'pending_confirmation',
      token: 'tok-2',
      message: '',
      summary: '1. 把 600519 加入自选股',
      intent: 'write_intent',
      expiresAt: null,
      items: [
        {
          op: 'watchlist_add',
          label: '加入自选股',
          description: '把 600519 加入自选股',
          params: {},
          valid: true,
          error: '',
          before: null,
          after: null,
          status: '',
          receipt: '',
        },
      ],
    });
    execute.mockResolvedValue({
      status: 'executed',
      ok: true,
      receipt: '1. ✅ 600519 已加入自选股',
      items: [],
    });
    const onExecuted = vi.fn();

    renderCard({ message: '添加 600519 到自选股', onExecuted });
    const confirm = await screen.findByRole('button', { name: /确认执行/ });
    fireEvent.click(confirm);

    await waitFor(() => expect(execute).toHaveBeenCalledWith('tok-2'));
    expect(await screen.findByText(/已执行/)).toBeInTheDocument();
    expect(onExecuted).toHaveBeenCalled();
  });

  it('cancels without executing', async () => {
    preview.mockResolvedValue({
      status: 'pending_confirmation',
      token: 'tok-3',
      message: '',
      summary: '1. 把 600519 加入自选股',
      intent: 'write_intent',
      expiresAt: null,
      items: [
        {
          op: 'watchlist_add',
          label: '加入自选股',
          description: '把 600519 加入自选股',
          params: {},
          valid: true,
          error: '',
          before: null,
          after: null,
          status: '',
          receipt: '',
        },
      ],
    });
    cancel.mockResolvedValue({ status: 'cancelled', message: '已取消，未做任何修改。' });

    renderCard({ message: '添加 600519 到自选股' });
    const cancelButton = await screen.findByRole('button', { name: /取消/ });
    fireEvent.click(cancelButton);

    await waitFor(() => expect(cancel).toHaveBeenCalledWith('tok-3'));
    expect(execute).not.toHaveBeenCalled();
  });

  it('shows the rejection reason for whitelist-out requests', async () => {
    preview.mockResolvedValue({
      status: 'rejected',
      token: '',
      message: '以下操作未通过校验，请修正后重试：\n1. ⛔ 仓位比例不在白名单内',
      summary: '',
      intent: 'write_intent',
      expiresAt: null,
      items: [],
    });

    renderCard({ message: '把仓位比例调到 30%' });
    expect(await screen.findByText(/未通过校验/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /确认执行/ })).toBeNull();
  });
});
