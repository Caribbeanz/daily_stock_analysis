import type React from 'react';
import { Badge, Card } from '../../../components/common';
import type {
  AssistantConclusion,
  AssistantKeyLevel,
  AssistantLlmText,
  AssistantPlan,
  AssistantRisk,
  AssistantSnapshot,
  AssistantStageDetail,
} from '../../../types/assistant';
import { displayName, moneyText, pctClass, pctText, planTriggerText, priceText, ratioText, riskBadgeVariant } from './format';

/** 快照三元组条：任何结论都要能一眼看到「这是哪天的、哪版规则算的」。 */
export const ConsistencyBar: React.FC<{ snapshot: AssistantSnapshot; barDate?: string | null }> = ({
  snapshot,
  barDate,
}) => {
  const stale = barDate && barDate !== snapshot.data_date;
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-secondary-text">
      <span>快照日 {snapshot.data_date}</span>
      <span className={stale ? 'text-warning' : undefined}>
        数据截至 {barDate || snapshot.data_date}
        {stale ? '（收盘数据未出，按上一交易日）' : ''}
      </span>
      <span>规则 {snapshot.calc_version}</span>
      <span>参数 {snapshot.param_hash.slice(0, 8)}</span>
      <span>来源 {snapshot.source_tag}</span>
      {snapshot.gap_days > 0 ? <span className="text-warning">缺口 {snapshot.gap_days} 日</span> : null}
    </div>
  );
};

const levelLabel: Record<string, string> = {
  support: '支撑',
  resistance: '压力',
  invalid: '趋势失效',
};

/** 第 2 区块的关键价位：每个价位都必须带来源（需求 §5.3），缺来源的不展示。 */
export const KeyLevelBlock: React.FC<{ levels: AssistantKeyLevel[] }> = ({ levels }) => {
  const rows = levels.filter((item) => item.source);
  if (rows.length === 0) {
    return <p className="text-sm text-secondary-text">本次快照未产出关键价位</p>;
  }
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      {rows.map((item) => (
        <div
          key={`${item.kind}-${item.price}-${item.source}`}
          className="flex items-baseline justify-between gap-2 rounded-lg border border-border/60 bg-elevated/50 px-3 py-2"
        >
          <span className="text-xs text-secondary-text">{levelLabel[item.kind] ?? item.kind}</span>
          <span className="text-sm font-semibold text-foreground">{priceText(item.price)}</span>
          <span className="text-xs text-secondary-text">{item.source}</span>
        </div>
      ))}
    </div>
  );
};

/** 第 3 区块：阶段定位。打法/允许/禁止/切换全部来自后端（与飞书同源）。 */
export const StageBlock: React.FC<{ detail: AssistantStageDetail }> = ({ detail }) => {
  if (!detail?.code) {
    return <p className="text-sm text-secondary-text">阶段未判定（S0）</p>;
  }
  return (
    <div className="space-y-2 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="info" size="sm">{`${detail.code} ${detail.name}`.trim()}</Badge>
        <span className="text-xs text-secondary-text">置信度 {detail.confidence || '--'}</span>
      </div>
      <p className="text-secondary-text">打法：{detail.playbook || '--'}</p>
      <p className="text-secondary-text">允许：{detail.allowed.join('、') || '--'}</p>
      <p className="text-secondary-text">禁止：{detail.forbidden.join('、') || '--'}</p>
      <p className="text-xs text-secondary-text">切换：{detail.switch || '--'}</p>
    </div>
  );
};

/** 第 4 区块：风险信号结构化块（等级 + 类别 + 一句人话 + 是否影响建议）。 */
export const RiskBlock: React.FC<{ items: AssistantRisk[] }> = ({ items }) => {
  if (items.length === 0) {
    return <p className="text-sm text-secondary-text">今日无风险信号</p>;
  }
  return (
    <ul className="space-y-2">
      {items.map((item) => (
        <li key={`${item.code}-${item.risk_id}`} className="rounded-lg border border-border/60 bg-elevated/50 p-3">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={riskBadgeVariant(item.level)} size="sm">{item.level}</Badge>
            <span className="text-xs text-secondary-text">{item.category}</span>
            {item.affects_advice ? <span className="text-xs text-warning">已影响建议</span> : null}
          </div>
          <p className="mt-1 text-sm text-foreground">{item.human_text}</p>
          {item.trigger_basis ? (
            <p className="mt-1 text-xs text-secondary-text">{item.trigger_basis}</p>
          ) : null}
        </li>
      ))}
    </ul>
  );
};

/** 第 5 区块：条件计划（分支 + 触发价 + 股数 + 有效期）。 */
export const PlanBlock: React.FC<{ plans: AssistantPlan[]; emptyText?: string }> = ({
  plans,
  emptyText = '本次无符合条件的计划',
}) => {
  if (plans.length === 0) {
    return <p className="text-sm text-secondary-text">{emptyText}</p>;
  }
  return (
    <ul className="space-y-2">
      {plans.map((plan) => (
        <li key={`${plan.code}-${plan.branch_id}`} className="rounded-lg border border-border/60 bg-elevated/50 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-sm font-medium text-foreground">{plan.scenario || plan.branch_id}</span>
            <span className="text-sm text-foreground">{planTriggerText(plan)}</span>
          </div>
          <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-secondary-text">
            <span>动作 {plan.action}</span>
            <span>股数 {plan.shares ?? '--'}</span>
            <span>预估金额 {moneyText(plan.est_amount)}</span>
            <span>占总资金 {ratioText(plan.pct_of_total)}</span>
            <span>有效期 {plan.valid_days ?? '--'} 个交易日</span>
            <span>{plan.settable ? '可设条件单' : '不建议设单'}</span>
          </div>
          {plan.notes ? <p className="mt-1 text-xs text-secondary-text">{plan.notes}</p> : null}
        </li>
      ))}
    </ul>
  );
};

