# Portfolio Risk Budget Evaluation Design

## Goal

Replace the frozen research snapshot's permanent `risk_budget_evaluated=false`
placeholder with deterministic, fail-closed evaluation of the saved DSA risk
policy.

## Scope And Currency

Risk is evaluated in currency buckets, not by forcing the whole portfolio into
CNY. Active accounts with the same `base_currency` are combined. Therefore CN
accounts remain CNY, US accounts remain USD, and HK accounts remain HKD.

Every currency bucket independently evaluates:

- minimum cash buffer;
- maximum single-position weight;
- maximum verified sector exposure;
- maximum daily-reset/high-risk product exposure;
- maximum historical drawdown.

The top-level result is evaluated only when every non-empty currency bucket has
complete current prices, current cash, verified sector evidence, and sufficient
drawdown history. A threshold breach is a valid evaluated result; missing
evidence is not.

## Sector Evidence

Instrument metadata may include a `risk_sector` object:

```json
{
  "taxonomy": "portfolio-risk-v1",
  "as_of": "2026-07-23",
  "source": "https://official.example/factsheet",
  "exposures": [
    {"sector": "Semiconductors", "weight_pct": 100.0}
  ]
}
```

Exposure weights must be positive and total 100% within tolerance. Ordinary
companies normally have one exposure. Funds use official issuer/index sector
weights where available. Missing, malformed, or unverified exposure evidence
fails closed; live provider board lookups never silently fill the registry.

## Drawdown Evidence

Drawdown uses persisted DSA daily snapshots only. Each currency series includes
dates on which every active account in that currency has a snapshot. At least
two complete dates are required. Rows marked `fx_stale` are rejected because an
account may contain instruments quoted outside its base currency.

## Output

`risk_budget` contains `evaluated`, `base_scope=currency`, per-currency metrics,
breaches, evidence blockers, and the configured thresholds. The existing sizing
gate remains closed whenever `evaluated` is false. No order, scheduler, worker,
or broker path is added.
