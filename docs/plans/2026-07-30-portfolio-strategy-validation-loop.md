# Portfolio Strategy Validation Loop Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build an auditable daily-decision-to-learning loop that links frozen portfolio recommendations, human judgment, actual DSA trades, 5/20/60-bar outcomes, point-in-time historical replay, walk-forward out-of-sample checks, prospective shadow comparison, and human-approved strategy promotion.

**Architecture:** Keep the DSA ledger as the only holdings, cash, and transaction truth. Extend the existing decision-quality sidecars instead of replacing `DecisionSignal`, and add separate immutable manifests for execution attribution, strategy versions, validation runs, and shadow comparisons. Historical replay evaluates only point-in-time evidence and deterministic policy stages; LLM/news quality is validated prospectively from frozen outputs because current historical records cannot reproduce those inputs without leakage.

**Tech Stack:** Python 3.11, FastAPI/Pydantic, SQLAlchemy/SQLite, pytest, React 19/TypeScript/Vitest, JSON validation artifacts, existing DSA portfolio and DecisionSignal services.

---

## Decision Before Implementation

The chain is directionally correct, but the current checkout is not yet reliable enough for an end-to-end performance claim.

The existing implementation can already support daily frozen decisions, human feedback, explicit outcome runs, attribution, and a review UI. It cannot yet support a defensible statement that a strategy version improves returns because:

1. quality statistics currently pool 5d/20d/60d rows into one sample;
2. `weekly_review()` has no actual week boundary and trigger/expiry fields are placeholders;
3. outcome upsert can overwrite a completed result after market-data revisions;
4. the outcome anchor uses the cutoff calendar date's close, which can leak an intraday close;
5. material-event identity includes the full snapshot hash, so daily refreshes can inflate samples even when the decision contract is unchanged;
6. frozen contexts do not persist the full strategy, prompt, model, code, and data-contract versions needed for later comparison;
7. `reduce` and `add_in_batches` remain `exposure_contract_missing` because DecisionSignals are not linked to DSA trades;
8. the legacy backtest evaluates historical analysis records, not versioned portfolio policies, and has no walk-forward/OOS or champion/challenger contract;
9. `StockDaily` identity is code/date only, so validation must prove canonical market-specific symbols before cross-market use;
10. there is no manual promotion/rollback registry for candidate rules.

Therefore implementation must start with data and evaluation integrity. Do not start by tuning rules.

## Existing Capability Matrix

| Chain stage | Current state | Evidence | Decision |
| --- | --- | --- | --- |
| DSA ledger and trade replay | Implemented | `src/storage.py`, `src/services/portfolio_service.py` | Reuse unchanged |
| Frozen portfolio snapshot | Implemented | `src/services/portfolio_research_snapshot_service.py` | Reuse; add version references only |
| Daily baseline and exception routing | Implemented | `src/services/portfolio_research_baseline_service.py`, orchestrator skill | Reuse |
| Frozen two-axis DecisionSignal | Implemented | `src/services/decision_signal_service.py` | Harden version manifest |
| Human `accept/modify/veto/no_action` | Implemented | shadow feedback service/API/UI | Reuse |
| Actual trade recording | Implemented | `portfolio_trades` | Never duplicate; add sidecar link |
| 5/20/60 price-path outcomes | Partially implemented | `src/services/decision_quality_service.py` | Fix anchoring, immutability, segmentation |
| Weekly attribution/patterns | Partially implemented | `weekly_review()`, `get_learning_patterns()` | Add real window and evidence states |
| Historical strategy replay | Missing | legacy backtest has different semantics | Add isolated validation runner |
| Walk-forward/OOS | Missing | no current contract | Add after replay integrity |
| Prospective shadow A/B | Missing | one production decision only | Add champion/challenger sidecar |
| Human promotion and rollback | Missing | candidates stay `observed` | Add explicit registry; never auto-promote |

## Invariants

