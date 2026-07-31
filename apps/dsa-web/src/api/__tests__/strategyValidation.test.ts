import { beforeEach, describe, expect, it, vi } from 'vitest';
import { strategyValidationApi } from '../strategyValidation';

const { get, post } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock('../index', () => ({
  default: { get, post },
}));

describe('strategyValidationApi', () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
  });

  it('maps strategy versions, latest runs, metrics, and unable reasons deeply', async () => {
    get.mockResolvedValueOnce({
      data: {
        items: [{
          strategy_key: 'portfolio-hold-baseline',
          version: '1.0.0',
          name: '持有比较基线',
          change_summary: '固定持有作为比较基准',
          changed_dimension: 'baseline',
          markets: ['cn'],
          instrument_types: ['equity'],
          horizons: ['5d'],
          evaluation_mode: 'historical_and_forward',
          policy: {},
          cost_model: { commission_bps: 3 },
          benchmark_policy: { selection: 'decision_time_market', benchmarks: { cn: '000300' } },
          status: 'simulation',
          status_label: '模拟观察',
          allowed_transitions: [],
          latest_run: {
            run_id: 'svr-1',
            strategy_key: 'portfolio-hold-baseline',
            strategy_version: '1.0.0',
            validation_kind: 'historical_backtest',
            protocol: {},
            dataset_hash: 'a'.repeat(64),
            engine_version: 'portfolio-strategy-v1',
            status: 'completed',
            status_label: '已完成',
            qualifying: true,
            result: {
              unable_reasons: ['fx_evidence_missing'],
              buckets: [{
                dimensions: { product_type: 'equity', market_regime: 'uptrend' },
                metrics: { sample_count: 12, win_rate_pct: 58.3 },
              }],
            },
            run_hash: 'b'.repeat(64),
            created_at: '2026-07-31T08:00:00',
          },
          manifest_hash: 'c'.repeat(64),
          created_at: '2026-07-31T07:00:00',
        }],
      },
    });

    const response = await strategyValidationApi.listStrategies();

    expect(get).toHaveBeenCalledWith('/api/v1/strategy-validation/strategies');
    expect(response[0].strategyKey).toBe('portfolio-hold-baseline');
    expect(response[0].latestRun?.engineVersion).toBe('portfolio-strategy-v1');
    expect(response[0].latestRun?.result.unableReasons).toEqual(['fx_evidence_missing']);
    expect(response[0].latestRun?.result.buckets[0].dimensions.productType).toBe('equity');
    expect(response[0].latestRun?.result.buckets[0].metrics.winRatePct).toBe(58.3);
  });

  it('sends only the explicit human transition fields', async () => {
    post.mockResolvedValueOnce({
      data: {
        strategy_key: 'portfolio-hold-baseline',
        version: '1.0.0',
        from_status: 'draft',
        status: 'backtest_running',
        status_label: '回测中',
        human_reason: '人工开始回测',
        transition_id: 1,
      },
    });

    await strategyValidationApi.transition(
      'portfolio-hold-baseline',
      '1.0.0',
      { toStatus: 'backtest_running', humanReason: '人工开始回测' },
    );

    expect(post).toHaveBeenCalledWith(
      '/api/v1/strategy-validation/strategies/portfolio-hold-baseline/versions/1.0.0/transition',
      { to_status: 'backtest_running', human_reason: '人工开始回测' },
    );
  });
});