type CardProps = {
  item: AssistantConclusion;
  plans?: AssistantPlan[];
  risks?: AssistantRisk[];
  stageDetail?: AssistantStageDetail;
  aiText?: AssistantLlmText;
};

/**
 * 卡片（需求 §6.4 固定顺序，不许重排）：
 * 操作意见 → 趋势结论 → 当前位置 → 关键价位 → 理由 → 风险提示 → 仓位建议 → 做 T 计划。
 *
 * 「理由」用引擎产出的 `human_text`（它已经是规则模板的一句话），不是 LLM 文案；
 * LLM 文案单独标 AI 来源，放在操作意见下方，用户一眼能分清哪句是算出来的。
 */
export const ConclusionCard: React.FC<CardProps> = ({ item, plans, risks, stageDetail, aiText }) => {
  const tPlans = (plans ?? []).filter((plan) => (plan.scenario || '').includes('T 仓'));
  const riskItems = risks ?? [];
  const worstRisk = riskItems.reduce(
    (acc, cur) => (riskBadgeVariant(cur.level) === 'danger' ? '严重' : acc),
    item.risk_level,
  );

  return (
    <Card padding="md" variant="gradient">
      <div className="space-y-3">
        <div>
          <div className="flex flex-wrap items-baseline gap-2">
            <h2 className="text-lg font-semibold text-foreground">{displayName(item.name, item.code)}</h2>
            <span className="text-xs text-secondary-text">{item.code}</span>
            {item.is_holding ? <Badge size="sm">持仓</Badge> : null}
            {item.degraded ? <Badge size="sm" variant="warning">降级</Badge> : null}
          </div>
          {/* ① 操作意见 */}
          <p className="mt-1 text-xl font-semibold text-cyan">{item.advice_display || '--'}</p>
          {aiText?.available && aiText.text ? (
            <p className="mt-1 text-xs leading-5 text-secondary-text">{aiText.text}</p>
          ) : null}
        </div>

        {/* ②③ 趋势 / 位置 */}
        <div className="grid grid-cols-2 gap-2 text-sm">
          <div>
            <span className="label-uppercase">趋势结论</span>
            <p className="text-foreground">{item.trend || '--'}</p>
          </div>
          <div>
            <span className="label-uppercase">当前位置</span>
            <p className="text-foreground">{item.position_tag || '--'}</p>
          </div>
        </div>

        {/* ④ 关键价位 */}
        <div>
          <span className="label-uppercase">关键价位</span>
          <div className="mt-1">
            <KeyLevelBlock levels={item.key_levels} />
          </div>
        </div>

        {/* ⑤ 理由 */}
        <div>
          <span className="label-uppercase">理由</span>
          <p className="text-sm text-foreground">{item.human_text || '--'}</p>
        </div>

        {/* ⑥ 风险提示 */}
        <div>
          <span className="label-uppercase">风险提示</span>
          <div className="mt-1 flex items-center gap-2">
            <Badge variant={riskBadgeVariant(worstRisk)} size="sm">{worstRisk}</Badge>
            <span className="text-xs text-secondary-text">
              {riskItems.length > 0 ? `共 ${riskItems.length} 条信号` : '今日无风险信号'}
            </span>
          </div>
        </div>

        {/* ⑦ 仓位建议 */}
        <div>
          <span className="label-uppercase">仓位建议</span>
          <p className="text-sm text-foreground">
            {item.position?.tier || '--'} 档 · 目标占比 {ratioText(item.position?.pct ?? null)}
          </p>
          {item.position?.reasons?.length ? (
            <p className="text-xs text-secondary-text">裁剪原因：{item.position.reasons.join('；')}</p>
          ) : null}
        </div>

        {/* ⑧ 做 T 计划 */}
        <div>
          <span className="label-uppercase">做 T 计划</span>
          <div className="mt-1">
            <PlanBlock plans={tPlans} emptyText="本次无做 T 分支" />
          </div>
        </div>

        {stageDetail ? (
          <details className="rounded-lg border border-border/60 bg-elevated/40 p-3">
            <summary className="cursor-pointer text-xs text-secondary-text">
              阶段定位（点击展开：打法 / 允许 / 禁止 / 切换）
            </summary>
            <div className="mt-2">
              <StageBlock detail={stageDetail} />
            </div>
          </details>
        ) : null}

        <p className="text-xs text-secondary-text">
          评分 {item.score?.toFixed(0) ?? '--'} · 收盘 {priceText(item.last_close)} ·{' '}
          <span className={pctClass(item.pct_change)}>{pctText(item.pct_change)}</span>
        </p>
      </div>
    </Card>
  );
};
