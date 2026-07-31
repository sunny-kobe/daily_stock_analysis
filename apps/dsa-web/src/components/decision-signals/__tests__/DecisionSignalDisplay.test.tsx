import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import type { DecisionSignalItem } from '../../../types/decisionSignals';
import { DecisionSignalCard, DecisionSignalDetails, PortfolioSignalSummary } from '../DecisionSignalDisplay';

const signal: DecisionSignalItem = {
  id: 7,
  stockCode: '600519',
  stockName: '贵州茅台',
  market: 'cn',
  sourceType: 'analysis',
  sourceReportId: 3001,
  decisionProfile: 'aggressive',
  marketPhase: 'intraday',
  triggerSource: 'web',
  action: 'hold',
  actionLabel: null,
  confidence: 0.72,
  score: 82,
  horizon: '3d',
  entryLow: 1600,
  entryHigh: 1620,
  stopLoss: 1550,
  targetPrice: 1700,
  invalidation: '跌破 1550',
  watchConditions: '观察成交量',
  reason: '趋势保持',
  riskSummary: '放量下跌风险',
  catalystSummary: '业绩窗口',
  evidence: { technical: 'ma' },
  dataQualitySummary: { freshness: 'ok' },
  planQuality: 'complete',
  status: 'active',
  expiresAt: '2026-06-18T09:30:00',
  createdAt: '2026-06-17T09:30:00',
  updatedAt: '2026-06-17T09:30:00',
  metadata: { source: 'test', decision_profile: 'balanced' },
};

function renderCard(onSelect?: (item: DecisionSignalItem) => void) {
  window.localStorage.setItem('dsa.uiLanguage', 'zh');
  render(
    <UiLanguageProvider>
      <DecisionSignalCard item={signal} onSelect={onSelect} />
    </UiLanguageProvider>,
  );
}

