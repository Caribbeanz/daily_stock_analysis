import type React from 'react';
import { useMemo } from 'react';
import { Badge } from '../../../components/common';
import type { AssistantConclusion } from '../../../types/assistant';
import { displayName, pctClass, pctText, priceText, riskBadgeVariant } from './format';

export type HoldingSortKey = 'risk_level' | 'score' | 'pct_change';

const riskRank: Record<string, number> = { 严重: 0, 警示: 1, 观察: 2 };

const sortLabels: { key: HoldingSortKey; label: string }[] = [
  { key: 'risk_level', label: '按风险' },
  { key: 'score', label: '按评分' },
  { key: 'pct_change', label: '按涨跌' },
];

function sortItems(items: AssistantConclusion[], key: HoldingSortKey): AssistantConclusion[] {
  const copy = [...items];
  copy.sort((a, b) => {
    if (key === 'risk_level') {
      const diff = (riskRank[a.risk_level] ?? 9) - (riskRank[b.risk_level] ?? 9);
      if (diff !== 0) return diff;
      return a.code.localeCompare(b.code);
    }
    if (key === 'score') return (b.score ?? 0) - (a.score ?? 0);
    return (b.pct_change ?? -Infinity) - (a.pct_change ?? -Infinity);
  });
  return copy;
}

/** Tab B：个股列表。列顺序按需求 §6.3；红涨绿跌、不复权展示。 */
export const HoldingTable: React.FC<{
  items: AssistantConclusion[];
  sortKey: HoldingSortKey;
  onSortChange: (key: HoldingSortKey) => void;
  onSelect: (code: string) => void;
}> = ({ items, sortKey, onSortChange, onSelect }) => {
  const rows = useMemo(() => sortItems(items, sortKey), [items, sortKey]);

  if (rows.length === 0) {
    return <p className="text-sm text-secondary-text">本快照没有标的（关注清单与持仓均为空）。</p>;
  }

  return (
    <div>
      <div className="mb-3 flex flex-wrap gap-2">
        {sortLabels.map((option) => (
          <button
            key={option.key}
            type="button"
            onClick={() => onSortChange(option.key)}
            className={
              option.key === sortKey
                ? 'rounded-full border border-cyan/40 bg-cyan/10 px-3 py-1 text-xs text-cyan'
                : 'rounded-full border border-border/60 px-3 py-1 text-xs text-secondary-text'
            }
          >
            {option.label}
          </button>
        ))}
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-secondary-text">
              <th className="py-2 pr-3 font-normal">名称</th>
              <th className="py-2 pr-3 font-normal">编码</th>
              <th className="py-2 pr-3 text-right font-normal">收盘</th>
              <th className="py-2 pr-3 text-right font-normal">涨跌幅</th>
              <th className="py-2 pr-3 font-normal">操作建议</th>
              <th className="py-2 pr-3 font-normal">风险</th>
              <th className="py-2 text-right font-normal">评分</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((item) => (
              <tr
                key={item.code}
                className="cursor-pointer border-t border-border/40 hover:bg-elevated/40"
                onClick={() => onSelect(item.code)}
              >
                <td className="py-2 pr-3 text-foreground">
                  {displayName(item.name, item.code)}
                  {item.is_holding ? <span className="ml-1 text-xs text-secondary-text">持仓</span> : null}
                </td>
                <td className="py-2 pr-3 text-secondary-text">{item.code}</td>
                <td className="py-2 pr-3 text-right text-foreground">{priceText(item.last_close)}</td>
                <td className={`py-2 pr-3 text-right ${pctClass(item.pct_change)}`}>{pctText(item.pct_change)}</td>
                <td className="py-2 pr-3 text-foreground">{item.advice_display || '--'}</td>
                <td className="py-2 pr-3">
                  <Badge variant={riskBadgeVariant(item.risk_level)} size="sm">
                    {item.risk_level}
                  </Badge>
                </td>
                <td className="py-2 text-right text-secondary-text">{item.score?.toFixed(0) ?? '--'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};
