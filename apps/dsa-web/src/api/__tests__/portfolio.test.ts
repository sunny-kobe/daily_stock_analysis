import { beforeEach, describe, expect, it, vi } from 'vitest';
import { portfolioApi } from '../portfolio';

const { get, post } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock('../index', () => ({
  default: {
    get,
    post,
  },
}));

describe('portfolioApi research workflow', () => {
  beforeEach(() => {
    post.mockReset();
    get.mockReset();
  });

  it('prepares research evidence through the dedicated endpoint', async () => {
    post.mockResolvedValueOnce({
      data: {
        schema_version: 'portfolio-research-evidence-prepare-v2',
        scope: [{ account_id: 2, market: 'us', symbol: 'AAPL' }],
        prepared_at: '2026-07-31T08:00:00Z',
        cutoff: '2026-07-31T08:00:00Z',
        as_of: '2026-07-31',
        status: 'ready',
        position_count: 2,
        ready_count: 2,
        insufficient_count: 0,
        items: [],
      },
    });

    const scope = [{ accountId: 2, market: 'us' as const, symbol: 'AAPL' }];
    const result = await portfolioApi.prepareResearchEvidence(scope, '2026-07-31T08:00:00Z');

    expect(post).toHaveBeenCalledWith(
      '/api/v1/portfolio/research-evidence/prepare',
      {
        research_cutoff: '2026-07-31T08:00:00Z',
        scope: [{ account_id: 2, market: 'us', symbol: 'AAPL' }],
      },
      { timeout: 300_000 },
    );
    expect(result.positionCount).toBe(2);
  });

  it('uses the explicit cutoff when freezing a scoped research snapshot', async () => {
    get.mockResolvedValueOnce({
      data: {
        snapshot_hash: 'a'.repeat(64),
        cutoff: '2026-07-31T08:00:00Z',
        scope: [{ account_id: 2, market: 'us', symbol: 'AAPL' }],
        completeness: 'COMPLETE',
        positions: [],
        instruments: [],
        point_in_time: {},
        decision_signals: [],
        hard_blockers: [],
        limitations: [],
      },
    });
    const scope = [{ accountId: 2, market: 'us' as const, symbol: 'AAPL' }];

    await portfolioApi.getResearchSnapshot(scope, '2026-07-31T08:00:00Z');

    expect(get).toHaveBeenCalledWith('/api/v1/portfolio/research-snapshot', {
      params: {
        cutoff: '2026-07-31T08:00:00Z',
        scope: ['2:us:AAPL'],
      },
      paramsSerializer: {
        serialize: expect.any(Function),
      },
    });
    const snapshotConfig = get.mock.calls[0][1] as {
      params: Record<string, unknown>;
      paramsSerializer: { serialize: (params: Record<string, unknown>) => string };
    };
    expect(snapshotConfig.paramsSerializer.serialize({
      cutoff: '2026-07-31T08:00:00Z',
      scope: ['2:us:AAPL', '3:cn:510980'],
    })).toBe(
      'cutoff=2026-07-31T08%3A00%3A00Z&scope=2%3Aus%3AAAPL&scope=3%3Acn%3A510980',
    );
  });

  it('keeps the research snapshot scope optional for full-portfolio callers', async () => {
    get.mockResolvedValueOnce({
      data: {
        snapshot_hash: 'a'.repeat(64),
        cutoff: '2026-07-31T08:00:00Z',
        scope: [],
        scope_hash: 'b'.repeat(64),
        completeness: 'COMPLETE',
        positions: [],
        instruments: [],
        point_in_time: {},
        decision_signals: [],
        hard_blockers: [],
        limitations: [],
      },
    });

    await portfolioApi.getResearchSnapshot();

    expect(get).toHaveBeenCalledWith('/api/v1/portfolio/research-snapshot');
  });

  it('binds the baseline request to the frozen snapshot', async () => {
    post.mockResolvedValueOnce({
      data: {
        schema_version: 'portfolio-research-baseline-v1',
        snapshot_hash: 'a'.repeat(64),
        cutoff: '2026-07-31T08:00:00Z',
        market_data_cutoff: '2026-07-31T08:00:00Z',
        ledger_position_count: 2,
        baseline_row_count: 2,
        coverage_reconciled: true,
        portfolio_risk_flags: [],
        items: [],
        suggested_deep_analysis: [],
        deep_analysis_started: false,
      },
    });

    const researchScope = [{ accountId: 2, market: 'us' as const, symbol: 'AAPL' }];
    const result = await portfolioApi.buildResearchBaseline({
      researchSnapshotHash: 'a'.repeat(64),
      researchCutoff: '2026-07-31T08:00:00Z',
      researchScope,
    });

    expect(post).toHaveBeenCalledWith(
      '/api/v1/portfolio/research-baseline',
      {
        research_snapshot_hash: 'a'.repeat(64),
        research_cutoff: '2026-07-31T08:00:00Z',
        research_scope: [{ account_id: 2, market: 'us', symbol: 'AAPL' }],
      },
      { timeout: 300_000 },
    );
    expect(result.coverageReconciled).toBe(true);
  });

  it('binds detailed position analysis to the same frozen snapshot', async () => {
    post.mockResolvedValueOnce({
      data: {
        status: 'accepted',
        task_id: 'task-1',
        message: 'accepted',
      },
    });

    await portfolioApi.analyzePosition('AAPL', {
      accountId: 2,
      analysisPhase: 'intraday',
      force: false,
      researchSnapshotHash: 'b'.repeat(64),
      researchCutoff: '2026-07-31T08:00:00Z',
      researchScope: [{ accountId: 2, market: 'us', symbol: 'AAPL' }],
    });

    expect(post).toHaveBeenCalledWith('/api/v1/portfolio/positions/AAPL/analysis', {
      account_id: 2,
      analysis_phase: 'intraday',
      force: false,
      research_snapshot_hash: 'b'.repeat(64),
      research_cutoff: '2026-07-31T08:00:00Z',
      research_scope: [{ account_id: 2, market: 'us', symbol: 'AAPL' }],
    });
  });

  it('checks execution evidence against the same frozen scope', async () => {
    post.mockResolvedValueOnce({
      data: {
        schema_version: 'portfolio-research-execution-check-v1',
        checked_at: '2026-07-31T06:45:00Z',
        research_snapshot_hash: 'a'.repeat(64),
        scope: [{ account_id: 2, market: 'us', symbol: 'AAPL' }],
        status: 'ready',
        requires_reconfirmation: false,
        items: [],
      },
    });
    const researchScope = [{ accountId: 2, market: 'us' as const, symbol: 'AAPL' }];

    await portfolioApi.checkResearchExecution({
      researchSnapshotHash: 'a'.repeat(64),
      researchExecutionIdentityHash: 'e'.repeat(64),
      researchCutoff: '2026-07-31T08:00:00Z',
      researchScope,
    });

    expect(post).toHaveBeenCalledWith(
      '/api/v1/portfolio/research-execution-check',
      {
        research_snapshot_hash: 'a'.repeat(64),
        research_execution_identity_hash: 'e'.repeat(64),
        research_cutoff: '2026-07-31T08:00:00Z',
        research_scope: [{ account_id: 2, market: 'us', symbol: 'AAPL' }],
      },
      { timeout: 300_000 },
    );
  });
});
