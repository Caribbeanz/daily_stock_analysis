import type React from 'react';
import { useId, useMemo } from 'react';
import type { StockKLinePoint } from '../../api/stocks';

type MiniKLineChartProps = {
  points: StockKLinePoint[];
  /** 显示根数（取最近 N 根）。 */
  limit?: number;
  height?: number;
  className?: string;
};

const WIDTH = 640;
const PADDING = { top: 12, right: 8, bottom: 18, left: 8 };
const VOLUME_RATIO = 0.18;

const isFiniteNumber = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value);

function movingAverage(closes: number[], period: number): (number | null)[] {
  return closes.map((_, index) => {
    if (index < period - 1) return null;
    const window = closes.slice(index - period + 1, index + 1);
    const sum = window.reduce((acc, item) => acc + item, 0);
    return sum / period;
  });
}

function buildPath(values: (number | null)[], x: (i: number) => number, y: (v: number) => number): string {
  let path = '';
  let started = false;
  values.forEach((value, index) => {
    if (value === null) {
      started = false;
      return;
    }
    path += `${started ? ' L' : ' M'} ${x(index).toFixed(2)} ${y(value).toFixed(2)}`;
    started = true;
  });
  return path.trim();
}

/**
 * 迷你 K 线图（纯 SVG，无第三方依赖 · P2 · FR-14.2.1）。
 *
 * 蜡烛按**中国市场惯例**着色：收 ≥ 开 用红（涨），否则用绿（跌）。
 * 叠加 MA5 / MA20 与成交量柱；数据不足时自动降级为收盘价折线。
 */
