import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { portfolioApi } from '../../../api/portfolio';
import { PortfolioDailyPlan } from '../PortfolioDailyPlan';
import { UI_LANGUAGE_STORAGE_KEY } from '../../../utils/uiLanguage';

vi.mock('../../../api/portfolio', () => ({
  portfolioApi: {
    prepareResearchEvidence: vi.fn(),
    getResearchSnapshot: vi.fn(),
    buildResearchBaseline: vi.fn(),
  },
}));

const snapshotHash = 'a'.repeat(64);
const cutoff = '2026-07-31T08:00:00Z';

const prepared = {
  schemaVersion: 'portfolio-research-evidence-prepare-v1' as const,
  preparedAt: cutoff,
  asOf: '2026-07-31',
  status: 'ready' as const,
  positionCount: 2,
  readyCount: 2,
  insufficientCount: 0,
  items: [],
};

const snapshot = {
  snapshotHash,
  cutoff,
  completeness: 'COMPLETE',
  positions: [{ accountId: 1 }, { accountId: 2 }],
  instruments: [],
  pointInTime: {
    scope: 'current_prospective' as const,
    prospectiveDecisionEligible: true,
    historicalReplayEligible: false as const,
    sourceCutoffs: {},
    blockers: [],
  },
  decisionSignals: [],
  hardBlockers: [],
  limitations: [],
};

const baseline = {
  schemaVersion: 'portfolio-research-baseline-v1' as const,
  snapshotHash,
  cutoff,
  marketDataCutoff: cutoff,
  ledgerPositionCount: 2,
  baselineRowCount: 2,
  coverageReconciled: true,
  portfolioRiskFlags: [],
  items: [
    {
      accountId: 1,
      market: 'us',
      symbol: 'AAPL',
      name: '苹果',
      displayLabel: '苹果（AAPL）',
      selectionKey: 'us:AAPL',
      instrumentType: 'equity',
      quote: {},
      history: {},
      positionAction: 'hold' as const,
      incrementalAction: 'wait' as const,
      userInstruction: 'hold' as const,
      hardBlockers: [],
      riskFlags: [],
      exceptionReasons: [],
      evidenceStatus: 'baseline',
      researchLevel: 'baseline' as const,
      detailRecommended: false,
      sizingAllowed: false,
    },
    {
      accountId: 2,
      market: 'cn',
      symbol: '513870',
      name: '纳指ETF',
      displayLabel: '纳指ETF（513870）',
      selectionKey: 'cn:513870',
      instrumentType: 'qdii',
      quote: {},
      history: {},
      positionAction: 'hold' as const,
      incrementalAction: 'wait' as const,
      userInstruction: 'insufficient' as const,
      hardBlockers: ['nav_premium_missing'],
      riskFlags: [],
      exceptionReasons: ['nav_premium_missing'],
      evidenceStatus: 'INSUFFICIENT_EVIDENCE',
      researchLevel: 'baseline' as const,
      detailRecommended: true,
      sizingAllowed: false,
    },
  ],
  suggestedDeepAnalysis: [],
  deepAnalysisStarted: false,
};

function renderPlan(props: Partial<React.ComponentProps<typeof PortfolioDailyPlan>> = {}) {
  const onPlanReady = vi.fn();
  const onAnalyze = vi.fn();
  render(
    <UiLanguageProvider>
      <PortfolioDailyPlan
        onPlanReady={onPlanReady}
        onAnalyze={onAnalyze}
        {...props}
      />
    </UiLanguageProvider>,
  );
  return { onPlanReady, onAnalyze };
}

describe('PortfolioDailyPlan', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'zh');
    vi.mocked(portfolioApi.prepareResearchEvidence).mockResolvedValue(prepared);
    vi.mocked(portfolioApi.getResearchSnapshot).mockResolvedValue(snapshot);
    vi.mocked(portfolioApi.buildResearchBaseline).mockResolvedValue(baseline);
  });

  it('runs prepare, snapshot, and baseline in strict order', async () => {
    const order: string[] = [];
    vi.mocked(portfolioApi.prepareResearchEvidence).mockImplementation(async () => {
      order.push('prepare');
      return prepared;
    });
    vi.mocked(portfolioApi.getResearchSnapshot).mockImplementation(async () => {
      order.push('snapshot');
      return snapshot;
    });
    vi.mocked(portfolioApi.buildResearchBaseline).mockImplementation(async () => {
      order.push('baseline');
      return baseline;
    });
    const { onPlanReady } = renderPlan();

    fireEvent.click(screen.getByRole('button', { name: '生成今日计划' }));

    expect(await screen.findByText('2 项持仓，2 项已生成')).toBeInTheDocument();
    expect(order).toEqual(['prepare', 'snapshot', 'baseline']);
    expect(portfolioApi.buildResearchBaseline).toHaveBeenCalledWith({
      researchSnapshotHash: snapshotHash,
      researchCutoff: cutoff,
    });
    expect(onPlanReady).toHaveBeenLastCalledWith({ snapshotHash, cutoff });
  });

  it('shows simple instructions without internal status, blockers, or hashes', async () => {
    renderPlan();
    fireEvent.click(screen.getByRole('button', { name: '生成今日计划' }));

    expect(await screen.findByText('持有')).toBeInTheDocument();
    expect(screen.getAllByText('资料不足').length).toBeGreaterThan(0);
    expect(screen.getByText('参考建议')).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(
      /INSUFFICIENT_EVIDENCE|nav_premium_missing|current_prospective|[a-f0-9]{64}/i,
    );
  });

  it('fails closed on coverage mismatch', async () => {
    vi.mocked(portfolioApi.buildResearchBaseline).mockResolvedValueOnce({
      ...baseline,
      baselineRowCount: 1,
      coverageReconciled: false,
    });
    const { onPlanReady } = renderPlan();

    fireEvent.click(screen.getByRole('button', { name: '生成今日计划' }));

    expect(await screen.findByText('持仓数量不一致，请重新生成今日计划。')).toBeInTheDocument();
    expect(onPlanReady).toHaveBeenLastCalledWith(null);
  });

  it('allows retry after preparation fails', async () => {
    vi.mocked(portfolioApi.prepareResearchEvidence)
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(prepared);
    renderPlan();

    fireEvent.click(screen.getByRole('button', { name: '生成今日计划' }));
    expect(await screen.findByText('今日计划生成失败，请重试。')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重试' }));

    await waitFor(() => expect(portfolioApi.prepareResearchEvidence).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('2 项持仓，2 项已生成')).toBeInTheDocument();
  });

  it('passes the frozen binding when detailed analysis is selected', async () => {
    const { onAnalyze } = renderPlan();
    fireEvent.click(screen.getByRole('button', { name: '生成今日计划' }));

    fireEvent.click(await screen.findByRole('button', { name: '详细分析 纳指ETF（513870）' }));

    expect(onAnalyze).toHaveBeenCalledWith(baseline.items[1], { snapshotHash, cutoff });
  });
});
