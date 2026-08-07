import type { DecisionSignalItem } from './decisionSignals';

export type PortfolioCostMethod = 'fifo' | 'avg';
export type PortfolioSide = 'buy' | 'sell';
export type PortfolioCashDirection = 'in' | 'out';
export type PortfolioCorporateActionType = 'cash_dividend' | 'split_adjustment';
export type PortfolioInstrumentType = 'equity' | 'etf' | 'qdii' | 'adr_ads' | 'daily_leveraged_product' | 'unknown';
export type PortfolioVerificationStatus = 'verified' | 'provisional' | 'missing';

export interface PortfolioInstrumentItem {
  id: number;
  symbol: string;
  market: 'cn' | 'hk' | 'us' | 'jp' | 'kr' | 'tw';
  quoteCurrency: string;
  instrumentType: PortfolioInstrumentType;
  underlyingSymbol?: string | null;
  underlyingMarket?: 'cn' | 'hk' | 'us' | 'jp' | 'kr' | 'tw' | null;
  underlyingCurrency?: string | null;
  leverageFactor?: number | null;
  dailyReset: boolean;
  conversionRatio?: number | null;
  tradeLotSize: number;
  requiresPremiumCheck: boolean;
  verificationStatus: PortfolioVerificationStatus;
  evidenceSource?: string | null;
  evidenceAsOf?: string | null;
  metadata: Record<string, unknown>;
}

export type PortfolioInstrumentInput = Omit<PortfolioInstrumentItem, 'id' | 'metadata'> & {
  metadata?: Record<string, unknown>;
};

export interface PortfolioRiskPolicyItem {
  id: number;
  minCashBufferPct: number;
  maxSinglePositionPct: number;
  maxSectorPct: number;
  maxHighRiskProductPct: number;
  maxPortfolioDrawdownPct: number;
}

export type PortfolioRiskPolicyInput = Omit<PortfolioRiskPolicyItem, 'id'>;

export interface PortfolioPointInTimeEligibility {
  scope: 'current_prospective';
  prospectiveDecisionEligible: boolean;
  historicalReplayEligible: false;
  sourceCutoffs: Record<string, string | null>;
  blockers: string[];
}

export interface PortfolioFrozenDecisionSignal {
  id: number;
  market: string;
  stockCode: string;
  stockName?: string | null;
  reason?: string | null;
  status: string;
  createdAt?: string | null;
  updatedAt?: string | null;
  metadata: Record<string, unknown>;
}

export interface PortfolioResearchSnapshotResponse {
  snapshotHash: string;
  executionIdentityHash: string;
  cutoff: string;
  scope: PortfolioResearchScopeItem[];
  scopeHash?: string | null;
  completeness: string;
  positions: Array<Record<string, unknown>>;
  instruments: Array<Record<string, unknown>>;
  pointInTime: PortfolioPointInTimeEligibility;
  decisionSignals: PortfolioFrozenDecisionSignal[];
  hardBlockers: Array<{ code: string; scope: string; symbol?: string; market?: string }>;
  limitations: string[];
}

export interface PortfolioResearchScopeItem {
  accountId: number;
  market: 'cn' | 'hk' | 'us' | 'jp' | 'kr' | 'tw';
  symbol: string;
}

export interface PortfolioResearchEvidenceItem {
  accountId: number;
  symbol: string;
  market: string;
  currency: string;
  benchmarkCode?: string | null;
  status: 'ready' | 'insufficient';
  price?: Record<string, unknown> | null;
  benchmark?: Record<string, unknown> | null;
  fx?: Record<string, unknown> | null;
  productEvidence?: Record<string, unknown> | null;
  blockers: string[];
}

export interface PortfolioResearchEvidencePrepareResponse {
  schemaVersion: 'portfolio-research-evidence-prepare-v2';
  scope: PortfolioResearchScopeItem[];
  preparedAt: string;
  cutoff: string;
  asOf: string;
  status: 'ready' | 'partial' | 'empty';
  positionCount: number;
  readyCount: number;
  insufficientCount: number;
  items: PortfolioResearchEvidenceItem[];
}