export const MiniKLineChart: React.FC<MiniKLineChartProps> = ({
  points,
  limit = 60,
  height = 220,
  className = '',
}) => {
  const gradientId = useId();

  const model = useMemo(() => {
    const usable = (points ?? [])
      .filter((point) => isFiniteNumber(point.close))
      .slice(-Math.max(1, limit));
    if (usable.length === 0) return null;

    const closes = usable.map((point) => point.close as number);
    const highs = usable.map((point, index) => (isFiniteNumber(point.high) ? point.high : closes[index]));
    const lows = usable.map((point, index) => (isFiniteNumber(point.low) ? point.low : closes[index]));
    const opens = usable.map((point, index) => (isFiniteNumber(point.open) ? point.open : closes[index]));
    const volumes = usable.map((point) => (isFiniteNumber(point.volume) ? point.volume : 0));

    const priceMax = Math.max(...highs);
    const priceMin = Math.min(...lows);
    const span = priceMax - priceMin || Math.max(priceMax * 0.01, 0.01);

    const plotTop = PADDING.top;
    const plotBottom = height - PADDING.bottom;
    const volumeHeight = (plotBottom - plotTop) * VOLUME_RATIO;
    const priceBottom = plotBottom - volumeHeight - 6;
    const plotLeft = PADDING.left;
    const plotRight = WIDTH - PADDING.right;

    const step = (plotRight - plotLeft) / usable.length;
    const bodyWidth = Math.max(2, Math.min(step * 0.62, 10));

    const y = (value: number) => priceBottom - ((value - priceMin) / span) * (priceBottom - plotTop);
    const xCenter = (index: number) => plotLeft + step * (index + 0.5);

    const maxVolume = Math.max(...volumes, 1);
    const volumeY = (value: number) => plotBottom - (value / maxVolume) * volumeHeight;

    const candles = usable.map((point, index) => {
      const open = opens[index];
      const close = closes[index];
      const up = close >= open;
      const bodyTop = y(Math.max(open, close));
      const bodyBottom = y(Math.min(open, close));
      return {
        key: `${point.date}-${index}`,
        x: xCenter(index) - bodyWidth / 2,
        width: bodyWidth,
        bodyTop,
        bodyHeight: Math.max(1, bodyBottom - bodyTop),
        wickTop: y(highs[index]),
        wickBottom: y(lows[index]),
        centerX: xCenter(index),
        up,
        volumeX: xCenter(index) - bodyWidth / 2,
        volumeY: volumeY(volumes[index]),
        volumeHeight: Math.max(0, plotBottom - volumeY(volumes[index])),
      };
    });

    return {
      candles,
      plotLeft,
      plotRight,
      plotTop,
      priceBottom,
      plotBottom,
      step,
      ma5: buildPath(movingAverage(closes, 5), xCenter, y),
      ma20: buildPath(movingAverage(closes, 20), xCenter, y),
      firstDate: usable[0].date,
      lastDate: usable[usable.length - 1].date,
      priceMax,
      priceMin,
      slopeUp: closes[closes.length - 1] >= closes[0],
    };
  }, [points, limit, height]);

  if (!model) {
    return (
      <div
        className={`flex items-center justify-center rounded-xl border border-border/60 bg-elevated/40 text-xs text-secondary-text ${className}`}
        style={{ height }}
      >
        暂无 K 线数据
      </div>
    );
  }

  return (
    <div className={className}>
      <svg
        role="img"
        aria-label={`K 线图 ${model.firstDate} 至 ${model.lastDate}`}
        viewBox={`0 0 ${WIDTH} ${height}`}
        className="h-auto w-full"
      >
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="hsl(var(--price-up) / 0.18)" />
            <stop offset="100%" stopColor="hsl(var(--price-up) / 0)" />
          </linearGradient>
        </defs>

        {/* 网格 */}
        {[0, 0.25, 0.5, 0.75, 1].map((ratio) => {
          const gridY = model.plotTop + (model.priceBottom - model.plotTop) * ratio;
          return (
            <line
              key={ratio}
              x1={model.plotLeft}
              x2={model.plotRight}
              y1={gridY}
              y2={gridY}
              stroke="hsl(var(--border))"
              strokeWidth={0.6}
              strokeDasharray="3 5"
            />
          );
        })}

        {/* 成交量 */}
        {model.candles.map((candle) => (
          <rect
            key={`vol-${candle.key}`}
            x={candle.volumeX}
            y={candle.volumeY}
            width={candle.width}
            height={candle.volumeHeight}
            fill={candle.up ? 'hsl(var(--price-up) / 0.35)' : 'hsl(var(--price-down) / 0.35)'}
          />
        ))}

        {/* 蜡烛 */}
        {model.candles.map((candle) => (
          <g key={candle.key}>
            <line
              x1={candle.centerX}
              x2={candle.centerX}
              y1={candle.wickTop}
              y2={candle.wickBottom}
              stroke={candle.up ? 'hsl(var(--price-up))' : 'hsl(var(--price-down))'}
              strokeWidth={0.9}
            />
            <rect
              x={candle.x}
              y={candle.bodyTop}
              width={candle.width}
              height={candle.bodyHeight}
              fill={candle.up ? 'hsl(var(--price-up))' : 'hsl(var(--price-down))'}
            />
          </g>
        ))}

        {/* 均线 */}
        {model.ma20 && (
          <path d={model.ma20} fill="none" stroke="hsl(var(--color-warning))" strokeWidth={1.2} />
        )}
        {model.ma5 && (
          <path d={model.ma5} fill="none" stroke="hsl(var(--primary))" strokeWidth={1.2} />
        )}

        {/* 极值标注 */}
        <text x={model.plotLeft} y={model.plotTop - 3} fontSize={10} fill="hsl(var(--muted-foreground))">
          {model.priceMax.toFixed(2)}
        </text>
        <text
          x={model.plotLeft}
          y={model.priceBottom + 10}
          fontSize={10}
          fill="hsl(var(--muted-foreground))"
        >
          {model.priceMin.toFixed(2)}
        </text>
        <text
          x={model.plotRight}
          y={model.priceBottom + 10}
          fontSize={10}
          textAnchor="end"
          fill="hsl(var(--muted-foreground))"
        >
          {model.lastDate}
        </text>
      </svg>

      <div className="mt-1 flex items-center gap-3 text-[11px] text-secondary-text">
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2 w-3 rounded-sm" style={{ background: 'hsl(var(--price-up))' }} />
          涨
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2 w-3 rounded-sm" style={{ background: 'hsl(var(--price-down))' }} />
          跌
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-0.5 w-3" style={{ background: 'hsl(var(--primary))' }} />
          MA5
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-0.5 w-3" style={{ background: 'hsl(var(--color-warning))' }} />
          MA20
        </span>
      </div>
    </div>
  );
};

export default MiniKLineChart;