describe('DecisionSignalCard', () => {
  it('uses a dedicated details button for interactive cards', () => {
    const onSelect = vi.fn();
    renderCard(onSelect);

    expect(screen.getByText('贵州茅台').closest('button')).toBeNull();
    expect(screen.getByText('72%')).toBeInTheDocument();
    expect(screen.getByText('风格: 进取')).toBeInTheDocument();
    expect(screen.getByText('1600 - 1620')).toBeInTheDocument();
    expect(screen.getByText('业绩窗口')).toBeInTheDocument();
    expect(screen.getByText('跌破 1550')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '查看 贵州茅台 AI 建议详情' }));

    expect(onSelect).toHaveBeenCalledWith(signal);
    expect(screen.getByText('3 日')).toBeInTheDocument();
    expect(screen.getByText('计划质量: 完整')).toBeInTheDocument();
    expect(screen.getByText('阶段: 盘中')).toBeInTheDocument();
    expect(screen.queryByText('3d')).not.toBeInTheDocument();
    expect(screen.queryByText('complete')).not.toBeInTheDocument();
    expect(screen.queryByText('intraday')).not.toBeInTheDocument();
  });

  it('renders non-interactive cards without a details button', () => {
    renderCard();

    expect(screen.getByText('贵州茅台')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '查看 贵州茅台 AI 建议详情' })).not.toBeInTheDocument();
  });

  it('hides missing optional plan text for sparse legacy signals', () => {
    window.localStorage.setItem('dsa.uiLanguage', 'zh');
    render(
      <UiLanguageProvider>
        <DecisionSignalCard
          item={{
            ...signal,
            score: null,
            confidence: null,
            horizon: null,
            entryLow: null,
            entryHigh: null,
            stopLoss: null,
            targetPrice: null,
            invalidation: null,
            watchConditions: null,
            catalystSummary: null,
          }}
        />
      </UiLanguageProvider>,
    );

    expect(screen.getByText('评分')).toBeInTheDocument();
    expect(screen.getByText('置信度')).toBeInTheDocument();
    expect(screen.getByText('周期')).toBeInTheDocument();
    expect(screen.getAllByText('-').length).toBeGreaterThanOrEqual(3);
    expect(screen.queryByText('入场区间')).not.toBeInTheDocument();
    expect(screen.queryByText('止损')).not.toBeInTheDocument();
    expect(screen.queryByText('目标价')).not.toBeInTheDocument();
    expect(screen.queryByText('催化')).not.toBeInTheDocument();
    expect(screen.queryByText('失效条件')).not.toBeInTheDocument();
  });

  it('renders an executable 100-share plan from opaque metadata', () => {
    window.localStorage.setItem('dsa.uiLanguage', 'zh');
    render(
      <UiLanguageProvider>
        <DecisionSignalCard
          item={{
            ...signal,
            action: 'sell',
            actionLabel: '卖出',
            metadata: {
              execution_status: 'executable',
              position_quantity: 100,
              suggested_trade_quantity: 100,
              remaining_quantity: 0,
              trade_lot_size: 100,
              execution_blockers: [],
            },
          }}
        />
      </UiLanguageProvider>,
    );

    expect(screen.getByText('可执行')).toBeInTheDocument();
    expect(screen.getByText('建议交易 100 股')).toBeInTheDocument();
    expect(screen.getByText('执行后剩余 0 股')).toBeInTheDocument();
    expect(screen.getByText('最小单位 100 股')).toBeInTheDocument();
  });

  it('renders a blocked plan without presenting a sell quantity', () => {
    window.localStorage.setItem('dsa.uiLanguage', 'zh');
    render(
      <UiLanguageProvider>
        <DecisionSignalCard
          item={{
            ...signal,
            action: 'alert',
            actionLabel: '预警',
            metadata: {
              execution_status: 'blocked',
              model_action: 'sell',
              position_quantity: 100,
              suggested_trade_quantity: 0,
              remaining_quantity: 100,
              trade_lot_size: 100,
              execution_blockers: ['portfolio_price_stale', 'underlying_get_stock_info_missing'],
            },
          }}
        />
      </UiLanguageProvider>,
    );

    expect(screen.getByText('暂不执行')).toBeInTheDocument();
    expect(screen.getByText('建议交易 0 股')).toBeInTheDocument();
    expect(screen.getByText('执行后剩余 100 股')).toBeInTheDocument();
    expect(screen.getByText(/portfolio_price_stale/)).toBeInTheDocument();
    expect(screen.queryByText('建议交易 100 股')).not.toBeInTheDocument();
  });
});

