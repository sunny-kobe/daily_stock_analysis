import apiClient from './index';
import { toCamelCase } from './utils';
import type { TaskAccepted } from '../types/analysis';
import type {
  PortfolioAccountItem,
  PortfolioAccountCreateRequest,
  PortfolioAccountListResponse,
  PortfolioCashLedgerCreateRequest,
  PortfolioCashLedgerListResponse,
  PortfolioCorporateActionCreateRequest,
  PortfolioCorporateActionListResponse,
  PortfolioCostMethod,
  PortfolioDeleteResponse,
  PortfolioEventCreatedResponse,
  PortfolioFxRefreshResponse,
  PortfolioImportBrokerListResponse,
  PortfolioImportCommitResponse,
  PortfolioImportParseResponse,
  PortfolioInstrumentInput,
  PortfolioInstrumentItem,
  PortfolioPositionAnalysisRequest,
  PortfolioRiskResponse,
  PortfolioRiskPolicyInput,
  PortfolioRiskPolicyItem,
  PortfolioResearchSnapshotResponse,
  PortfolioSnapshotResponse,
  PortfolioTradeCreateRequest,
  PortfolioTradeListResponse,
} from '../types/portfolio';

type SnapshotQuery = {
  accountId?: number;
  asOf?: string;
  costMethod?: PortfolioCostMethod;
  includeRealtime?: boolean;
};

type FxRefreshQuery = {
  accountId?: number;
  asOf?: string;
};

type EventQuery = {
  accountId?: number;
  dateFrom?: string;
  dateTo?: string;
  page?: number;
  pageSize?: number;
};

type TradeListQuery = EventQuery & {
  symbol?: string;
  side?: 'buy' | 'sell';
};

type CashListQuery = EventQuery & {
  direction?: 'in' | 'out';
};

type CorporateListQuery = EventQuery & {
  symbol?: string;
  actionType?: 'cash_dividend' | 'split_adjustment';
};

function buildSnapshotParams(query: SnapshotQuery): Record<string, string | number> {
  const params: Record<string, string | number> = {};
  if (query.accountId != null) {
    params.account_id = query.accountId;
  }
  if (query.asOf) {
    params.as_of = query.asOf;
  }
  if (query.costMethod) {
    params.cost_method = query.costMethod;
  }
  if (query.includeRealtime !== undefined) {
    params.include_realtime = query.includeRealtime ? 'true' : 'false';
  }
  return params;
}

function buildFxRefreshParams(query: FxRefreshQuery): Record<string, string | number> {
  const params: Record<string, string | number> = {};
  if (query.accountId != null) {
    params.account_id = query.accountId;
  }
  if (query.asOf) {
    params.as_of = query.asOf;
  }
  return params;
}

function buildEventParams(query: EventQuery): Record<string, string | number> {
  const params: Record<string, string | number> = {};
  if (query.accountId != null) {
    params.account_id = query.accountId;
  }
  if (query.dateFrom) {
    params.date_from = query.dateFrom;
  }
  if (query.dateTo) {
    params.date_to = query.dateTo;
  }
  if (query.page != null) {
    params.page = query.page;
  }
  if (query.pageSize != null) {
    params.page_size = query.pageSize;
  }
  return params;
}

