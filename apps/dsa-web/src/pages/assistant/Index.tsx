import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import assistantApi from '../../api/assistant';
import type { ParsedApiError } from '../../api/error';
import { getParsedApiError } from '../../api/error';
import { ApiErrorAlert, EmptyState, Loading } from '../../components/common';
import type {
  AssistantConclusion,
  AssistantLlmText,
  AssistantMarketSnapshot,
  AssistantSectorSnapshot,
  AssistantSnapshot,
} from '../../types/assistant';
import { Detail } from './Detail';
import { Layout } from './Layout';
import { Settings } from './Settings';
import { HoldingTable, type HoldingSortKey } from './components/HoldingTable';
import { MarketPanel } from './components/MarketPanel';
import { ConsistencyBar } from './components/blocks';

type TabKey = 'market' | 'stocks';

/** 快照尚未生成时给用户一句人话，而不是抛一个 404 堆栈。 */
function isNotReady(error: ParsedApiError): boolean {
  return error.code === 'snapshot_not_found' || error.message.includes('没有可用快照');
}

/**
 * 助手独立页面（方案 §6.1）：Tab A 大盘板块 + Tab B 个股列表。
 *
 * 三个视图共用一条路由 `/assistant`，用查询参数区分：
 * `?code=600000` 详情、`?view=settings` 配置、`?tab=stocks|market` 页签。
 * 这样刷新/分享链接都还原到同一屏，也不必在路由表里塞三个入口。
 */
export const Index: React.FC = () => {
  const [searchParams, setSearchParams] = useSearchParams();
  const code = searchParams.get('code');
  const view = searchParams.get('view');
  const tab = (searchParams.get('tab') as TabKey | null) ?? 'market';

  const [snapshot, setSnapshot] = useState<AssistantSnapshot | null>(null);
  const [holdings, setHoldings] = useState<AssistantConclusion[]>([]);
  const [market, setMarket] = useState<AssistantMarketSnapshot | null>(null);
  const [sectors, setSectors] = useState<AssistantSectorSnapshot[]>([]);
  const [summary, setSummary] = useState<AssistantLlmText | null>(null);
  const [sortKey, setSortKey] = useState<HoldingSortKey>('risk_level');
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const snap = await assistantApi.snapshot();
      setSnapshot(snap);
      const [holdingBody, marketBody, sectorBody, textBody] = await Promise.all([
        assistantApi.holdings(snap.snapshot_id),
        assistantApi.market(snap.snapshot_id),
        assistantApi.sectors(snap.snapshot_id),
        assistantApi.llmText(snap.snapshot_id, 'summary'),
      ]);
      setHoldings(holdingBody.holding_conclusion ?? []);
      setMarket(marketBody.market_snapshot);
      setSectors(sectorBody.sector_snapshot ?? []);
      setSummary(textBody.llm_text);
    } catch (exc) {
      setError(getParsedApiError(exc));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const updateParams = useCallback(
    (patch: Record<string, string | null>) => {
      const next = new URLSearchParams(searchParams);
      Object.entries(patch).forEach(([key, value]) => {
        if (value === null) next.delete(key);
        else next.set(key, value);
      });
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const riskCount = useMemo(
    () => holdings.filter((item) => item.risk_level === '严重' || item.risk_level === '警示').length,
    [holdings],
  );

  if (view === 'settings') {
    return <Settings onBack={() => updateParams({ view: null })} />;
  }

  if (code) {
    return <Detail code={code} onBack={() => updateParams({ code: null })} />;
  }

  if (loading) return <Loading />;

  return (
    <Layout
      title="今日快照"
      description={
        snapshot
          ? `${snapshot.data_date} · 规则 ${snapshot.calc_version} · 参数 ${snapshot.param_hash.slice(0, 8)}`
          : '每个交易日 20:00 生成一份，Web 与推送读同一份'
      }
      actions={
        <>
          <button type="button" className="btn-primary px-3 py-1.5 text-xs" onClick={() => updateParams({ view: 'settings' })}>
            参数配置
          </button>
          <button
            type="button"
            className="rounded-lg border border-border/60 px-3 py-1.5 text-xs text-secondary-text hover:text-foreground"
            onClick={() => void load()}
          >
            刷新
          </button>
        </>
      }
    >
      {error ? (
        isNotReady(error) ? (
          <EmptyState
            title="今日快照尚未生成"
            description="助手在交易日 20:00 生成当日快照；生成后这里会显示大盘环境与个股结论。"
            action={
              <button type="button" className="btn-primary px-3 py-1.5 text-xs" onClick={() => void load()}>
                重新检查
              </button>
            }
          />
        ) : (
          <ApiErrorAlert error={error} onAction={() => void load()} actionLabel="重试" />
        )
      ) : null}

      {!error && snapshot ? (
        <div className="space-y-4">
          <div className="rounded-xl border border-border/60 bg-card/60 px-4 py-3">
            <ConsistencyBar snapshot={snapshot} />
          </div>

          <div className="flex gap-2">
            {([
              { key: 'market', label: '大盘与板块' },
              { key: 'stocks', label: `个股列表（${holdings.length}）` },
            ] as { key: TabKey; label: string }[]).map((item) => (
              <button
                key={item.key}
                type="button"
                onClick={() => updateParams({ tab: item.key })}
                className={
                  tab === item.key
                    ? 'rounded-full border border-cyan/40 bg-cyan/10 px-4 py-1.5 text-sm text-cyan'
                    : 'rounded-full border border-border/60 px-4 py-1.5 text-sm text-secondary-text'
                }
              >
                {item.label}
              </button>
            ))}
          </div>

          {tab === 'market' ? <MarketPanel market={market} sectors={sectors} summary={summary} /> : null}

          {tab === 'stocks' ? (
            <section className="terminal-card rounded-2xl p-4">
              <div className="mb-3 flex items-center justify-between">
                <h2 className="text-sm font-semibold text-foreground">个股结论</h2>
                <span className="text-xs text-secondary-text">
                  需关注风险 {riskCount} 只 · 点行看详情
                </span>
              </div>
              <HoldingTable
                items={holdings}
                sortKey={sortKey}
                onSortChange={setSortKey}
                onSelect={(next) => updateParams({ code: next })}
              />
            </section>
          ) : null}
        </div>
      ) : null}
    </Layout>
  );
};

export default Index;
