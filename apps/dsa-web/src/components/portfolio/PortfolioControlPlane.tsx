import { useCallback, useEffect, useState } from 'react';
import { portfolioApi } from '../../api/portfolio';
import { getParsedApiError } from '../../api/error';
import type {
  PortfolioInstrumentInput,
  PortfolioInstrumentItem,
  PortfolioInstrumentType,
  PortfolioRiskPolicyInput,
  PortfolioVerificationStatus,
} from '../../types/portfolio';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { Card, InlineAlert } from '../common';

const INPUT = 'input-surface input-focus-glow h-10 w-full rounded-lg border bg-transparent px-3 text-sm focus:outline-none';
const MARKETS = ['cn', 'hk', 'us', 'jp', 'kr', 'tw'] as const;
const TYPES: PortfolioInstrumentType[] = ['equity', 'etf', 'qdii', 'adr_ads', 'daily_leveraged_product', 'unknown'];

function toLocalDateTimeInput(value?: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const localDate = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return localDate.toISOString().slice(0, 16);
}

function toUtcIso(value: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

type InstrumentForm = {
  symbol: string;
  market: typeof MARKETS[number];
  quoteCurrency: string;
  instrumentType: PortfolioInstrumentType;
  underlyingSymbol: string;
  underlyingMarket: typeof MARKETS[number];
  underlyingCurrency: string;
  leverageFactor: string;
  conversionRatio: string;
  tradeLotSize: string;
  requiresPremiumCheck: boolean;
  verificationStatus: PortfolioVerificationStatus;
  evidenceSource: string;
  evidenceAsOf: string;
};

const EMPTY_INSTRUMENT: InstrumentForm = {
  symbol: '', market: 'cn', quoteCurrency: 'CNY', instrumentType: 'equity',
  underlyingSymbol: '', underlyingMarket: 'us', underlyingCurrency: '',
  leverageFactor: '', conversionRatio: '', tradeLotSize: '1',
  requiresPremiumCheck: false, verificationStatus: 'missing', evidenceSource: '', evidenceAsOf: '',
};

const EMPTY_POLICY: Record<keyof PortfolioRiskPolicyInput, string> = {
  minCashBufferPct: '', maxSinglePositionPct: '', maxSectorPct: '',
  maxHighRiskProductPct: '', maxPortfolioDrawdownPct: '',
};

function toForm(item: PortfolioInstrumentItem): InstrumentForm {
  return {
    symbol: item.symbol, market: item.market, quoteCurrency: item.quoteCurrency,
    instrumentType: item.instrumentType, underlyingSymbol: item.underlyingSymbol || '',
    underlyingMarket: item.underlyingMarket || 'us', underlyingCurrency: item.underlyingCurrency || '',
    leverageFactor: item.leverageFactor?.toString() || '', conversionRatio: item.conversionRatio?.toString() || '',
    tradeLotSize: item.tradeLotSize.toString(), requiresPremiumCheck: item.requiresPremiumCheck,
    verificationStatus: item.verificationStatus, evidenceSource: item.evidenceSource || '',
    evidenceAsOf: toLocalDateTimeInput(item.evidenceAsOf),
  };
}

type PortfolioControlPlaneProps = {
  onInstrumentsLoaded?: (items: PortfolioInstrumentItem[]) => void;
};

export function PortfolioControlPlane({ onInstrumentsLoaded }: PortfolioControlPlaneProps) {
  const { t } = useUiLanguage();
  const [instruments, setInstruments] = useState<PortfolioInstrumentItem[]>([]);
  const [instrumentForm, setInstrumentForm] = useState<InstrumentForm>(EMPTY_INSTRUMENT);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [policyForm, setPolicyForm] = useState(EMPTY_POLICY);
  const [blockers, setBlockers] = useState<Array<{ code: string; symbol?: string; market?: string }>>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const [registry, policy, snapshot] = await Promise.all([
        portfolioApi.getInstruments(), portfolioApi.getRiskPolicy(), portfolioApi.getResearchSnapshot(),
      ]);
      const loadedInstruments = registry.items || [];
      setInstruments(loadedInstruments);
      onInstrumentsLoaded?.(loadedInstruments);
      setBlockers(snapshot.hardBlockers || []);
      if (policy.policy) {
        setPolicyForm({
          minCashBufferPct: String(policy.policy.minCashBufferPct),
          maxSinglePositionPct: String(policy.policy.maxSinglePositionPct),
          maxSectorPct: String(policy.policy.maxSectorPct),
          maxHighRiskProductPct: String(policy.policy.maxHighRiskProductPct),
          maxPortfolioDrawdownPct: String(policy.policy.maxPortfolioDrawdownPct),
        });
      }
    } catch (loadError) {
      setError(getParsedApiError(loadError).message);
    }
  }, [onInstrumentsLoaded]);

  useEffect(() => { void load(); }, [load]);

  const complex = instrumentForm.instrumentType === 'adr_ads' || instrumentForm.instrumentType === 'daily_leveraged_product';
  const leveraged = instrumentForm.instrumentType === 'daily_leveraged_product';

  const saveInstrument = async () => {
    const payload: PortfolioInstrumentInput = {
      symbol: instrumentForm.symbol.trim(), market: instrumentForm.market,
      quoteCurrency: instrumentForm.quoteCurrency.trim().toUpperCase(),
      instrumentType: instrumentForm.instrumentType,
      underlyingSymbol: complex ? instrumentForm.underlyingSymbol.trim() || null : null,
      underlyingMarket: complex ? instrumentForm.underlyingMarket : null,
      underlyingCurrency: complex ? instrumentForm.underlyingCurrency.trim().toUpperCase() || null : null,
      leverageFactor: leveraged && instrumentForm.leverageFactor ? Number(instrumentForm.leverageFactor) : null,
      dailyReset: leveraged,
      conversionRatio: instrumentForm.instrumentType === 'adr_ads' && instrumentForm.conversionRatio ? Number(instrumentForm.conversionRatio) : null,
      tradeLotSize: Number(instrumentForm.tradeLotSize),
      requiresPremiumCheck: ['qdii', 'adr_ads'].includes(instrumentForm.instrumentType) || instrumentForm.requiresPremiumCheck,
      verificationStatus: instrumentForm.verificationStatus,
      evidenceSource: instrumentForm.evidenceSource.trim() || null,
      evidenceAsOf: toUtcIso(instrumentForm.evidenceAsOf),
    };
    try {
      setSaving(true); setError(null); setMessage(null);
      if (editingKey) await portfolioApi.updateInstrument(payload.market, payload.symbol, payload);
      else await portfolioApi.createInstrument(payload);
      setInstrumentForm(EMPTY_INSTRUMENT); setEditingKey(null); setMessage(t('portfolio.controlPlane.identitySaved'));
      await load();
    } catch (saveError) {
      setError(getParsedApiError(saveError).message);
    } finally { setSaving(false); }
  };

  const savePolicy = async () => {
    const payload = Object.fromEntries(
      Object.entries(policyForm).map(([key, value]) => [key, Number(value)]),
    ) as unknown as PortfolioRiskPolicyInput;
    try {
      setSaving(true); setError(null); setMessage(null);
      await portfolioApi.saveRiskPolicy(payload);
      setMessage(t('portfolio.controlPlane.riskSaved'));
      await load();
    } catch (saveError) {
      setError(getParsedApiError(saveError).message);
    } finally { setSaving(false); }
  };

  return (
    <section className="grid grid-cols-1 xl:grid-cols-2 gap-3" aria-label={t('portfolio.controlPlane.aria')}>
      <Card padding="md">
        <div className="flex items-center justify-between gap-3">
          <h2 className="text-sm font-semibold text-foreground">{t('portfolio.controlPlane.identityTitle')}</h2>
          <span className="text-xs text-secondary">{instruments.filter((item) => item.verificationStatus === 'verified').length}/{instruments.length} verified</span>
        </div>
        <div className="mt-3 max-h-28 overflow-auto border-y border-white/10 py-1">
          {instruments.map((item) => (
            <button key={`${item.market}:${item.symbol}`} type="button" className="flex w-full items-center justify-between px-1 py-1.5 text-left text-xs hover:bg-white/5" onClick={() => { setInstrumentForm(toForm(item)); setEditingKey(`${item.market}:${item.symbol}`); }}>
              <span className="font-mono text-foreground">{item.symbol} · {item.market}</span>
              <span className={item.verificationStatus === 'verified' ? 'text-success' : 'text-warning'}>{item.instrumentType} / {item.verificationStatus}</span>
            </button>
          ))}
          {instruments.length === 0 ? <p className="py-2 text-xs text-secondary">{t('portfolio.controlPlane.emptyIdentity')}</p> : null}
        </div>
        <div className="mt-3 grid grid-cols-2 gap-2">
          <input aria-label={t('portfolio.controlPlane.symbol')} className={INPUT} value={instrumentForm.symbol} disabled={Boolean(editingKey)} onChange={(e) => setInstrumentForm((prev) => ({ ...prev, symbol: e.target.value }))} placeholder={t('portfolio.controlPlane.symbol')} />
          <select aria-label={t('portfolio.controlPlane.market')} className={INPUT} value={instrumentForm.market} disabled={Boolean(editingKey)} onChange={(e) => setInstrumentForm((prev) => ({ ...prev, market: e.target.value as InstrumentForm['market'] }))}>{MARKETS.map((market) => <option key={market}>{market}</option>)}</select>
          <input aria-label={t('portfolio.controlPlane.quoteCurrency')} className={INPUT} value={instrumentForm.quoteCurrency} onChange={(e) => setInstrumentForm((prev) => ({ ...prev, quoteCurrency: e.target.value }))} placeholder={t('portfolio.controlPlane.quoteCurrency')} />
          <select aria-label={t('portfolio.controlPlane.instrumentType')} className={INPUT} value={instrumentForm.instrumentType} onChange={(e) => setInstrumentForm((prev) => ({ ...prev, instrumentType: e.target.value as PortfolioInstrumentType }))}>{TYPES.map((type) => <option key={type}>{type}</option>)}</select>
          <input aria-label={t('portfolio.controlPlane.tradeLotSize')} className={INPUT} type="number" min="0" step="any" value={instrumentForm.tradeLotSize} onChange={(e) => setInstrumentForm((prev) => ({ ...prev, tradeLotSize: e.target.value }))} placeholder={t('portfolio.controlPlane.tradeLotSize')} />
          <select aria-label={t('portfolio.controlPlane.verificationStatus')} className={INPUT} value={instrumentForm.verificationStatus} onChange={(e) => setInstrumentForm((prev) => ({ ...prev, verificationStatus: e.target.value as PortfolioVerificationStatus }))}><option value="missing">missing</option><option value="provisional">provisional</option><option value="verified">verified</option></select>
          {complex ? <><input aria-label={t('portfolio.controlPlane.underlyingSymbol')} className={INPUT} value={instrumentForm.underlyingSymbol} onChange={(e) => setInstrumentForm((prev) => ({ ...prev, underlyingSymbol: e.target.value }))} placeholder={t('portfolio.controlPlane.underlyingSymbol')} /><select aria-label={t('portfolio.controlPlane.underlyingMarket')} className={INPUT} value={instrumentForm.underlyingMarket} onChange={(e) => setInstrumentForm((prev) => ({ ...prev, underlyingMarket: e.target.value as InstrumentForm['market'] }))}>{MARKETS.map((market) => <option key={market}>{market}</option>)}</select><input aria-label={t('portfolio.controlPlane.underlyingCurrency')} className={INPUT} value={instrumentForm.underlyingCurrency} onChange={(e) => setInstrumentForm((prev) => ({ ...prev, underlyingCurrency: e.target.value }))} placeholder={t('portfolio.controlPlane.underlyingCurrency')} /></> : null}
          {leveraged ? <input aria-label={t('portfolio.controlPlane.leverageFactor')} className={INPUT} type="number" step="any" value={instrumentForm.leverageFactor} onChange={(e) => setInstrumentForm((prev) => ({ ...prev, leverageFactor: e.target.value }))} placeholder={t('portfolio.controlPlane.leverageFactor')} /> : null}
          {instrumentForm.instrumentType === 'adr_ads' ? <input aria-label={t('portfolio.controlPlane.conversionRatio')} className={INPUT} type="number" step="any" value={instrumentForm.conversionRatio} onChange={(e) => setInstrumentForm((prev) => ({ ...prev, conversionRatio: e.target.value }))} placeholder={t('portfolio.controlPlane.conversionRatio')} /> : null}
          <input aria-label={t('portfolio.controlPlane.evidenceSource')} className={`${INPUT} col-span-2`} value={instrumentForm.evidenceSource} onChange={(e) => setInstrumentForm((prev) => ({ ...prev, evidenceSource: e.target.value }))} placeholder={t('portfolio.controlPlane.evidenceSource')} />
          <input aria-label={t('portfolio.controlPlane.evidenceAsOf')} className={INPUT} type="datetime-local" value={instrumentForm.evidenceAsOf} onChange={(e) => setInstrumentForm((prev) => ({ ...prev, evidenceAsOf: e.target.value }))} />
          <button type="button" className="btn-secondary text-sm" disabled={saving} onClick={() => void saveInstrument()}>{editingKey ? t('portfolio.controlPlane.updateIdentity') : t('portfolio.controlPlane.createIdentity')}</button>
        </div>
      </Card>
      <Card padding="md">
        <h2 className="text-sm font-semibold text-foreground">{t('portfolio.controlPlane.riskTitle')}</h2>
        <div className="mt-3 grid grid-cols-2 gap-2">
          {([
            ['minCashBufferPct', t('portfolio.controlPlane.minCashBufferPct')], ['maxSinglePositionPct', t('portfolio.controlPlane.maxSinglePositionPct')],
            ['maxSectorPct', t('portfolio.controlPlane.maxSectorPct')], ['maxHighRiskProductPct', t('portfolio.controlPlane.maxHighRiskProductPct')],
            ['maxPortfolioDrawdownPct', t('portfolio.controlPlane.maxPortfolioDrawdownPct')],
          ] as const).map(([key, label]) => <label key={key} className="text-xs text-secondary">{label}<input aria-label={label} className={`${INPUT} mt-1`} type="number" min="0" max="100" step="any" value={policyForm[key]} onChange={(e) => setPolicyForm((prev) => ({ ...prev, [key]: e.target.value }))} /></label>)}
          <button type="button" className="btn-secondary self-end text-sm" disabled={saving} onClick={() => void savePolicy()}>{t('portfolio.controlPlane.saveRisk')}</button>
        </div>
        <div className="mt-4 border-t border-white/10 pt-3">
          <div className="flex items-center justify-between"><h3 className="text-xs font-semibold text-foreground">{t('portfolio.controlPlane.blockersTitle')}</h3><span className="text-xs text-secondary">{blockers.length}</span></div>
          <div className="mt-2 flex flex-wrap gap-1.5">{blockers.map((item, index) => <span key={`${item.code}-${index}`} className="rounded border border-amber-400/30 px-2 py-1 text-[11px] text-warning">{item.code}</span>)}{blockers.length === 0 ? <span className="text-xs text-success">{t('portfolio.controlPlane.noBlockers')}</span> : null}</div>
        </div>
        {message ? <InlineAlert className="mt-3" variant="success" message={message} /> : null}
        {error ? <InlineAlert className="mt-3" variant="danger" message={error} /> : null}
      </Card>
    </section>
  );
}
