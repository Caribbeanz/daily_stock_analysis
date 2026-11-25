import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import assistantApi from '../../api/assistant';
import type { ParsedApiError } from '../../api/error';
import { getParsedApiError } from '../../api/error';
import { ApiErrorAlert, InlineAlert, Loading, SectionCard } from '../../components/common';
import type { AssistantParamSpec, AssistantParams } from '../../types/assistant';
import { Layout } from './Layout';

const presetLabels: { key: string; label: string; hint: string }[] = [
  { key: 'loose', label: '宽松', hint: '拦得少、动得多' },
  { key: 'standard', label: '标准', hint: '回测基线' },
  { key: 'strict', label: '严格', hint: '拦得多、动得少' },
];

function valueToInput(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value);
}

function parseInput(spec: AssistantParamSpec, raw: string): unknown {
  if (spec.kind === 'bool') return raw === 'true';
  if (spec.kind === 'enum' || spec.kind === 'str') return raw;
  const num = Number(raw);
  return Number.isNaN(num) ? raw : num;
}

function formatValue(spec: AssistantParamSpec, value: unknown): string {
  if (spec.kind === 'bool') return value ? '开启' : '关闭';
  if (spec.kind === 'ratio' && typeof value === 'number') return `${(value * 100).toFixed(2)}%`;
  if (value === null || value === undefined) return '--';
  return `${String(value)}${spec.unit || ''}`;
}

/**
 * 参数配置（方案 §5.4）：三档切换、逐项覆盖、区间校验、变更记录。
 *
 * 页面上所有键名/区间/分组都来自 `/params` 的 `registry`，不在这里写死——
 * 后端拒绝的规则与页面提示的规则必须是同一份。
 */
