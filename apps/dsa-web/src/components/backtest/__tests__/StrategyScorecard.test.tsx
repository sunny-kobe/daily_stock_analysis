import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import type { StrategyVersion } from '../../../types/strategyValidation';
import { UI_LANGUAGE_STORAGE_KEY } from '../../../utils/uiLanguage';
import { StrategyScorecard } from '../StrategyScorecard';

const strategy: StrategyVersion = {
  strategyKey: 'portfolio-hold-baseline',
  version: '1.0.0',
  name: '持有比较基线',
  changeSummary: '固定持有作为比较基准',
  changedDimension: 'baseline',
  markets: ['cn'],
  instrumentTypes: ['equity'],
  horizons: ['5d'],
  evaluationMode: 'historical_and_forward',
  policy: {},
  costModel: {},
  benchmarkPolicy: {},
  status: 'backtest_running',
  statusLabel: '回测中',
  allowedTransitions: ['simulation'],
  manifestHash: 'a'.repeat(64),
  createdAt: '2026-07-31T08:00:00',
  latestRun: {
    runId: 'svr-1',
    strategyKey: 'portfolio-hold-baseline',
    strategyVersion: '1.0.0',
    validationKind: 'historical_backtest',
    protocol: {},
    datasetHash: 'b'.repeat(64),
    engineVersion: 'portfolio-strategy-v1',
    status: 'completed',
    statusLabel: '已完成',
    qualifying: true,
    result: {
      historicalStatus: 'complete',
      eligibleEventCount: 12,
      evaluationCount: 12,
      unableReasons: ['fx_evidence_missing'],
      buckets: [{
        dimensions: {
          horizon: '5d',
          market: 'cn',
          productType: 'equity',
          instruction: 'hold',
          marketRegime: 'downtrend',
          period: 'validation',
        },
        metrics: {
          sampleCount: 12,
          winRatePct: 58.3,
          winDefinition: '持有期间扣除成本后的收益大于 0',
          netReturnAfterCostPct: 4.2,
          benchmarkExcessPct: 1.1,
          maximumDrawdownPct: -8.4,
          averageGainPct: 6.3,
          averageLossPct: -3.1,
          turnoverPct: 0,
          totalCostPct: 0.2,
          unableCount: 1,
        },
      }],
      resultHash: 'c'.repeat(64),
    },
    runHash: 'd'.repeat(64),
    createdAt: '2026-07-31T09:00:00',
  },
};

function renderScorecard(
  strategyOverrides: Partial<StrategyVersion> = {},
  onTransition = vi.fn(),
) {
  window.localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'zh');
  render(
    <UiLanguageProvider>
      <StrategyScorecard strategy={{ ...strategy, ...strategyOverrides }} onTransition={onTransition} />
    </UiLanguageProvider>,
  );
  return onTransition;
}

describe('StrategyScorecard', () => {
  it('shows the strategy change, stage, separated performance, and visible blockers', () => {
    renderScorecard();

    expect(screen.getByText('持有比较基线')).toBeInTheDocument();
    expect(screen.getByText('v1.0.0')).toBeInTheDocument();
    expect(screen.getByText('固定持有作为比较基准')).toBeInTheDocument();
    expect(screen.getByText('回测中')).toBeInTheDocument();
    expect(screen.getByText('12')).toBeInTheDocument();
    expect(screen.getByText('58.3%')).toBeInTheDocument();
    expect(screen.getByText('4.2%')).toBeInTheDocument();
    expect(screen.getByText('1.1%')).toBeInTheDocument();
    expect(screen.getByText('-8.4%')).toBeInTheDocument();
    expect(screen.getByText('6.3% / -3.1%')).toBeInTheDocument();
    expect(screen.getByText('0.2%')).toBeInTheDocument();
    expect(screen.getByText('验证期 · 下跌趋势')).toBeInTheDocument();
    expect(screen.getByText('资料不足：fx_evidence_missing')).toBeInTheDocument();
    expect(screen.queryByText('champion')).not.toBeInTheDocument();
    expect(screen.queryByText('challenger')).not.toBeInTheDocument();
    expect(screen.queryByText('position_action')).not.toBeInTheDocument();
    expect(screen.queryByText('incremental_action')).not.toBeInTheDocument();
  });

  it('requires a human reason before changing stage', () => {
    const onTransition = vi.fn();
    renderScorecard({}, onTransition);

    fireEvent.click(screen.getByRole('button', { name: '进入模拟观察' }));
    expect(onTransition).not.toHaveBeenCalled();
    expect(screen.getByText('请填写人工判断理由')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('人工判断理由'), {
      target: { value: '回测样本合格，进入模拟观察' },
    });
    fireEvent.click(screen.getByRole('button', { name: '进入模拟观察' }));
    expect(onTransition).toHaveBeenCalledWith('simulation', '回测样本合格，进入模拟观察');
  });

  it('calls the forward-only simulation entry a start instead of a completed backtest', () => {
    const onTransition = renderScorecard({
      evaluationMode: 'forward_only',
      status: 'draft',
      allowedTransitions: ['simulation'],
      latestRun: {
        ...strategy.latestRun!,
        qualifying: false,
        result: {
          ...strategy.latestRun!.result,
          historicalStatus: 'not_available',
          displayMessage: '历史回测不可用，等待模拟样本',
        },
      },
    });

    fireEvent.click(screen.getByRole('button', { name: '开始模拟观察' }));
    expect(onTransition).not.toHaveBeenCalled();
    expect(screen.getByText('请填写人工判断理由')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('人工判断理由'), {
      target: { value: '从今天开始记录模拟建议' },
    });
    fireEvent.click(screen.getByRole('button', { name: '开始模拟观察' }));
    expect(onTransition).toHaveBeenCalledWith('simulation', '从今天开始记录模拟建议');
  });
});