export interface PortfolioResearchBaselineRequest {
  researchSnapshotHash: string;
  researchCutoff: string;
  researchScope: PortfolioResearchScopeItem[];
}

export interface PortfolioResearchExecutionCheckRequest {
  researchSnapshotHash: string;
  researchExecutionIdentityHash: string;
  researchCutoff: string;
  researchScope: PortfolioResearchScopeItem[];
}

export interface PortfolioResearchExecutionCheckItem {
  accountId: number;
  market: string;
  symbol: string;
  name?: string | null;
  status: 'ready' | 'insufficient';
  referenceEvidence: Record<string, unknown>;
  currentEvidence: Record<string, unknown>;
  changedFields: string[];
  blockers: string[];
  requiresReconfirmation: boolean;
}

export interface PortfolioResearchExecutionCheckResponse {
  schemaVersion: 'portfolio-research-execution-check-v1';
  checkedAt: string;
  researchSnapshotHash: string;
  scope: PortfolioResearchScopeItem[];
  status: 'ready' | 'partial';
  requiresReconfirmation: boolean;
  items: PortfolioResearchExecutionCheckItem[];
}

export type PortfolioUserInstruction = 'add' | 'hold' | 'reduce' | 'exit' | 'insufficient';

export interface PortfolioResearchBaselineItem {
  accountId: number;
  accountName?: string | null;
  market: string;
  symbol: string;
  name?: string | null;
  displayLabel: string;
  selectionKey: string;
  currency?: string | null;
  quantity?: number | null;
  instrumentType: string;
  quote: Record<string, unknown>;
  history: Record<string, unknown>;
  trend?: Record<string, unknown> | null;
  currentSignalId?: number | null;
  positionAction: 'hold' | 'reduce' | 'exit';
  incrementalAction: 'add_in_batches' | 'wait' | 'no_add';
  userInstruction: PortfolioUserInstruction;
  coreReason?: string | null;
  hardBlockers: string[];
  riskFlags: Array<Record<string, unknown>>;
  exceptionReasons: string[];
  evidenceStatus: string;
  researchLevel: 'baseline';
  detailRecommended: boolean;
  sizingAllowed: boolean;
}

export interface PortfolioResearchBaselineCandidate {
  selectionKey: string;
  displayLabel: string;
  market: string;
  symbol: string;
  accountIds: number[];
  reasons: string[];
  priority: number;
  recommended: boolean;
}

export interface PortfolioResearchBaselineResponse {
  schemaVersion: 'portfolio-research-baseline-v1';
  snapshotHash: string;
  cutoff: string;
  marketDataCutoff: string;
  ledgerPositionCount: number;
  baselineRowCount: number;
  coverageReconciled: boolean;
  portfolioRiskFlags: Array<Record<string, unknown>>;
  items: PortfolioResearchBaselineItem[];
  suggestedDeepAnalysis: PortfolioResearchBaselineCandidate[];
  deepAnalysisStarted: boolean;
}

export interface PortfolioAccountItem {
  id: number;
  ownerId?: string | null;
  name: string;
  broker?: string | null;
  market: 'cn' | 'hk' | 'us' | 'jp' | 'kr' | 'tw';
  baseCurrency: string;
  isActive: boolean;
  createdAt?: string | null;
  updatedAt?: string | null;
}

export interface PortfolioAccountListResponse {
  accounts: PortfolioAccountItem[];
}

export interface PortfolioAccountCreateRequest {
  name: string;
  broker?: string;
  market: 'cn' | 'hk' | 'us' | 'jp' | 'kr' | 'tw';
  baseCurrency: string;
  ownerId?: string;
}