- DSA remains the only holdings, cash, cost, and transaction truth.
- Existing portfolio rows and trades are never copied into a second ledger.
- `position_action` and `incremental_action` remain independent.
- Missing identity, point-in-time evidence, benchmark, execution ordering, or risk budget produces `unable`, `WAIT`, or `INSUFFICIENT_EVIDENCE`.
- No broker, order, scheduler, live runner, automatic multi-agent run, or automatic rule promotion.
- Historical replay cannot fetch current news, current fundamentals, or current registry values to fill an old cutoff.
- A completed validation result is immutable. Corrections create a new data revision or engine version.
- Every comparison uses the same eligible events, execution convention, costs, benchmark, and horizon.
- `HOLD/NO_ADD` is a counterfactual baseline; the current frozen production strategy is the champion baseline.
- Long-term improvement remains `PROVISIONAL` until 60-bar prospective outcomes mature.

## Phase Gates

```text
Phase 0 integrity
  -> Phase 1 daily decision/execution link
  -> Phase 2 mature 5/20/60 outcomes
  -> Phase 3 point-in-time historical replay
  -> Phase 4 purged walk-forward OOS
  -> Phase 5 prospective champion/challenger shadow
  -> Phase 6 human promotion or rollback
```

Failure at any phase stops the next phase. Historical profit cannot compensate for a leakage, identity, benchmark, or hard-gate failure.

### Task 1: Freeze The Current Champion And Validation Contract

**Files:**
- Create: `src/schemas/portfolio_strategy_validation.py`
- Create: `tests/test_portfolio_strategy_validation_schema.py`
- Modify: `docs/portfolio-research-workflow.md`

**Step 1: Write failing schema tests**

Cover immutable version identity, point-in-time cutoff, allowed statuses, and canonical hashing:

```python
def test_strategy_manifest_hash_changes_when_policy_changes():
    left = strategy_manifest_hash({"strategy_version": "v1", "policy_hash": "a" * 64})
    right = strategy_manifest_hash({"strategy_version": "v1", "policy_hash": "b" * 64})
    assert left != right

def test_validation_manifest_rejects_future_evidence():
    with pytest.raises(ValueError, match="evidence_after_cutoff"):
        validate_point_in_time_manifest(payload_with_future_evidence())
```

**Step 2: Run the focused test and verify failure**

```bash
.venv/bin/python -m pytest tests/test_portfolio_strategy_validation_schema.py -q
```

Expected: FAIL because the schema module does not exist.

**Step 3: Define immutable manifest fields**

```text
strategy_id, strategy_version, status=draft|shadow|champion|retired,
policy_hash, prompt_template_hash, model_descriptor, generation_config_hash,
code_commit, snapshot_schema_version, decision_contract_version,
outcome_engine_version, benchmark_mapping_version, cost_model_version,
effective_from, created_at, approved_by, approved_at
```

Store model/provider names and hashes only; never store credentials. If a model alias cannot prove the resolved model, record `model_identity_unverified` and exclude the run from model-version comparisons.

**Step 4: Freeze `champion-v1`**

Generate a read-only JSON manifest from current code/config. Do not call it reliable or promoted; label it `PROVISIONAL_BASELINE` until Phase 0 passes.

**Step 5: Run tests**

Expected: PASS.

### Task 2: Repair Current Outcome And Sample Integrity

**Files:**
- Modify: `src/schemas/portfolio_decision_quality.py`
- Modify: `src/storage.py`
- Modify: `src/repositories/decision_quality_repo.py`
- Modify: `src/repositories/stock_repo.py`
- Modify: `src/services/decision_quality_service.py`
- Modify: `api/v1/schemas/decision_signals.py`
- Modify: `api/v1/endpoints/decision_signals.py`
- Modify: `tests/test_portfolio_decision_quality_schema.py`
- Modify: `tests/test_decision_quality_repo.py`
- Modify: `tests/test_decision_quality_outcomes.py`
- Modify: `tests/test_decision_quality_api.py`

**Step 1: Add failing regression tests**

Test all five defects before implementation:

- 5d, 20d, and 60d statistics are separate;
- a completed outcome cannot be overwritten under the same engine/data revision;
- an intraday cutoff never uses that day's final close;
- a quiet refresh with an unchanged decision/evidence contract does not create a new sample;
- market/symbol ambiguity returns `instrument_identity_ambiguous`.

**Step 2: Split observation and execution anchors**

Use:

```text
observation_anchor = last fully known bar at decision_cutoff
shadow_execution_anchor = first tradable bar after decision_cutoff
actual_execution_anchor = linked DSA trade price/time when confirmed
horizon bars = bars after execution_anchor
```

