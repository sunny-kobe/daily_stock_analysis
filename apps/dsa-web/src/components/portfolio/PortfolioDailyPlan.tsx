import { CalendarCheck2, RefreshCw, Search } from 'lucide-react';
import { useState } from 'react';
import { portfolioApi } from '../../api/portfolio';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { formatUiText } from '../../i18n/uiText';
import type {
  PortfolioResearchBaselineItem,
  PortfolioResearchBaselineResponse,
} from '../../types/portfolio';
import { Button, InlineAlert } from '../common';

export type PortfolioDailyPlanBinding = {
  snapshotHash: string;
  cutoff: string;
};

type PortfolioDailyPlanProps = {
  onPlanReady: (binding: PortfolioDailyPlanBinding | null) => void;
  onAnalyze: (
    item: PortfolioResearchBaselineItem,
    binding: PortfolioDailyPlanBinding,
  ) => void | Promise<void>;
  analysisLoadingKey?: string | null;
};

type PlanPhase = 'idle' | 'preparing' | 'freezing' | 'building' | 'ready' | 'error';

const INSTRUCTION_KEYS = {
  add: 'portfolio.dailyPlan.instruction.add',
  hold: 'portfolio.dailyPlan.instruction.hold',
  reduce: 'portfolio.dailyPlan.instruction.reduce',
  exit: 'portfolio.dailyPlan.instruction.exit',
  insufficient: 'portfolio.dailyPlan.instruction.insufficient',
} as const;

export function PortfolioDailyPlan({
  onPlanReady,
  onAnalyze,
  analysisLoadingKey = null,
}: PortfolioDailyPlanProps) {
  const { t } = useUiLanguage();
  const [phase, setPhase] = useState<PlanPhase>('idle');
  const [plan, setPlan] = useState<PortfolioResearchBaselineResponse | null>(null);
  const [binding, setBinding] = useState<PortfolioDailyPlanBinding | null>(null);
  const [coverageMismatch, setCoverageMismatch] = useState(false);

  const generate = async () => {
    setPlan(null);
    setBinding(null);
    setCoverageMismatch(false);
    onPlanReady(null);
    try {
      setPhase('preparing');
      await portfolioApi.prepareResearchEvidence();
      setPhase('freezing');
      const snapshot = await portfolioApi.getResearchSnapshot();
      const nextBinding = {
        snapshotHash: snapshot.snapshotHash,
        cutoff: snapshot.cutoff,
      };
      setPhase('building');
      const baseline = await portfolioApi.buildResearchBaseline({
        researchSnapshotHash: nextBinding.snapshotHash,
        researchCutoff: nextBinding.cutoff,
      });
      const reconciled = (
        baseline.coverageReconciled
        && baseline.baselineRowCount === baseline.ledgerPositionCount
        && baseline.items.length === baseline.baselineRowCount
      );
      if (!reconciled) {
        setCoverageMismatch(true);
        setPhase('error');
        return;
      }
      setPlan(baseline);
      setBinding(nextBinding);
      setPhase('ready');
      onPlanReady(nextBinding);
    } catch {
      setPhase('error');
    }
  };

  const loading = phase === 'preparing' || phase === 'freezing' || phase === 'building';
  const loadingText = phase === 'preparing'
    ? t('portfolio.dailyPlan.preparing')
    : phase === 'freezing'
      ? t('portfolio.dailyPlan.freezing')
      : t('portfolio.dailyPlan.building');

  return (
    <section
      aria-labelledby="portfolio-daily-plan-title"
      className="rounded-lg border border-white/10 bg-card/40 p-4"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 id="portfolio-daily-plan-title" className="text-sm font-semibold text-foreground">
            {t('portfolio.dailyPlan.title')}
          </h2>
          {plan ? (
            <p className="mt-1 text-xs text-secondary">
              {formatUiText(t('portfolio.dailyPlan.coverage'), {
                total: plan.ledgerPositionCount,
                ready: plan.baselineRowCount,
              })}
            </p>
          ) : null}
        </div>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => void generate()}
          isLoading={loading}
          loadingText={loadingText}
        >
          {phase === 'ready' ? <RefreshCw aria-hidden="true" className="h-4 w-4" /> : <CalendarCheck2 aria-hidden="true" className="h-4 w-4" />}
          {phase === 'ready' ? t('portfolio.dailyPlan.regenerate') : t('portfolio.dailyPlan.generate')}
        </Button>
      </div>

      {phase === 'error' ? (
        <div className="mt-3">
          <InlineAlert
            variant="warning"
            message={coverageMismatch
              ? t('portfolio.dailyPlan.coverageMismatch')
              : t('portfolio.dailyPlan.failed')}
          />
          <Button className="mt-2" variant="outline" size="sm" onClick={() => void generate()}>
            <RefreshCw aria-hidden="true" className="h-4 w-4" />
            {t('common.retry')}
          </Button>
        </div>
      ) : null}

      {plan && binding ? (
        <div className="mt-4 divide-y divide-white/10 border-t border-white/10">
          {plan.items.map((item) => {
            const insufficient = item.userInstruction === 'insufficient';
            const rowKey = `${item.accountId}-${item.symbol}-${item.market}`;
            const analyzing = analysisLoadingKey === rowKey;
            return (
              <div
                key={`${item.accountId}:${item.market}:${item.symbol}`}
                className="grid min-h-16 grid-cols-[minmax(0,1fr)_auto] items-center gap-3 py-3"
              >
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="truncate text-sm font-medium text-foreground">
                      {item.displayLabel}
                    </span>
                    <span className={insufficient ? 'text-xs text-warning' : 'text-xs text-success'}>
                      {insufficient
                        ? t('portfolio.dailyPlan.insufficient')
                        : t('portfolio.dailyPlan.reference')}
                    </span>
                  </div>
                  <p className="mt-1 text-sm text-foreground">
                    {t(INSTRUCTION_KEYS[item.userInstruction])}
                  </p>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={formatUiText(t('portfolio.dailyPlan.analyzeAria'), {
                    label: item.displayLabel,
                  })}
                  isLoading={analyzing}
                  loadingText={t('portfolio.dailyPlan.submitting')}
                  onClick={() => void onAnalyze(item, binding)}
                >
                  <Search aria-hidden="true" className="h-4 w-4" />
                  {t('portfolio.dailyPlan.analyze')}
                </Button>
              </div>
            );
          })}
        </div>
      ) : null}
    </section>
  );
}
