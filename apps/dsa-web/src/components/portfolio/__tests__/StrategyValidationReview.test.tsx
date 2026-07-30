import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { decisionSignalsApi } from '../../../api/decisionSignals';
import type { StrategyValidationReviewSummary } from '../../../types/decisionSignals';
import { StrategyValidationReview } from '../StrategyValidationReview';

vi.mock('../../../api/decisionSignals', () => ({
  decisionSignalsApi: {
    getStrategyValidationReviewSummary: vi.fn(),
  },
}));

const summary: StrategyValidationReviewSummary = {
  strategyId: 'portfolio-champion',
  protocolId: 'shadow-001',
  champion: { strategyVersion: 'champion-v1' },
  challenger: { strategyVersion: 'challenger-v1' },
  historicalOos: { status: 'positive', eventCount: 8 },
  prospectiveShadow: { status: 'collecting', comparisonCount: 4 },
  hardGateFailures: ['identity_regression'],
  sampleConcentration: { dominantInstrumentPct: 62.5 },
  costDeltaPct: 0.4,
  drawdownDeltaPct: 1.2,
  unableReasons: ['mature_shadow_evidence_not_recorded'],
  matureHorizons: ['5d', '20d'],
  maturityDecision: 'CONTINUE_SHADOW',
  rollbackTarget: 'champion-v0',
  longTermImprovementStatus: 'PROVISIONAL',
  automaticPromotion: false,
  runtimeActivated: false,
};

describe('StrategyValidationReview', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.removeItem('dsa.uiLanguage');
    vi.mocked(decisionSignalsApi.getStrategyValidationReviewSummary).mockResolvedValue(summary);
  });

  it('shows paired evidence, gates, concentration, costs, maturity, and rollback target', async () => {
    render(<StrategyValidationReview />);

    expect(await screen.findByRole('heading', { name: '策略验证复盘' })).toBeInTheDocument();
    expect(screen.getByText('champion-v1')).toBeInTheDocument();
    expect(screen.getByText('challenger-v1')).toBeInTheDocument();
    expect(screen.getByText(/positive/)).toBeInTheDocument();
    expect(screen.getByText(/collecting/)).toBeInTheDocument();
    expect(screen.getByText('identity_regression')).toBeInTheDocument();
    expect(screen.getByText('62.50%')).toBeInTheDocument();
    expect(screen.getByText('+0.40%')).toBeInTheDocument();
    expect(screen.getByText('+1.20%')).toBeInTheDocument();
    expect(screen.getByText('mature_shadow_evidence_not_recorded')).toBeInTheDocument();
    expect(screen.getByText('5d · 20d')).toBeInTheDocument();
    expect(screen.getByText('champion-v0')).toBeInTheDocument();
    expect(screen.getByText('PROVISIONAL')).toBeInTheDocument();
  });

  it('does not expose order, automatic activation, approval, or rollback controls', async () => {
    render(<StrategyValidationReview />);
    await screen.findByText('champion-v1');

    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.queryByText(/下单|自动激活|批准策略|执行回滚/)).not.toBeInTheDocument();
  });

  it('keeps unavailable evidence visible instead of displaying success', async () => {
    vi.mocked(decisionSignalsApi.getStrategyValidationReviewSummary).mockResolvedValue({
      ...summary,
      historicalOos: { status: 'unable', unableReasons: ['validation_evidence_not_recorded'] },
      matureHorizons: [],
      unableReasons: ['validation_evidence_not_recorded'],
    });
    render(<StrategyValidationReview />);

    expect(await screen.findByText('validation_evidence_not_recorded')).toBeInTheDocument();
    expect(screen.getByText('尚无成熟周期')).toBeInTheDocument();
  });
});