export const Settings: React.FC<{ onBack: () => void }> = ({ onBack }) => {
  const [params, setParams] = useState<AssistantParams | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [funds, setFunds] = useState({ total: '', cash: '' });

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const body = await assistantApi.params();
      setParams(body);
      setDrafts({});
      setFunds({
        total: body.total_capital === null ? '' : String(body.total_capital),
        cash: body.cash_available === null ? '' : String(body.cash_available),
      });
    } catch (exc) {
      setError(getParsedApiError(exc));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const overrideMap = useMemo(() => {
    const map: Record<string, unknown> = {};
    (params?.overrides ?? []).forEach((item) => {
      map[item.key] = item.value;
    });
    return map;
  }, [params]);

  const dirtyKeys = useMemo(() => {
    if (!params) return [] as string[];
    return Object.entries(drafts)
      .filter(([key, raw]) => raw !== valueToInput(params.values[key]))
      .map(([key]) => key);
  }, [drafts, params]);

  const savePreset = async (preset: string) => {
    setSaving(true);
    setNotice(null);
    try {
      const result = await assistantApi.setPreset(preset);
      await load();
      setNotice(`档位已切换为「${preset}」，下一次快照生成时生效（${result.effective_snapshot_date || '--'}）。`);
    } catch (exc) {
      setError(getParsedApiError(exc));
    } finally {
      setSaving(false);
    }
  };

  const saveFunds = async () => {
    setSaving(true);
    setNotice(null);
    try {
      const total = Number(funds.total);
      if (Number.isNaN(total) || total <= 0) throw new Error('总资金必须是正数');
      const cash = funds.cash.trim() === '' ? null : Number(funds.cash);
      const result = await assistantApi.setFunds(total, cash);
      await load();
      setNotice(`资金已保存，下一次快照生成时生效（${result.effective_snapshot_date || '--'}）。`);
    } catch (exc) {
      setError(getParsedApiError(exc));
    } finally {
      setSaving(false);
    }
  };

  const saveOverrides = async () => {
    if (!params || dirtyKeys.length === 0) return;
    const registry = new Map(params.registry.map((spec) => [spec.key, spec]));
    const needConfirm = dirtyKeys.filter((key) => registry.get(key)?.requires_confirm);
    if (needConfirm.length > 0) {
      const names = needConfirm.map((key) => registry.get(key)?.label || key).join('、');
      // 仓位硬上限属「改须二次确认」（需求 §5.16.3），这里显式确认一次
      if (!window.confirm(`即将修改硬上限：${names}。确认继续？`)) return;
    }

    setSaving(true);
    setNotice(null);
    try {
      let effective = '';
      for (const key of dirtyKeys) {
        const spec = registry.get(key);
        if (!spec) continue;
        const result = await assistantApi.setOverride(key, parseInput(spec, drafts[key]));
        effective = result.effective_date || effective;
      }
      await load();
      setNotice(`已保存 ${dirtyKeys.length} 项覆盖，下一次快照生成时生效（${effective || '--'}）。`);
    } catch (exc) {
      setError(getParsedApiError(exc));
    } finally {
      setSaving(false);
    }
  };

  const rollback = async (key: string) => {
    setSaving(true);
    setNotice(null);
    try {
      const result = await assistantApi.rollbackOverride(key);
      await load();
      setNotice(`已回滚 ${key}，下一次快照生成时生效（${result.effective_date || '--'}）。`);
    } catch (exc) {
      setError(getParsedApiError(exc));
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <Loading />;

  return (
    <Layout
      title="参数配置"
      description="只改参数、不改当日结论；保存后在下一次快照生成时生效"
      actions={
        <button type="button" className="rounded-lg border border-border/60 px-3 py-1.5 text-xs text-secondary-text hover:text-foreground" onClick={onBack}>
          返回快照
        </button>
      }
    >
      {error ? <ApiErrorAlert error={error} className="mb-4" onAction={() => void load()} actionLabel="重试" /> : null}
      {notice ? <InlineAlert variant="success" className="mb-4" message={notice} /> : null}

      {params ? (
        <div className="space-y-4">
          <SectionCard title="档位" subtitle="PRESET">
            <div className="grid gap-2 sm:grid-cols-3">
              {presetLabels.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  disabled={saving}
                  onClick={() => void savePreset(item.key)}
                  className={
                    params.baseline_preset === item.key
                      ? 'rounded-xl border border-cyan/40 bg-cyan/10 p-3 text-left'
                      : 'rounded-xl border border-border/60 p-3 text-left hover:border-cyan/30'
                  }
                >
                  <p className="text-sm font-semibold text-foreground">{item.label}</p>
                  <p className="text-xs text-secondary-text">{item.hint}</p>
                </button>
              ))}
            </div>
            <p className="mt-3 flex flex-wrap gap-x-2 text-xs text-secondary-text">
              <span>当前参数指纹 {params.param_hash.slice(0, 8)}</span>
              <span>来源 <span>{params.config_source === 'l4' ? '配置页' : '环境变量（初始值）'}</span></span>
            </p>
          </SectionCard>

          <SectionCard title="资金" subtitle="FUNDS">
            <div className="grid gap-3 sm:grid-cols-3">
              <label className="text-sm">
                <span className="label-uppercase">总资金（元）</span>
                <input
                  className="mt-1 w-full rounded-lg border border-border/60 bg-base px-3 py-2 text-sm text-foreground"
                  value={funds.total}
                  onChange={(event) => setFunds((prev) => ({ ...prev, total: event.target.value }))}
                  inputMode="decimal"
                />
              </label>
              <label className="text-sm">
                <span className="label-uppercase">可用现金（留空=自动）</span>
                <input
                  className="mt-1 w-full rounded-lg border border-border/60 bg-base px-3 py-2 text-sm text-foreground"
                  value={funds.cash}
                  onChange={(event) => setFunds((prev) => ({ ...prev, cash: event.target.value }))}
                  inputMode="decimal"
                />
              </label>
              <div className="flex items-end">
                <button type="button" className="btn-primary px-3 py-2 text-xs" disabled={saving} onClick={() => void saveFunds()}>
                  保存资金
                </button>
              </div>
            </div>
            <p className="mt-2 text-xs text-secondary-text">
              总资金没有默认值：缺它算不出股数与占比，宁可报错也不给假金额。
            </p>
          </SectionCard>

          <SectionCard
            title="单项覆盖"
            subtitle="OVERRIDES"
            actions={
              <button
                type="button"
                className="btn-primary px-3 py-1.5 text-xs"
                disabled={saving || dirtyKeys.length === 0}
                onClick={() => void saveOverrides()}
              >
                保存修改{dirtyKeys.length > 0 ? `（${dirtyKeys.length}）` : ''}
              </button>
            }
          >
            {(params.overrides ?? []).length === 0 ? (
              <p className="text-sm text-secondary-text">当前没有单项覆盖，全部取档位默认值。</p>
            ) : (
              <ul className="space-y-2">
                {params.overrides.map((item) => (
                  <li key={item.key} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border/60 bg-elevated/40 px-3 py-2">
                    <span className="text-sm text-foreground">{item.label}</span>
                    <span className="text-xs text-secondary-text">{item.key} = {String(item.value)}</span>
                    <button
                      type="button"
                      className="text-xs text-cyan"
                      disabled={saving}
                      onClick={() => void rollback(item.key)}
                    >
                      回滚
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </SectionCard>

          {params.groups.map((group) => {
            const rows = params.registry.filter((spec) => spec.group === group.key);
            if (rows.length === 0) return null;
            return (
              <SectionCard key={group.key} title={group.label} subtitle="PARAMS">
                <div className="grid gap-3 md:grid-cols-2">
                  {rows.map((spec) => {
                    const current = params.values[spec.key];
                    const draft = drafts[spec.key] ?? valueToInput(current);
                    const isOverridden = overrideMap[spec.key] !== undefined;
                    return (
                      <label key={spec.key} className="rounded-lg border border-border/50 p-3 text-sm">
                        <div className="flex items-center justify-between gap-2">
                          <span className="text-foreground">
                            {spec.label}
                            {spec.provisional ? <span className="ml-1 text-xs text-warning">待回看</span> : null}
                            {spec.requires_confirm ? <span className="ml-1 text-xs text-danger">硬上限</span> : null}
                          </span>
                          {isOverridden ? <span className="text-xs text-cyan">已覆盖</span> : null}
                        </div>
                        <div className="mt-1 flex items-center gap-2">
                          {spec.kind === 'bool' ? (
                            <select
                              className="w-full rounded-lg border border-border/60 bg-base px-2 py-1 text-sm text-foreground"
                              value={draft}
                              onChange={(event) => setDrafts((prev) => ({ ...prev, [spec.key]: event.target.value }))}
                            >
                              <option value="true">开启</option>
                              <option value="false">关闭</option>
                            </select>
                          ) : spec.kind === 'enum' ? (
                            <select
                              className="w-full rounded-lg border border-border/60 bg-base px-2 py-1 text-sm text-foreground"
                              value={draft}
                              onChange={(event) => setDrafts((prev) => ({ ...prev, [spec.key]: event.target.value }))}
                            >
                              {spec.choices.map((choice) => (
                                <option key={choice} value={choice}>{choice}</option>
                              ))}
                            </select>
                          ) : (
                            <input
                              className="w-full rounded-lg border border-border/60 bg-base px-2 py-1 text-sm text-foreground"
                              value={draft}
                              inputMode={spec.kind === 'str' ? 'text' : 'decimal'}
                              onChange={(event) => setDrafts((prev) => ({ ...prev, [spec.key]: event.target.value }))}
                            />
                          )}
                          <span className="shrink-0 text-xs text-secondary-text">
                            当前 {formatValue(spec, current)}
                          </span>
                        </div>
                        <p className="mt-1 text-xs text-secondary-text">
                          {spec.minimum !== null && spec.maximum !== null
                            ? `区间 ${spec.minimum} ~ ${spec.maximum}`
                            : spec.choices.length > 0
                              ? `可选 ${spec.choices.join(' / ')}`
                              : ''}
                          {spec.preset_scoped ? ' · 随档位变化' : ''}
                        </p>
                      </label>
                    );
                  })}
                </div>
              </SectionCard>
            );
          })}
        </div>
      ) : null}
    </Layout>
  );
};

export default Settings;
