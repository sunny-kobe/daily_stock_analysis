# DSA Portfolio Control Plane Design

Date: 2026-07-22
Status: approved

## Goal

Make DSA the only writable source for holdings, cost, cash, instrument identity,
and portfolio risk budget. Daily research must start from the DSA ledger, fail
closed on decision-critical missing evidence, invoke external research workers
only for declared exceptions, and always stop before trade execution.

## Current Problems

- Scheduled and batch analysis read `STOCK_LIST`, while the ledger contains a
  different non-zero position universe.
- Stable product identity is absent from the portfolio schema. The current
  HK07709 policy is a useful special case, but it cannot cover ordinary ETFs,
  QDII, ADR/ADS, or other daily-reset products.
- Risk thresholds live in environment configuration rather than one explicit
  portfolio policy record.
- Portfolio risk reads may create historical snapshots as a side effect.
- Missing news, fundamentals, price, FX, product terms, or portfolio policy can
  still result in an apparently actionable signal.
- Vibe-Trading, TradingAgents, ai_sentiney, and frozen orchestration artifacts
  can retain independent copies of holdings or derived position state.

## Chosen Architecture

DSA is the control plane. It owns five kinds of state:

1. trade, cash, and corporate-action events;
2. replayed positions and valuation snapshots;
3. stable instrument identity and product structure;
4. one portfolio-level risk policy;
5. feedback and prospective decision outcomes.

The Orchestrator is stateless with respect to business truth. It receives a
frozen, hashed DSA research snapshot and returns routing or adjudication output.
Vibe-Trading and TradingAgents receive only the minimum snapshot fields required
for one approved research question. They cannot write DSA state or execution
state. ai_sentiney remains outside the daily mainline.

```text
portfolio events -> replayed positions --+
instrument registry ---------------------+--> frozen research snapshot
portfolio risk policy -------------------+              |
                                                        v
                                               DSA baseline + gates
                                                        |
                          +-----------------------------+------------------+
                          | sufficient                                     | exception
                          v                                                v
                    human confirmation                         one approved worker
                                                                          |
                                                                          v
                                                          claim-level adjudication
                                                                          |
                                                                          v
                                                               human confirmation
                                                                          |
                                                                          v
                                                         feedback + forward outcome
```

## Persistent Contracts

### Instrument registry

Add `portfolio_instruments`, unique by canonical `(symbol, market)`.

Required fields:

- `symbol`, `market`, `quote_currency`;
- `instrument_type`: `equity`, `etf`, `qdii`, `adr_ads`,
  `daily_leveraged_product`, or `unknown`;
- optional `underlying_symbol`, `underlying_market`, `underlying_currency`;
- optional `leverage_factor`, `daily_reset`, `conversion_ratio`;
- `trade_lot_size`;
- `requires_premium_check`;
- `verification_status`: `verified`, `provisional`, or `missing`;
- `evidence_source`, `evidence_as_of`, `metadata_json`, timestamps.

Identity records are explicit manual/reference data. Price providers may propose
metadata but cannot silently mark an instrument verified.

### Portfolio risk policy

Add a singleton `portfolio_risk_policy` record. It is the only runtime source
for:

- minimum cash buffer percentage;
- maximum single-position percentage;
- maximum sector percentage;
- maximum daily-reset/high-risk product percentage;
- maximum portfolio drawdown percentage.

No default policy is silently inserted into an existing real ledger. Until a
human saves the policy, sizing and actionable add/reduce decisions fail closed.
Existing environment thresholds remain compatibility inputs for legacy alert
paths only and are not presented as the portfolio risk budget.

## Analysis Universe

Add a pure `PortfolioUniverseService` that reads cached non-zero positions from
active DSA accounts without replaying or refreshing the ledger.

- `portfolio_holdings`: daily default for this installation;
- `watchlist`: existing `STOCK_LIST` behavior for screening and ad hoc research;
- `union`: explicit compatibility mode, never an implicit fallback.

`ANALYSIS_UNIVERSE_SOURCE` defaults to `watchlist` for upstream compatibility.
This installation will explicitly set it to `portfolio_holdings`. If the ledger
cannot be read or contains no non-zero positions, the run stops with a clear
error. It does not fall back to `STOCK_LIST`.

