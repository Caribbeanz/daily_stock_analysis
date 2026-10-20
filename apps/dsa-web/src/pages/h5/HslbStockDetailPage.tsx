import type React from 'react';
import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, ChevronDown, RefreshCw } from 'lucide-react';
import { h5Api, type H5Detail } from '../../api/h5';
import { H5ActionBadge } from '../../components/h5/H5ActionBadge';
import { formatMoment, formatPct, formatPrice, priceTone, sectionTone } from '../../components/h5/format';
import { cn } from '../../utils/cn';

/**
 * 高抛低吸 · 移动 H5 详情（`/h5/stocks/:code`）。
 *
 * 信息层级：
 * 1. 名称 / 代码 + 建议标签；
 * 2. 当日四价（开 / 高 / 低 / 收）；
 * 3. 建议总结（一句话结论 + 理由），右上角展开「分析过程」；
 * 4. 四要素（参考价位 / 止损 / 目标 / 仓位）始终可见——可执行信息不藏在展开区；
 * 5. 展开后：技术面 / 消息面 / 风险 / 大盘 / 板块，逐块给结论与依据。
 *
 * 只读：不提供改持仓、加自选等写操作（仍走旧版或对话）。
 */
const HslbStockDetailPage: React.FC = () => {
  const { code = '' } = useParams<{ code: string }>();
  const navigate = useNavigate();
  const [detail, setDetail] = useState<H5Detail | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState(false);

  const symbol = code.trim().toUpperCase();

  const load = useCallback(async () => {
    if (!symbol) {
      setError('缺少标的代码');
      setIsLoading(false);
      return;
    }
    setIsLoading(true);
    try {
      const payload = await h5Api.detail(symbol);
      setDetail(payload);
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败，请稍后重试');
    } finally {
      setIsLoading(false);
    }
  }, [symbol]);

  useEffect(() => {
    void load();
  }, [load]);

  const goBack = () => {
    if (window.history.length > 1) {
      navigate(-1);
      return;
    }
    navigate('/h5');
  };

  const quote = detail?.quote ?? null;
  const sections = detail?.analysis ?? [];

  // 后端 conclusion 已形如「减仓（上涨趋势）：<reason>」，reason 再渲染一次会重复，
  // 这里做一次包含判断（后端口径不变，旧页面照旧使用完整 conclusion）。
  const reasonText = (detail?.reason ?? '').trim();
  const showReason = reasonText !== '' && !(detail?.conclusion ?? '').includes(reasonText);

  return (
    <div className="min-h-screen bg-background pb-10 text-foreground">
      <header className="sticky top-0 z-20 flex items-center gap-2 border-b border-border bg-background/95 px-3 py-2 backdrop-blur">
        <button
          type="button"
          aria-label="返回"
          onClick={goBack}
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-secondary-text active:bg-muted"
        >
          <ArrowLeft className="h-5 w-5" aria-hidden />
        </button>
        <div className="min-w-0 flex-1">
          <p className="truncate text-[15px] font-medium leading-5">{detail?.name || symbol || '标的详情'}</p>
          <p className="truncate font-mono text-[11px] text-muted-text">{detail?.symbol || symbol}</p>
        </div>
        <button
          type="button"
          aria-label="刷新"
          onClick={() => void load()}
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-secondary-text active:bg-muted"
        >
          <RefreshCw className={cn('h-4 w-4', isLoading && 'animate-spin')} aria-hidden />
        </button>
      </header>

      {isLoading && !detail ? (
        <div className="space-y-3 px-3 py-3">
          <div className="h-24 animate-pulse rounded-xl bg-muted" />
          <div className="h-40 animate-pulse rounded-xl bg-muted" />
          <div className="h-32 animate-pulse rounded-xl bg-muted" />
        </div>
      ) : null}

      {error && !detail ? (
        <div className="mx-3 mt-3 rounded-xl border border-border bg-card p-4">
          <p className="text-[13px]">加载失败</p>
          <p className="mt-1 text-[12px] text-muted-text">{error}</p>
          <button
            type="button"
            onClick={() => void load()}
            className="mt-3 rounded-lg border border-border px-3 py-1.5 text-[12px] text-secondary-text active:bg-muted"
          >
            重试
          </button>
        </div>
      ) : null}

      {detail ? (
        <main className="space-y-3 px-3 py-3">
          {detail.isUrgent ? (
            <div className="rounded-lg border border-price-down bg-muted px-3 py-2 text-[12px] text-price-down">
              紧急信号：{detail.displayAction}，请优先处理
            </div>
          ) : null}

          <section className="rounded-xl border border-border bg-card px-4 py-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <h1 className="truncate text-[18px] font-medium leading-6">{detail.name || detail.symbol}</h1>
                <p className="mt-0.5 font-mono text-[11px] text-muted-text">{detail.symbol}</p>
              </div>
              <H5ActionBadge label={detail.displayAction} tone={detail.actionTone} urgent={detail.isUrgent} />
            </div>

            <div className="mt-3 flex items-end gap-2">
              <span className={cn('text-[30px] font-medium leading-none tabular-nums', priceTone(quote?.changePercent))}>
                {formatPrice(quote?.close)}
              </span>
              <span className={cn('pb-0.5 text-[13px] tabular-nums', priceTone(quote?.changePercent))}>
                {formatPct(quote?.changePercent)}
              </span>
            </div>

            <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 border-t border-border pt-3">
              {[
                { label: '开盘价', value: quote?.open },
                { label: '最高价', value: quote?.high },
                { label: '最低价', value: quote?.low },
                { label: '收盘价', value: quote?.close },
              ].map((row) => (
                <div key={row.label} className="flex items-baseline justify-between gap-2">
                  <dt className="text-[12px] text-muted-text">{row.label}</dt>
                  <dd className="text-[13px] tabular-nums text-foreground">{formatPrice(row.value)}</dd>
                </div>
              ))}
            </dl>
            <p className="mt-2 text-[10px] leading-4 text-muted-text">
              盘中打开时，「收盘价」为当日最新成交价；收盘后即为当日收盘价。
            </p>
          </section>

          <section className="rounded-xl border border-border bg-card px-4 py-3">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <p className="text-[11px] text-muted-text">操作建议</p>
                <p className="mt-1 text-[15px] font-medium leading-6">{detail.conclusion || '暂无决策信号'}</p>
                {showReason ? (
                  <p className="mt-1 text-[12px] leading-5 text-secondary-text">{reasonText}</p>
                ) : null}
              </div>
              <button
                type="button"
                aria-expanded={expanded}
                onClick={() => setExpanded((prev) => !prev)}
                className="flex shrink-0 items-center gap-1 rounded-lg border border-border px-2 py-1 text-[11px] text-secondary-text active:bg-muted"
              >
                {expanded ? '收起' : `看分析过程（${sections.length}）`}
                <ChevronDown className={cn('h-3.5 w-3.5 transition-transform', expanded && 'rotate-180')} aria-hidden />
              </button>
            </div>

            <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 border-t border-border pt-3">
              <div className="flex items-baseline justify-between gap-2">
                <dt className="text-[12px] text-muted-text">参考价位</dt>
                <dd className="text-[13px] tabular-nums">{detail.entryZone || '-'}</dd>
              </div>
              <div className="flex items-baseline justify-between gap-2">
                <dt className="text-[12px] text-muted-text">止损</dt>
                <dd className="text-[13px] tabular-nums text-price-down">{formatPrice(detail.stopLoss)}</dd>
              </div>
              <div className="flex items-baseline justify-between gap-2">
                <dt className="text-[12px] text-muted-text">目标</dt>
                <dd className="text-[13px] tabular-nums text-price-up">{formatPrice(detail.targetPrice)}</dd>
              </div>
              <div className="flex items-baseline justify-between gap-2">
                <dt className="text-[12px] text-muted-text">仓位建议</dt>
                <dd className="text-[13px]">{detail.positionRatioLabel || '-'}</dd>
              </div>
            </dl>

            {expanded ? (
              <div className="mt-3 space-y-3 border-t border-border pt-3">
                {sections.map((section) => (
                  <div key={section.key}>
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-[13px] font-medium">{section.label}</span>
                      <span className="text-[10px] text-muted-text">
                        {section.available ? '' : '暂无数据'}
                      </span>
                    </div>
                    <p className={cn('mt-1 text-[13px] leading-5', sectionTone(section.tone))}>
                      {section.verdict || '暂无依据'}
                    </p>
                    {section.details.length > 0 ? (
                      <dl className="mt-1.5 space-y-1">
                        {section.details.map((row) => (
                          <div key={`${section.key}-${row.label}-${row.value}`} className="flex gap-2 text-[11px] leading-5">
                            <dt className="w-16 shrink-0 text-muted-text">{row.label}</dt>
                            <dd className="min-w-0 flex-1 text-secondary-text">{row.value}</dd>
                          </div>
                        ))}
                      </dl>
                    ) : null}
                  </div>
                ))}

                {detail.discipline ? (
                  <p className="border-t border-border pt-2 text-[11px] leading-5 text-muted-text">
                    操作纪律：{detail.discipline}
                  </p>
                ) : null}
              </div>
            ) : null}
          </section>

          {detail.degraded.length > 0 ? (
            <section className="rounded-xl border border-warning bg-card px-3 py-2">
              <p className="text-[11px] font-medium text-warning">数据说明</p>
              <ul className="mt-1 space-y-0.5">
                {detail.degraded.map((note) => (
                  <li key={note} className="text-[11px] leading-5 text-muted-text">
                    · {note}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          <footer className="space-y-1 px-1 pb-2">
            <p className="text-[10px] leading-4 text-muted-text">
              数据日期：{detail.asOf || '-'}
              {detail.signalCreatedAt ? ` · 信号生成：${formatMoment(detail.signalCreatedAt)}` : ''}
              {quote?.updateTime ? ` · 行情：${formatMoment(quote.updateTime)}` : ''}
            </p>
            <p className="text-[10px] leading-4 text-muted-text">{detail.disclaimer}</p>
          </footer>
        </main>
      ) : null}
    </div>
  );
};

export default HslbStockDetailPage;
