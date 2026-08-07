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
    checkResearchExecution: vi.fn(),
  },
}));

const snapshotHash = 'a'.repeat(64);
const executionIdentityHash = 'e'.repeat(64);
const cutoff = '2026-07-31T08:00:00Z';

const prepared = {
  schemaVersion: 'portfolio-research-evidence-prepare-v2' as const,
  scope: [
    { accountId: 1, market: 'us' as const, symbol: 'AAPL' },
    { accountId: 2, market: 'cn' as const, symbol: '513870' },
  ],
  preparedAt: cutoff,
  cutoff,
  asOf: '2026-07-31',
  status: 'ready' as const,
  positionCount: 2,
  readyCount: 2,
  insufficientCount: 0,
  items: [],
};

const snapshot = {
  snapshotHash,
  executionIdentityHash,
  cutoff,
  completeness: 'COMPLETE',
  scope: prepared.scope,
  scopeHash: 'b'.repeat(64),
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
        scopeOptions={[
          { accountId: 1, market: 'us', symbol: 'AAPL', label: 'Main · AAPL' },
          { accountId: 2, market: 'cn', symbol: '513870', label: 'ETF · 513870' },
        ]}
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
    vi.mocked(portfolioApi.prepareResearchEvidence).mockImplementation(
      async (_scope, requestedCutoff) => ({ ...prepared, cutoff: requestedCutoff }),
    );
    vi.mocked(portfolioApi.getResearchSnapshot).mockImplementation(
      async (_scope, requestedCutoff) => ({ ...snapshot, cutoff: requestedCutoff ?? cutoff }),
    );
    vi.mocked(portfolioApi.buildResearchBaseline).mockResolvedValue(baseline);
  });

  it('runs prepare, snapshot, and baseline in strict order', async () => {
    const order: string[] = [];
    vi.mocked(portfolioApi.prepareResearchEvidence).mockImplementation(async (_scope, requestedCutoff) => {
      order.push('prepare');
      return { ...prepared, cutoff: requestedCutoff };
    });
    vi.mocked(portfolioApi.getResearchSnapshot).mockImplementation(async (_scope, requestedCutoff) => {
      order.push('snapshot');
      return { ...snapshot, cutoff: requestedCutoff ?? cutoff };
    });
    vi.mocked(portfolioApi.buildResearchBaseline).mockImplementation(async () => {
      order.push('baseline');
      return baseline;
    });
    const { onPlanReady } = renderPlan();

    fireEvent.click(screen.getByRole('button', { name: '生成今日计划' }));

    expect(await screen.findByText('2 项持仓，2 项已生成')).toBeInTheDocument();
    expect(order).toEqual(['prepare', 'snapshot', 'baseline']);
    const prepareCutoff = vi.mocked(portfolioApi.prepareResearchEvidence).mock.calls[0][1];
    expect(prepareCutoff).toMatch(/^\d{4}-\d{2}-\d{2}T/);
    expect(portfolioApi.prepareResearchEvidence).toHaveBeenCalledWith(prepared.scope, prepareCutoff);
    expect(portfolioApi.getResearchSnapshot).toHaveBeenCalledWith(prepared.scope, prepareCutoff);
    expect(portfolioApi.buildResearchBaseline).toHaveBeenCalledWith({
      researchSnapshotHash: snapshotHash,
      researchCutoff: prepareCutoff,
      researchScope: prepared.scope,
    });
    expect(onPlanReady).toHaveBeenLastCalledWith({
      snapshotHash,
      executionIdentityHash,
      cutoff: prepareCutoff,
      scope: prepared.scope,
    });
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
      .mockImplementationOnce(async (_scope, requestedCutoff) => ({
        ...prepared,
        cutoff: requestedCutoff,
      }));
    renderPlan();

    fireEvent.click(screen.getByRole('button', { name: '生成今日计划' }));
    expect(await screen.findByText('今日计划生成失败，请重试。')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重试' }));

    await waitFor(() => expect(portfolioApi.prepareResearchEvidence).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('2 项持仓，2 项已生成')).toBeInTheDocument();
  });

  it('fails closed when prepare returns a different cutoff', async () => {
    vi.mocked(portfolioApi.prepareResearchEvidence).mockImplementationOnce(
      async (_scope, requestedCutoff) => ({
        ...prepared,
        cutoff: new Date(new Date(requestedCutoff).getTime() + 1000).toISOString(),
      }),
    );
    renderPlan();

    fireEvent.click(screen.getByRole('button', { name: '生成今日计划' }));

    expect(await screen.findByText('今日计划生成失败，请重试。')).toBeInTheDocument();
    expect(portfolioApi.getResearchSnapshot).not.toHaveBeenCalled();
    expect(portfolioApi.buildResearchBaseline).not.toHaveBeenCalled();
  });

  it('fails closed when snapshot returns a different cutoff', async () => {
    vi.mocked(portfolioApi.prepareResearchEvidence).mockImplementationOnce(
      async (_scope, requestedCutoff) => ({ ...prepared, cutoff: requestedCutoff }),
    );
    vi.mocked(portfolioApi.getResearchSnapshot).mockImplementationOnce(
      async (_scope, requestedCutoff) => ({
        ...snapshot,
        cutoff: new Date(new Date(requestedCutoff ?? '').getTime() + 1000).toISOString(),
      }),
    );
    renderPlan();

    fireEvent.click(screen.getByRole('button', { name: '生成今日计划' }));

    expect(await screen.findByText('今日计划生成失败，请重试。')).toBeInTheDocument();
    expect(portfolioApi.buildResearchBaseline).not.toHaveBeenCalled();
  });

  it('passes the frozen binding when detailed analysis is selected', async () => {
    const { onAnalyze } = renderPlan();
    fireEvent.click(screen.getByRole('button', { name: '生成今日计划' }));

    fireEvent.click(await screen.findByRole('button', { name: '详细分析 苹果（AAPL）' }));

    const prepareCutoff = vi.mocked(portfolioApi.prepareResearchEvidence).mock.calls[0][1];

    expect(onAnalyze).toHaveBeenCalledWith(baseline.items[0], {
      snapshotHash,
      executionIdentityHash,
      cutoff: prepareCutoff,
      scope: prepared.scope,
    });
  });

  it('keeps insufficient rows isolated and disables their deep analysis', async () => {
    renderPlan();
    fireEvent.click(screen.getByRole('button', { name: '生成今日计划' }));

    const button = await screen.findByRole('button', { name: '详细分析 纳指ETF（513870）' });
    expect(button).toBeDisabled();
    expect(screen.queryByText('ETF · 513870')).not.toBeInTheDocument();
  });

  it('sends only the explicitly selected scope through prepare, snapshot, and baseline', async () => {
    const selectedScope = [prepared.scope[0]];
    vi.mocked(portfolioApi.getResearchSnapshot).mockImplementationOnce(
      async (_scope, requestedCutoff) => ({
        ...snapshot,
        cutoff: requestedCutoff ?? cutoff,
        scope: selectedScope,
        positions: [{ accountId: 1 }],
      }),
    );
    vi.mocked(portfolioApi.buildResearchBaseline).mockResolvedValueOnce({
      ...baseline,
      ledgerPositionCount: 1,
      baselineRowCount: 1,
      items: [baseline.items[0]],
    });
    renderPlan();

    fireEvent.click(screen.getByRole('checkbox', { name: 'ETF · 513870' }));
    fireEvent.click(screen.getByRole('button', { name: '生成今日计划' }));

    expect(await screen.findByText('1 项持仓，1 项已生成')).toBeInTheDocument();
    const prepareCutoff = vi.mocked(portfolioApi.prepareResearchEvidence).mock.calls[0][1];
    expect(portfolioApi.prepareResearchEvidence).toHaveBeenCalledWith(selectedScope, prepareCutoff);
    expect(portfolioApi.getResearchSnapshot).toHaveBeenCalledWith(selectedScope, prepareCutoff);
    expect(portfolioApi.buildResearchBaseline).toHaveBeenCalledWith({
      researchSnapshotHash: snapshotHash,
      researchCutoff: prepareCutoff,
      researchScope: selectedScope,
    });
  });

  it('shows a row-level reconfirmation warning when execution evidence changes', async () => {
    vi.mocked(portfolioApi.checkResearchExecution).mockResolvedValueOnce({
      schemaVersion: 'portfolio-research-execution-check-v1',
      checkedAt: '2026-07-31T06:45:00Z',
      researchSnapshotHash: snapshotHash,
      scope: prepared.scope,
      status: 'partial',
      requiresReconfirmation: true,
      items: [{
        accountId: 1,
        market: 'us',
        symbol: 'AAPL',
        name: '苹果',
        status: 'ready',
        referenceEvidence: { price: 200 },
        currentEvidence: { price: 190 },
        changedFields: ['price'],
        blockers: [],
        requiresReconfirmation: true,
      }],
    });
    renderPlan();
    fireEvent.click(screen.getByRole('button', { name: '生成今日计划' }));
    await screen.findByText('2 项持仓，2 项已生成');

    fireEvent.click(screen.getByRole('button', { name: '14:45 执行复核' }));

    const prepareCutoff = vi.mocked(portfolioApi.prepareResearchEvidence).mock.calls[0][1];

    expect(await screen.findByText('执行证据已变化，需重新确认')).toBeInTheDocument();
    expect(portfolioApi.checkResearchExecution).toHaveBeenCalledWith({
      researchSnapshotHash: snapshotHash,
      researchExecutionIdentityHash: executionIdentityHash,
      researchCutoff: prepareCutoff,
      researchScope: prepared.scope,
    });
  });

  it('isolates a failed row and reveals technical identifiers only after audit disclosure', async () => {
    const qualifiedSecond = {
      ...baseline.items[1],
      userInstruction: 'hold' as const,
      hardBlockers: [],
      exceptionReasons: [],
      evidenceStatus: 'baseline',
    };
    vi.mocked(portfolioApi.buildResearchBaseline).mockResolvedValueOnce({
      ...baseline,
      items: [baseline.items[0], qualifiedSecond],
    });
    renderPlan({
      analysisStates: {
        '1-AAPL-us': {
          status: 'insufficient',
          message: '资料不足',
          audit: {
            taskId: 'task-secret-1',
            traceId: 'trace-secret-1',
            blockers: ['quality_symbol_identity_mismatch'],
          },
        },
      },
    });
    fireEvent.click(screen.getByRole('button', { name: '生成今日计划' }));
    await screen.findByText('2 项持仓，2 项已生成');

    expect(screen.getByRole('button', { name: '详细分析 纳指ETF（513870）' })).toBeEnabled();
    expect(document.body).not.toHaveTextContent(/task-secret-1|trace-secret-1|quality_symbol_identity_mismatch|a{64}/);

    fireEvent.click(screen.getByText('审计详情'));

    expect(await screen.findByText('task: task-secret-1')).toBeInTheDocument();
    expect(screen.getByText('trace: trace-secret-1')).toBeInTheDocument();
    expect(screen.getByText('quality_symbol_identity_mismatch')).toBeInTheDocument();
    expect(screen.getByText(`snapshot: ${snapshotHash}`)).toBeInTheDocument();
  });

  it('shows the deepened action while awaiting confirmation instead of the baseline action', async () => {
    const singleScope = [prepared.scope[0]];
    vi.mocked(portfolioApi.getResearchSnapshot).mockImplementationOnce(
      async (_scope, requestedCutoff) => ({
        ...snapshot,
        cutoff: requestedCutoff ?? cutoff,
        scope: singleScope,
        positions: [{ accountId: 1 }],
      }),
    );
    vi.mocked(portfolioApi.buildResearchBaseline).mockResolvedValueOnce({
      ...baseline,
      ledgerPositionCount: 1,
      baselineRowCount: 1,
      items: [{
        ...baseline.items[0],
        positionAction: 'reduce',
        userInstruction: 'reduce',
      }],
    });
    renderPlan({
      scopeOptions: [{ accountId: 1, market: 'us', symbol: 'AAPL', label: 'Main · AAPL' }],
      onReview: vi.fn(),
      analysisStates: {
        '1-AAPL-us': {
          status: 'awaiting_confirmation',
          signalId: 77,
          userInstruction: 'hold',
        },
      } as unknown as React.ComponentProps<typeof PortfolioDailyPlan>['analysisStates'],
    });

    fireEvent.click(screen.getByRole('button', { name: '生成今日计划' }));

    expect(await screen.findByText('持有')).toBeInTheDocument();
    expect(screen.queryByText('减仓')).not.toBeInTheDocument();
  });
});