The resolver returns eligible symbols, blocked symbols, ledger timestamp, and a
coverage diagnostic. Unknown instruments remain visible but cannot produce an
actionable signal.

## Frozen Research Snapshot

Add a read-only API/service that returns canonical JSON containing:

- cutoff and timezone;
- account and non-zero position context;
- instrument registry records;
- risk policy;
- price and FX availability/freshness;
- ledger/universe hash;
- limitations and hard blockers.

The hash excludes volatile serialization details and is computed from sorted,
canonical fields. The endpoint never creates positions, lots, daily snapshots,
signals, reports, or outcomes.

## Fail-Closed Decision Gate

The gate runs after extraction and before an actionable decision signal is
persisted or displayed.

Hard blockers for `buy`, `add`, `sell`, or `reduce`:

- unresolved instrument type, market, currency, or tradable symbol;
- unavailable or stale decision price;
- missing FX where portfolio conversion is required;
- absent portfolio risk policy for sizing or exposure-changing actions;
- missing underlying/reset/leverage terms for a daily-reset product;
- missing underlying/conversion data for ADR/ADS parity checks;
- missing NAV/premium evidence for an action on a product requiring that check;
- trade quantity incompatible with the verified lot size.

Missing news alone does not block a quiet `hold`. It does block an event-driven
action when no other fresh primary event evidence exists. A blocked action is
stored as `watch` or `alert`; metadata retains `raw_action`, blocker codes,
evidence cutoff, and required next research capability. Workers cannot override
hard blockers.

## Read-Only Risk Semantics

GET and research paths must not backfill snapshots. Risk calculation reads
existing daily snapshots only. If drawdown history is incomplete it returns an
explicit limitation and `available=false` for that block. Snapshot creation
remains an explicit write path used by ledger refresh/import operations.

## Worker Routing

Phase 1 does not start Vibe-Trading or TradingAgents. DSA emits at most one
specific upgrade request:

- deterministic timing for an entry/exit timing question;
- Vibe evidence for QDII, ADR/ADS, daily-reset products, official terms, or a
  current source conflict;
- TradingAgents for an ordinary company's thesis, bear case, earnings, or a
  high-impact reduce/exit second opinion.

Every request includes the exact question, frozen input hash, expected evidence
contract, and human-approval state. No generic multi-worker fan-out is allowed.

## Web Experience

Extend the portfolio page with compact, editable sections for:

- instrument type and verification state;
- underlying/product fields shown only when relevant;
- one portfolio risk policy form;
- universe coverage and blocker diagnostics;
- raw versus gated signal action.

Do not add order buttons, broker connections, or automatic execution controls.

## Forward Validation

Reuse `decision_signal_feedback` and `decision_signal_outcomes`. Add a daily
manual/shadow workflow that records the frozen snapshot hash, evidence sources,
gated recommendation, human `ACCEPT/VETO/MODIFY/NO_ACTION`, actual manual action,
manual correction minutes, latency/tokens, and 5/10/20-bar outcomes. No result
may be backfilled as though it were known at recommendation time.

The architecture remains `PROVISIONAL` until at least 20 completed trading days
have outcomes. Twenty days can reject the design or support continued use; it
does not prove permanent superiority.

## Migration And Compatibility

- New tables are additive. Existing trades, positions, snapshots, signals, and
  reports are preserved.
- Existing HK07709 context/execution work is migrated to registry-driven logic;
  the policy resolver may remain as a compatibility adapter during rollout.
- Existing watchlist endpoints retain `STOCK_LIST` semantics.
- Existing API fields remain; new fields are additive.
- Real ledger records are not automatically populated with guessed identity or
  risk policy values.

## Rollback

Switch `ANALYSIS_UNIVERSE_SOURCE` back to `watchlist` and stop exposing the new
API/UI surfaces. Additive tables may remain unused; no destructive migration is
required. The old HK07709 policy adapter remains available until the registry
path has passed regression and local smoke checks.

## Verification

- storage/repository round trips for both new tables;
- canonical universe and snapshot hash tests;
- no-fallback and no-write tests;
- per-instrument hard-gate tests;
- API compatibility and privacy tests;
- Web lint, focused component tests, and production build;
- backend focused tests, `python -m py_compile`, then `./scripts/ci_gate.sh`;
- local read-only smoke against a copy of the real database before any explicit
  real-database policy/identity setup.