Do not use a same-day close when the market session was still open at the cutoff. If the market calendar or phase is unavailable, return `execution_anchor_unverified`.

**Step 3: Replace broad outcome upsert with revisioned immutability**

Add `data_revision_hash`, `input_bar_hash`, and `computed_at`. A repeated identical run returns the existing row. Corrected inputs require a new `engine_version` or `data_revision_hash`; never mutate the prior completed row.

**Step 4: Separate material-event identity from full snapshot identity**

Keep `frozen_snapshot_hash` for audit, but calculate sample identity from the action-changing subset:

```text
account + instrument + both actions + trigger/invalidation + benchmark +
evidence cutoff/version + decision/strategy version + material evidence fingerprint
```

Daily price refresh alone does not create a new independent event unless it changes a declared trigger, action, evidence version, or material evidence fingerprint.

**Step 5: Segment statistics**

`GET /quality/stats` must require or return explicit groups by horizon, market, product type, strategy version, and outcome engine. Report `event_count` and `effective_sample_count` separately.

**Step 6: Run focused tests**

```bash
.venv/bin/python -m pytest \
  tests/test_portfolio_decision_quality_schema.py \
  tests/test_decision_quality_repo.py \
  tests/test_decision_quality_outcomes.py \
  tests/test_decision_quality_api.py -q
```

Expected: PASS.

### Task 3: Link Frozen Decisions To Real DSA Trades

**Files:**
- Modify: `src/storage.py`
- Modify: `src/repositories/decision_quality_repo.py`
- Create: `src/services/decision_execution_link_service.py`
- Modify: `api/v1/schemas/decision_signals.py`
- Modify: `api/v1/endpoints/decision_signals.py`
- Create: `tests/test_decision_execution_link_service.py`
- Modify: `tests/test_decision_quality_api.py`

**Step 1: Write failing sidecar tests**

Create `decision_signal_execution_links` with:

```text
id, signal_id, trade_id, link_status=proposed|confirmed|rejected,
temporal_relation=after_signal_confirmed|before_signal|same_day_unknown,
linked_by=human|import, note, created_at, updated_at
```

Require unique `(signal_id, trade_id)` and prevent one trade from being counted against two strategy events.

**Step 2: Validate against ledger truth**

The service reads, but never rewrites, the referenced `portfolio_trades` row. It must prove account, canonical market/symbol, side, quantity, price, fee/tax, and temporal ordering. `created_at` is record time, not execution time; a same-day trade without user-confirmed ordering remains `same_day_unknown` and cannot support execution-return attribution.

**Step 3: Derive actual action without inventing it**

```text
sell quantity == frozen quantity -> actual_position_action=exit
0 < sell quantity < frozen quantity -> actual_position_action=reduce
buy after signal -> actual_incremental_action=add_in_batches only when linked
no linked trade -> actual action remains null, never HOLD by default
```

Use the frozen position quantity from the decision snapshot and the trade quantity from DSA. Multiple confirmed trades may satisfy one staged decision; calculate total exposure change only after all linked rows are explicit.

**Step 4: Add API operations**

```text
PUT /api/v1/decision-signals/{signal_id}/execution-links/{trade_id}
GET /api/v1/decision-signals/{signal_id}/execution-links
```

Linking is an attribution write, not a portfolio-trade write.

**Step 5: Run focused tests**

```bash
.venv/bin/python -m pytest tests/test_decision_execution_link_service.py tests/test_decision_quality_api.py -q
```

Expected: PASS, including ledger row/count/hash unchanged assertions.

### Task 4: Persist Strategy Versions And Validation Runs

**Files:**
- Modify: `src/storage.py`
- Create: `src/repositories/portfolio_strategy_validation_repo.py`
- Create: `src/services/portfolio_strategy_version_service.py`
- Create: `tests/test_portfolio_strategy_validation_repo.py`
- Create: `tests/test_portfolio_strategy_version_service.py`

**Step 1: Write failing persistence tests**

Add append-only tables:

```text
portfolio_strategy_versions
portfolio_validation_runs
portfolio_validation_events
portfolio_rule_candidates
```

The run manifest must freeze the eligible universe hash, cutoff range, split boundaries, purge/embargo, cost model, benchmark mapping, code SHA, strategy hashes, and input artifact hashes.

**Step 2: Enforce state transitions**

```text
draft -> shadow -> champion -> retired
observed -> proposed -> approved|rejected
```