export interface PortfolioPositionItem {
  symbol: string;
  market: string;
  currency: string;
  quantity: number;
  avgCost: number;
  totalCost: number;
  lastPrice: number;
  marketValueBase: number;
  unrealizedPnlBase: number;
  unrealizedPnlPct?: number | null;
  valuationCurrency: string;
  priceSource?: 'realtime_quote' | 'history_close' | 'missing' | string;
  priceProvider?: string | null;
  priceDate?: string | null;
  priceStale?: boolean;
  priceAvailable?: boolean;
  dataQuality?: 'ok' | 'partial' | string;
  limitations?: string[];
}

type PortfolioPositionAnalysisOptions = {
  accountId?: number;
  analysisPhase?: 'auto' | 'premarket' | 'intraday' | 'postmarket';
  force?: boolean;
};

type PortfolioResearchBinding =
  | {
      researchSnapshotHash: string;
      researchCutoff: string;
      researchScope: PortfolioResearchScopeItem[];
    }
  | {
      researchSnapshotHash?: never;
      researchCutoff?: never;
      researchScope?: never;
    };

export type PortfolioPositionAnalysisRequest = PortfolioPositionAnalysisOptions & PortfolioResearchBinding;

export interface PortfolioAccountSnapshot {
  accountId: number;
  accountName: string;
  ownerId?: string | null;
  broker?: string | null;
  market: string;
  baseCurrency: string;
  asOf: string;
  costMethod: PortfolioCostMethod;
  totalCash: number;
  totalMarketValue: number;
  totalEquity: number;
  realizedPnl: number;
  unrealizedPnl: number;
  feeTotal: number;
  taxTotal: number;
  fxStale: boolean;
  dataQuality?: 'ok' | 'partial' | string;
  limitations?: string[];
  positions: PortfolioPositionItem[];
}

export interface PortfolioSnapshotResponse {
  asOf: string;
  costMethod: PortfolioCostMethod;
  currency: string;
  accountCount: number;
  totalCash: number;
  totalMarketValue: number;
  totalEquity: number;
  realizedPnl: number;
  unrealizedPnl: number;
  feeTotal: number;
  taxTotal: number;
  fxStale: boolean;
  dataQuality?: 'ok' | 'partial' | string;
  limitations?: string[];
  accounts: PortfolioAccountSnapshot[];
}

export interface PortfolioConcentrationItem {
  symbol: string;
  marketValueBase: number;
  weightPct: number;
  isAlert: boolean;
}

export interface PortfolioSectorConcentrationItem {
  sector: string;
  marketValueBase: number;
  weightPct: number;
  symbolCount: number;
  isAlert: boolean;
}

export interface PortfolioDrawdownBlock {
  available?: boolean;
  seriesPoints: number;
  maxDrawdownPct: number | null;
  currentDrawdownPct: number | null;
  alert: boolean;
  fxStale: boolean;
  limitations?: string[];
}

export interface PortfolioStopLossItem {
  accountId: number;
  symbol: string;
  avgCost: number;
  lastPrice: number;
  lossPct: number;
  nearThresholdPct: number;
  isTriggered: boolean;
}

export interface PortfolioDecisionSignalRiskItem {
  accountId?: number | null;
  symbol: string;
  market: string;
  signal: Partial<DecisionSignalItem>;
}

export interface PortfolioDecisionSignalRiskBlock {
  available: boolean;
  total: number;
  actions: {
    sell?: number;
    reduce?: number;
    alert?: number;
    [key: string]: number | undefined;
  };
  items: PortfolioDecisionSignalRiskItem[];
}

export interface PortfolioRiskResponse {
  asOf: string;
  accountId?: number | null;
  costMethod: PortfolioCostMethod;
  currency: string;
  thresholds: Record<string, number>;
  concentration: {
    totalMarketValue: number;
    topWeightPct: number;
    alert: boolean;
    topPositions: PortfolioConcentrationItem[];
  };
  sectorConcentration: {
    totalMarketValue: number;
    topWeightPct: number;
    alert: boolean;
    topSectors: PortfolioSectorConcentrationItem[];
    coverage: Record<string, number>;
    errors: string[];
  };
  drawdown: PortfolioDrawdownBlock;
  stopLoss: {
    nearAlert: boolean;
    triggeredCount: number;
    nearCount: number;
    items: PortfolioStopLossItem[];
  };
  decisionSignalRisk?: PortfolioDecisionSignalRiskBlock;
}

