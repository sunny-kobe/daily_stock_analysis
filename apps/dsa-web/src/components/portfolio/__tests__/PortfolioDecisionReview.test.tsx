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
    userInstruction: 'insufficient',
    benchmark: { market: 'us', code: 'SPY', type: 'market_index' },
    contextStatus: 'insufficient_evidence',
    unableReasons: ['benchmark_evidence_stale'],
  },
  evidenceSnapshot: {
    signalId: 13,
    status: 'complete',
    displayStatus: '已保存',
    strategyKey: 'portfolio-current-policy',
    strategyVersion: '1.0.0',
    strategyName: '当前持仓策略',
    unableReasons: [],
    createdAt: '2026-07-31T08:00:00',
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

  it('loads 5/20/60 outcomes and weekly patterns only after history review is expanded', async () => {
    render(<PortfolioDecisionReview signalId={13} />);
    expect(await screen.findByText('操作建议: 资料不足')).toBeInTheDocument();
    expect(screen.queryByText(/当前持仓:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/新增资金:/)).not.toBeInTheDocument();
    expect(screen.getByText('部分复盘资料暂不可用。')).toBeInTheDocument();
    expect(screen.getByText(/SPY/)).toBeInTheDocument();
    expect(screen.getByText('复盘资料: 已保存')).toBeInTheDocument();
    expect(screen.getByText('策略: 当前持仓策略 1.0.0')).toBeInTheDocument();
    expect(decisionSignalsApi.getQualityWeeklyReview).not.toHaveBeenCalled();
    expect(screen.queryByText('5日 · 可评价')).not.toBeInTheDocument();
    expect(screen.queryByText('20日 · 等待数据')).not.toBeInTheDocument();
    expect(screen.queryByText('60日 · 资料不足')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('历史效果复盘（5/20/60）'));
    expect(screen.getByText('5日 · 可评价')).toBeInTheDocument();
    expect(screen.getByText('20日 · 等待数据')).toBeInTheDocument();
    expect(screen.getByText('60日 · 资料不足')).toBeInTheDocument();
    expect(screen.getByText(/2.50%/)).toBeInTheDocument();
    expect(screen.getByText(/最高浮盈 6.00% · 最大回撤 -3.00%/)).toBeInTheDocument();
    expect(screen.queryByText(/下单|买入数量|卖出数量/)).not.toBeInTheDocument();
    expect(await screen.findByText('时机问题 · 5日 · 股票')).toBeInTheDocument();
    expect(decisionSignalsApi.getQualityWeeklyReview).toHaveBeenCalledTimes(1);
    expect(screen.getByText('样本 1 · 观察中')).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(
      /INSUFFICIENT_EVIDENCE|benchmark_evidence_stale|corporate_action_adjustment_unknown|complete|insufficient_evidence|mature|pending|unable|timing_error|equity|observed|decision_input_hash|[a-f0-9]{64}/i,
    );
  });

  it('shows a simple insufficient evidence message without internal reason codes', async () => {
    vi.mocked(decisionSignalsApi.getQuality).mockResolvedValueOnce({
      ...quality,
      evidenceSnapshot: {
        ...quality.evidenceSnapshot!,
        status: 'insufficient_evidence',
        displayStatus: '资料不足',
        unableReasons: ['benchmark_evidence_missing'],
      },
    });

    render(<PortfolioDecisionReview signalId={13} />);

    expect(await screen.findByText('复盘资料: 资料不足')).toBeInTheDocument();
    expect(screen.getByText('缺少关键复盘资料，本条建议不会计入有效策略成绩。')).toBeInTheDocument();
    expect(screen.queryByText('部分复盘资料暂不可用。')).not.toBeInTheDocument();
    expect(screen.queryByText('benchmark_evidence_missing')).not.toBeInTheDocument();
  });

  it('keeps an unable evaluation as insufficient even when maturity says mature', async () => {
    vi.mocked(decisionSignalsApi.getQuality).mockResolvedValueOnce({
      ...quality,
      context: { ...quality.context, unableReasons: [] },
      outcomes: [
        { horizon: '5d', evalStatus: 'unable', maturity: 'mature', unableReasons: ['price_missing'] },
      ],
    });

    render(<PortfolioDecisionReview signalId={13} />);

    await screen.findByText('操作建议: 资料不足');
    fireEvent.click(screen.getByText('历史效果复盘（5/20/60）'));
    expect(await screen.findByText('5日 · 资料不足')).toBeInTheDocument();
    expect(screen.queryByText('5日 · 可评价')).not.toBeInTheDocument();
    expect(screen.queryByText('price_missing')).not.toBeInTheDocument();
  });

  it.each([
    ['null', null],
    ['missing', undefined],
    ['non-string', { status: 'complete' }],
  ])('maps a %s evaluation status to insufficient evidence', async (_label, evalStatus) => {
    vi.mocked(decisionSignalsApi.getQuality).mockResolvedValueOnce({
      ...quality,
      context: { ...quality.context, unableReasons: [] },
      outcomes: [
        {
          horizon: '5d',
          evalStatus,
          maturity: 'mature',
          unableReasons: [],
        } as unknown as DecisionQualityDetail['outcomes'][number],
      ],
    });

    render(<PortfolioDecisionReview signalId={13} />);

    await screen.findByText('操作建议: 资料不足');
    fireEvent.click(screen.getByText('历史效果复盘（5/20/60）'));
    expect(await screen.findByText('5日 · 资料不足')).toBeInTheDocument();
  });

  it('requires a reason before modify or veto feedback', async () => {
    render(<PortfolioDecisionReview signalId={13} />);
    await screen.findByText('操作建议: 资料不足');
    expect(decisionSignalsApi.getQualityWeeklyReview).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '修改' }));
    fireEvent.click(screen.getByRole('button', { name: '提交修改' }));
    expect(screen.getByText('请填写修改或否决原因')).toBeInTheDocument();
    expect(decisionSignalsApi.putShadowFeedback).not.toHaveBeenCalled();
  });

  it('maps one human-selected instruction to the required internal axes', async () => {
    render(<PortfolioDecisionReview signalId={13} />);
    await screen.findByText('操作建议: 资料不足');
    fireEvent.click(screen.getByRole('button', { name: '修改' }));
    fireEvent.change(screen.getByRole('combobox', { name: '修改后的操作建议' }), { target: { value: 'reduce' } });
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
    await screen.findByText('操作建议: 资料不足');
    fireEvent.click(screen.getByRole('button', { name: '接受' }));
    expect(await screen.findByText('人工反馈已保存')).toBeInTheDocument();

    vi.mocked(decisionSignalsApi.putShadowFeedback).mockRejectedValueOnce(new Error('反馈保存失败'));
    rerender(<PortfolioDecisionReview signalId={14} />);
    await screen.findByText('操作建议: 资料不足');
    fireEvent.click(screen.getByRole('button', { name: '暂不行动' }));
    expect(await screen.findByText('反馈保存失败')).toBeInTheDocument();
  });

  it('uses the active UI language for the complete review surface', async () => {
    window.localStorage.setItem('dsa.uiLanguage', 'en');
    render(<UiLanguageProvider><PortfolioDecisionReview signalId={13} /></UiLanguageProvider>);

    expect(await screen.findByRole('heading', { name: 'Portfolio decision review' })).toBeInTheDocument();
    expect(screen.getByText('Instruction: Insufficient evidence')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Historical outcome review (5/20/60)'));
    expect(screen.getByText('5 days · Ready to evaluate')).toBeInTheDocument();
    expect(screen.getByText('20 days · Waiting for data')).toBeInTheDocument();
    expect(screen.getByText('60 days · Insufficient evidence')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Modify' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Weekly review record' })).toBeInTheDocument();
    expect(screen.getByText('Candidate patterns never modify scoring, prompts, or risk policy automatically.')).toBeInTheDocument();
  });

  it('ignores stale quality responses when switching signals', async () => {
    let resolveFirst!: (value: typeof quality) => void;
    vi.mocked(decisionSignalsApi.getQuality)
      .mockReturnValueOnce(new Promise((resolve) => { resolveFirst = resolve; }))
      .mockResolvedValueOnce({ ...quality, context: { ...quality.context, signalId: 14, positionAction: 'exit', userInstruction: 'exit' } });
    const { rerender } = render(<PortfolioDecisionReview signalId={13} />);
    rerender(<PortfolioDecisionReview signalId={14} />);
    expect(await screen.findByText('操作建议: 清仓')).toBeInTheDocument();
    resolveFirst(quality);
    await waitFor(() => expect(screen.queryByText('操作建议: 资料不足')).not.toBeInTheDocument());
  });
});