Only an explicit human-confirmed service call may move a strategy to `champion` or a rule to `approved`. The repository must reject automatic promotion flags and in-place mutation of immutable version fields.

**Step 3: Keep activation separate from risk policy**

Approving a strategy version records governance state only. It does not change `portfolio_risk_policy`, prompts, scheduler settings, or orders. Applying it to the daily pipeline is a separate, explicit implementation and confirmation.

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_portfolio_strategy_validation_repo.py tests/test_portfolio_strategy_version_service.py -q
```

Expected: PASS.

### Task 5: Build A Point-In-Time Historical Replay Runner

**Files:**
- Create: `src/services/portfolio_strategy_replay_service.py`
- Create: `scripts/portfolio_strategy_validation.py`
- Create: `tests/test_portfolio_strategy_replay_service.py`
- Create: `tests/test_portfolio_strategy_validation_cli.py`

**Step 1: Write leakage-first tests**

The runner must fail when:

- evidence timestamp is after the event cutoff;
- current instrument registry data replaces a frozen historical value;
- a network/data-provider method is called during replay;
- the benchmark was selected after seeing the outcome;
- the event lacks frozen holdings, both action axes, or strategy identity;
- bars are insufficient or adjustment identity changes.

**Step 2: Add explicit CLI stages**

```bash
.venv/bin/python scripts/portfolio_strategy_validation.py preflight --as-of 2026-07-30
.venv/bin/python scripts/portfolio_strategy_validation.py build-dataset --from YYYY-MM-DD --to YYYY-MM-DD --output <artifact.json>
.venv/bin/python scripts/portfolio_strategy_validation.py replay --dataset <artifact.json> --strategy <manifest.json> --output <run.json>
.venv/bin/python scripts/portfolio_strategy_validation.py report --run <run.json>
```

The runner is manual and bounded. It starts no service, scheduler, broker, worker, or external research task.

**Step 3: Define eligible historical events**

Only replay events with frozen point-in-time inputs. Older Markdown reports without a complete quality context may be evaluated by the legacy backtest, but they are excluded from strategy-version comparisons.

**Step 4: Limit historical replay to reproducible layers**

Replay deterministic gates, action mappings, thresholds, and risk rules. Do not regenerate historical LLM/news judgments from current data. Compare already frozen LLM decisions prospectively instead.

**Step 5: Add transaction costs and execution rules**

Use DSA-recorded fees/tax for actual trades. For shadow trades, use a frozen cost model and next-tradable-bar execution convention. If sizing/risk budget is unavailable, report action-level outcomes only and keep portfolio-return metrics `unable`.

**Step 6: Run tests**

```bash
.venv/bin/python -m pytest tests/test_portfolio_strategy_replay_service.py tests/test_portfolio_strategy_validation_cli.py -q
```

Expected: PASS with zero network calls.

### Task 6: Add Purged Walk-Forward And OOS Evaluation

**Files:**
- Create: `src/services/portfolio_walk_forward_service.py`
- Create: `tests/test_portfolio_walk_forward_service.py`
- Modify: `scripts/portfolio_strategy_validation.py`

**Step 1: Write failing temporal-split tests**

Require expanding training windows, untouched validation/test windows, and a 60-trading-bar purge/embargo so labels from an earlier event cannot leak into the next fold. Group duplicate/correlated material events before splitting.

**Step 2: Compare paired events**

Every candidate/champion/HOLD comparison uses the exact same event IDs. Report exclusions and unable reasons rather than changing the denominator per strategy.

**Step 3: Report metrics by segment**

For each horizon, market, product type, and action axis report:

```text
eligible events, effective sample size, unable/abstention rate,
excess return, decision value vs HOLD, MFE, MAE, drawdown,
turnover, costs, opportunity cost, human override value,
instrument concentration, regime concentration, confidence interval
```

Do not aggregate daily-reset leveraged products, QDII, ETFs, and ordinary equities into one performance claim.

**Step 4: Add robustness checks**

Run cost sensitivity, one-instrument-out, one-regime-out, parameter perturbation, and alternate eligible-date checks. A rule that works only under one exact threshold remains `observed`.

**Step 5: Run tests**

```bash
.venv/bin/python -m pytest tests/test_portfolio_walk_forward_service.py -q
```

Expected: PASS.

### Task 7: Run Prospective Champion/Challenger Shadow Comparison

**Files:**
- Create: `src/services/portfolio_shadow_validation_service.py`
- Create: `tests/test_portfolio_shadow_validation_service.py`
- Modify: `scripts/portfolio_strategy_validation.py`
- Modify: `api/v1/schemas/decision_signals.py`
- Modify: `api/v1/endpoints/decision_signals.py`

**Step 1: Write same-input comparison tests**

Champion and challenger must consume the same frozen snapshot/evidence artifact. They cannot fetch independently or refresh the portfolio between runs. Both outputs are immutable and neither can place or imply an order.

**Step 2: Keep challenger output out of production DecisionSignals**

Store shadow decisions in validation sidecars so the user-facing active recommendation remains unambiguous. A challenger decision must include both action axes, blockers, triggers, invalidation, confidence, and strategy version.

**Step 3: Capture daily and weekly evidence**

The daily operation records eligible decisions and explicit no-decision/abstention cases. The weekly review compares paired disagreements and mature outcomes; it does not tune a rule during the same evaluation window.

**Step 4: Require minimum operational maturity**

Before considering limited promotion, require at least:

- 20 trading days with no snapshot, identity, or immutability violations;
- mature 20-bar paired outcomes for enough independent events to avoid one-position dominance;
- no new hard-gate regression;
- costs and drawdown not materially worse than champion;
- positive evidence in both historical OOS and prospective shadow.

Sample thresholds and non-inferiority margins must be approved and frozen before the shadow run starts. Do not choose them after seeing results.

**Step 5: Run tests**

```bash
.venv/bin/python -m pytest tests/test_portfolio_shadow_validation_service.py -q
```

Expected: PASS.

### Task 8: Add Human Promotion, Rejection, And Rollback

**Files:**
- Modify: `src/services/portfolio_strategy_version_service.py`
- Modify: `api/v1/schemas/decision_signals.py`
- Modify: `api/v1/endpoints/decision_signals.py`
- Modify: `apps/dsa-web/src/types/decisionSignals.ts`
- Modify: `apps/dsa-web/src/api/decisionSignals.ts`
- Create: `apps/dsa-web/src/components/portfolio/StrategyValidationReview.tsx`
- Create: `apps/dsa-web/src/components/portfolio/__tests__/StrategyValidationReview.test.tsx`
- Modify: `tests/test_portfolio_strategy_version_service.py`

**Step 1: Write approval-boundary tests**

Verify that only an explicit request with the target version, frozen evidence summary, reason, and rollback target can approve/reject/retire a version. Missing mature horizons remain visible and cannot be represented as success.

**Step 2: Implement a review-only UI**

Show champion vs challenger, OOS vs prospective evidence, hard-gate failures, sample concentration, costs, drawdown, unable reasons, and rollback target. Do not add an order control or automatic activation toggle.

**Step 3: Define rollback**

Rollback changes only the selected strategy-version reference after confirmation. It never deletes DecisionSignals, feedback, trades, outcomes, validation runs, or prior manifests.

**Step 4: Run backend and Web tests**

```bash
.venv/bin/python -m pytest tests/test_portfolio_strategy_version_service.py -q
cd apps/dsa-web && npm test -- src/components/portfolio/__tests__/StrategyValidationReview.test.tsx
```

Expected: PASS.

### Task 9: Pilot The Full Chain With 512480 Trade 23

**Files:**
- Create outside repository: one local validation artifact under an explicitly chosen artifact directory
- No portfolio ledger edits

**Step 1: Find the exact frozen signal**

Find the same-day `DecisionSignal` for `cn:512480` whose frozen actions were `reduce + no_add`. Require matching account, snapshot hash, cutoff, and complete quality context.

**Step 2: Decide eligibility**

- If the exact signal exists and the user confirms the trade occurred after it, link DSA trade `23`.
- If the signal or temporal ordering is missing, retain the trade as ledger truth but mark the case `execution_attribution_unverified`; do not manufacture a historical decision event.

**Step 3: Record human feedback**

Record the user's response as `accept` or `modify` according to the frozen AI axes. Preserve the actual trade separately: sell 6,800 units, trade ID 23, leaving 6,800 units according to the already-recorded DSA ledger.

**Step 4: Run only mature horizons**

5d, 20d, and 60d are evaluated only when exact aligned trading bars exist. Until then they remain pending. This one case is a pipeline acceptance case, not evidence that the strategy works.

**Step 5: Verify invariants**

Confirm portfolio trade count, cash ledger, remaining position, risk policy, original DecisionSignal, and frozen context are unchanged by feedback/outcome/validation operations.

### Task 10: Documentation And Final Verification

**Files:**
- Modify: `docs/decision-signals.md`
- Modify: `docs/portfolio-research-workflow.md`
- Create: `docs/portfolio-strategy-validation.md`
- Modify: `docs/full-guide.md`
- Modify: `docs/full-guide_EN.md` or document why synchronization is deferred
- Modify: `docs/CHANGELOG.md`
- Modify: `docs/architecture/api_spec.json`

**Step 1: Document the operating cadence**

```text
Daily: baseline -> selected deepening -> frozen decision -> feedback -> optional trade link
Weekly: trigger/expiry review -> mature outcomes -> attribution -> candidate observations
Monthly: replay diagnostics and data-quality audit
Per candidate: frozen OOS protocol -> shadow run -> human promote/reject/continue
```

**Step 2: Run focused backend tests**

```bash
.venv/bin/python -m pytest \
  tests/test_portfolio_decision_quality_schema.py \
  tests/test_decision_quality_repo.py \
  tests/test_decision_quality_service.py \
  tests/test_decision_quality_outcomes.py \
  tests/test_decision_quality_api.py \
  tests/test_decision_execution_link_service.py \
  tests/test_portfolio_strategy_validation_schema.py \
  tests/test_portfolio_strategy_validation_repo.py \
  tests/test_portfolio_strategy_replay_service.py \
  tests/test_portfolio_walk_forward_service.py \
  tests/test_portfolio_shadow_validation_service.py -q
