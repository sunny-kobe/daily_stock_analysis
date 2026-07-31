import { useEffect, useRef, useState } from 'react';
import { AlertTriangle, Check, X } from 'lucide-react';
import { decisionSignalsApi } from '../../api/decisionSignals';
import { getParsedApiError } from '../../api/error';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { UiTextKey } from '../../i18n/uiText';
import type {
  DecisionQualityDetail,
  DecisionQualityHorizon,
  DecisionQualityHumanDecision,
  DecisionQualityOutcome,
  DecisionQualityWeeklyReview,
} from '../../types/decisionSignals';
import {
  instructionToAxes,
  selectableInstructionFromAxes,
  type HoldingInstruction,
  type SelectableHoldingInstruction,
} from '../../utils/portfolioInstruction';

const INSTRUCTION_LABELS: Record<HoldingInstruction, UiTextKey> = {
  add: 'portfolio.decisionReview.instruction.add',
  hold: 'portfolio.decisionReview.instruction.hold',
  reduce: 'portfolio.decisionReview.instruction.reduce',
  exit: 'portfolio.decisionReview.instruction.exit',
  insufficient: 'portfolio.decisionReview.instruction.insufficient',
};
const SELECTABLE_INSTRUCTIONS: SelectableHoldingInstruction[] = ['add', 'hold', 'reduce', 'exit'];
const DECISIONS: Array<[DecisionQualityHumanDecision, UiTextKey]> = [
  ['accept', 'portfolio.decisionReview.accept'],
  ['modify', 'portfolio.decisionReview.modify'],
  ['veto', 'portfolio.decisionReview.veto'],
  ['no_action', 'portfolio.decisionReview.noAction'],
];
const WEEKLY_QUESTIONS: UiTextKey[] = Array.from(
  { length: 7 },
  (_, index) => `portfolio.decisionReview.question${index + 1}` as UiTextKey,
);
const HORIZON_LABELS: Record<DecisionQualityHorizon, UiTextKey> = {
  '5d': 'portfolio.decisionReview.horizon.5d',
  '20d': 'portfolio.decisionReview.horizon.20d',
  '60d': 'portfolio.decisionReview.horizon.60d',
};
const PATTERN_CATEGORY_LABELS: Record<string, UiTextKey> = {
  fact_error: 'portfolio.decisionReview.pattern.factError',
  evidence_error: 'portfolio.decisionReview.pattern.evidenceError',
  thesis_error: 'portfolio.decisionReview.pattern.thesisError',
  valuation_error: 'portfolio.decisionReview.pattern.valuationError',
  timing_error: 'portfolio.decisionReview.pattern.timingError',
  risk_error: 'portfolio.decisionReview.pattern.riskError',
  execution_error: 'portfolio.decisionReview.pattern.executionError',
  unattributed: 'portfolio.decisionReview.pattern.other',
};
const INSTRUMENT_TYPE_LABELS: Record<string, UiTextKey> = {
  equity: 'portfolio.decisionReview.instrument.equity',
  etf: 'portfolio.decisionReview.instrument.etf',
  qdii: 'portfolio.decisionReview.instrument.qdii',
  adr_ads: 'portfolio.decisionReview.instrument.adrAds',
  daily_leveraged_product: 'portfolio.decisionReview.instrument.dailyReset',
};

function instructionLabel(
  value: HoldingInstruction,
  t: (key: UiTextKey, params?: Record<string, string | number>) => string,
) {
  return t(INSTRUCTION_LABELS[value]);
}

function pct(value?: number | null) {
  return value == null ? '--' : `${value.toFixed(2)}%`;
}

function horizonLabel(
  value: unknown,
  t: (key: UiTextKey, params?: Record<string, string | number>) => string,
) {
  const key = HORIZON_LABELS[value as DecisionQualityHorizon];
  return t(key || 'portfolio.decisionReview.horizon.unknown');
}

function outcomeStatusLabel(
  outcome: DecisionQualityOutcome,
  t: (key: UiTextKey, params?: Record<string, string | number>) => string,
) {
  const evalStatus = typeof outcome.evalStatus === 'string'
    ? outcome.evalStatus.toLowerCase()
    : '';
  if (evalStatus === 'complete') {
    return t('portfolio.decisionReview.outcome.ready');
  }
  if (evalStatus === 'pending') {
    return t('portfolio.decisionReview.outcome.waiting');
  }
  return t('portfolio.decisionReview.outcome.insufficient');
}

