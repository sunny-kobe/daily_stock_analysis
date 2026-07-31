import { useState } from 'react';
import { AlertTriangle, ArrowRight, CheckCircle2 } from 'lucide-react';
import { Badge, Button } from '../common';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type {
  StrategyMetricBucket,
  StrategyStatus,
  StrategyVersion,
} from '../../types/strategyValidation';

interface StrategyScorecardProps {
  strategy: StrategyVersion;
  onTransition: (status: StrategyStatus, reason: string) => void | Promise<void>;
  isTransitioning?: boolean;
}

const STATUS_KEYS: Record<StrategyStatus, Parameters<ReturnType<typeof useUiLanguage>['t']>[0]> = {
  draft: 'strategy.status.draft',
  backtest_running: 'strategy.status.backtestRunning',
  backtest_failed: 'strategy.status.backtestFailed',
  simulation: 'strategy.status.simulation',
  small_capital: 'strategy.status.smallCapital',
  active: 'strategy.status.active',
  retired: 'strategy.status.retired',
};

const TRANSITION_KEYS: Record<StrategyStatus, Parameters<ReturnType<typeof useUiLanguage>['t']>[0]> = {
  draft: 'strategy.status.draft',
  backtest_running: 'strategy.transition.startBacktest',
  backtest_failed: 'strategy.transition.markFailed',
  simulation: 'strategy.transition.enterSimulation',
  small_capital: 'strategy.transition.enterSmallCapital',
  active: 'strategy.transition.activate',
  retired: 'strategy.transition.retire',
};

function transitionKey(
  strategy: StrategyVersion,
  status: StrategyStatus,
): Parameters<ReturnType<typeof useUiLanguage>['t']>[0] {
  if (strategy.evaluationMode === 'forward_only' && status === 'simulation') {
    return 'strategy.transition.startSimulation';
  }
  return TRANSITION_KEYS[status];
}

const MARKET_LABELS: Record<string, string> = { cn: 'A 股', hk: '港股', us: '美股' };
const PRODUCT_LABELS: Record<string, string> = {
  equity: '股票',
  etf: 'ETF',
  qdii: 'QDII',
  adr_ads: 'ADR/ADS',
  daily_leveraged_product: '每日重置产品',
};

function pct(value?: number | null): string {
  return value == null ? '--' : `${value.toFixed(1)}%`;
}

function instructionLabel(value?: string): string {
  return ({ add: '加仓', hold: '持有', reduce: '减仓', exit: '清仓' } as Record<string, string>)[value ?? ''] ?? '资料不足';
}

function periodLabel(value?: string): string {
  return value === 'development' ? '开发期' : value === 'validation' ? '验证期' : value ?? '--';
}

function regimeLabel(value?: string): string {
  return ({ uptrend: '上涨趋势', downtrend: '下跌趋势', sideways: '震荡行情' } as Record<string, string>)[value ?? ''] ?? value ?? '--';
}

function bucketLabel(bucket: StrategyMetricBucket): string {
  const item = bucket.dimensions;
  return [
    item.horizon,
    MARKET_LABELS[item.market ?? ''] ?? item.market,
    PRODUCT_LABELS[item.productType ?? ''] ?? item.productType,
    instructionLabel(item.instruction),
  ].filter(Boolean).join(' · ');
}

function findWorstBucket(buckets: StrategyMetricBucket[]): StrategyMetricBucket | null {
  let worst: StrategyMetricBucket | null = null;
  for (const bucket of buckets) {
    const value = bucket.metrics.netReturnAfterCostPct;
    const worstValue = worst?.metrics.netReturnAfterCostPct;
    if (value != null && (worstValue == null || value < worstValue)) worst = bucket;
  }
  return worst;
}

