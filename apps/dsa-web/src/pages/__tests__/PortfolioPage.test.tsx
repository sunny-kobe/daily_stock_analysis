import type React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { decisionSignalsApi } from '../../api/decisionSignals';
import { createApiError, createParsedApiError } from '../../api/error';
import { UiLanguageProvider } from '../../contexts/UiLanguageContext';
import type { DecisionSignalItem } from '../../types/decisionSignals';
import { UI_LANGUAGE_STORAGE_KEY } from '../../utils/uiLanguage';
import PortfolioPage from '../PortfolioPage';

const {
  getAccounts,
  getSnapshot,
  getRealtimeSnapshot,
  getRisk,
  getInstruments,
  getRiskPolicy,
  getResearchSnapshot,
  prepareResearchEvidence,
  buildResearchBaseline,
  createInstrument,
  updateInstrument,
  saveRiskPolicy,
  refreshFx,
  listImportBrokers,
  listTrades,
  listCashLedger,
  listCorporateActions,
  createTrade,
  deleteTrade,
  createCashLedger,
  deleteCashLedger,
  createCorporateAction,
  deleteCorporateAction,
  parseCsvImport,
  commitCsvImport,
  createAccount,
  deleteAccount,
  analyzePosition,
  getAnalysisStatus,
  listDecisionSignals,
  getLatestDecisionSignals,
  getDecisionQuality,
  putShadowFeedback,
  getQualityWeeklyReview,
} = vi.hoisted(() => ({
  getAccounts: vi.fn(),
  getSnapshot: vi.fn(),
  getRealtimeSnapshot: vi.fn(),
  getRisk: vi.fn(),
  getInstruments: vi.fn(),
  getRiskPolicy: vi.fn(),
  getResearchSnapshot: vi.fn(),
  prepareResearchEvidence: vi.fn(),
  buildResearchBaseline: vi.fn(),
  createInstrument: vi.fn(),
  updateInstrument: vi.fn(),
  saveRiskPolicy: vi.fn(),
  refreshFx: vi.fn(),
  listImportBrokers: vi.fn(),
  listTrades: vi.fn(),
  listCashLedger: vi.fn(),
  listCorporateActions: vi.fn(),
  createTrade: vi.fn(),
  deleteTrade: vi.fn(),
  createCashLedger: vi.fn(),
  deleteCashLedger: vi.fn(),
  createCorporateAction: vi.fn(),
  deleteCorporateAction: vi.fn(),
  parseCsvImport: vi.fn(),
  commitCsvImport: vi.fn(),
  createAccount: vi.fn(),
  deleteAccount: vi.fn(),
  analyzePosition: vi.fn(),
  getAnalysisStatus: vi.fn(),
  listDecisionSignals: vi.fn(),
  getLatestDecisionSignals: vi.fn(),
  getDecisionQuality: vi.fn(),
  putShadowFeedback: vi.fn(),
  getQualityWeeklyReview: vi.fn(),
}));

vi.mock('../../api/analysis', () => ({
  analysisApi: {
    getStatus: getAnalysisStatus,
  },
}));

vi.mock('../../api/decisionSignals', () => ({
  decisionSignalsApi: {
    list: listDecisionSignals,
    getLatest: getLatestDecisionSignals,
    getQuality: getDecisionQuality,
    putShadowFeedback,
    getQualityWeeklyReview,
  },
}));

vi.mock('../../api/portfolio', () => ({
  portfolioApi: {
    getAccounts,
    getSnapshot,
    getRealtimeSnapshot,
    getRisk,
    getInstruments,
    getRiskPolicy,
    getResearchSnapshot,
    prepareResearchEvidence,
    buildResearchBaseline,
    createInstrument,
    updateInstrument,
    saveRiskPolicy,
    refreshFx,
    listImportBrokers,
    listTrades,
    listCashLedger,
    listCorporateActions,
    createTrade,
    deleteTrade,
    createCashLedger,
    deleteCashLedger,
    createCorporateAction,
    deleteCorporateAction,
    parseCsvImport,
    commitCsvImport,
    createAccount,
    deleteAccount,
    analyzePosition,
  },
}));

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  PieChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Pie: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Tooltip: () => null,
  Legend: () => null,
  Cell: () => null,
}));

type AccountItem = {
  id: number;
  name: string;
  market?: 'cn' | 'hk' | 'us' | 'jp' | 'kr' | 'tw';
  baseCurrency?: string;
};

function makeAccounts(items: AccountItem[] = [{ id: 1, name: 'Main' }]) {
  return {
    accounts: items.map((item) => ({
      id: item.id,
      name: item.name,
      broker: 'Demo',
      market: item.market ?? 'us',
      baseCurrency: item.baseCurrency ?? 'CNY',
      isActive: true,
      ownerId: null,
      createdAt: '2026-03-19T00:00:00Z',
      updatedAt: '2026-03-19T00:00:00Z',
    })),
  };
}

function makeSnapshot(options: {
  accountId?: number;
  fxStale?: boolean;
  accountCount?: number;
  dataQuality?: string;
  limitations?: string[];
  positions?: Array<Record<string, unknown>>;
} = {}) {
  const accountId = options.accountId ?? 1;
  return {
    asOf: '2026-03-19',
    costMethod: 'fifo' as const,
    currency: 'CNY',
    accountCount: options.accountCount ?? 1,
    totalCash: 1000,
    totalMarketValue: 2000,
    totalEquity: 3000,
    realizedPnl: 0,
    unrealizedPnl: 0,
    feeTotal: 0,
    taxTotal: 0,
    fxStale: options.fxStale ?? true,
    dataQuality: options.dataQuality ?? 'ok',
    limitations: options.limitations ?? [],
    accounts: [
      {
        accountId,
        accountName: `Account ${accountId}`,
        ownerId: null,
        broker: 'Demo',
        market: 'us',
        baseCurrency: 'CNY',
        asOf: '2026-03-19',
        costMethod: 'fifo' as const,
        totalCash: 1000,
        totalMarketValue: 2000,
        totalEquity: 3000,
        realizedPnl: 0,
        unrealizedPnl: 0,
        feeTotal: 0,
        taxTotal: 0,
        fxStale: options.fxStale ?? true,
        positions: options.positions ?? [],
      },
    ],
  };
}

function makePosition(overrides: Record<string, unknown> = {}) {
  return {
    symbol: '600519',
    market: 'cn',
    currency: 'CNY',
    quantity: 1,
    avgCost: 1500,
    totalCost: 1500,
    lastPrice: 1600,
    marketValueBase: 1600,
    unrealizedPnlBase: 100,
    unrealizedPnlPct: 6.67,
    valuationCurrency: 'CNY',
    priceSource: 'history_close',
    priceDate: '2026-06-17',
    priceStale: false,
    priceAvailable: true,
    ...overrides,
  };
}

function makeRisk(overrides: Record<string, unknown> = {}) {
  return {
    asOf: '2026-03-19',
    accountId: null,
    costMethod: 'fifo' as const,
    currency: 'CNY',
    thresholds: {},
    concentration: {
      totalMarketValue: 0,
      topWeightPct: 0,
      alert: false,
      topPositions: [],
    },
    sectorConcentration: {
      totalMarketValue: 0,
      topWeightPct: 0,
      alert: false,
      topSectors: [],
      coverage: {},
      errors: [],
    },
    drawdown: {
      seriesPoints: 0,
      maxDrawdownPct: 0,
      currentDrawdownPct: 0,
      alert: false,
      fxStale: false,
    },
    stopLoss: {
      nearAlert: false,
      triggeredCount: 0,
      nearCount: 0,
      items: [],
    },
    decisionSignalRisk: {
      available: true,
      total: 0,
      actions: { sell: 0, reduce: 0, alert: 0 },
      items: [],
    },
    ...overrides,
  };
}

