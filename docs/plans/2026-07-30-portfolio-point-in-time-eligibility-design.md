# Portfolio Point-in-Time Eligibility Design

## Status

- Approved by the user on 2026-07-30.
- This is Phase 1A of the personal investment decision system.
- It changes the shared portfolio research API contract additively, without a database migration.
- It does not authorize remote Git operations, historical replay, strategy promotion, orders, or portfolio-data mutation.

## Problem

`GET /api/v1/portfolio/research-snapshot` accepts an arbitrary cutoff but currently reads the latest mutable portfolio position cache, instrument registry, singleton risk policy, daily snapshots, and active DecisionSignals. A content hash proves only that the same current payload was rebuilt; it does not prove that the payload existed at a historical cutoff.

The current storage cannot reconstruct historical state safely:

- trades, cash events, corporate actions, and instruments can be deleted;
- cached positions and lots are replaced in place;
- instrument identity and risk policy are updated in place;
- DecisionSignal status and metadata are mutable;
- there are no revision or tombstone records for all of these sources.

Therefore the current implementation must never claim historical replay eligibility.

## Considered Approaches

### 1. Infer historical eligibility from current `updated_at` values

Rejected. A timestamp on a surviving current row cannot reveal a deleted row or the previous value of an overwritten singleton.

### 2. Add historical revision storage now

Deferred. This is the correct dependency for future historical replay, but it requires schema/storage design and migration approval. It is larger than the first vertical slice.

### 3. Add an honest prospective-only eligibility contract

Selected. Keep the existing snapshot and cutoff workflow compatible, freeze all decision inputs used by the deterministic baseline, expose source cutoffs and exact blockers, and always return `historical_replay_eligible=false` until revisioned historical sources exist.

## Contract

### Snapshot response

Keep `schema_version=portfolio-research-snapshot-v1` and add:

```json
{
  "point_in_time": {
    "scope": "current_prospective",
    "prospective_decision_eligible": true,
    "historical_replay_eligible": false,
    "source_cutoffs": {
      "accounts": "2026-07-30T01:00:00Z",
      "position_cache": "2026-07-30T01:00:00Z",
      "daily_snapshots": "2026-07-30T01:00:00Z",
      "instrument_registry": "2026-07-30T01:00:00Z",
      "risk_policy": "2026-07-30T01:00:00Z",
      "decision_signals": "2026-07-30T01:00:00Z"
    },
    "blockers": []
  },
  "decision_signals": []
}
```

Source cutoff values are nullable when a source has no rows. A missing optional DecisionSignal does not make temporal capture ineligible; it remains a separate baseline evidence blocker. Missing timestamps for present required rows, source rows updated after the requested cutoff, or truncated DecisionSignal capture make prospective eligibility false.

The following temporal blockers are stable contract values:

- `account_state_cutoff_missing`
- `account_state_after_cutoff`
- `position_cache_cutoff_missing`
- `position_cache_after_cutoff`
- `daily_snapshot_cutoff_missing`
- `daily_snapshot_after_cutoff`
- `instrument_registry_cutoff_missing`
- `instrument_registry_after_cutoff`
- `risk_policy_cutoff_missing`
- `risk_policy_after_cutoff`
- `decision_signal_cutoff_missing`
- `decision_signal_after_cutoff`
- `decision_signal_snapshot_truncated`

Temporal blockers are also included in `hard_blockers` with `scope=point_in_time`, so existing fail-closed consumers continue to see them. `completeness` remains `INSUFFICIENT_EVIDENCE` whenever any hard blocker exists.

### Frozen DecisionSignals

The snapshot freezes only the active signal fields required by the deterministic baseline:

- `id`, `market`, `stock_code`, `stock_name`;
- `reason`, `status`, `created_at`, `updated_at`;
- sanitized `metadata`, including the structured portfolio decision and its evidence cutoff.

The snapshot hash covers these fields. The production baseline must read `decision_signals` from the snapshot and must not query current active signals after preflight. This makes signal changes observable as snapshot drift.

### Submission gates

After rebuilding and comparing the requested snapshot hash:

- `POST /api/v1/portfolio/research-baseline` rejects an ineligible prospective snapshot with HTTP 409 and `research_snapshot_not_point_in_time_eligible`.
- A position analysis that supplies snapshot binding applies the same rejection.
- Existing unbound manual analysis remains unchanged in Phase 1A and is reported as a remaining boundary; making binding mandatory is a separate shared-contract decision.

Hash mismatch takes precedence over eligibility failure because it proves the caller is no longer using the preflight payload.

### Web behavior

Portfolio Control Plane shows:

- `current_prospective` scope;
- prospective status as ready or not ready;
- historical replay as unavailable;
- source cutoff values;
- temporal blockers without hiding existing identity, product, price, FX, or risk blockers.

## Non-Goals

- Historical snapshot reconstruction or replay.
- Database migrations, tombstones, or revision tables.
- Mandatory snapshot binding for every legacy manual-analysis caller.
- Opportunity discovery, strategy comparison, policy tuning, or model changes.
- Any broker, order, scheduler, live runner, or automatic multi-agent capability.

## Compatibility

The shared CCSwitch `portfolio_review.py` and `portfolio_acceptance.py` continue sending `research_snapshot_hash`, `research_cutoff`, and cutoff queries. Phase 1A preserves these fields and endpoints. Their contract tests must be run without modifying the shared skill.

## Done When

- Current mutable state is explicitly prospective-only and cannot claim historical replay eligibility.
- Every source used by the snapshot exposes a bounded source cutoff or an exact blocker.
- Baseline decisions use the frozen signal set in the snapshot.
- Ineligible bound submissions fail closed with a stable 409 error.
- Control Plane displays temporal scope, eligibility, source cutoffs, and blockers.
- Focused backend, API, Web, shared-runner compatibility, repository gate, lint, build, and diff checks are reported from fresh runs.