export function StrategyScorecard({ strategy, onTransition, isTransitioning = false }: StrategyScorecardProps) {
  const { t } = useUiLanguage();
  const [reason, setReason] = useState('');
  const [reasonError, setReasonError] = useState(false);
  const run = strategy.latestRun;
  const buckets = run?.result.buckets ?? [];
  const worstBucket = findWorstBucket(buckets);

  const handleTransition = (status: StrategyStatus) => {
    const normalized = reason.trim();
    if (!normalized) {
      setReasonError(true);
      return;
    }
    setReasonError(false);
    void onTransition(status, normalized);
  };

  return (
    <section className="min-w-0 flex-1" aria-label={t('strategy.scorecard')}>
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border/60 pb-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-lg font-semibold text-foreground">{strategy.name}</h2>
            <span className="font-mono text-xs text-muted-text">v{strategy.version}</span>
            <Badge variant={strategy.status === 'active' ? 'success' : strategy.status === 'backtest_failed' ? 'danger' : 'info'}>
              {t(STATUS_KEYS[strategy.status])}
            </Badge>
          </div>
          <p className="mt-2 text-sm text-secondary-text">{strategy.changeSummary}</p>
        </div>
        <span className="text-xs text-muted-text">{strategy.evaluationMode === 'forward_only' ? t('strategy.forwardOnly') : t('strategy.historicalAndForward')}</span>
      </div>

      {run?.result.displayMessage ? (
        <div className="mt-4 flex items-center gap-2 border-l-2 border-warning pl-3 text-sm text-warning">
          <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />
          {run.result.displayMessage}
        </div>
      ) : null}

      {buckets.length > 0 ? (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[980px] text-sm">
            <thead>
              <tr className="border-b border-border/70 text-left text-xs text-muted-text">
                <th className="px-2 py-2 font-medium">{t('strategy.group')}</th>
                <th className="px-2 py-2 font-medium">{t('strategy.sampleCount')}</th>
                <th className="px-2 py-2 font-medium">{t('strategy.winRate')}</th>
                <th className="px-2 py-2 font-medium">{t('strategy.netReturn')}</th>
                <th className="px-2 py-2 font-medium">{t('strategy.benchmarkExcess')}</th>
                <th className="px-2 py-2 font-medium">{t('strategy.maxDrawdown')}</th>
                <th className="px-2 py-2 font-medium">{t('strategy.averageGainLoss')}</th>
                <th className="px-2 py-2 font-medium">{t('strategy.totalCost')}</th>
              </tr>
            </thead>
            <tbody>
              {buckets.map((bucket) => (
                <tr key={JSON.stringify(bucket.dimensions)} className="border-b border-border/40 align-top">
                  <td className="px-2 py-3 text-foreground">{bucketLabel(bucket)}</td>
                  <td className="px-2 py-3 tabular-nums">{bucket.metrics.sampleCount}</td>
                  <td className="px-2 py-3 tabular-nums" title={bucket.metrics.winDefinition ?? undefined}>{pct(bucket.metrics.winRatePct)}</td>
                  <td className="px-2 py-3 tabular-nums">{pct(bucket.metrics.netReturnAfterCostPct)}</td>
                  <td className="px-2 py-3 tabular-nums">{pct(bucket.metrics.benchmarkExcessPct)}</td>
                  <td className="px-2 py-3 tabular-nums text-danger">{pct(bucket.metrics.maximumDrawdownPct)}</td>
                  <td className="px-2 py-3 tabular-nums">{pct(bucket.metrics.averageGainPct)} / {pct(bucket.metrics.averageLossPct)}</td>
                  <td className="px-2 py-3 tabular-nums">{pct(bucket.metrics.totalCostPct)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="mt-6 border-y border-border/50 py-8 text-center text-sm text-secondary-text">
          {run ? t('strategy.noHistoricalMetrics') : t('strategy.noRun')}
        </div>
      )}

      {worstBucket ? (
        <div className="mt-4 flex flex-wrap items-center gap-2 text-sm">
          <span className="text-muted-text">{t('strategy.worstPeriod')}</span>
          <span className="font-medium text-foreground">
            {periodLabel(worstBucket.dimensions.period)} · {regimeLabel(worstBucket.dimensions.marketRegime)}
          </span>
        </div>
      ) : null}

      {(run?.result.unableReasons.length ?? 0) > 0 ? (
        <div className="mt-4 space-y-2" aria-label={t('strategy.unableReasons')}>
          {run?.result.unableReasons.map((item) => (
            <div key={item} className="flex items-start gap-2 border-l-2 border-warning pl-3 text-sm text-warning">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
              资料不足：{item}
            </div>
          ))}
        </div>
      ) : run?.status === 'completed' ? (
        <div className="mt-4 flex items-center gap-2 text-sm text-success">
          <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
          {t('strategy.runComplete')}
        </div>
      ) : null}

      {strategy.allowedTransitions.length > 0 ? (
        <div className="mt-6 border-t border-border/60 pt-4">
          <label htmlFor="strategy-human-reason" className="text-xs font-medium text-muted-text">
            {t('strategy.humanReason')}
          </label>
          <div className="mt-2 flex flex-col gap-2 sm:flex-row">
            <input
              id="strategy-human-reason"
              aria-label={t('strategy.humanReason')}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              className="input-surface input-focus-glow h-10 min-w-0 flex-1 rounded-lg border bg-transparent px-3 text-sm outline-none"
              placeholder={t('strategy.humanReasonPlaceholder')}
            />
            {strategy.allowedTransitions.map((status) => (
              <Button
                key={status}
                variant="secondary"
                size="md"
                isLoading={isTransitioning}
                onClick={() => handleTransition(status)}
              >
                {t(transitionKey(strategy, status))}
                <ArrowRight className="h-4 w-4" aria-hidden="true" />
              </Button>
            ))}
          </div>
          {reasonError ? <p className="mt-2 text-xs text-danger">{t('strategy.humanReasonRequired')}</p> : null}
        </div>
      ) : null}
    </section>
  );
}