export const portfolioApi = {
  async getInstruments(): Promise<{ items: PortfolioInstrumentItem[] }> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/portfolio/instruments');
    return toCamelCase<{ items: PortfolioInstrumentItem[] }>(response.data);
  },

  async createInstrument(payload: PortfolioInstrumentInput): Promise<PortfolioInstrumentItem> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/portfolio/instruments', {
      symbol: payload.symbol,
      market: payload.market,
      quote_currency: payload.quoteCurrency,
      instrument_type: payload.instrumentType,
      underlying_symbol: payload.underlyingSymbol,
      underlying_market: payload.underlyingMarket,
      underlying_currency: payload.underlyingCurrency,
      leverage_factor: payload.leverageFactor,
      daily_reset: payload.dailyReset,
      conversion_ratio: payload.conversionRatio,
      trade_lot_size: payload.tradeLotSize,
      requires_premium_check: payload.requiresPremiumCheck,
      verification_status: payload.verificationStatus,
      evidence_source: payload.evidenceSource,
      evidence_as_of: payload.evidenceAsOf,
      metadata: payload.metadata,
    });
    return toCamelCase<PortfolioInstrumentItem>(response.data);
  },

  async updateInstrument(market: string, symbol: string, payload: Partial<PortfolioInstrumentInput>): Promise<PortfolioInstrumentItem> {
    const request: Record<string, unknown> = {};
    const fields: Array<[keyof PortfolioInstrumentInput, string]> = [
      ['quoteCurrency', 'quote_currency'], ['instrumentType', 'instrument_type'],
      ['underlyingSymbol', 'underlying_symbol'], ['underlyingMarket', 'underlying_market'],
      ['underlyingCurrency', 'underlying_currency'], ['leverageFactor', 'leverage_factor'],
      ['dailyReset', 'daily_reset'], ['conversionRatio', 'conversion_ratio'],
      ['tradeLotSize', 'trade_lot_size'], ['requiresPremiumCheck', 'requires_premium_check'],
      ['verificationStatus', 'verification_status'], ['evidenceSource', 'evidence_source'],
      ['evidenceAsOf', 'evidence_as_of'], ['metadata', 'metadata'],
    ];
    for (const [key, apiKey] of fields) if (key in payload) request[apiKey] = payload[key];
    const response = await apiClient.patch<Record<string, unknown>>(
      `/api/v1/portfolio/instruments/${encodeURIComponent(market)}/${encodeURIComponent(symbol)}`,
      request,
    );
    return toCamelCase<PortfolioInstrumentItem>(response.data);
  },

  async getRiskPolicy(): Promise<{ policy: PortfolioRiskPolicyItem | null }> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/portfolio/risk-policy');
    return toCamelCase<{ policy: PortfolioRiskPolicyItem | null }>(response.data);
  },

  async saveRiskPolicy(payload: PortfolioRiskPolicyInput): Promise<PortfolioRiskPolicyItem> {
    const response = await apiClient.put<Record<string, unknown>>('/api/v1/portfolio/risk-policy', {
      min_cash_buffer_pct: payload.minCashBufferPct,
      max_single_position_pct: payload.maxSinglePositionPct,
      max_sector_pct: payload.maxSectorPct,
      max_high_risk_product_pct: payload.maxHighRiskProductPct,
      max_portfolio_drawdown_pct: payload.maxPortfolioDrawdownPct,
    });
    return toCamelCase<PortfolioRiskPolicyItem>(response.data);
  },

  async getResearchSnapshot(): Promise<PortfolioResearchSnapshotResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/portfolio/research-snapshot');
    return toCamelCase<PortfolioResearchSnapshotResponse>(response.data);
  },
  async getAccounts(includeInactive = false): Promise<PortfolioAccountListResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/portfolio/accounts', {
      params: { include_inactive: includeInactive },
    });
    return toCamelCase<PortfolioAccountListResponse>(response.data);
  },

  async createAccount(payload: PortfolioAccountCreateRequest): Promise<PortfolioAccountItem> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/portfolio/accounts', {
      name: payload.name,
      broker: payload.broker,
      market: payload.market,
      base_currency: payload.baseCurrency,
      owner_id: payload.ownerId,
    });
    return toCamelCase<PortfolioAccountItem>(response.data);
  },

  async deleteAccount(accountId: number): Promise<PortfolioDeleteResponse> {
    const response = await apiClient.delete<Record<string, unknown>>(`/api/v1/portfolio/accounts/${accountId}`);
    return toCamelCase<PortfolioDeleteResponse>(response.data);
  },

  async getSnapshot(query: SnapshotQuery = {}): Promise<PortfolioSnapshotResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/portfolio/snapshot', {
      params: buildSnapshotParams(query),
    });
    return toCamelCase<PortfolioSnapshotResponse>(response.data);
  },

  async getRealtimeSnapshot(query: SnapshotQuery = {}): Promise<PortfolioSnapshotResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/portfolio/snapshot', {
      params: buildSnapshotParams({ ...query, includeRealtime: true }),
      timeout: 15000,
    });
    return toCamelCase<PortfolioSnapshotResponse>(response.data);
  },

  async analyzePosition(symbol: string, payload: PortfolioPositionAnalysisRequest = {}): Promise<TaskAccepted> {
    const response = await apiClient.post<Record<string, unknown>>(
      `/api/v1/portfolio/positions/${encodeURIComponent(symbol)}/analysis`,
      {
        account_id: payload.accountId,
        analysis_phase: payload.analysisPhase ?? 'auto',
        force: payload.force ?? false,
      },
    );
    return toCamelCase<TaskAccepted>(response.data);
  },

  async getRisk(query: SnapshotQuery = {}): Promise<PortfolioRiskResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/portfolio/risk', {
      params: buildSnapshotParams(query),
    });
    return toCamelCase<PortfolioRiskResponse>(response.data);
  },

  async refreshFx(query: FxRefreshQuery = {}): Promise<PortfolioFxRefreshResponse> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/portfolio/fx/refresh', undefined, {
      params: buildFxRefreshParams(query),
    });
    return toCamelCase<PortfolioFxRefreshResponse>(response.data);
  },

  async createTrade(payload: PortfolioTradeCreateRequest): Promise<PortfolioEventCreatedResponse> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/portfolio/trades', {
      account_id: payload.accountId,
      symbol: payload.symbol,
      trade_date: payload.tradeDate,
      side: payload.side,
      quantity: payload.quantity,
      price: payload.price,
      fee: payload.fee ?? 0,
      tax: payload.tax ?? 0,
      market: payload.market,
      currency: payload.currency,
      trade_uid: payload.tradeUid,
      note: payload.note,
    });
    return toCamelCase<PortfolioEventCreatedResponse>(response.data);
  },

  async deleteTrade(tradeId: number): Promise<PortfolioDeleteResponse> {
    const response = await apiClient.delete<Record<string, unknown>>(`/api/v1/portfolio/trades/${tradeId}`);
    return toCamelCase<PortfolioDeleteResponse>(response.data);
  },

  async createCashLedger(payload: PortfolioCashLedgerCreateRequest): Promise<PortfolioEventCreatedResponse> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/portfolio/cash-ledger', {
      account_id: payload.accountId,
      event_date: payload.eventDate,
      direction: payload.direction,
      amount: payload.amount,
      currency: payload.currency,
      note: payload.note,
    });
    return toCamelCase<PortfolioEventCreatedResponse>(response.data);
  },

  async deleteCashLedger(entryId: number): Promise<PortfolioDeleteResponse> {
    const response = await apiClient.delete<Record<string, unknown>>(`/api/v1/portfolio/cash-ledger/${entryId}`);
    return toCamelCase<PortfolioDeleteResponse>(response.data);
  },

  async createCorporateAction(payload: PortfolioCorporateActionCreateRequest): Promise<PortfolioEventCreatedResponse> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/portfolio/corporate-actions', {
      account_id: payload.accountId,
      symbol: payload.symbol,
      effective_date: payload.effectiveDate,
      action_type: payload.actionType,
      market: payload.market,
      currency: payload.currency,
      cash_dividend_per_share: payload.cashDividendPerShare,
      split_ratio: payload.splitRatio,
      note: payload.note,
    });
    return toCamelCase<PortfolioEventCreatedResponse>(response.data);
  },

  async deleteCorporateAction(actionId: number): Promise<PortfolioDeleteResponse> {
    const response = await apiClient.delete<Record<string, unknown>>(`/api/v1/portfolio/corporate-actions/${actionId}`);
    return toCamelCase<PortfolioDeleteResponse>(response.data);
  },

  async listTrades(query: TradeListQuery = {}): Promise<PortfolioTradeListResponse> {
    const params = buildEventParams(query);
    if (query.symbol) {
      params.symbol = query.symbol;
    }
    if (query.side) {
      params.side = query.side;
    }
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/portfolio/trades', { params });
    return toCamelCase<PortfolioTradeListResponse>(response.data);
  },

  async listCashLedger(query: CashListQuery = {}): Promise<PortfolioCashLedgerListResponse> {
    const params = buildEventParams(query);
    if (query.direction) {
      params.direction = query.direction;
    }
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/portfolio/cash-ledger', { params });
    return toCamelCase<PortfolioCashLedgerListResponse>(response.data);
  },

  async listCorporateActions(query: CorporateListQuery = {}): Promise<PortfolioCorporateActionListResponse> {
    const params = buildEventParams(query);
    if (query.symbol) {
      params.symbol = query.symbol;
    }
    if (query.actionType) {
      params.action_type = query.actionType;
    }
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/portfolio/corporate-actions', { params });
    return toCamelCase<PortfolioCorporateActionListResponse>(response.data);
  },

  async listImportBrokers(): Promise<PortfolioImportBrokerListResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/portfolio/imports/csv/brokers');
    return toCamelCase<PortfolioImportBrokerListResponse>(response.data);
  },

  async parseCsvImport(broker: string, file: File): Promise<PortfolioImportParseResponse> {
    const formData = new FormData();
    formData.append('broker', broker);
    formData.append('file', file);
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/portfolio/imports/csv/parse', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
    return toCamelCase<PortfolioImportParseResponse>(response.data);
  },

  async commitCsvImport(
    accountId: number,
    broker: string,
    file: File,
    dryRun = false,
  ): Promise<PortfolioImportCommitResponse> {
    const formData = new FormData();
    formData.append('account_id', String(accountId));
    formData.append('broker', broker);
    formData.append('dry_run', dryRun ? 'true' : 'false');
    formData.append('file', file);
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/portfolio/imports/csv/commit', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
    return toCamelCase<PortfolioImportCommitResponse>(response.data);
  },
};
