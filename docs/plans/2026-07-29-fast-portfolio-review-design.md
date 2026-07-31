# Fast Portfolio Review Design

## Goal

Reduce a normal full-portfolio review from roughly 52 minutes to a two-stage workflow:

1. Build a deterministic baseline for every non-zero holding without news or LLM calls.
2. Present recommended deep-research candidates as `name（symbol）`, wait for the user's selection, and run the existing detailed news/LLM analysis only for selected symbols.

The expected target is 1-3 minutes for baseline-only review and 3-8 minutes when 3-5 symbols are deepened.

## Boundaries

- DSA remains the sole source of holdings, accounts, cash, costs, instrument identity, and risk policy.
- The baseline and all selected deep analysis stay bound to one `research_snapshot_hash` and timezone-aware `research_cutoff`.
- No broker, orders, scheduler, live runner, external worker, or automatic multi-agent execution.
- The existing full `portfolio_acceptance.py` process-acceptance behavior remains unchanged.
- The baseline must not call the stock analysis pipeline, task queue, news services, or an LLM.
- The baseline must not persist portfolio snapshots, holdings, risk policy, DecisionSignals, or market-data caches. It reads quote evidence from the frozen snapshot and daily bars from the local DB cache only.
- Missing evidence remains visible as `WAIT`, `PRELIMINARY`, or `INSUFFICIENT_EVIDENCE`.

## Options Considered

### Increase Queue Concurrency Only

Changing `MAX_WORKERS=1` to three workers reduces the observed run to an estimated 18-22 minutes but still performs full news and LLM work for every holding. It also increases provider, SQLite, and model contention. This is not sufficient.

### Add a Depth Flag to the Existing Per-Position Endpoint

This keeps one endpoint but couples cheap baseline calculation to the full stock-analysis pipeline and risks accidentally invoking news or LLM work. It also makes full process acceptance and daily review semantics harder to distinguish.

### Separate Batch Baseline and Selected Deepening

This is the selected design. A new batch baseline service and API perform cache-only deterministic work. A separate daily runner checkpoints the baseline, asks the conversation layer for selection, and reuses the existing detailed endpoint for selected symbols.

## Architecture

### Batch Baseline API

Add `POST /api/v1/portfolio/research-baseline` with a request containing the preflight `research_snapshot_hash` and `research_cutoff`.

The endpoint rebuilds the same frozen snapshot and returns HTTP 409 on drift. It then:

1. Builds one work item per positive account-position row.
2. Resolves display names from local/static/cache-only sources.
3. Reads quote evidence already frozen into the research snapshot without calling a market-data provider.
4. Loads daily bars from the local DB cache with a shared cutoff without warming or refreshing that cache.
5. Computes local technical summaries with `StockTrendAnalyzer` where history is sufficient.
6. Loads active DecisionSignals in one batch and preserves their existing two-axis actions.
7. Applies instrument, freshness, risk, and product blockers without inventing a new action from technical indicators.
8. Returns all rows plus stable deep-research candidates.

Rows without a valid active DecisionSignal use conservative `position_action=hold` and `incremental_action=wait` only as an explicitly labeled deterministic fallback. They cannot produce sizing or an add recommendation.

### Candidate Selection

A row is a candidate when at least one action-changing condition is present:

- an existing signal recommends reduce/exit/add or is not evidence-complete;
- price, FX, history, identity, or required product evidence is missing or stale;
- a portfolio or instrument risk-policy breach applies;
- a QDII, ADR/ADS, or daily-reset product requires evidence unavailable to the baseline;
- the user explicitly selects the symbol, even when it was not recommended.

Candidate ordering is deterministic and auditable. The API returns all exceptions and separately marks up to five as recommended; it does not silently discard lower-ranked exceptions.

### Daily Runner

Add `scripts/portfolio_review.py` to the personal skill with two explicit stages:

- `baseline`: call preflight and the batch baseline API, then write an atomic manifest with `workflow_stage=awaiting_selection`.
- `deepen`: read that manifest and accept one or more `market:symbol` keys. Submit only those keys to the existing detailed analysis endpoint and collect exact-trace DecisionSignals.

The CLI does not prompt on stdin. Codex presents the candidate list in conversation and invokes the second stage after the user's reply. This keeps the flow resumable and testable.

Selecting one symbol applies to every account row holding that market/symbol. Rows for the same symbol are submitted serially, preserving existing duplicate-task behavior.

## Manifest Contract

The manifest keeps baseline and deepening evidence separate:

```text
workflow_stage
snapshot { snapshot_hash, cutoff }
baseline_rows[]
deepen_candidates[]
selection { source, requested_keys }
deepening { tasks[], decisions[] }
reconciliation {
  ledger_position_count,
  baseline_row_count,
  selected_row_count,
  deepened_decision_row_count,
  baseline_coverage_reconciled,
  deepening_coverage_reconciled,
  consolidated_ready
}
```

Every row includes `name`, `symbol`, `display_label`, and `selection_key`. The display form is always `name（symbol）`; unresolved names use `名称待核验（symbol）` and carry an identity blocker. A deepened result never overwrites its baseline.

## Failure Behavior

- Snapshot drift stops baseline or deepening before submitting work.
- One quote/history failure degrades only the affected symbol and remains visible.
- Invalid or unknown selection keys fail before any detailed-analysis POST.
- A selected detailed task failure preserves partial checkpoints and leaves the run `NOT_READY`.
- Selecting zero symbols is valid; a fully reconciled baseline can complete without LLM calls.
- The baseline makes no provider calls. Selected detailed tasks use at most three-way bounded concurrency; same-symbol rows across accounts remain serial.

## Verification

- Unit tests prove baseline coverage, symbol-level data deduplication, name formatting, deterministic candidate routing, and zero task/news/LLM calls.
- API tests prove snapshot binding, no persistence, and partial evidence behavior.
- Runner tests prove baseline-only behavior, explicit selection, non-candidate selection, same-symbol multi-account sequencing, exact-trace collection, drift failure, atomic checkpoints, and reconciliation.
- Existing acceptance-runner tests remain unchanged and passing.
- A live timing check will compare 17-row baseline wall time against the previous 52-minute full run. It will be reported separately from deterministic tests.