function safeMappedLabel(
  value: unknown,
  labels: Record<string, UiTextKey>,
  fallback: UiTextKey,
  t: (key: UiTextKey, params?: Record<string, string | number>) => string,
) {
  return t(labels[String(value)] || fallback);
}

export function PortfolioDecisionReview({ signalId, onClose }: { signalId: number; onClose?: () => void }) {
  const { t } = useUiLanguage();
  const [quality, setQuality] = useState<DecisionQualityDetail | null>(null);
  const [weekly, setWeekly] = useState<DecisionQualityWeeklyReview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState('');
  const [validation, setValidation] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState<{ type: 'success' | 'error'; message: string } | null>(null);
  const [modifying, setModifying] = useState(false);
  const [humanInstruction, setHumanInstruction] = useState<SelectableHoldingInstruction>('hold');
  const requestRef = useRef(0);

  useEffect(() => {
    const requestId = ++requestRef.current;
    setQuality(null);
    setError(null);
    setSaveStatus(null);
    void decisionSignalsApi.getQuality(signalId).then((result) => {
      if (requestRef.current === requestId) setQuality(result);
    }).catch(() => {
      if (requestRef.current === requestId) setError(t('portfolio.decisionReview.unavailable'));
    });
    void decisionSignalsApi.getQualityWeeklyReview().then((result) => {
      if (requestRef.current === requestId) setWeekly(result);
    }).catch(() => undefined);
    return () => { requestRef.current += 1; };
  }, [signalId, t]);

  const submit = async (decision: DecisionQualityHumanDecision) => {
    if ((decision === 'modify' || decision === 'veto') && !reason.trim()) {
      setValidation(t('portfolio.decisionReview.reasonRequired'));
      return;
    }
    if (!quality) return;
    setSaving(true);
    setValidation(null);
    setSaveStatus(null);
    try {
      const humanAxes = quality && decision === 'modify'
        ? instructionToAxes(
          humanInstruction,
          quality.context.positionAction,
          quality.context.incrementalAction,
        )
        : null;
      await decisionSignalsApi.putShadowFeedback(signalId, {
        humanDecision: decision,
        humanPositionAction: humanAxes?.positionAction,
        humanIncrementalAction: humanAxes?.incrementalAction,
        decisionReasonCode: reason.trim() || undefined,
      });
      setSaveStatus({ type: 'success', message: t('portfolio.decisionReview.feedbackSaved') });
    } catch (saveError) {
      setSaveStatus({ type: 'error', message: getParsedApiError(saveError).message });
    } finally {
      setSaving(false);
    }
  };

  if (error) return <div className="text-sm text-warning">{error}</div>;
  if (!quality) return <div className="text-sm text-secondary">{t('portfolio.decisionReview.loading')}</div>;
  const { context } = quality;
  const evidenceSnapshot = quality.evidenceSnapshot;
  const evidenceComplete = evidenceSnapshot?.status === 'complete';
  const strategyLabel = [
    evidenceSnapshot?.strategyName,
    evidenceSnapshot?.strategyVersion,
  ].filter(Boolean).join(' ');
  return (
    <section className="border-t border-white/10 py-4" aria-label={t('portfolio.decisionReview.title')}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-base font-semibold text-foreground">{t('portfolio.decisionReview.title')}</h3>
          <div className="mt-2 flex flex-wrap gap-x-5 gap-y-2 text-sm">
            <span>{t('portfolio.decisionReview.instruction', { value: instructionLabel(context.userInstruction, t) })}</span>
            <span>{t('portfolio.decisionReview.benchmark', { value: context.benchmark?.code || t('portfolio.decisionReview.insufficientBenchmark') })}</span>
            <span>{t('portfolio.decisionReview.evidence', {
              value: t(evidenceComplete
                ? 'portfolio.decisionReview.evidenceSaved'
                : 'portfolio.decisionReview.evidenceInsufficient'),
            })}</span>
            {strategyLabel ? <span>{t('portfolio.decisionReview.strategy', { value: strategyLabel })}</span> : null}
          </div>
        </div>
        {onClose ? <button type="button" aria-label={t('portfolio.decisionReview.close')} onClick={onClose} className="btn-secondary p-2"><X size={16} /></button> : null}
      </div>
      {!evidenceComplete ? (
        <div className="mt-3 flex items-start gap-2 text-sm text-warning">
          <AlertTriangle size={16} className="mt-0.5 shrink-0" />
          <span>{t('portfolio.decisionReview.evidenceMissingMessage')}</span>
        </div>
      ) : null}
      {evidenceComplete && context.unableReasons.length ? (
        <div className="mt-3 flex items-start gap-2 text-sm text-warning"><AlertTriangle size={16} className="mt-0.5 shrink-0" /><span>{t('portfolio.decisionReview.contextInsufficient')}</span></div>
      ) : null}
      <div className="mt-4 grid gap-3 sm:grid-cols-3">
        {quality.outcomes.map((outcome) => (
          <div key={outcome.horizon} className="border-l-2 border-white/15 pl-3 text-sm">
            <div className="font-medium text-foreground">{horizonLabel(outcome.horizon, t)} · {outcomeStatusLabel(outcome, t)}</div>
            <div className="mt-1 text-secondary">{t('portfolio.decisionReview.excess', { value: pct(outcome.excessReturnPct) })}</div>
            <div className="text-secondary">{t('portfolio.decisionReview.bestGain', { value: pct(outcome.maxFavorableExcursionPct) })} · {t('portfolio.decisionReview.maxDrawdown', { value: pct(outcome.maxAdverseExcursionPct) })}</div>
          </div>
        ))}
      </div>
      <div className="mt-4 flex flex-wrap gap-2">
        {DECISIONS.map(([value, textKey]) => <button key={value} type="button" disabled={saving} onClick={() => {
          if (value === 'modify') {
            setHumanInstruction(selectableInstructionFromAxes(context.positionAction, context.incrementalAction));
            setModifying(true);
            setValidation(null);
            return;
          }
          setModifying(false);
          void submit(value);
        }} className="btn-secondary px-3 py-2 text-sm">{value === 'accept' ? <Check size={14} className="mr-1 inline" /> : null}{t(textKey)}</button>)}
      </div>
      {modifying ? (
        <div className="mt-3 grid gap-3">
          <label className="text-sm text-secondary">{t('portfolio.decisionReview.modifiedInstruction')}
            <select aria-label={t('portfolio.decisionReview.modifiedInstruction')} value={humanInstruction} onChange={(event) => setHumanInstruction(event.target.value as SelectableHoldingInstruction)} className="input-surface mt-1 h-10 w-full border px-3 text-foreground">
              {SELECTABLE_INSTRUCTIONS.map((value) => <option key={value} value={value}>{instructionLabel(value, t)}</option>)}
            </select>
          </label>
          <button type="button" disabled={saving} onClick={() => void submit('modify')} className="btn-secondary px-3 py-2 text-sm">{t('portfolio.decisionReview.submitModify')}</button>
        </div>
      ) : null}
      <label className="mt-3 block text-sm text-secondary">{t('portfolio.decisionReview.reason')}<input value={reason} onChange={(event) => setReason(event.target.value)} className="input-surface mt-1 h-10 w-full border px-3 text-foreground" /></label>
      {validation ? <div className="mt-2 text-sm text-danger">{validation}</div> : null}
      {saveStatus ? <div role="status" className={`mt-2 text-sm ${saveStatus.type === 'success' ? 'text-success' : 'text-danger'}`}>{saveStatus.message}</div> : null}
      <div className="mt-5 border-t border-white/10 pt-4">
        <h4 className="text-sm font-semibold text-foreground">{t('portfolio.decisionReview.weeklyTitle')}</h4>
        <ol className="mt-2 grid gap-1 text-sm text-secondary sm:grid-cols-2">
          {WEEKLY_QUESTIONS.map((key) => <li key={key}>{t(key)}</li>)}
        </ol>
        {weekly?.candidatePatterns.map((pattern, index) => {
          const item = pattern as Record<string, unknown>;
          const counterexamples = Array.isArray(item.counterexamples) ? item.counterexamples : [];
          return (
            <div key={`${String(item.category)}-${String(item.horizon)}-${index}`} className="mt-3 border-l-2 border-warning pl-3 text-sm">
              <div className="font-medium text-foreground">{safeMappedLabel(item.category, PATTERN_CATEGORY_LABELS, 'portfolio.decisionReview.pattern.other', t)} · {horizonLabel(item.horizon, t)} · {safeMappedLabel(item.instrumentType, INSTRUMENT_TYPE_LABELS, 'portfolio.decisionReview.instrument.other', t)}</div>
              <div className="text-secondary">{t('portfolio.decisionReview.sample', { count: String(item.eligibleSampleCount), status: t('portfolio.decisionReview.pattern.observed') })}</div>
              {counterexamples.length ? <div className="break-words text-secondary">{t('portfolio.decisionReview.hasCounterexamples')}</div> : null}
            </div>
          );
        })}
        <div className="mt-2 text-xs text-secondary">{t('portfolio.decisionReview.noAutomaticRules')}</div>
      </div>
    </section>
  );
}