describe('DecisionSignalDetails', () => {
  it('renders the deterministic execution plan in signal details', () => {
    window.localStorage.setItem('dsa.uiLanguage', 'zh');
    render(
      <UiLanguageProvider>
        <DecisionSignalDetails
          item={{
            ...signal,
            metadata: {
              execution_status: 'executable',
              position_quantity: 100,
              suggested_trade_quantity: 100,
              remaining_quantity: 0,
              trade_lot_size: 100,
              execution_blockers: [],
            },
          }}
        />
      </UiLanguageProvider>,
    );

    expect(screen.getByText('执行计划')).toBeInTheDocument();
    expect(screen.getByText('建议交易数量').closest('div')).toHaveTextContent('100 股');
    expect(screen.getByText('执行后剩余').closest('div')).toHaveTextContent('0 股');
    expect(screen.getByText('最小交易单位').closest('div')).toHaveTextContent('100 股');
  });

  it('renders secondary-only entry_high as a valid entry range', () => {
    window.localStorage.setItem('dsa.uiLanguage', 'zh');
    render(
      <UiLanguageProvider>
        <DecisionSignalDetails item={{ ...signal, entryLow: null, entryHigh: 1680 }} />
      </UiLanguageProvider>,
    );

    const entryRange = screen.getByText('入场区间').closest('div');
    expect(entryRange).not.toBeNull();
    expect(entryRange as HTMLElement).toHaveTextContent('1680');
    expect(screen.getByText('3 日')).toBeInTheDocument();
    expect(screen.getByText('完整')).toBeInTheDocument();
    expect(screen.getByText('盘中')).toBeInTheDocument();
    expect(screen.getByText('风格')).toBeInTheDocument();
    expect(screen.getAllByText('进取').length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText('3d')).not.toBeInTheDocument();
  });

  it('renders explicit null profile as unknown on card and details', () => {
    window.localStorage.setItem('dsa.uiLanguage', 'zh');
    render(
      <UiLanguageProvider>
        <>
          <DecisionSignalCard item={{ ...signal, decisionProfile: null, metadata: { decision_profile: 'balanced' } }} />
          <DecisionSignalDetails item={{ ...signal, decisionProfile: null, metadata: { decision_profile: 'balanced' } }} />
        </>
      </UiLanguageProvider>,
    );

    expect(screen.getAllByText('风格: 未知').length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText('风格').closest('div')).toHaveTextContent('未知');
    expect(screen.queryByText('均衡')).not.toBeInTheDocument();
  });

  it('renders opaque JSON fields without creating html nodes from their string values', () => {
    window.localStorage.setItem('dsa.uiLanguage', 'zh');
    const { container } = render(
      <UiLanguageProvider>
        <DecisionSignalDetails
          item={{
            ...signal,
            evidence: { headline: '<img src=x onerror="window.__signalEvidenceXss = true">' },
            dataQualitySummary: { note: '<script>window.__signalQualityXss = true</script>' },
            metadata: { raw: '<svg onload="window.__signalMetadataXss = true"></svg>' },
          }}
        />
      </UiLanguageProvider>,
    );

    expect(container.textContent).toContain('<img src=x onerror=\\"window.__signalEvidenceXss = true\\">');
    expect(container.textContent).toContain('<script>window.__signalQualityXss = true</script>');
    expect(container.textContent).toContain('<svg onload=\\"window.__signalMetadataXss = true\\"></svg>');
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('svg')).toBeNull();
    expect(container.querySelector('[onerror]')).toBeNull();
    expect(container.querySelector('[onload]')).toBeNull();
  });

  it('renders outcome results and feedback controls', () => {
    const onFeedbackSubmit = vi.fn();
    window.localStorage.setItem('dsa.uiLanguage', 'zh');
    render(
      <UiLanguageProvider>
        <DecisionSignalDetails
          item={signal}
          outcomes={[
            {
              id: 31,
              signalId: 7,
              horizon: '3d',
              engineVersion: 'decision-signal-v1',
              evalStatus: 'completed',
              outcome: 'hit',
              directionExpected: 'not_down',
              directionCorrect: true,
              anchorDate: '2024-01-02',
              evalWindowDays: 3,
              startPrice: 100,
              endClose: 105,
              stockReturnPct: 5,
              action: 'hold',
              market: 'cn',
              planQuality: 'complete',
              dataQualityLevel: 'good',
              holdingState: 'holding',
            },
          ]}
          feedback={{
            signalId: 7,
            feedbackValue: 'useful',
            reasonCode: null,
            note: null,
            source: 'web',
          }}
          onFeedbackSubmit={onFeedbackSubmit}
        />
      </UiLanguageProvider>,
    );

    expect(screen.getByText('后验结果')).toBeInTheDocument();
    expect(screen.getAllByText('3 日').length).toBeGreaterThan(1);
    expect(screen.getByText('命中')).toBeInTheDocument();
    expect(screen.getByText('5%')).toBeInTheDocument();
    expect(screen.getByText('催化')).toBeInTheDocument();
    expect(screen.getByText('业绩窗口')).toBeInTheDocument();
    expect(screen.getByText('失效条件')).toBeInTheDocument();
    expect(screen.getByText('跌破 1550')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '无用' }));
    expect(onFeedbackSubmit).toHaveBeenCalledWith('not_useful');
  });

  it('renders portfolio signal horizon using the current UI language', () => {
    window.localStorage.setItem('dsa.uiLanguage', 'en');
    render(
      <UiLanguageProvider>
        <PortfolioSignalSummary item={{ ...signal, horizon: '10d', action: 'sell', actionLabel: null }} />
      </UiLanguageProvider>,
    );

    expect(screen.getByText('10 days')).toBeInTheDocument();
    expect(screen.queryByText('10d')).not.toBeInTheDocument();
  });
});
