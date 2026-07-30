import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { decisionSignalsApi } from '../../../api/decisionSignals';
import { PortfolioDecisionReview } from '../PortfolioDecisionReview';
import type { DecisionQualityDetail } from '../../../types/decisionSignals';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';

vi.mock('../../../api/decisionSignals', () => ({
  decisionSignalsApi: {
    getQuality: vi.fn(),
    getQualityWeeklyReview: vi.fn(),
    putShadowFeedback: vi.fn(),
  },
}));

const quality: DecisionQualityDetail = {
  context: {
    signalId: 13,
    positionAction: 'hold',
    incrementalAction: 'wait',
    benchmark: { market: 'us', code: 'SPY', type: 'market_index' },
    contextStatus: 'insufficient_evidence',
    unableReasons: ['benchmark_evidence_stale'],
  },
  outcomes: [
    { horizon: '5d', evalStatus: 'complete', maturity: 'mature', excessReturnPct: 2.5, maxFavorableExcursionPct: 6, maxAdverseExcursionPct: -3, unableReasons: [] },
    { horizon: '20d', evalStatus: 'pending', maturity: 'pending', unableReasons: ['horizon_not_mature'] },
    { horizon: '60d', evalStatus: 'unable', maturity: 'unable', unableReasons: ['corporate_action_adjustment_unknown'] },
  ],
  attributions: [],
};

describe('PortfolioDecisionReview', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.removeItem('dsa.uiLanguage');
    vi.mocked(decisionSignalsApi.getQuality).mockResolvedValue(quality);
    vi.mocked(decisionSignalsApi.getQualityWeeklyReview).mockResolvedValue({
      materialDecisionCount: 1,
      decisions: [],
      aiHumanDisagreements: [],
      confirmedAttributionCounts: { timing_error: 1 },
      candidatePatterns: [{ category: 'timing_error', horizon: '5d', instrumentType: 'equity', status: 'observed', eligibleSampleCount: 1, counterexamples: ['20d 未成熟'] }],
      automaticRulesActivated: false,
    });
    vi.mocked(decisionSignalsApi.putShadowFeedback).mockResolvedValue({ signalId: 13, humanDecision: 'accept' });
  });

  it('shows both axes, blockers, benchmark, and 5/20/60 outcome states', async () => {
    render(<PortfolioDecisionReview signalId={13} />);
    expect(await screen.findByText('当前持仓: 持有')).toBeInTheDocument();
    expect(screen.getByText('新增资金: 等待')).toBeInTheDocument();
    expect(screen.getByText(/benchmark_evidence_stale/)).toBeInTheDocument();
    expect(screen.getByText(/SPY/)).toBeInTheDocument();
    expect(screen.getByText('5d · mature')).toBeInTheDocument();
    expect(screen.getByText('20d · pending')).toBeInTheDocument();
    expect(screen.getByText('60d · unable')).toBeInTheDocument();
    expect(screen.getByText(/2.50%/)).toBeInTheDocument();
    expect(screen.queryByText(/下单|买入数量|卖出数量/)).not.toBeInTheDocument();
    expect(await screen.findByText(/timing_error · 5d · equity/)).toBeInTheDocument();
    expect(screen.getByText(/样本 1 · observed/)).toBeInTheDocument();
  });

  it('requires a reason before modify or veto feedback', async () => {
    render(<PortfolioDecisionReview signalId={13} />);
    await screen.findByText('当前持仓: 持有');
    fireEvent.click(screen.getByRole('button', { name: '修改' }));
    fireEvent.click(screen.getByRole('button', { name: '提交修改' }));
    expect(screen.getByText('请填写修改或否决原因')).toBeInTheDocument();
    expect(decisionSignalsApi.putShadowFeedback).not.toHaveBeenCalled();
  });

  it('submits the two human-selected axes for modify feedback', async () => {
    render(<PortfolioDecisionReview signalId={13} />);
    await screen.findByText('当前持仓: 持有');
    fireEvent.click(screen.getByRole('button', { name: '修改' }));
    fireEvent.change(screen.getByRole('combobox', { name: '修改后的当前持仓' }), { target: { value: 'reduce' } });
    fireEvent.change(screen.getByRole('combobox', { name: '修改后的新增资金' }), { target: { value: 'no_add' } });
    fireEvent.change(screen.getByRole('textbox', { name: '修改或否决原因' }), { target: { value: '风险收益不匹配' } });
    fireEvent.click(screen.getByRole('button', { name: '提交修改' }));

    await waitFor(() => expect(decisionSignalsApi.putShadowFeedback).toHaveBeenCalledWith(13, {
      humanDecision: 'modify',
      humanPositionAction: 'reduce',
      humanIncrementalAction: 'no_add',
      decisionReasonCode: '风险收益不匹配',
    }));
  });

  it('shows visible success and failure states after saving feedback', async () => {
    const { rerender } = render(<PortfolioDecisionReview signalId={13} />);
    await screen.findByText('当前持仓: 持有');
    fireEvent.click(screen.getByRole('button', { name: '接受' }));
    expect(await screen.findByText('人工反馈已保存')).toBeInTheDocument();

    vi.mocked(decisionSignalsApi.putShadowFeedback).mockRejectedValueOnce(new Error('反馈保存失败'));
    rerender(<PortfolioDecisionReview signalId={14} />);
    await screen.findByText('当前持仓: 持有');
    fireEvent.click(screen.getByRole('button', { name: '暂不行动' }));
    expect(await screen.findByText('反馈保存失败')).toBeInTheDocument();
  });

  it('uses the active UI language for the complete review surface', async () => {
    window.localStorage.setItem('dsa.uiLanguage', 'en');
    render(<UiLanguageProvider><PortfolioDecisionReview signalId={13} /></UiLanguageProvider>);

    expect(await screen.findByRole('heading', { name: 'Portfolio decision review' })).toBeInTheDocument();
    expect(screen.getByText('Existing position: Hold')).toBeInTheDocument();
    expect(screen.getByText('Incremental capital: Wait')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Modify' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Weekly review case' })).toBeInTheDocument();
    expect(screen.getByText('Candidate patterns never modify scoring, prompts, or risk policy automatically.')).toBeInTheDocument();
  });

  it('ignores stale quality responses when switching signals', async () => {
    let resolveFirst!: (value: typeof quality) => void;
    vi.mocked(decisionSignalsApi.getQuality)
      .mockReturnValueOnce(new Promise((resolve) => { resolveFirst = resolve; }))
      .mockResolvedValueOnce({ ...quality, context: { ...quality.context, signalId: 14, positionAction: 'exit' } });
    const { rerender } = render(<PortfolioDecisionReview signalId={13} />);
    rerender(<PortfolioDecisionReview signalId={14} />);
    expect(await screen.findByText('当前持仓: 退出')).toBeInTheDocument();
    resolveFirst(quality);
    await waitFor(() => expect(screen.queryByText('当前持仓: 持有')).not.toBeInTheDocument());
  });
});
