import type React from 'react';
import { useCallback, useEffect, useState } from 'react';
import assistantApi from '../../api/assistant';
import type { ParsedApiError } from '../../api/error';
import { getParsedApiError } from '../../api/error';
import { ApiErrorAlert, EmptyState, Loading } from '../../components/common';
import type { AssistantHoldingDetail, AssistantSnapshot } from '../../types/assistant';
import { Layout } from './Layout';
import { ConclusionCard, ConsistencyBar, KeyLevelBlock, PlanBlock, RiskBlock, StageBlock } from './components/blocks';
import { displayName } from './components/format';

/** 详情页：需求 §6.4 五区块 + 卡片结构，全部只读快照，不重算。 */
export const Detail: React.FC<{ code: string; onBack: () => void }> = ({ code, onBack }) => {
  const [snapshot, setSnapshot] = useState<AssistantSnapshot | null>(null);
  const [detail, setDetail] = useState<AssistantHoldingDetail | null>(null);
  const [barDate, setBarDate] = useState<string | null>(null);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const snap = await assistantApi.snapshot();
      setSnapshot(snap);
      const body = await assistantApi.holding(code, snap.snapshot_id);
      setDetail(body.holding_conclusion);
      setBarDate(body.bar_date ?? null);
    } catch (exc) {
      setError(getParsedApiError(exc));
    } finally {
      setLoading(false);
    }
  }, [code]);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) return <Loading />;

  return (
    <Layout
      title={detail ? displayName(detail.name, detail.code) : code}
      description={detail ? `阶段 ${detail.stage} · 建议 ${detail.advice_display || '--'}` : undefined}
      actions={
        <button type="button" className="btn-primary px-3 py-1.5 text-xs" onClick={onBack}>
          返回列表
        </button>
      }
    >
      {error ? <ApiErrorAlert error={error} className="mb-4" onAction={() => void load()} actionLabel="重试" /> : null}

      {!detail && !error ? (
        <EmptyState title="没有这只标的" description="该快照中没有这个代码，可能已移出关注清单。" />
      ) : null}

      {detail && snapshot ? (
        <div className="space-y-4">
          <div className="rounded-xl border border-border/60 bg-card/60 px-4 py-3">
            <ConsistencyBar snapshot={snapshot} barDate={barDate} />
          </div>

          <div className="grid gap-4 lg:grid-cols-[1.1fr_1fr]">
            <ConclusionCard
              item={detail}
              plans={detail.conditional_plan}
              risks={detail.risk_signals}
              stageDetail={detail.stage_detail}
              aiText={detail.llm_text}
            />

            <div className="space-y-4">
              {/* 第 2 区块：趋势 / 位置 / 关键价位 */}
              <section className="terminal-card rounded-2xl p-4">
                <h2 className="text-sm font-semibold text-foreground">趋势与位置</h2>
                <div className="mt-2 grid grid-cols-2 gap-2 text-sm">
                  <div>
                    <span className="label-uppercase">趋势结论</span>
                    <p className="text-foreground">{detail.trend || '--'}</p>
                  </div>
                  <div>
                    <span className="label-uppercase">当前位置</span>
                    <p className="text-foreground">{detail.position_tag || '--'}</p>
                  </div>
                </div>
                <div className="mt-3">
                  <span className="label-uppercase">关键价位</span>
                  <div className="mt-1">
                    <KeyLevelBlock levels={detail.key_levels} />
                  </div>
                </div>
              </section>

              {/* 第 3 区块：阶段定位（默认展开，卡片里折叠） */}
              <section className="terminal-card rounded-2xl p-4">
                <h2 className="text-sm font-semibold text-foreground">阶段定位</h2>
                <div className="mt-2">
                  <StageBlock detail={detail.stage_detail} />
                </div>
              </section>

              {/* 第 4 区块：风险信号 */}
              <section className="terminal-card rounded-2xl p-4">
                <h2 className="text-sm font-semibold text-foreground">风险信号</h2>
                <div className="mt-2">
                  <RiskBlock items={detail.risk_signals} />
                </div>
              </section>

              {/* 第 5 区块：条件计划 */}
              <section className="terminal-card rounded-2xl p-4">
                <h2 className="text-sm font-semibold text-foreground">条件计划</h2>
                <div className="mt-2">
                  <PlanBlock plans={detail.conditional_plan} />
                </div>
              </section>
            </div>
          </div>

          <p className="text-xs text-secondary-text">
            以上全部来自 {snapshot.data_date} 快照（规则 {snapshot.calc_version}），页面不重算结论；
            LLM 文案仅润色措辞，不参与评分与建议。
            {detail.llm_text?.available ? ` 文案来源：${detail.llm_text.model || '规则模板'}` : ''}
          </p>
        </div>
      ) : null}
    </Layout>
  );
};

export default Detail;
