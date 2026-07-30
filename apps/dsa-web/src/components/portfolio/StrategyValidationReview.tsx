import { useEffect, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle2, GitCompareArrows } from 'lucide-react';
import { decisionSignalsApi } from '../../api/decisionSignals';
import { getParsedApiError } from '../../api/error';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { StrategyValidationReviewSummary } from '../../types/decisionSignals';
import { Badge, InlineAlert } from '../common';

const COPY = {
  zh: {
    aria: '策略验证复盘',
    title: '策略验证复盘',
    subtitle: '只读治理视图',
    champion: '当前基线',
    challenger: '影子候选',
    historical: '历史 OOS',
    prospective: '前瞻 shadow',
    gates: 'Hard gates',
    concentration: '样本集中度',
    cost: '成本差异',
    drawdown: '回撤差异',
    mature: '成熟周期',
    rollback: 'Rollback target',
    none: '无',
    noMature: '尚无成熟周期',
    unavailable: '策略验证摘要暂不可用',
    loading: '正在读取策略验证摘要...',
    provisional: '60-bar 证据未成熟，长期改善仅可视为 PROVISIONAL。',
    unable: '未决证据',
  },
  en: {
    aria: 'Strategy validation review',
    title: 'Strategy validation review',
    subtitle: 'Read-only governance view',
    champion: 'Champion baseline',
    challenger: 'Shadow challenger',
    historical: 'Historical OOS',
    prospective: 'Prospective shadow',
    gates: 'Hard gates',
    concentration: 'Sample concentration',
    cost: 'Cost delta',
    drawdown: 'Drawdown delta',
    mature: 'Mature horizons',
    rollback: 'Rollback target',
    none: 'None',
    noMature: 'No mature horizons',
    unavailable: 'Strategy validation summary is unavailable',
    loading: 'Loading strategy validation summary...',
    provisional: 'Long-term improvement remains PROVISIONAL until 60-bar evidence matures.',
    unable: 'Unable evidence',
  },
} as const;

function formatDelta(value?: number | null): string {
  if (value == null) return '--';
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
}

function version(value?: string | null): string {
  return value || '--';
}

