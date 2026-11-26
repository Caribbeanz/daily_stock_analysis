import type React from 'react';
import { Badge, SectionCard } from '../../../components/common';
import type { AssistantLlmText, AssistantMarketSnapshot, AssistantSectorSnapshot } from '../../../types/assistant';
import { pctClass, pctText } from './format';

const regimeVariant = (regime?: string): 'danger' | 'warning' | 'success' | 'default' => {
  if (regime === '偏强') return 'danger';
  if (regime === '偏弱') return 'success';
  if (regime === '中性') return 'warning';
  return 'default';
};

const factorLabel: Record<string, string> = {
  'above+up': '价格在 MA 上方 · 均线上行',
  'above+not_up': '价格在 MA 上方 · 均线未上行',
  'below+up': '价格在 MA 下方 · 均线上行',
  'below+not_up': '价格在 MA 下方 · 均线未上行',
};

/** Tab A 上半：大盘环境 + 总结段 + 板块。缺数据就写缺，不编造档位。 */
export const MarketPanel: React.FC<{
  market: AssistantMarketSnapshot | null;
  sectors: AssistantSectorSnapshot[];
  summary: AssistantLlmText | null;
}> = ({ market, sectors, summary }) => {
  if (!market || !market.available) {
    return (
      <SectionCard title="大盘环境" subtitle="MARKET">
        <p className="text-sm text-secondary-text">
          大盘数据缺失（{market?.reason || '本次未取到指数日线'}），本次不判断环境档。
        </p>
      </SectionCard>
    );
  }

  return (
    <div className="space-y-4">
      <SectionCard title="大盘环境" subtitle="MARKET">
        <div className="flex flex-wrap items-end gap-4">
          <div>
            <span className="label-uppercase">指数</span>
            <p className="text-lg font-semibold text-foreground">{market.index_name || market.index_code}</p>
          </div>
          <div>
            <span className="label-uppercase">涨跌</span>
            <p className={`text-lg font-semibold ${pctClass(market.pct_change)}`}>
              {pctText(market.pct_change)}
            </p>
          </div>
          <div>
            <span className="label-uppercase">环境档</span>
            <p className="mt-1">
              <Badge variant={regimeVariant(market.market_regime)}>{market.market_regime || '--'}</Badge>
            </p>
          </div>
          {market.degraded ? (
            <Badge variant="warning" size="sm">
              指数当日未发布，档位按上一交易日
            </Badge>
          ) : null}
        </div>
        <p className="mt-3 text-xs text-secondary-text">
          M1 因子：{factorLabel[market.regime_cells || ''] || market.regime_cells || '--'}
        </p>

        <div className="mt-4 rounded-xl border border-border/60 bg-elevated/40 p-3">
          <span className="label-uppercase">总结段</span>
          {summary?.available && summary.text ? (
            <p className="mt-1 whitespace-pre-line text-sm leading-6 text-foreground">{summary.text}</p>
          ) : (
            <p className="mt-1 text-sm text-secondary-text">
              本次未生成总结段（文案层关闭或校验未通过，以结构化字段为准）
            </p>
          )}
        </div>
      </SectionCard>

      <SectionCard title="板块" subtitle="SECTORS">
        {sectors.length === 0 ? (
          <p className="text-sm text-secondary-text">
            板块数据未接入：本次未取到板块快照（或已关闭），这里不做推断。
          </p>
        ) : (
          <>
            {sectors.some((sector) => sector.degraded) ? (
              <p className="mb-2 text-xs text-secondary-text">
                强度按板块指数日线判定；标注「截至」的行表示当日指数尚未发布，档位沿用上一交易日。
              </p>
            ) : null}
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-secondary-text">
                    <th className="py-2 pr-3 font-normal">板块</th>
                    <th className="py-2 pr-3 font-normal">涨跌</th>
                    <th className="py-2 pr-3 font-normal">强度</th>
                    <th className="py-2 pr-3 font-normal">相对大盘</th>
                    <th className="py-2 pr-3 font-normal">涨/跌家数</th>
                    <th className="py-2 font-normal">领涨</th>
                  </tr>
                </thead>
                <tbody>
                  {sectors.map((sector) => (
                    <tr key={sector.sector_code} className="border-t border-border/40">
                      <td className="py-2 pr-3 text-foreground">{sector.sector_name || sector.sector_code}</td>
                      <td className={`py-2 pr-3 ${pctClass(sector.pct_change)}`}>{pctText(sector.pct_change)}</td>
                      <td className="py-2 pr-3 text-secondary-text">
                        {sector.sector_strength || '--'}
                        {sector.degraded && sector.as_of ? (
                          <span className="ml-1 text-xs">（截至 {sector.as_of.slice(5)}）</span>
                        ) : null}
                      </td>
                      <td className={`py-2 pr-3 ${pctClass(sector.rs_t)}`}>
                        {pctText(sector.rs_t)}
                        {sector.rs_label ? (
                          <span className="ml-1 text-xs text-secondary-text">{sector.rs_label}</span>
                        ) : null}
                      </td>
                      <td className="py-2 pr-3 text-secondary-text">
                        {sector.up_count ?? '--'}/{sector.down_count ?? '--'}
                      </td>
                      <td className="py-2 text-secondary-text">{sector.lead_stock || '--'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </SectionCard>
    </div>
  );
};
