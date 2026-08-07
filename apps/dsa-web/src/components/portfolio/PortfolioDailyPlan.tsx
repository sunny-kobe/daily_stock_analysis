import { CalendarCheck2, ChevronDown, RefreshCw, Search, ShieldCheck } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { portfolioApi } from '../../api/portfolio';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { formatUiText } from '../../i18n/uiText';
import type {
  PortfolioResearchBaselineItem,
  PortfolioResearchBaselineResponse,
  PortfolioResearchExecutionCheckResponse,
  PortfolioResearchScopeItem,
  PortfolioUserInstruction,
} from '../../types/portfolio';
import { Button, InlineAlert } from '../common';

export type PortfolioDailyPlanBinding = {
  snapshotHash: string;
  executionIdentityHash: string;
  cutoff: string;
  scope: PortfolioResearchScopeItem[];
};

export type PortfolioDailyPlanScopeOption = PortfolioResearchScopeItem & {
  label: string;
};

export type PortfolioDailyAnalysisState = {
  status: 'analyzing' | 'awaiting_confirmation' | 'insufficient' | 'failed';
  message?: string;
  signalId?: number;
  userInstruction?: PortfolioUserInstruction;
  audit?: { taskId?: string; traceId?: string; blockers?: string[] };
};

type PortfolioDailyPlanProps = {
  scopeOptions: PortfolioDailyPlanScopeOption[];
  onPlanReady: (binding: PortfolioDailyPlanBinding | null) => void;
  onAnalyze: (
    item: PortfolioResearchBaselineItem,
    binding: PortfolioDailyPlanBinding,
  ) => void | Promise<void>;
  onReview?: (signalId: number) => void;
  analysisStates?: Record<string, PortfolioDailyAnalysisState>;
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

function scopeKey(item: PortfolioResearchScopeItem) {
  return `${item.accountId}:${item.market}:${item.symbol}`;
}

function isSameCutoff(left: string, right: string) {
  const leftTime = Date.parse(left);
  const rightTime = Date.parse(right);
  return Number.isFinite(leftTime) && Number.isFinite(rightTime) && leftTime === rightTime;
}

export function PortfolioDailyPlan({
  scopeOptions,
  onPlanReady,
  onAnalyze,
  onReview,
  analysisStates = {},
  analysisLoadingKey = null,
}: PortfolioDailyPlanProps) {
  const { t } = useUiLanguage();
  const [phase, setPhase] = useState<PlanPhase>('idle');
  const [plan, setPlan] = useState<PortfolioResearchBaselineResponse | null>(null);
  const [binding, setBinding] = useState<PortfolioDailyPlanBinding | null>(null);
  const [coverageMismatch, setCoverageMismatch] = useState(false);
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set());
  const [execution, setExecution] = useState<PortfolioResearchExecutionCheckResponse | null>(null);
  const [executionLoading, setExecutionLoading] = useState(false);
  const [executionFailed, setExecutionFailed] = useState(false);
  const [openAuditKeys, setOpenAuditKeys] = useState<Set<string>>(new Set());

  useEffect(() => {
    setSelectedKeys((current) => {
      const available = new Set(scopeOptions.map(scopeKey));
      const kept = new Set([...current].filter((key) => available.has(key)));
      return kept.size > 0 ? kept : available;
    });
  }, [scopeOptions]);

  const selectedScope = useMemo(
    () => scopeOptions
      .filter((item) => selectedKeys.has(scopeKey(item)))
      .map(({ accountId, market, symbol }) => ({ accountId, market, symbol })),
    [scopeOptions, selectedKeys],
  );

  const generate = async () => {
    if (selectedScope.length === 0) return;
    setPlan(null);
    setBinding(null);
    setExecution(null);
    setCoverageMismatch(false);
    onPlanReady(null);
    try {
      const researchCutoff = new Date().toISOString();
      setPhase('preparing');
      const preparation = await portfolioApi.prepareResearchEvidence(selectedScope, researchCutoff);
      if (!isSameCutoff(preparation.cutoff, researchCutoff)) {
        throw new Error('research_evidence_cutoff_mismatch');
      }
      setPhase('freezing');
      const snapshot = await portfolioApi.getResearchSnapshot(selectedScope, researchCutoff);
      if (!isSameCutoff(snapshot.cutoff, researchCutoff)) {
        throw new Error('research_snapshot_cutoff_mismatch');
      }
      const nextBinding: PortfolioDailyPlanBinding = {
        snapshotHash: snapshot.snapshotHash,
        executionIdentityHash: snapshot.executionIdentityHash,
        cutoff: snapshot.cutoff,
        scope: snapshot.scope,
      };
      setPhase('building');
      const baseline = await portfolioApi.buildResearchBaseline({
        researchSnapshotHash: nextBinding.snapshotHash,
        researchCutoff: nextBinding.cutoff,
        researchScope: nextBinding.scope,
      });
      const reconciled = (
        baseline.coverageReconciled
        && baseline.baselineRowCount === baseline.ledgerPositionCount
        && baseline.items.length === baseline.baselineRowCount
        && baseline.baselineRowCount === nextBinding.scope.length
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

  const checkExecution = async () => {
    if (!binding) return;
    setExecutionLoading(true);
    setExecutionFailed(false);
    try {
      setExecution(await portfolioApi.checkResearchExecution({
        researchSnapshotHash: binding.snapshotHash,
        researchExecutionIdentityHash: binding.executionIdentityHash,
        researchCutoff: binding.cutoff,
        researchScope: binding.scope,
      }));
    } catch {
      setExecutionFailed(true);
    } finally {
      setExecutionLoading(false);
    }
  };

  const loading = phase === 'preparing' || phase === 'freezing' || phase === 'building';
  const loadingText = phase === 'preparing'
    ? t('portfolio.dailyPlan.preparing')
    : phase === 'freezing'
      ? t('portfolio.dailyPlan.freezing')
      : t('portfolio.dailyPlan.building');

  return (
    <section aria-labelledby="portfolio-daily-plan-title" className="border-y border-white/10 py-4">
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
        <div className="flex flex-wrap gap-2">
          {binding ? (
            <Button variant="outline" size="sm" onClick={() => void checkExecution()} isLoading={executionLoading}>
              <ShieldCheck aria-hidden="true" className="h-4 w-4" />
              {t('portfolio.dailyPlan.executionCheck')}
            </Button>
          ) : null}
          <Button
            variant="secondary"
            size="sm"
            onClick={() => void generate()}
            disabled={selectedScope.length === 0}
            isLoading={loading}
            loadingText={loadingText}
          >
            {phase === 'ready' ? <RefreshCw aria-hidden="true" className="h-4 w-4" /> : <CalendarCheck2 aria-hidden="true" className="h-4 w-4" />}
            {phase === 'ready' ? t('portfolio.dailyPlan.regenerate') : t('portfolio.dailyPlan.generate')}
          </Button>
        </div>
      </div>

      {!plan ? (
        <fieldset className="mt-4">
          <legend className="text-xs font-medium text-secondary">{t('portfolio.dailyPlan.scope')}</legend>
          <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {scopeOptions.map((item) => {
              const key = scopeKey(item);
              return (
                <label key={key} className="flex min-w-0 items-center gap-2 text-sm text-foreground">
                  <input
                    type="checkbox"
                    checked={selectedKeys.has(key)}
                    onChange={() => setSelectedKeys((current) => {
                      const next = new Set(current);
                      if (next.has(key)) next.delete(key); else next.add(key);
                      return next;
                    })}
                  />
                  <span className="truncate">{item.label}</span>
                </label>
              );
            })}
          </div>
        </fieldset>
      ) : null}

      {phase === 'error' ? (
        <div className="mt-3">
          <InlineAlert variant="warning" message={coverageMismatch ? t('portfolio.dailyPlan.coverageMismatch') : t('portfolio.dailyPlan.failed')} />
          <Button className="mt-2" variant="outline" size="sm" onClick={() => void generate()}>
            <RefreshCw aria-hidden="true" className="h-4 w-4" />
            {t('common.retry')}
          </Button>
        </div>
      ) : null}

      {executionFailed ? <div className="mt-3"><InlineAlert variant="warning" message={t('portfolio.dailyPlan.executionFailed')} /></div> : null}

      {plan && binding ? (
        <div className="mt-4 divide-y divide-white/10 border-t border-white/10">
          {plan.items.map((item) => {
            const insufficient = item.userInstruction === 'insufficient';
            const rowKey = `${item.accountId}-${item.symbol}-${item.market}`;
            const state = analysisStates[rowKey];
            const analyzing = analysisLoadingKey === rowKey || state?.status === 'analyzing';
            const displayedInstruction = state?.status === 'awaiting_confirmation' && state.userInstruction
              ? state.userInstruction
              : item.userInstruction;
            const executionRow = execution?.items.find((candidate) => (
              candidate.accountId === item.accountId
              && candidate.market === item.market
              && candidate.symbol === item.symbol
            ));
            return (
              <div key={`${item.accountId}:${item.market}:${item.symbol}`} className="grid min-h-16 grid-cols-[minmax(0,1fr)_auto] items-center gap-3 py-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="truncate text-sm font-medium text-foreground">{item.displayLabel}</span>
                    <span className={insufficient || state?.status === 'insufficient' ? 'text-xs text-warning' : 'text-xs text-success'}>
                      {state?.status === 'awaiting_confirmation'
                        ? t('portfolio.dailyPlan.awaitingConfirmation')
                        : state?.status === 'failed'
                          ? t('portfolio.dailyPlan.analysisFailed')
                          : insufficient || state?.status === 'insufficient'
                            ? t('portfolio.dailyPlan.insufficient')
                            : t('portfolio.dailyPlan.reference')}
                    </span>
                    {executionRow ? (
                      <span className={executionRow.requiresReconfirmation ? 'text-xs text-warning' : 'text-xs text-success'}>
                        {executionRow.requiresReconfirmation
                          ? t('portfolio.dailyPlan.reconfirmationRequired')
                          : t('portfolio.dailyPlan.executionCurrent')}
                      </span>
                    ) : null}
                  </div>
                  <p className="mt-1 text-sm text-foreground">{t(INSTRUCTION_KEYS[displayedInstruction])}</p>
                  {state?.message ? <p className="mt-1 text-xs text-secondary">{state.message}</p> : null}
                  {(state?.audit || item.hardBlockers.length > 0 || executionRow?.blockers.length) ? (
                    <details
                      className="mt-2 text-xs text-secondary"
                      onToggle={(event) => {
                        const open = event.currentTarget.open;
                        setOpenAuditKeys((current) => {
                          const next = new Set(current);
                          if (open) next.add(rowKey); else next.delete(rowKey);
                          return next;
                        });
                      }}
                    >
                      <summary className="flex cursor-pointer list-none items-center gap-1">
                        <ChevronDown aria-hidden="true" className="h-3 w-3" />
                        {t('portfolio.dailyPlan.auditDetails')}
                      </summary>
                      {openAuditKeys.has(rowKey) ? <div className="mt-2 break-all font-mono">
                        {state?.audit?.taskId ? <div>task: {state.audit.taskId}</div> : null}
                        {state?.audit?.traceId ? <div>trace: {state.audit.traceId}</div> : null}
                        <div>snapshot: {binding.snapshotHash}</div>
                        {[...item.hardBlockers, ...(state?.audit?.blockers || []), ...(executionRow?.blockers || [])].map((blocker) => <div key={blocker}>{blocker}</div>)}
                      </div> : null}
                    </details>
                  ) : null}
                </div>
                {state?.status === 'awaiting_confirmation' && state.signalId && onReview ? (
                  <Button variant="secondary" size="sm" onClick={() => onReview(state.signalId!)}>
                    {t('portfolio.dailyPlan.review')}
                  </Button>
                ) : (
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={insufficient || analyzing}
                    aria-label={formatUiText(t('portfolio.dailyPlan.analyzeAria'), { label: item.displayLabel })}
                    isLoading={analyzing}
                    loadingText={t('portfolio.dailyPlan.submitting')}
                    onClick={() => void onAnalyze(item, binding)}
                  >
                    <Search aria-hidden="true" className="h-4 w-4" />
                    {t('portfolio.dailyPlan.analyze')}
                  </Button>
                )}
              </div>
            );
          })}
        </div>
      ) : null}
    </section>
  );
}
