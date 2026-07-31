import { beforeEach, describe, expect, it, vi } from 'vitest';
import { portfolioApi } from '../portfolio';

const { post } = vi.hoisted(() => ({
  post: vi.fn(),
}));

vi.mock('../index', () => ({
  default: {
    get: vi.fn(),
    post,
  },
}));

describe('portfolioApi research workflow', () => {
  beforeEach(() => {
    post.mockReset();
  });

  it('prepares research evidence through the dedicated endpoint', async () => {
    post.mockResolvedValueOnce({
      data: {
        schema_version: 'portfolio-research-evidence-prepare-v1',
        prepared_at: '2026-07-31T08:00:00Z',
        as_of: '2026-07-31',
        status: 'ready',
        position_count: 2,
        ready_count: 2,
        insufficient_count: 0,
        items: [],
      },
    });

    const result = await portfolioApi.prepareResearchEvidence();

    expect(post).toHaveBeenCalledWith('/api/v1/portfolio/research-evidence/prepare');
    expect(result.positionCount).toBe(2);
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

    const result = await portfolioApi.buildResearchBaseline({
      researchSnapshotHash: 'a'.repeat(64),
      researchCutoff: '2026-07-31T08:00:00Z',
    });

    expect(post).toHaveBeenCalledWith('/api/v1/portfolio/research-baseline', {
      research_snapshot_hash: 'a'.repeat(64),
      research_cutoff: '2026-07-31T08:00:00Z',
    });
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
    });

    expect(post).toHaveBeenCalledWith('/api/v1/portfolio/positions/AAPL/analysis', {
      account_id: 2,
      analysis_phase: 'intraday',
      force: false,
      research_snapshot_hash: 'b'.repeat(64),
      research_cutoff: '2026-07-31T08:00:00Z',
    });
  });
});