function makeDecisionSignal(overrides: Partial<DecisionSignalItem> = {}): DecisionSignalItem {
  return {
    id: 100,
    stockCode: '600519',
    stockName: '贵州茅台',
    market: 'cn',
    sourceType: 'analysis',
    sourceReportId: 1,
    traceId: null,
    marketPhase: 'intraday',
    triggerSource: 'portfolio',
    action: 'hold',
    actionLabel: null,
    confidence: 0.7,
    score: 80,
    horizon: '3d',
    entryLow: null,
    entryHigh: null,
    stopLoss: null,
    targetPrice: null,
    invalidation: null,
    watchConditions: '观察量能',
    reason: '趋势延续',
    riskSummary: '短线回撤风险',
    catalystSummary: null,
    evidence: undefined,
    dataQualitySummary: undefined,
    planQuality: 'partial',
    status: 'active',
    expiresAt: null,
    createdAt: '2026-06-17T08:00:00',
    updatedAt: '2026-06-17T08:00:00',
    metadata: undefined,
    ...overrides,
  };
}

function deferredPromise<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function waitForInitialLoad() {
  await waitFor(() => expect(getAccounts).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(getSnapshot).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(getRisk).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(listTrades).toHaveBeenCalledTimes(1));
}

describe('PortfolioPage FX refresh', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();

    getAccounts.mockResolvedValue(makeAccounts());
    getSnapshot.mockImplementation(async ({ accountId }: { accountId?: number } = {}) => makeSnapshot({ accountId, fxStale: true }));
    getRealtimeSnapshot.mockImplementation(() => new Promise(() => {}));
    getRisk.mockResolvedValue(makeRisk());
    getInstruments.mockResolvedValue({ items: [] });
    getRiskPolicy.mockResolvedValue({ policy: null });
    getResearchSnapshot.mockImplementation(async (_scope, cutoff) => ({
      snapshotHash: 'snapshot-1',
      cutoff,
      scope: [{ accountId: 1, market: 'hk', symbol: 'HK00700' }],
      scopeHash: 'scope-1',
      completeness: 'INSUFFICIENT_EVIDENCE',
      positions: [],
      instruments: [],
      pointInTime: {
        scope: 'current_prospective',
        prospectiveDecisionEligible: false,
        historicalReplayEligible: false,
        sourceCutoffs: {
          accounts: '2026-07-30T01:00:00Z',
          positionCache: '2026-07-30T01:00:00Z',
          dailySnapshots: '2026-07-30T01:00:00Z',
          instrumentRegistry: '2026-07-30T01:00:00Z',
          riskPolicy: '2026-07-30T01:00:00Z',
          decisionSignals: null,
        },
        blockers: ['position_cache_after_cutoff'],
      },
      decisionSignals: [],
      hardBlockers: [{ code: 'instrument_identity_missing', scope: 'instrument', symbol: '600519', market: 'cn' }],
      limitations: ['cached_portfolio_state_only'],
    }));
    prepareResearchEvidence.mockImplementation(async (_scope, cutoff) => ({
      schemaVersion: 'portfolio-research-evidence-prepare-v2',
      scope: [{ accountId: 1, market: 'hk', symbol: 'HK00700' }],
      preparedAt: '2026-07-31T08:00:00Z',
      cutoff,
      asOf: '2026-07-31',
      status: 'ready',
      positionCount: 1,
      readyCount: 1,
      insufficientCount: 0,
      items: [],
    }));
    buildResearchBaseline.mockResolvedValue({
      schemaVersion: 'portfolio-research-baseline-v1',
      snapshotHash: 'snapshot-1',
      cutoff: '2026-07-22T09:00:00',
      marketDataCutoff: '2026-07-22T09:00:00',
      ledgerPositionCount: 1,
      baselineRowCount: 1,
      coverageReconciled: true,
      portfolioRiskFlags: [],
      items: [{
        accountId: 1,
        market: 'hk',
        symbol: 'HK00700',
        name: '腾讯控股',
        displayLabel: '腾讯控股（HK00700）',
        selectionKey: 'hk:HK00700',
        instrumentType: 'equity',
        quote: {},
        history: {},
        positionAction: 'hold',
        incrementalAction: 'wait',
        userInstruction: 'hold',
        hardBlockers: [],
        riskFlags: [],
        exceptionReasons: [],
        evidenceStatus: 'baseline',
        researchLevel: 'baseline',
        detailRecommended: false,
        sizingAllowed: false,
      }],
      suggestedDeepAnalysis: [],
      deepAnalysisStarted: false,
    });
    createInstrument.mockImplementation(async (payload) => ({ id: 1, ...payload }));
    updateInstrument.mockImplementation(async (_market, _symbol, payload) => ({ id: 1, symbol: '600519', market: 'cn', ...payload }));
    saveRiskPolicy.mockImplementation(async (payload) => ({ id: 1, ...payload }));
    refreshFx.mockResolvedValue({
      asOf: '2026-03-19',
      accountCount: 1,
      refreshEnabled: true,
      disabledReason: null,
      pairCount: 1,
      updatedCount: 1,
      staleCount: 0,
      errorCount: 0,
    });
    listImportBrokers.mockResolvedValue({
      brokers: [{ broker: 'huatai', aliases: [], displayName: '华泰' }],
    });
    listTrades.mockResolvedValue({ items: [], total: 0, page: 1, pageSize: 20 });
    listCashLedger.mockResolvedValue({ items: [], total: 0, page: 1, pageSize: 20 });
    listCorporateActions.mockResolvedValue({ items: [], total: 0, page: 1, pageSize: 20 });
    createTrade.mockResolvedValue({ id: 1 });
    deleteTrade.mockResolvedValue({ deleted: 1 });
    createCashLedger.mockResolvedValue({ id: 1 });
    deleteCashLedger.mockResolvedValue({ deleted: 1 });
    createCorporateAction.mockResolvedValue({ id: 1 });
    deleteCorporateAction.mockResolvedValue({ deleted: 1 });
    parseCsvImport.mockResolvedValue({ broker: 'huatai', recordCount: 0, skippedCount: 0, errorCount: 0, records: [], errors: [] });
    commitCsvImport.mockResolvedValue({
      accountId: 1,
      recordCount: 0,
      insertedCount: 0,
      duplicateCount: 0,
      failedCount: 0,
      dryRun: true,
      errors: [],
    });
    createAccount.mockResolvedValue({ id: 1 });
    deleteAccount.mockResolvedValue({ deleted: 1 });
    analyzePosition.mockResolvedValue({
      taskId: 'task-portfolio-1',
      traceId: 'task-portfolio-1',
      status: 'pending',
      message: '分析任务已加入队列: HK00700',
      analysisPhase: 'auto',
    });
    getAnalysisStatus.mockResolvedValue({
      taskId: 'task-portfolio-1',
      traceId: 'task-portfolio-1',
      status: 'completed',
    });
    getLatestDecisionSignals.mockResolvedValue({ items: [], total: 0, page: 1, pageSize: 1 });
  });

  function renderEnglishPage() {
    window.localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'en');
    render(
      <UiLanguageProvider>
        <PortfolioPage />
      </UiLanguageProvider>,
    );
  }

  it('uses fast portfolio valuation for page snapshot and risk loads', async () => {
    render(<PortfolioPage />);

    await waitForInitialLoad();

    expect(getSnapshot).toHaveBeenCalledWith({ accountId: undefined, costMethod: 'fifo', includeRealtime: false });
    expect(getRealtimeSnapshot).toHaveBeenCalledWith({ accountId: undefined, costMethod: 'fifo', includeRealtime: true });
    expect(getRisk).toHaveBeenCalledWith({ accountId: undefined, costMethod: 'fifo', includeRealtime: false });
  });

  it('shows registry verification and frozen blocker diagnostics', async () => {
    getInstruments.mockResolvedValueOnce({
      items: [{
        id: 1,
        symbol: '600519',
        market: 'cn',
        quoteCurrency: 'CNY',
        instrumentType: 'equity',
        tradeLotSize: 100,
        dailyReset: false,
        requiresPremiumCheck: false,
        verificationStatus: 'verified',
        metadata: {},
      }],
    });

    render(<PortfolioPage />);
    await waitForInitialLoad();

    expect(await screen.findByText('600519 · cn')).toBeInTheDocument();
    expect(screen.getByText('equity / verified')).toBeInTheDocument();
    expect(screen.getByText('instrument_identity_missing')).toBeInTheDocument();
  });

  it('renders the portfolio control plane in English UI mode', async () => {
    renderEnglishPage();
    await waitForInitialLoad();

    expect(await screen.findByText('Instrument identity and product structure')).toBeInTheDocument();
    expect(screen.getByLabelText('Product type')).toBeInTheDocument();
    expect(screen.getByText('Portfolio risk budget')).toBeInTheDocument();
    expect(screen.getByText('Frozen snapshot blockers')).toBeInTheDocument();
    expect(screen.getByText('current_prospective')).toBeInTheDocument();
    expect(screen.getByText('Prospective not ready')).toBeInTheDocument();
    expect(screen.getByText('Historical replay unavailable')).toBeInTheDocument();
    expect(screen.getByText('positionCache')).toBeInTheDocument();
    expect(screen.getAllByText('2026-07-30T01:00:00Z')).toHaveLength(5);
    expect(screen.getByText('position_cache_after_cutoff')).toBeInTheDocument();
    expect(screen.queryByText('标的身份与产品结构')).not.toBeInTheDocument();
  });

  it('round-trips instrument evidence time with an explicit timezone', async () => {
    const evidenceTimestamp = '2026-07-22T09:00:00+08:00';
    const evidenceDate = new Date(evidenceTimestamp);
    const pad = (value: number) => String(value).padStart(2, '0');
    const expectedLocalValue = [
      evidenceDate.getFullYear(),
      '-',
      pad(evidenceDate.getMonth() + 1),
      '-',
      pad(evidenceDate.getDate()),
      'T',
      pad(evidenceDate.getHours()),
      ':',
      pad(evidenceDate.getMinutes()),
    ].join('');
    getInstruments.mockResolvedValueOnce({
      items: [{
        id: 1,
        symbol: 'AAPL',
        market: 'us',
        quoteCurrency: 'USD',
        instrumentType: 'equity',
        tradeLotSize: 1,
        dailyReset: false,
        requiresPremiumCheck: false,
        verificationStatus: 'verified',
        evidenceSource: 'NASDAQ symbol directory',
        evidenceAsOf: evidenceTimestamp,
        metadata: {},
      }],
    });

    render(<PortfolioPage />);
    await waitForInitialLoad();
    fireEvent.click(await screen.findByRole('button', { name: /AAPL/ }));

    const evidenceInput = screen.getByLabelText('证据时间');
    expect(evidenceInput).toHaveValue(expectedLocalValue);
    fireEvent.click(screen.getByRole('button', { name: '更新身份' }));

    await waitFor(() => expect(updateInstrument).toHaveBeenCalled());
    expect(updateInstrument.mock.calls[0][2].evidenceAsOf).toBe(
      new Date(expectedLocalValue).toISOString(),
    );
    expect(updateInstrument.mock.calls[0][2]).not.toHaveProperty('metadata');
  });

  it('shows product fields conditionally and saves the singleton risk budget', async () => {
    render(<PortfolioPage />);
    await waitForInitialLoad();

    fireEvent.change(await screen.findByLabelText('产品类型'), {
      target: { value: 'daily_leveraged_product' },
    });
    expect(screen.getByLabelText('底层标的')).toBeInTheDocument();
    expect(screen.getByLabelText('杠杆倍数')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('最低现金缓冲'), { target: { value: '12' } });
    fireEvent.change(screen.getByLabelText('单一持仓上限'), { target: { value: '28' } });
    fireEvent.change(screen.getByLabelText('行业上限'), { target: { value: '45' } });
    fireEvent.change(screen.getByLabelText('高风险产品上限'), { target: { value: '6' } });
    fireEvent.change(screen.getByLabelText('组合回撤上限'), { target: { value: '16' } });
    fireEvent.click(screen.getByRole('button', { name: '保存风险预算' }));

    await waitFor(() => expect(saveRiskPolicy).toHaveBeenCalledWith({
      minCashBufferPct: 12,
      maxSinglePositionPct: 28,
      maxSectorPct: 45,
      maxHighRiskProductPct: 6,
      maxPortfolioDrawdownPct: 16,
    }));
  });

  it('keeps the offline snapshot when the background realtime refresh fails', async () => {
    getSnapshot.mockResolvedValueOnce(makeSnapshot({
      positions: [makePosition({ symbol: 'AAOI', lastPrice: 41.25 })],
    }));
    getRealtimeSnapshot.mockRejectedValueOnce(
      createApiError(
        createParsedApiError({
          title: '实时行情超时',
          message: '部分现价暂未更新',
          category: 'upstream_timeout',
        }),
      ),
    );

    render(<PortfolioPage />);

    await waitForInitialLoad();

    expect(await screen.findByText('41.2500')).toBeInTheDocument();
    const warningTitle = await screen.findByText('实时行情刷新受限');
    expect(warningTitle.closest('[role="alert"]')).toHaveTextContent('部分现价暂未更新');
    expect(screen.queryByText('实时行情超时')).not.toBeInTheDocument();
  });

  it('replaces the offline price when the background realtime refresh succeeds', async () => {
    const realtimeSnapshot = deferredPromise<ReturnType<typeof makeSnapshot>>();
    getSnapshot.mockResolvedValueOnce(makeSnapshot({
      positions: [makePosition({ symbol: 'AAOI', lastPrice: 41.25 })],
    }));
    getRealtimeSnapshot.mockImplementationOnce(() => realtimeSnapshot.promise);

    render(<PortfolioPage />);

    await waitForInitialLoad();
    expect(await screen.findByText('41.2500')).toBeInTheDocument();

    await act(async () => {
      realtimeSnapshot.resolve(makeSnapshot({
        positions: [makePosition({
          symbol: 'AAOI',
          lastPrice: 42.5,
          priceSource: 'realtime_quote',
          priceProvider: 'YFinance',
        })],
      }));
      await realtimeSnapshot.promise;
    });

    expect(await screen.findByText('42.5000')).toBeInTheDocument();
    expect(screen.getByText('实时价 · YFinance')).toBeInTheDocument();
  });

  it('renders stale FX status with a manual refresh button', async () => {
    render(<PortfolioPage />);

    await waitForInitialLoad();

    expect(await screen.findByText('过期')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '刷新汇率' })).toBeInTheDocument();
  });

  it('shows aggregate partial valuation limitations near summary totals', async () => {
    getSnapshot.mockResolvedValueOnce(makeSnapshot({
      dataQuality: 'partial',
      limitations: ['realtime_quote_best_effort', 'fx_and_cost_basis_partial'],
    }));

    render(<PortfolioPage />);

    await waitForInitialLoad();

    expect(await screen.findByText('组合估值限制')).toBeInTheDocument();
    expect(screen.getByText(/实时行情为尽力获取/)).toBeInTheDocument();
    expect(screen.getByText(/汇率与成本基础为部分口径/)).toBeInTheDocument();
  });

  it('renders portfolio risk drawdown labels in English UI mode', async () => {
    renderEnglishPage();

    await waitForInitialLoad();

    expect(await screen.findByText('Portfolio management')).toBeInTheDocument();
    expect(screen.getByText('Drawdown monitor')).toBeInTheDocument();
    expect(screen.getByText(/Max drawdown:/)).toBeInTheDocument();
    expect(screen.getByText(/Current drawdown:/)).toBeInTheDocument();
    expect(screen.getByText('Stop-loss proximity warning')).toBeInTheDocument();
    expect(screen.getByText('Scope')).toBeInTheDocument();
    expect(screen.getByText('AI risk signals')).toBeInTheDocument();
    expect(screen.getByText('No defensive signals')).toBeInTheDocument();
    expect(screen.queryByText('回撤监控')).not.toBeInTheDocument();
  });

  it('renders portfolio decision signal risk summary', async () => {
    getRisk.mockResolvedValueOnce(makeRisk({
      decisionSignalRisk: {
        available: true,
        total: 2,
        actions: { sell: 1, reduce: 0, alert: 1 },
        items: [
          {
            accountId: 1,
            symbol: '600519',
            market: 'cn',
            signal: makeDecisionSignal({ id: 201, action: 'sell', actionLabel: null }),
          },
          {
            accountId: 1,
            symbol: '300750',
            market: 'cn',
            signal: makeDecisionSignal({ id: 202, stockCode: '300750', action: 'alert', actionLabel: null }),
          },
        ],
      },
    }));

    render(<PortfolioPage />);

    await waitForInitialLoad();

    expect(screen.getByText('AI 风险信号')).toBeInTheDocument();
    expect(screen.getByText(/风险信号: 2/)).toBeInTheDocument();
    expect(screen.getByText(/卖出: 1 · 减仓: 0 · 预警: 1/)).toBeInTheDocument();
    expect(screen.getByText('600519 · 卖出')).toBeInTheDocument();
    expect(screen.getByText('300750 · 预警')).toBeInTheDocument();
    expect(screen.queryByText('600519 · sell')).not.toBeInTheDocument();
    expect(screen.queryByText('300750 · alert')).not.toBeInTheDocument();
  });

  it('uses the current UI language for portfolio decision signal risk action labels', async () => {
    getRisk.mockResolvedValueOnce(makeRisk({
      decisionSignalRisk: {
        available: true,
        total: 1,
        actions: { sell: 1, reduce: 0, alert: 0 },
        items: [
          {
            accountId: 1,
            symbol: '600519',
            market: 'cn',
            signal: makeDecisionSignal({ id: 203, action: 'sell', actionLabel: '卖出' }),
          },
        ],
      },
    }));

    renderEnglishPage();

    await waitForInitialLoad();

    expect(screen.getByText('AI risk signals')).toBeInTheDocument();
    expect(screen.getByText('600519 · Sell')).toBeInTheDocument();
    expect(screen.queryByText('600519 · 卖出')).not.toBeInTheDocument();
    expect(screen.queryByText('600519 · sell')).not.toBeInTheDocument();
  });

  it('renders portfolio decision signal risk fail-open state', async () => {
    getRisk.mockResolvedValueOnce(makeRisk({
      decisionSignalRisk: {
        available: false,
        total: 0,
        actions: { sell: 0, reduce: 0, alert: 0 },
        items: [],
      },
    }));

    render(<PortfolioPage />);

    await waitForInitialLoad();

    expect(screen.getByText('信号风险暂不可用')).toBeInTheDocument();
  });

  it('refreshes FX for a single selected account and only reloads snapshot/risk', async () => {
    getSnapshot
      .mockResolvedValueOnce(makeSnapshot({ fxStale: true }))
      .mockResolvedValueOnce(makeSnapshot({ accountId: 1, fxStale: true }))
      .mockResolvedValueOnce(makeSnapshot({ accountId: 1, fxStale: false }));

    render(<PortfolioPage />);

    await waitForInitialLoad();

    const accountSelect = screen.getAllByRole('combobox')[0];
    fireEvent.change(accountSelect, { target: { value: '1' } });

    await waitFor(() => {
      expect(getSnapshot).toHaveBeenLastCalledWith({ accountId: 1, costMethod: 'fifo', includeRealtime: false });
    });

    const snapshotCallsBeforeRefresh = getSnapshot.mock.calls.length;
    const riskCallsBeforeRefresh = getRisk.mock.calls.length;
    const tradeCallsBeforeRefresh = listTrades.mock.calls.length;

    fireEvent.click(screen.getByRole('button', { name: '刷新汇率' }));

    await waitFor(() => expect(refreshFx).toHaveBeenCalledWith({ accountId: 1 }));
    expect(await screen.findByText('汇率已刷新，共更新 1 对。')).toBeInTheDocument();
    await waitFor(() => expect(getSnapshot).toHaveBeenCalledTimes(snapshotCallsBeforeRefresh + 1));
    await waitFor(() => expect(getRisk).toHaveBeenCalledTimes(riskCallsBeforeRefresh + 1));
    expect(listTrades).toHaveBeenCalledTimes(tradeCallsBeforeRefresh);
    expect(listCashLedger).not.toHaveBeenCalled();
    expect(listCorporateActions).not.toHaveBeenCalled();
    expect(screen.getByText('最新')).toBeInTheDocument();
  });

  it('refreshes FX for the full portfolio without sending accountId and shows neutral feedback when no pair exists', async () => {
    refreshFx.mockResolvedValueOnce({
      asOf: '2026-03-19',
      accountCount: 1,
      refreshEnabled: true,
      disabledReason: null,
      pairCount: 0,
      updatedCount: 0,
      staleCount: 0,
      errorCount: 0,
    });

    render(<PortfolioPage />);

    await waitForInitialLoad();

    fireEvent.click(screen.getByRole('button', { name: '刷新汇率' }));

    await waitFor(() => expect(refreshFx).toHaveBeenCalledWith({ accountId: undefined }));
    expect(await screen.findByText('当前范围无可刷新的汇率对。')).toBeInTheDocument();
  });

  it('shows disabled feedback when FX online refresh is disabled even without a disabled reason', async () => {
    refreshFx.mockResolvedValueOnce({
      asOf: '2026-03-19',
      accountCount: 1,
      refreshEnabled: false,
      pairCount: 1,
      updatedCount: 0,
      staleCount: 0,
      errorCount: 0,
    });

    render(<PortfolioPage />);

    await waitForInitialLoad();

    fireEvent.click(screen.getByRole('button', { name: '刷新汇率' }));

    expect(await screen.findByText('汇率在线刷新已被禁用。')).toBeInTheDocument();
  });

  it('renders backend-provided position valuation fields and stale missing-price hint', async () => {
    getSnapshot.mockResolvedValueOnce(makeSnapshot({ fxStale: true, positions: [
      { symbol: 'HK00700', market: 'hk', currency: 'HKD', quantity: 10, avgCost: 400, totalCost: 4000, lastPrice: 420, marketValueBase: 4200, unrealizedPnlBase: 200, unrealizedPnlPct: 5, valuationCurrency: 'HKD', priceSource: 'history_close', priceDate: '2026-03-18', priceStale: true, priceAvailable: true },
      { symbol: 'AAPL', market: 'us', currency: 'USD', quantity: 5, avgCost: 100, totalCost: 500, lastPrice: 0, marketValueBase: 0, unrealizedPnlBase: 0, unrealizedPnlPct: null, valuationCurrency: 'USD', priceSource: 'missing', priceDate: null, priceStale: true, priceAvailable: false },
    ] }));

    render(<PortfolioPage />);

    await waitForInitialLoad();

    expect(await screen.findByText('HK00700')).toBeInTheDocument();
    expect(screen.getByText('420.0000')).toBeInTheDocument();
    expect(screen.getByText('HKD 4,200.00')).toBeInTheDocument();
    expect(screen.getByText('+5.00%')).toBeInTheDocument();
    expect(screen.getByText('收盘价 · 2026-03-18')).toBeInTheDocument();
    expect(screen.getByText('缺价')).toBeInTheDocument();
    expect(screen.getAllByText('--').length).toBeGreaterThanOrEqual(2);

    const hkRow = screen.getByText('HK00700').closest('tr');
    const aaplRow = screen.getByText('AAPL').closest('tr');
    expect(hkRow).not.toBeNull();
    expect(aaplRow).not.toBeNull();

    const hkRowCells = within(hkRow as HTMLTableRowElement).getAllByRole('cell');
    const aaplRowCells = within(aaplRow as HTMLTableRowElement).getAllByRole('cell');
    expect(hkRowCells.at(-3)).toHaveClass('text-success');
    expect(aaplRowCells.at(-3)).toHaveClass('text-secondary');
  });

  it('loads latest active signals for holdings without scanning paginated signal lists', async () => {
    getSnapshot.mockResolvedValueOnce(makeSnapshot({ positions: [
      { symbol: '600519', market: 'cn', currency: 'CNY', quantity: 1, avgCost: 1500, totalCost: 1500, lastPrice: 1600, marketValueBase: 1600, unrealizedPnlBase: 100, unrealizedPnlPct: 6.67, valuationCurrency: 'CNY', priceSource: 'history_close', priceDate: '2026-06-17', priceStale: false, priceAvailable: true },
    ] }));
    const latestSignal = makeDecisionSignal({
      id: 101,
      stockCode: '600519',
      riskSummary: '分页后的风险摘要',
      watchConditions: '分页后的观察条件',
      metadata: { quality_context_signal_id: 77 },
    });
    getLatestDecisionSignals.mockResolvedValueOnce({ items: [latestSignal], total: 1, page: 1, pageSize: 1 });

    render(<PortfolioPage />);

    expect(await screen.findByText('600519')).toBeInTheDocument();
    expect(await screen.findByText('分页后的风险摘要')).toBeInTheDocument();
    expect(decisionSignalsApi.getLatest).toHaveBeenCalledWith('600519', {
      market: 'cn',
      limit: 1,
    });
    expect(decisionSignalsApi.list).not.toHaveBeenCalled();
    getDecisionQuality.mockResolvedValueOnce({
      context: {
        signalId: 77,
        positionAction: 'hold',
        incrementalAction: 'wait',
        benchmark: { code: '000300' },
        unableReasons: [],
      },
      outcomes: [],
      attributions: [],
    });
    getQualityWeeklyReview.mockResolvedValueOnce({
      materialDecisionCount: 0,
      decisions: [],
      aiHumanDisagreements: [],
      confirmedAttributionCounts: {},
      candidatePatterns: [],
      automaticRulesActivated: false,
    });
    fireEvent.click(screen.getByRole('button', { name: '复盘' }));
    expect(await screen.findByText('持仓决策复盘')).toBeInTheDocument();
    expect(getDecisionQuality).toHaveBeenCalledWith(77);
  });

  it('shows stock names above codes using registry names before signal fallbacks', async () => {
    getSnapshot.mockResolvedValueOnce(makeSnapshot({ positions: [
      makePosition({ symbol: 'AAOI', market: 'us' }),
      makePosition({ symbol: 'TSLA', market: 'us' }),
    ] }));
    getInstruments.mockResolvedValueOnce({
      items: [{
        id: 1,
        symbol: 'AAOI',
        market: 'us',
        quoteCurrency: 'USD',
        instrumentType: 'equity',
        tradeLotSize: 1,
        dailyReset: false,
        requiresPremiumCheck: false,
        verificationStatus: 'verified',
        metadata: { name: 'Applied Optoelectronics' },
      }],
    });
    getLatestDecisionSignals.mockImplementation(async (stockCode: string) => ({
      items: [makeDecisionSignal({
        stockCode,
        stockName: stockCode === 'AAOI' ? 'Signal AAOI Name' : 'Tesla, Inc.',
        market: 'us',
      })],
      total: 1,
      page: 1,
      pageSize: 1,
    }));

    render(<PortfolioPage />);

    const stockHeader = await screen.findByRole('columnheader', { name: '股票' });
    const positionsTable = stockHeader.closest('table');
    expect(positionsTable).not.toBeNull();
    const aaoiRow = within(positionsTable as HTMLTableElement).getByText('AAOI').closest('tr');
    const tslaRow = within(positionsTable as HTMLTableElement).getByText('TSLA').closest('tr');
    expect(aaoiRow).not.toBeNull();
    expect(tslaRow).not.toBeNull();
    expect(within(aaoiRow as HTMLTableRowElement).getByText('Applied Optoelectronics')).toBeInTheDocument();
    expect(within(aaoiRow as HTMLTableRowElement).queryByText('Signal AAOI Name')).not.toBeInTheDocument();
    expect(await within(tslaRow as HTMLTableRowElement).findByText('Tesla, Inc.')).toBeInTheDocument();
  });

  it('localizes the holding decision review entry in English', async () => {
    getSnapshot.mockResolvedValueOnce(makeSnapshot({ positions: [
      { symbol: 'AAPL', market: 'us', currency: 'USD', quantity: 1, avgCost: 200, totalCost: 200, lastPrice: 210, marketValueBase: 210, unrealizedPnlBase: 10, unrealizedPnlPct: 5, valuationCurrency: 'USD', priceSource: 'history_close', priceDate: '2026-06-17', priceStale: false, priceAvailable: true },
    ] }));
    getLatestDecisionSignals.mockResolvedValueOnce({
      items: [makeDecisionSignal({ id: 102, stockCode: 'AAPL', market: 'us' })],
      total: 1,
      page: 1,
      pageSize: 1,
    });

    renderEnglishPage();

    expect(await screen.findByRole('button', { name: 'Review' })).toBeInTheDocument();
  });

  it('refreshes holding signals when manually refreshing unchanged portfolio data', async () => {
    const position = { symbol: '600519', market: 'cn', currency: 'CNY', quantity: 1, avgCost: 1500, totalCost: 1500, lastPrice: 1600, marketValueBase: 1600, unrealizedPnlBase: 100, unrealizedPnlPct: 6.67, valuationCurrency: 'CNY', priceSource: 'history_close', priceDate: '2026-06-17', priceStale: false, priceAvailable: true };
    getSnapshot.mockResolvedValue(makeSnapshot({ positions: [position] }));
    getLatestDecisionSignals
      .mockResolvedValueOnce({
        items: [makeDecisionSignal({ stockCode: '600519', riskSummary: '旧 AI 风险' })],
        total: 1,
        page: 1,
        pageSize: 1,
      })
      .mockResolvedValueOnce({
        items: [makeDecisionSignal({ stockCode: '600519', riskSummary: '新 AI 风险' })],
        total: 1,
        page: 1,
        pageSize: 1,
      });

    render(<PortfolioPage />);

    expect(await screen.findByText('旧 AI 风险')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '刷新数据' }));

    expect(await screen.findByText('新 AI 风险')).toBeInTheDocument();
    await waitFor(() => expect(getLatestDecisionSignals).toHaveBeenCalledTimes(2));
    expect(screen.queryByText('旧 AI 风险')).not.toBeInTheDocument();
  });

  it('waits for the selected-account snapshot before loading account-scoped holding signals', async () => {
    getAccounts.mockResolvedValueOnce(makeAccounts([
      { id: 1, name: 'Main' },
      { id: 2, name: 'Alt' },
    ]));
    const accountTwoSnapshot = deferredPromise<ReturnType<typeof makeSnapshot>>();
    getSnapshot
      .mockResolvedValueOnce(makeSnapshot({
        accountCount: 2,
        positions: [
          { symbol: '600519', market: 'cn', currency: 'CNY', quantity: 1, avgCost: 1500, totalCost: 1500, lastPrice: 1600, marketValueBase: 1600, unrealizedPnlBase: 100, unrealizedPnlPct: 6.67, valuationCurrency: 'CNY', priceSource: 'history_close', priceDate: '2026-06-17', priceStale: false, priceAvailable: true },
        ],
      }))
      .mockReturnValueOnce(accountTwoSnapshot.promise);
    getLatestDecisionSignals.mockResolvedValue({
      items: [makeDecisionSignal({ stockCode: '600519', riskSummary: '账号信号' })],
      total: 1,
      page: 1,
      pageSize: 1,
    });

    render(<PortfolioPage />);

    expect(await screen.findByText('账号信号')).toBeInTheDocument();
    const signalCallsBeforeSwitch = getLatestDecisionSignals.mock.calls.length;

    const accountSelect = screen.getAllByRole('combobox')[0];
    fireEvent.change(accountSelect, { target: { value: '2' } });

    await waitFor(() => {
      expect(getSnapshot).toHaveBeenLastCalledWith({ accountId: 2, costMethod: 'fifo', includeRealtime: false });
    });
    expect(screen.queryByText('账号信号')).not.toBeInTheDocument();
    expect(getLatestDecisionSignals).toHaveBeenCalledTimes(signalCallsBeforeSwitch);

    await act(async () => {
      accountTwoSnapshot.resolve(makeSnapshot({
        accountId: 2,
        positions: [
          { symbol: '600519', market: 'cn', currency: 'CNY', quantity: 1, avgCost: 1500, totalCost: 1500, lastPrice: 1600, marketValueBase: 1600, unrealizedPnlBase: 100, unrealizedPnlPct: 6.67, valuationCurrency: 'CNY', priceSource: 'history_close', priceDate: '2026-06-17', priceStale: false, priceAvailable: true },
        ],
      }));
      await accountTwoSnapshot.promise;
    });

    await waitFor(() => {
      expect(getLatestDecisionSignals).toHaveBeenLastCalledWith('600519', {
        market: 'cn',
        limit: 1,
      });
    });
  });

  it('drops late holding-signal responses after switching account scope', async () => {
    getAccounts.mockResolvedValueOnce(makeAccounts([
      { id: 1, name: 'Main' },
      { id: 2, name: 'Alt' },
    ]));
    getSnapshot
      .mockResolvedValueOnce(makeSnapshot({
        accountCount: 2,
        positions: [
          { symbol: '600519', market: 'cn', currency: 'CNY', quantity: 1, avgCost: 1500, totalCost: 1500, lastPrice: 1600, marketValueBase: 1600, unrealizedPnlBase: 100, unrealizedPnlPct: 6.67, valuationCurrency: 'CNY', priceSource: 'history_close', priceDate: '2026-06-17', priceStale: false, priceAvailable: true },
        ],
      }))
      .mockResolvedValueOnce(makeSnapshot({
        accountId: 2,
        positions: [
          { symbol: '600519', market: 'cn', currency: 'CNY', quantity: 1, avgCost: 1500, totalCost: 1500, lastPrice: 1600, marketValueBase: 1600, unrealizedPnlBase: 100, unrealizedPnlPct: 6.67, valuationCurrency: 'CNY', priceSource: 'history_close', priceDate: '2026-06-17', priceStale: false, priceAvailable: true },
        ],
      }));
    const oldSignals = deferredPromise<{
      items: DecisionSignalItem[];
      total: number;
      page: number;
      pageSize: number;
    }>();
    getLatestDecisionSignals
      .mockReturnValueOnce(oldSignals.promise)
      .mockResolvedValueOnce({
        items: [makeDecisionSignal({ stockCode: '600519', riskSummary: '新账号信号' })],
        total: 1,
        page: 1,
        pageSize: 1,
      });

    render(<PortfolioPage />);

    expect(await screen.findByText('600519')).toBeInTheDocument();

    const accountSelect = screen.getAllByRole('combobox')[0];
    fireEvent.change(accountSelect, { target: { value: '2' } });

    expect(await screen.findByText('新账号信号')).toBeInTheDocument();

    await act(async () => {
      oldSignals.resolve({
        items: [makeDecisionSignal({ stockCode: '600519', riskSummary: '旧账号晚返回信号' })],
        total: 1,
        page: 1,
        pageSize: 1,
      });
      await oldSignals.promise;
    });

    expect(screen.getByText('新账号信号')).toBeInTheDocument();
    expect(screen.queryByText('旧账号晚返回信号')).not.toBeInTheDocument();
  });

  it('matches holding signals by stock-code equivalence and leaves unmatched rows empty', async () => {
    getSnapshot.mockResolvedValueOnce(makeSnapshot({ positions: [
      { symbol: '600519', market: 'cn', currency: 'CNY', quantity: 1, avgCost: 1500, totalCost: 1500, lastPrice: 1600, marketValueBase: 1600, unrealizedPnlBase: 100, unrealizedPnlPct: 6.67, valuationCurrency: 'CNY', priceSource: 'history_close', priceDate: '2026-06-17', priceStale: false, priceAvailable: true },
      { symbol: 'SH600519', market: 'cn', currency: 'CNY', quantity: 1, avgCost: 1500, totalCost: 1500, lastPrice: 1600, marketValueBase: 1600, unrealizedPnlBase: 100, unrealizedPnlPct: 6.67, valuationCurrency: 'CNY', priceSource: 'history_close', priceDate: '2026-06-17', priceStale: false, priceAvailable: true },
      { symbol: '00700.HK', market: 'hk', currency: 'HKD', quantity: 10, avgCost: 400, totalCost: 4000, lastPrice: 420, marketValueBase: 4200, unrealizedPnlBase: 200, unrealizedPnlPct: 5, valuationCurrency: 'HKD', priceSource: 'history_close', priceDate: '2026-06-17', priceStale: false, priceAvailable: true },
      { symbol: 'AAPL', market: 'us', currency: 'USD', quantity: 2, avgCost: 180, totalCost: 360, lastPrice: 190, marketValueBase: 380, unrealizedPnlBase: 20, unrealizedPnlPct: 5.56, valuationCurrency: 'USD', priceSource: 'history_close', priceDate: '2026-06-17', priceStale: false, priceAvailable: true },
    ] }));
    getLatestDecisionSignals.mockImplementation(async (stockCode: string) => {
      if (stockCode.includes('600519')) {
        return {
          items: [makeDecisionSignal({
            id: 1,
            stockCode: '600519',
            market: 'cn',
            riskSummary: 'A 股风险',
            action: 'alert',
            metadata: { portfolioGate: { rawAction: 'buy', finalAction: 'alert' } },
          })],
          total: 1,
          page: 1,
          pageSize: 1,
        };
      }
      if (stockCode.includes('00700')) {
        return {
          items: [makeDecisionSignal({ id: 2, stockCode: 'HK00700', market: 'hk', riskSummary: '港股风险', watchConditions: '观察回购' })],
          total: 1,
          page: 1,
          pageSize: 1,
        };
      }
      return { items: [], total: 0, page: 1, pageSize: 1 };
    });

    render(<PortfolioPage />);

    expect(await screen.findAllByText('A 股风险')).toHaveLength(2);
    expect(await screen.findAllByText('buy → alert')).toHaveLength(2);
    expect(screen.getByText('港股风险')).toBeInTheDocument();
    const latestLookupSymbols = getLatestDecisionSignals.mock.calls.map(([stockCode]) => String(stockCode));
    expect(latestLookupSymbols.filter((stockCode) => stockCode.includes('600519'))).toEqual(['600519']);
    expect(getLatestDecisionSignals).toHaveBeenCalledTimes(3);
    expect(getLatestDecisionSignals).toHaveBeenCalledWith('00700.HK', {
      market: 'hk',
      limit: 1,
    });
    const aaplRow = screen.getByText('AAPL').closest('tr');
    expect(aaplRow).not.toBeNull();
    expect(within(aaplRow as HTMLTableRowElement).getByText('—')).toBeInTheDocument();
  });

  it('shows a visible partial warning when one latest holding signal lookup fails', async () => {
    getSnapshot.mockResolvedValueOnce(makeSnapshot({ positions: [
      { symbol: '600519', market: 'cn', currency: 'CNY', quantity: 1, avgCost: 1500, totalCost: 1500, lastPrice: 1600, marketValueBase: 1600, unrealizedPnlBase: 100, unrealizedPnlPct: 6.67, valuationCurrency: 'CNY', priceSource: 'history_close', priceDate: '2026-06-17', priceStale: false, priceAvailable: true },
      { symbol: 'AAPL', market: 'us', currency: 'USD', quantity: 2, avgCost: 180, totalCost: 360, lastPrice: 190, marketValueBase: 380, unrealizedPnlBase: 20, unrealizedPnlPct: 5.56, valuationCurrency: 'USD', priceSource: 'history_close', priceDate: '2026-06-17', priceStale: false, priceAvailable: true },
    ] }));
    getLatestDecisionSignals
      .mockResolvedValueOnce({
        items: [makeDecisionSignal({ stockCode: '600519', riskSummary: '已加载风险' })],
        total: 1,
        page: 1,
        pageSize: 1,
      })
      .mockRejectedValueOnce(new Error('latest AAPL failed'));

    render(<PortfolioPage />);

    expect(await screen.findByText('已加载风险')).toBeInTheDocument();
    expect(await screen.findByText('AI 建议降级')).toBeInTheDocument();
    expect(screen.getByText(/latest AAPL failed/)).toBeInTheDocument();
  });

  it('loads each unique holding through the latest endpoint once', async () => {
    getSnapshot.mockResolvedValueOnce(makeSnapshot({ positions: [
      { symbol: '600519', market: 'cn', currency: 'CNY', quantity: 1, avgCost: 1500, totalCost: 1500, lastPrice: 1600, marketValueBase: 1600, unrealizedPnlBase: 100, unrealizedPnlPct: 6.67, valuationCurrency: 'CNY', priceSource: 'history_close', priceDate: '2026-06-17', priceStale: false, priceAvailable: true },
      { symbol: '600519', market: 'cn', currency: 'CNY', quantity: 2, avgCost: 1500, totalCost: 3000, lastPrice: 1600, marketValueBase: 3200, unrealizedPnlBase: 200, unrealizedPnlPct: 6.67, valuationCurrency: 'CNY', priceSource: 'history_close', priceDate: '2026-06-17', priceStale: false, priceAvailable: true },
    ] }));
    getLatestDecisionSignals.mockResolvedValueOnce({
      items: [makeDecisionSignal({ stockCode: '600519', riskSummary: '唯一 latest 风险' })],
      total: 1,
      page: 1,
      pageSize: 1,
    });

    render(<PortfolioPage />);

    expect(await screen.findAllByText('唯一 latest 风险')).toHaveLength(2);
    expect(getLatestDecisionSignals).toHaveBeenCalledTimes(1);
    expect(decisionSignalsApi.list).not.toHaveBeenCalled();
  });

  it('limits concurrent latest lookups for large portfolios', async () => {
    const positions = Array.from({ length: 10 }, (_, index) => makePosition({
      symbol: `AAPL${index}`,
      market: 'us',
      currency: 'USD',
      totalCost: 100 + index,
      marketValueBase: 120 + index,
    }));
    getSnapshot.mockResolvedValueOnce(makeSnapshot({ positions }));
    let inFlight = 0;
    let maxInFlight = 0;
    getLatestDecisionSignals.mockImplementation(async () => {
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      await new Promise((resolve) => setTimeout(resolve, 5));
      inFlight -= 1;
      return { items: [], total: 0, page: 1, pageSize: 1 };
    });

    render(<PortfolioPage />);

    expect(await screen.findByText('AAPL0')).toBeInTheDocument();
    await waitFor(() => expect(getLatestDecisionSignals).toHaveBeenCalledTimes(10));
    await waitFor(() => expect(inFlight).toBe(0));
    expect(maxInFlight).toBeLessThanOrEqual(6);
  });

  it('requires today\'s plan before submitting bound analysis for a held position', async () => {
    getSnapshot.mockResolvedValueOnce(makeSnapshot({ fxStale: true, positions: [
      { symbol: 'HK00700', market: 'hk', currency: 'HKD', quantity: 10, avgCost: 400, totalCost: 4000, lastPrice: 420, marketValueBase: 4200, unrealizedPnlBase: 200, unrealizedPnlPct: 5, valuationCurrency: 'HKD', priceSource: 'history_close', priceDate: '2026-03-18', priceStale: true, priceAvailable: true },
    ] }));
    listDecisionSignals.mockResolvedValueOnce({
      items: [makeDecisionSignal({
        id: 201,
        stockCode: 'HK00700',
        stockName: '腾讯控股',
        market: 'hk',
        traceId: 'task-portfolio-1',
      })],
      total: 1,
      page: 1,
      pageSize: 20,
    });
    getDecisionQuality.mockResolvedValueOnce({
      context: {
        signalId: 201,
        accountId: 1,
        market: 'hk',
        stockCode: 'HK00700',
        instrumentType: 'equity',
        positionAction: 'hold',
        incrementalAction: 'wait',
        userInstruction: 'hold',
        benchmark: { market: 'hk', code: 'HSI', type: 'strategy_benchmark' },
        contextStatus: 'complete',
        unableReasons: [],
        frozenSnapshotHash: 'snapshot-1',
      },
      outcomes: [],
      attributions: [],
      evidenceSnapshot: {
        signalId: 201,
        status: 'complete',
        displayStatus: '已保存',
        unableReasons: [],
        identity: { accountId: 1, market: 'hk', symbol: 'HK00700' },
        instrument: { name: '腾讯控股', instrumentType: 'equity', evidenceHash: 'a'.repeat(64) },
        benchmark: { market: 'hk', code: 'HSI', type: 'strategy_benchmark', evidenceHash: 'b'.repeat(64) },
        researchSnapshotHash: 'snapshot-1',
      },
    });

    render(<PortfolioPage />);

    await waitForInitialLoad();

    const row = screen.getByText('HK00700').closest('tr');
    expect(row).not.toBeNull();
    fireEvent.click(within(row as HTMLTableRowElement).getByRole('button', { name: '分析' }));

    expect(await screen.findByText('请先生成今日持仓计划，再选择详细分析。')).toBeInTheDocument();
    expect(analyzePosition).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '生成今日计划' }));
    await screen.findByText('1 项持仓，1 项已生成');
    fireEvent.click(screen.getByRole('button', { name: '详细分析 腾讯控股（HK00700）' }));

    await waitFor(() => {
      expect(analyzePosition).toHaveBeenCalledWith('HK00700', {
        accountId: 1,
        analysisPhase: 'auto',
        force: false,
        researchSnapshotHash: 'snapshot-1',
        researchCutoff: expect.any(String),
        researchScope: [{ accountId: 1, market: 'hk', symbol: 'HK00700' }],
      });
    });
    expect((await screen.findAllByText('待你确认')).length).toBeGreaterThan(0);
  });

  it('fails one row closed when exact-trace quality identity does not match the holding', async () => {
    getSnapshot.mockResolvedValueOnce(makeSnapshot({ positions: [
      { symbol: 'HK00700', market: 'hk', currency: 'HKD', quantity: 10, avgCost: 400, totalCost: 4000, lastPrice: 420, marketValueBase: 4200, unrealizedPnlBase: 200, unrealizedPnlPct: 5, valuationCurrency: 'HKD', priceSource: 'history_close', priceDate: '2026-07-31', priceStale: false, priceAvailable: true },
    ] }));
    listDecisionSignals.mockResolvedValueOnce({
      items: [makeDecisionSignal({ id: 202, stockCode: 'HK00700', stockName: '名称串位', market: 'hk', traceId: 'task-portfolio-1' })],
      total: 1,
      page: 1,
      pageSize: 20,
    });
    getDecisionQuality.mockResolvedValueOnce({
      context: {
        signalId: 202,
        accountId: 1,
        market: 'hk',
        stockCode: 'HK00700',
        instrumentType: 'equity',
        positionAction: 'hold',
        incrementalAction: 'wait',
        userInstruction: 'hold',
        benchmark: { market: 'hk', code: 'HSI', type: 'strategy_benchmark' },
        contextStatus: 'complete',
        unableReasons: [],
        frozenSnapshotHash: 'snapshot-1',
      },
      outcomes: [],
      attributions: [],
      evidenceSnapshot: {
        signalId: 202,
        status: 'complete',
        displayStatus: '已保存',
        unableReasons: [],
        identity: { accountId: 1, market: 'hk', symbol: 'HK00700' },
        instrument: { name: '腾讯控股', instrumentType: 'equity', evidenceHash: 'a'.repeat(64) },
        benchmark: { market: 'hk', code: 'HSI', type: 'strategy_benchmark', evidenceHash: 'b'.repeat(64) },
        researchSnapshotHash: 'snapshot-1',
      },
    });

    render(<PortfolioPage />);
    await waitForInitialLoad();
    fireEvent.click(screen.getByRole('button', { name: '生成今日计划' }));
    await screen.findByText('1 项持仓，1 项已生成');
    fireEvent.click(screen.getByRole('button', { name: '详细分析 腾讯控股（HK00700）' }));

    await waitFor(() => expect(listDecisionSignals).toHaveBeenCalledWith({
      traceId: 'task-portfolio-1',
      accountId: 1,
      page: 1,
      pageSize: 20,
    }));
    expect((await screen.findAllByText('资料不足')).length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: '查看并确认' })).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent('signal_name_identity_mismatch');

    fireEvent.click(screen.getByText('审计详情'));

    expect(await screen.findByText('signal_name_identity_mismatch')).toBeInTheDocument();
  });

  it('prefers disabled feedback over empty-pair feedback when refresh is disabled', async () => {
    refreshFx.mockResolvedValueOnce({
      asOf: '2026-03-19',
      accountCount: 1,
      refreshEnabled: false,
      disabledReason: 'portfolio_fx_update_disabled',
      pairCount: 0,
      updatedCount: 0,
      staleCount: 0,
      errorCount: 0,
    });

    render(<PortfolioPage />);

    await waitForInitialLoad();

    fireEvent.click(screen.getByRole('button', { name: '刷新汇率' }));

    expect(await screen.findByText('汇率在线刷新已被禁用。')).toBeInTheDocument();
    expect(screen.queryByText('当前范围无可刷新的汇率对。')).not.toBeInTheDocument();
  });

  it('shows warning feedback when FX refresh still falls back to stale rates', async () => {
    refreshFx.mockResolvedValueOnce({
      asOf: '2026-03-19',
      accountCount: 1,
      pairCount: 2,
      updatedCount: 1,
      staleCount: 1,
      errorCount: 0,
    });

    render(<PortfolioPage />);

    await waitForInitialLoad();

    fireEvent.click(screen.getByRole('button', { name: '刷新汇率' }));

    expect(await screen.findByText(/stale\/fallback 汇率/)).toBeInTheDocument();
  });

  it('shows warning feedback when FX refresh returns online errors without stale pairs', async () => {
    refreshFx.mockResolvedValueOnce({
      asOf: '2026-03-19',
      accountCount: 1,
      pairCount: 1,
      updatedCount: 0,
      staleCount: 0,
      errorCount: 1,
    });

    render(<PortfolioPage />);

    await waitForInitialLoad();

    const snapshotCallsBeforeRefresh = getSnapshot.mock.calls.length;
    const riskCallsBeforeRefresh = getRisk.mock.calls.length;
    const tradeCallsBeforeRefresh = listTrades.mock.calls.length;

    fireEvent.click(screen.getByRole('button', { name: '刷新汇率' }));

    expect(await screen.findByText(/在线刷新未完全成功/)).toBeInTheDocument();
    await waitFor(() => expect(getSnapshot).toHaveBeenCalledTimes(snapshotCallsBeforeRefresh + 1));
    await waitFor(() => expect(getRisk).toHaveBeenCalledTimes(riskCallsBeforeRefresh + 1));
    expect(listTrades).toHaveBeenCalledTimes(tradeCallsBeforeRefresh);
    expect(listCashLedger).not.toHaveBeenCalled();
    expect(listCorporateActions).not.toHaveBeenCalled();
  });

  it('restores the button state and shows the existing error alert when FX refresh fails', async () => {
    refreshFx.mockRejectedValueOnce(
      createApiError(
        createParsedApiError({
          title: '刷新失败',
          message: '汇率服务暂时不可用',
        }),
      ),
    );

    render(<PortfolioPage />);

    await waitForInitialLoad();

    const refreshButton = screen.getByRole('button', { name: '刷新汇率' });
    fireEvent.click(refreshButton);

    const fxAlertTitle = await screen.findByText('刷新失败');
    expect(fxAlertTitle.closest('[role="alert"]')).toHaveTextContent('汇率服务暂时不可用');
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新汇率' })).not.toBeDisabled());
  });

  it('does not keep success feedback when snapshot reload fails after FX refresh succeeds', async () => {
    getSnapshot
      .mockResolvedValueOnce(makeSnapshot({ fxStale: true }))
      .mockRejectedValueOnce(
        createApiError(
          createParsedApiError({
            title: '快照刷新失败',
            message: '无法加载最新持仓快照',
          }),
        ),
      );

    render(<PortfolioPage />);

    await waitForInitialLoad();

    fireEvent.click(screen.getByRole('button', { name: '刷新汇率' }));

    const fxAlertTitle = await screen.findByText('快照刷新失败');
    expect(fxAlertTitle.closest('[role="alert"]')).toHaveTextContent('无法加载最新持仓快照');
    await waitFor(() => expect(screen.queryByText('汇率已刷新，共更新 1 对。')).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新汇率' })).not.toBeDisabled());
  });

  it('drops late FX refresh results after switching to another account scope', async () => {
    getAccounts.mockResolvedValueOnce(makeAccounts([{ id: 1, name: 'Main' }, { id: 2, name: 'Alt' }]));
    getSnapshot.mockImplementation(async ({ accountId }: { accountId?: number } = {}) => {
      if (accountId === 2) {
        return makeSnapshot({ accountId: 2, fxStale: false });
      }
      return makeSnapshot({ accountId: accountId ?? 1, fxStale: true, accountCount: accountId ? 1 : 2 });
    });

    const pendingRefresh = deferredPromise<{
      asOf: string;
      accountCount: number;
      pairCount: number;
      updatedCount: number;
      staleCount: number;
      errorCount: number;
    }>();
    refreshFx.mockImplementationOnce(() => pendingRefresh.promise);

    render(<PortfolioPage />);

    await waitForInitialLoad();

    const accountSelect = screen.getAllByRole('combobox')[0];
    fireEvent.change(accountSelect, { target: { value: '1' } });
    await waitFor(() => expect(getSnapshot).toHaveBeenLastCalledWith({ accountId: 1, costMethod: 'fifo', includeRealtime: false }));

    fireEvent.click(screen.getByRole('button', { name: '刷新汇率' }));
    expect(await screen.findByRole('button', { name: '刷新中...' })).toBeDisabled();

    fireEvent.change(accountSelect, { target: { value: '2' } });
    await waitFor(() => expect(getSnapshot).toHaveBeenLastCalledWith({ accountId: 2, costMethod: 'fifo', includeRealtime: false }));
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新汇率' })).not.toBeDisabled());

    const snapshotCallsAfterSwitch = getSnapshot.mock.calls.length;
    const riskCallsAfterSwitch = getRisk.mock.calls.length;

    await act(async () => {
      pendingRefresh.resolve({
        asOf: '2026-03-19',
        accountCount: 1,
        pairCount: 1,
        updatedCount: 1,
        staleCount: 0,
        errorCount: 0,
      });
      await pendingRefresh.promise;
    });

    expect(getSnapshot).toHaveBeenCalledTimes(snapshotCallsAfterSwitch);
    expect(getRisk).toHaveBeenCalledTimes(riskCallsAfterSwitch);
    expect(screen.queryByText('汇率已刷新，共更新 1 对。')).not.toBeInTheDocument();
  });

  it('drops late FX refresh results after switching cost method', async () => {
    const pendingRefresh = deferredPromise<{
      asOf: string;
      accountCount: number;
      pairCount: number;
      updatedCount: number;
      staleCount: number;
      errorCount: number;
    }>();
    refreshFx.mockImplementationOnce(() => pendingRefresh.promise);

    render(<PortfolioPage />);

    await waitForInitialLoad();

    const costMethodSelect = screen.getAllByRole('combobox')[1];

    fireEvent.click(screen.getByRole('button', { name: '刷新汇率' }));
    expect(await screen.findByRole('button', { name: '刷新中...' })).toBeDisabled();

    fireEvent.change(costMethodSelect, { target: { value: 'avg' } });
    await waitFor(() => expect(getSnapshot).toHaveBeenLastCalledWith({ accountId: undefined, costMethod: 'avg', includeRealtime: false }));
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新汇率' })).not.toBeDisabled());

    const snapshotCallsAfterSwitch = getSnapshot.mock.calls.length;
    const riskCallsAfterSwitch = getRisk.mock.calls.length;

    await act(async () => {
      pendingRefresh.resolve({
        asOf: '2026-03-19',
        accountCount: 1,
        pairCount: 1,
        updatedCount: 1,
        staleCount: 0,
        errorCount: 0,
      });
      await pendingRefresh.promise;
    });

    expect(getSnapshot).toHaveBeenCalledTimes(snapshotCallsAfterSwitch);
    expect(getRisk).toHaveBeenCalledTimes(riskCallsAfterSwitch);
    expect(screen.queryByText('汇率已刷新，共更新 1 对。')).not.toBeInTheDocument();
  });

  it('deactivates the selected account from the account toolbar and reloads accounts', async () => {
    getAccounts
      .mockResolvedValueOnce(makeAccounts([{ id: 1, name: 'Main' }, { id: 2, name: 'Alt' }]))
      .mockResolvedValueOnce(makeAccounts([{ id: 2, name: 'Alt' }]));

    render(<PortfolioPage />);

    await waitForInitialLoad();

    const accountSelect = screen.getAllByRole('combobox')[0];
    fireEvent.change(accountSelect, { target: { value: '1' } });

    await waitFor(() => expect(getSnapshot).toHaveBeenLastCalledWith({ accountId: 1, costMethod: 'fifo', includeRealtime: false }));
    fireEvent.click(screen.getByRole('button', { name: '删除账户' }));

    const dialog = await screen.findByText('删除持仓账户');
    expect(dialog.closest('[role="dialog"]') ?? document.body).toHaveTextContent(
      '删除后该账户会从默认列表、快照、风险和录入入口隐藏',
    );
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));

    await waitFor(() => expect(deleteAccount).toHaveBeenCalledWith(1));
    await waitFor(() => expect(getAccounts).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText('Main (#1)')).not.toBeInTheDocument());
    expect(screen.getByRole('option', { name: 'Alt (#2)' })).toBeInTheDocument();
  });
});