export function StrategyValidationReview({
  strategyId = 'portfolio-champion',
  protocolId,
}: {
  strategyId?: string;
  protocolId?: string;
}) {
  const { language } = useUiLanguage();
  const text = COPY[language];
  const requestKey = `${strategyId}:${protocolId || ''}`;
  const [result, setResult] = useState<{
    requestKey: string;
    summary: StrategyValidationReviewSummary | null;
    error: string | null;
  }>({ requestKey: '', summary: null, error: null });
  const requestRef = useRef(0);

  useEffect(() => {
    const requestId = ++requestRef.current;
    void decisionSignalsApi.getStrategyValidationReviewSummary({ strategyId, protocolId })
      .then((result) => {
        if (requestRef.current === requestId) {
          setResult({ requestKey, summary: result, error: null });
        }
      })
      .catch((loadError) => {
        if (requestRef.current === requestId) {
          setResult({
            requestKey,
            summary: null,
            error: getParsedApiError(loadError).message || text.unavailable,
          });
        }
      });
    return () => { requestRef.current += 1; };
  }, [protocolId, requestKey, strategyId, text.unavailable]);

  const error = result.requestKey === requestKey ? result.error : null;
  const summary = result.requestKey === requestKey ? result.summary : null;
  if (error) {
    return <InlineAlert variant="warning" title={text.unavailable} message={error} />;
  }
  if (!summary) {
    return <div className="border-y border-white/10 py-4 text-sm text-secondary">{text.loading}</div>;
  }

  const concentration = summary.sampleConcentration.dominantInstrumentPct;
  const gatesClear = summary.hardGateFailures.length === 0;
  return (
    <section className="border-y border-white/10 py-5" aria-label={text.aria}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-secondary">
            <GitCompareArrows size={16} aria-hidden="true" />
            <span className="text-xs">{text.subtitle}</span>
          </div>
          <h2 className="mt-1 text-base font-semibold text-foreground">{text.title}</h2>
        </div>
        <div className="flex flex-wrap gap-2">
          <Badge variant={summary.maturityDecision === 'ELIGIBLE_FOR_HUMAN_REVIEW' ? 'success' : 'warning'}>
            {summary.maturityDecision}
          </Badge>
          <Badge variant={summary.longTermImprovementStatus === 'MATURE' ? 'success' : 'warning'}>
            {summary.longTermImprovementStatus}
          </Badge>
        </div>
      </div>

      <div className="mt-4 grid gap-x-6 gap-y-4 sm:grid-cols-2 xl:grid-cols-4">
        <div className="border-l-2 border-cyan/60 pl-3">
          <div className="text-xs text-secondary">{text.champion}</div>
          <div className="mt-1 break-all font-mono text-sm text-foreground">{version(summary.champion.strategyVersion)}</div>
        </div>
        <div className="border-l-2 border-warning/60 pl-3">
          <div className="text-xs text-secondary">{text.challenger}</div>
          <div className="mt-1 break-all font-mono text-sm text-foreground">{version(summary.challenger.strategyVersion)}</div>
        </div>
        <div>
          <div className="text-xs text-secondary">{text.historical}</div>
          <div className="mt-1 text-sm text-foreground">{summary.historicalOos.status}</div>
        </div>
        <div>
          <div className="text-xs text-secondary">{text.prospective}</div>
          <div className="mt-1 text-sm text-foreground">{summary.prospectiveShadow.status}</div>
        </div>
        <div>
          <div className="text-xs text-secondary">{text.concentration}</div>
          <div className="mt-1 text-sm text-foreground">{concentration == null ? '--' : `${concentration.toFixed(2)}%`}</div>
        </div>
        <div>
          <div className="text-xs text-secondary">{text.cost}</div>
          <div className="mt-1 text-sm text-foreground">{formatDelta(summary.costDeltaPct)}</div>
        </div>
        <div>
          <div className="text-xs text-secondary">{text.drawdown}</div>
          <div className="mt-1 text-sm text-foreground">{formatDelta(summary.drawdownDeltaPct)}</div>
        </div>
        <div>
          <div className="text-xs text-secondary">{text.rollback}</div>
          <div className="mt-1 break-all font-mono text-sm text-foreground">{version(summary.rollbackTarget)}</div>
        </div>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-3">
        <div>
          <div className="flex items-center gap-2 text-xs text-secondary">
            {gatesClear
              ? <CheckCircle2 size={14} className="text-success" aria-hidden="true" />
              : <AlertTriangle size={14} className="text-warning" aria-hidden="true" />}
            {text.gates}
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {gatesClear
              ? <span className="text-sm text-success">{text.none}</span>
              : summary.hardGateFailures.map((item) => (
                <span key={item} className="border border-warning/30 px-2 py-1 text-xs text-warning">{item}</span>
              ))}
          </div>
        </div>
        <div>
          <div className="text-xs text-secondary">{text.mature}</div>
          <div className="mt-2 text-sm text-foreground">
            {summary.matureHorizons.length ? summary.matureHorizons.join(' · ') : text.noMature}
          </div>
        </div>
        <div>
          <div className="text-xs text-secondary">{text.unable}</div>
          <div className="mt-2 space-y-1 text-xs text-warning">
            {summary.unableReasons.length
              ? summary.unableReasons.map((item) => <div key={item} className="break-words">{item}</div>)
              : <div className="text-success">{text.none}</div>}
          </div>
        </div>
      </div>

      {summary.longTermImprovementStatus === 'PROVISIONAL' ? (
        <div className="mt-4 text-xs text-secondary">{text.provisional}</div>
      ) : null}
    </section>
  );
}
