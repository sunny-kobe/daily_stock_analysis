export type StrategyStatus =
  | 'draft'
  | 'backtest_running'
  | 'backtest_failed'
  | 'simulation'
  | 'small_capital'
  | 'active'
  | 'retired';

export type StrategyValidationStatus = 'completed' | 'failed' | 'unable';

export interface StrategyBucketDimensions {
  horizon?: string;
  market?: string;
  productType?: string;
  instruction?: string;
  marketRegime?: string;
  period?: string;
}

export interface StrategyBucketMetrics {
  sampleCount: number;
  winRatePct?: number | null;
  winDefinition?: string | null;
  netReturnAfterCostPct?: number | null;
  benchmarkExcessPct?: number | null;
  maximumDrawdownPct?: number | null;
  averageGainPct?: number | null;
  averageLossPct?: number | null;
  turnoverPct?: number | null;
  totalCostPct?: number | null;
  unableCount: number;
}

export interface StrategyMetricBucket {
  dimensions: StrategyBucketDimensions;
  metrics: StrategyBucketMetrics;
}

export interface StrategyValidationResult {
  historicalStatus?: string;
  displayMessage?: string;
  eligibleEventCount?: number;
  evaluationCount?: number;
  buckets: StrategyMetricBucket[];
  unableReasons: string[];
  resultHash?: string;
}

export interface StrategyValidationRun {
  runId: string;
  strategyKey: string;
  strategyVersion: string;
  validationKind: 'historical_backtest' | 'forward_observation';
  protocol: Record<string, unknown>;
  datasetHash: string;
  engineVersion: string;
  status: StrategyValidationStatus;
  statusLabel: string;
  qualifying: boolean;
  result: StrategyValidationResult;
  runHash: string;
  createdAt: string;
}

export interface StrategyVersion {
  strategyKey: string;
  version: string;
  name: string;
  changeSummary: string;
  changedDimension: string;
  markets: string[];
  instrumentTypes: string[];
  horizons: string[];
  evaluationMode: 'historical_and_forward' | 'forward_only';
  policy: Record<string, unknown>;
  costModel: Record<string, unknown>;
  benchmarkPolicy: Record<string, unknown>;
  status: StrategyStatus;
  statusLabel: string;
  allowedTransitions: StrategyStatus[];
  latestRun: StrategyValidationRun | null;
  manifestHash: string;
  createdAt: string;
}

export interface StrategyTransitionRequest {
  toStatus: StrategyStatus;
  humanReason: string;
}

export interface StrategyTransitionResponse {
  strategyKey: string;
  version: string;
  fromStatus: StrategyStatus;
  status: StrategyStatus;
  statusLabel: string;
  humanReason: string;
  transitionId: number;
}