```

Expected: PASS.

**Step 3: Run repository gates**

```bash
./scripts/ci_gate.sh
cd apps/dsa-web && npm run lint && npm run build
```

Expected: all commands exit 0. Record exact pre-existing failures if the dirty checkout prevents a clean result.

**Step 4: Run point-in-time and mutation smoke checks**

Verify:

- zero network calls during historical replay;
- identical input hashes produce identical deterministic results;
- no same-day-close leakage for intraday decisions;
- completed outcomes are immutable;
- no DSA ledger, risk policy, prompt, or scheduler mutation;
- champion and challenger use identical eligible event IDs;
- failed hard gates remain visible in reports.

**Step 5: Check documentation and diff integrity**

```bash
python -m py_compile <changed_python_files>
python scripts/check_ai_assets.py
git diff --check
```

Expected: exit 0. `check_ai_assets.py` is required only if AI governance assets changed.

## Promotion Decision Rubric

A candidate strategy may be proposed for limited use only when all statements below are true:

- the version and evaluation protocol were frozen before seeing test/shadow outcomes;
- no identity, leakage, benchmark, adjustment, or immutable-context violation occurred;
- paired OOS results are not driven by one instrument, one regime, or one threshold;
- prospective shadow results agree in direction with OOS results;
- 20-bar decision value improves without materially worse MAE/drawdown after costs;
- 60-bar evidence is mature before making a long-term improvement claim;
- abstention/`WAIT` quality does not deteriorate;
- the user explicitly approves the version and rollback target.

Otherwise the result is `CONTINUE_SHADOW`, `REJECT`, or `INSUFFICIENT_EVIDENCE`, never automatic promotion.

## Execution Preconditions

- The current checkout is detached and contains extensive overlapping modified/untracked work. Before code implementation, confirm the correct baseline and create an isolated worktree without stashing, resetting, or overwriting this checkout.
- Tasks 2-4 introduce or change persistence/API contracts and require explicit user confirmation under `AGENTS.md`.
- No commit, merge, rebase, push, PR, tag, or worktree deletion without explicit user authorization.
- Suggested commit commands from the generic planning workflow are intentionally omitted until authorization exists.

## Rollback

Disable the validation/review entry points and select the previous champion manifest. Keep all historical sidecars and manifests for audit. Never delete or rewrite portfolio trades, cash entries, positions, risk policy, DecisionSignals, feedback, or completed outcome revisions. Any schema/data removal is a separate destructive operation requiring explicit approval.
