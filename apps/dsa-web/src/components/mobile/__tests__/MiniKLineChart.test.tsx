import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MiniKLineChart } from '../MiniKLineChart';
import type { StockKLinePoint } from '../../../api/stocks';

const points: StockKLinePoint[] = [
  { date: '2026-10-09', open: 10, high: 10.4, low: 9.8, close: 10.2, volume: 1000 },
  { date: '2026-10-10', open: 10.2, high: 10.6, low: 10.1, close: 10.5, volume: 1200 },
  { date: '2026-10-13', open: 10.5, high: 10.9, low: 10.3, close: 10.4, volume: 900 },
];

describe('MiniKLineChart', () => {
  it('renders a chart when data is available', () => {
    const { container } = render(<MiniKLineChart points={points} />);
    expect(container.querySelector('svg')).not.toBeNull();
    expect(screen.getByRole('img')).toHaveAccessibleName(/2026-10-09/);
  });

  it('shows an empty state when there is no data', () => {
    render(<MiniKLineChart points={[]} />);
    expect(screen.getByText('暂无 K 线数据')).toBeInTheDocument();
  });

  it('ignores points without a valid close price', () => {
    render(<MiniKLineChart points={[{ date: '2026-10-13', close: null }]} />);
    expect(screen.getByText('暂无 K 线数据')).toBeInTheDocument();
  });

  it('uses red for up candles and green for down candles (CN convention)', () => {
    const { container } = render(<MiniKLineChart points={points} />);
    const bodyRects = Array.from(container.querySelectorAll('rect')).filter(
      (rect) => rect.getAttribute('height') !== '0',
    );
    const fills = bodyRects.map((rect) => rect.getAttribute('fill') ?? '');
    expect(fills.some((fill) => fill.includes('price-up'))).toBe(true);
    expect(fills.some((fill) => fill.includes('price-down'))).toBe(true);
  });

  it('renders the legend with both directions', () => {
    render(<MiniKLineChart points={points} />);
    expect(screen.getByText('涨')).toBeInTheDocument();
    expect(screen.getByText('跌')).toBeInTheDocument();
  });
});