export interface PortfolioTradeCreateRequest {
  accountId: number;
  symbol: string;
  tradeDate: string;
  side: PortfolioSide;
  quantity: number;
  price: number;
  fee?: number;
  tax?: number;
  market?: 'cn' | 'hk' | 'us' | 'jp' | 'kr' | 'tw';
  currency?: string;
  tradeUid?: string;
  note?: string;
}

export interface PortfolioCashLedgerCreateRequest {
  accountId: number;
  eventDate: string;
  direction: PortfolioCashDirection;
  amount: number;
  currency?: string;
  note?: string;
}

export interface PortfolioCorporateActionCreateRequest {
  accountId: number;
  symbol: string;
  effectiveDate: string;
  actionType: PortfolioCorporateActionType;
  market?: 'cn' | 'hk' | 'us' | 'jp' | 'kr' | 'tw';
  currency?: string;
  cashDividendPerShare?: number;
  splitRatio?: number;
  note?: string;
}

export interface PortfolioEventCreatedResponse {
  id: number;
}

export interface PortfolioDeleteResponse {
  deleted: number;
}

export interface PortfolioTradeListItem {
  id: number;
  accountId: number;
  tradeUid?: string | null;
  symbol: string;
  market: string;
  currency: string;
  tradeDate: string;
  side: PortfolioSide;
  quantity: number;
  price: number;
  fee: number;
  tax: number;
  note?: string | null;
  createdAt?: string | null;
}

export interface PortfolioTradeListResponse {
  items: PortfolioTradeListItem[];
  total: number;
  page: number;
  pageSize: number;
}

export interface PortfolioCashLedgerListItem {
  id: number;
  accountId: number;
  eventDate: string;
  direction: PortfolioCashDirection;
  amount: number;
  currency: string;
  note?: string | null;
  createdAt?: string | null;
}

export interface PortfolioCashLedgerListResponse {
  items: PortfolioCashLedgerListItem[];
  total: number;
  page: number;
  pageSize: number;
}

export interface PortfolioCorporateActionListItem {
  id: number;
  accountId: number;
  symbol: string;
  market: string;
  currency: string;
  effectiveDate: string;
  actionType: PortfolioCorporateActionType;
  cashDividendPerShare?: number | null;
  splitRatio?: number | null;
  note?: string | null;
  createdAt?: string | null;
}

export interface PortfolioCorporateActionListResponse {
  items: PortfolioCorporateActionListItem[];
  total: number;
  page: number;
  pageSize: number;
}

export interface PortfolioImportTradeItem {
  tradeDate: string;
  symbol: string;
  side: PortfolioSide;
  quantity: number;
  price: number;
  fee: number;
  tax: number;
  tradeUid?: string | null;
  dedupHash: string;
  currency?: string | null;
}

export interface PortfolioImportParseResponse {
  broker: string;
  recordCount: number;
  skippedCount: number;
  errorCount: number;
  records: PortfolioImportTradeItem[];
  errors: string[];
}

export interface PortfolioImportCommitResponse {
  accountId: number;
  recordCount: number;
  insertedCount: number;
  duplicateCount: number;
  failedCount: number;
  dryRun: boolean;
  errors: string[];
}

export interface PortfolioImportBrokerItem {
  broker: string;
  aliases: string[];
  displayName?: string;
}

export interface PortfolioImportBrokerListResponse {
  brokers: PortfolioImportBrokerItem[];
}

export interface PortfolioFxRefreshResponse {
  asOf: string;
  accountCount: number;
  refreshEnabled?: boolean;
  disabledReason?: string | null;
  pairCount: number;
  updatedCount: number;
  staleCount: number;
  errorCount: number;
}
