# Portfolio Decision Quality And Learning Loop Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a prospective, human-confirmed decision-quality loop for existing holdings that evaluates two action axes over 5/20/60 trading bars, measures benchmark-relative and downside outcomes, and turns confirmed errors into reviewable personal learning patterns.

**Architecture:** Keep the DSA ledger, instrument registry, risk policy, and `DecisionSignal` as existing sources of truth. Add a frozen decision-context sidecar, a separate quality-outcome sidecar, and a confirmed-attribution sidecar; extend shadow feedback only with nullable human action-axis fields. Run all new persistence through services/repositories, fail closed on missing evidence, and keep the existing report and direction-outcome paths operational when the quality loop is unavailable.

**Tech Stack:** Python 3.11, FastAPI/Pydantic, SQLAlchemy/SQLite, pytest, React 19, TypeScript, Vitest/Testing Library.

---

## Execution Preconditions

- The current checkout is detached at `e8a9ca7` and contains extensive pre-existing modified and untracked portfolio/DecisionSignal work. Do not execute this plan until the user identifies the correct baseline and the overlapping work is committed or otherwise safely isolated.
- Execute in a dedicated worktree created from that confirmed baseline.
- Tasks 2-7 change persistence and API contracts. Obtain explicit user confirmation before starting those tasks, as required by `AGENTS.md`.
- Do not commit, merge, rebase, push, create a PR, or modify another worktree without explicit user authorization. The commit suggestions below are checkpoints only.
- Preserve the DSA ledger as the only holdings/cash/transaction truth. Do not add broker, order, scheduler, or live-runner code.
- Re-read `AGENTS.md`, this plan, and `docs/plans/2026-07-25-portfolio-decision-quality-design.md` after the execution worktree is created.

### Task 1: Define The Two-Axis Decision Contract

**Files:**
- Create: `src/schemas/portfolio_decision_quality.py`
- Create: `tests/test_portfolio_decision_quality_schema.py`

**Step 1: Write failing normalization tests**

Cover valid values, invalid values, explicit missing evidence, stable material-event hashing, and the rule that current-position and incremental-capital actions are independent.

```python
def test_normalizes_independent_action_axes() -> None:
    decision = normalize_portfolio_decision({
        "position_action": "hold",
        "incremental_action": "add_in_batches",
        "confidence_by_horizon": {"5d": 0.55, "20d": 0.68, "60d": 0.61},
    })
    assert decision["position_action"] == "hold"
    assert decision["incremental_action"] == "add_in_batches"


def test_material_fingerprint_is_order_independent() -> None:
    left = material_event_fingerprint({"watch_conditions": ["b", "a"], "position_action": "hold"})
    right = material_event_fingerprint({"position_action": "hold", "watch_conditions": ["a", "b"]})
    assert left == right
```

**Step 2: Run the focused test and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_portfolio_decision_quality_schema.py -q
```

Expected: FAIL because `src.schemas.portfolio_decision_quality` does not exist.

**Step 3: Implement the minimal domain contract**

Define these constants and pure functions:

```python
POSITION_ACTIONS = frozenset({"hold", "reduce", "exit"})
INCREMENTAL_ACTIONS = frozenset({"add_in_batches", "wait", "no_add"})
QUALITY_HORIZONS = {"5d": 5, "20d": 20, "60d": 60}
ATTRIBUTION_CATEGORIES = frozenset({
    "fact_error", "evidence_error", "thesis_error", "valuation_error",
    "timing_error", "risk_error", "execution_error", "unattributed",
})
ATTRIBUTION_STATUSES = frozenset({"proposed", "confirmed", "rejected"})
```

Implement:

- `normalize_portfolio_decision(payload)` with strict enum and `[0, 1]` confidence checks;
- `material_event_fingerprint(payload)` using canonical JSON and SHA-256;
- `is_materially_evaluable(payload)` that returns blocker codes instead of guessing missing values.

The fingerprint input must include account/instrument identity, both axes, snapshot hash, evidence cutoff/version, triggers, invalidation, benchmark identity, and decision profile/version. Sort set-like lists before hashing; preserve ordered plans where order changes meaning.

**Step 4: Run focused tests**

Expected: PASS.

**Step 5: Checkpoint**

Review the public enum wording with the user before persistence work. If commit authorization exists:

```bash
git add src/schemas/portfolio_decision_quality.py tests/test_portfolio_decision_quality_schema.py
git commit -m "feat: define portfolio decision quality contract"
```

### Task 2: Add Non-Destructive Quality Sidecars

**Files:**
- Modify: `src/storage.py`
- Create: `src/repositories/decision_quality_repo.py`
- Create: `tests/test_decision_quality_repo.py`
- Modify: `tests/test_storage.py`

**Step 1: Write failing storage tests**

Assert that a fresh database creates:

- `decision_signal_quality_contexts`;
- `decision_signal_quality_outcomes`;
- `decision_signal_attributions`.

Also assert:

- one context per material-event fingerprint;
- one quality outcome per `(signal_id, horizon, engine_version)`;
- one attribution per `(signal_id, horizon, engine_version)`;
- a second write cannot change immutable recommendation-time context;
- existing `decision_signals` and existing feedback/outcome rows remain readable.

**Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_storage.py tests/test_decision_quality_repo.py -q
```

Expected: FAIL because the models and repository do not exist.

**Step 3: Add the sidecar models**

Add `DecisionSignalQualityContextRecord` with these fields:

```text
id, signal_id, account_id, market, stock_code, instrument_type,
frozen_snapshot_hash, material_event_fingerprint,
position_action, incremental_action, confidence_by_horizon_json,
benchmark_market, benchmark_code, benchmark_type,
benchmark_evidence_url, benchmark_evidence_as_of,
decision_cutoff, context_status, unable_reasons_json,
created_at, updated_at
```

Recommendation-time fields are immutable. Add unique indexes for `signal_id` and `material_event_fingerprint`.

Add `DecisionSignalQualityOutcomeRecord` with:

```text
id, signal_id, horizon, engine_version, eval_status, unable_reason,
anchor_date, eval_window_days,
start_price, end_close, max_high, min_low,
stock_return_pct, benchmark_start_price, benchmark_end_close,
benchmark_return_pct, excess_return_pct,
max_favorable_excursion_pct, max_adverse_excursion_pct,
normalized_action_return_pct, decision_value_vs_hold_pct,
hindsight_regret_pct, decision_value_status,
position_action, incremental_action, market, instrument_type,
data_quality_level, created_at, updated_at
```

Add `DecisionSignalAttributionRecord` with:

```text
id, signal_id, horizon, engine_version,
category, status, summary, evidence_json,
counterexamples_json, user_note, created_at, updated_at
```

Do not add destructive migration code. New tables are created through `Base.metadata.create_all()`; an existing SQLite quality-context table may receive the nullable `instrument_type` column through idempotent `ALTER TABLE ADD COLUMN`, without rebuilding the table or backfilling an unevidenced value.

**Step 4: Add nullable feedback columns**

Add these nullable columns to `DecisionSignalFeedbackRecord` and to the existing idempotent SQLite shadow-column migration:

```text
human_position_action, human_incremental_action,
actual_position_action, actual_incremental_action,
decision_reason_code
```

Do not change the meaning of `human_decision` or `actual_manual_action`; keep them for compatibility.

**Step 5: Implement repository methods**

Implement explicit methods, not a generic arbitrary-field writer:

- `create_context_if_absent()`;
- `get_context_by_signal()`;
- `list_contexts_for_weekly_review()`;
- `upsert_quality_outcome()`;
- `list_quality_outcomes()`;
- `upsert_attribution()`;
- `list_confirmed_attributions()`.

Enforce immutable fields in the repository transaction.

**Step 6: Run tests**

Expected: PASS, including repeated database initialization.

**Step 7: Checkpoint**

Inspect SQLite schema and confirm no existing table was rebuilt or dropped. If commit authorization exists:

```bash
git add src/storage.py src/repositories/decision_quality_repo.py tests/test_storage.py tests/test_decision_quality_repo.py
git commit -m "feat: add decision quality sidecars"
```

### Task 3: Generate And Freeze Explicit Portfolio Decisions

**Files:**
- Modify: `src/analyzer.py`
- Modify: `src/agent/executor.py`
- Modify: `src/services/decision_signal_extractor.py`
- Modify: `src/services/portfolio_decision_gate.py`
- Modify: `src/services/decision_signal_service.py`
- Create: `src/services/decision_quality_service.py`
- Modify: `tests/test_decision_signal_extractor.py`
- Modify: `tests/test_decision_signal_service.py`
- Modify: `tests/test_portfolio_decision_gate.py`
- Create: `tests/test_decision_quality_service.py`

**Step 1: Write failing report-contract tests**

For portfolio analyses, require a `dashboard.portfolio_decision` object containing:

```json
{
  "position_action": "hold",
  "incremental_action": "wait",
  "confidence_by_horizon": {"5d": 0.55, "20d": 0.65, "60d": 0.60},
  "supporting_evidence": [],
  "opposing_evidence": [],
  "invalidation": "observable condition",
  "watch_conditions": ["observable trigger"],
  "next_review": "date or event"
}
```

Assert that ordinary non-portfolio reports remain backward compatible. For a portfolio report missing this object, assert quality-context status `insufficient_evidence`; do not infer an executable `ADD_IN_BATCHES` from the legacy single action.

**Step 2: Run focused tests and verify failure**

```bash
.venv/bin/python -m pytest \
  tests/test_decision_signal_extractor.py \
  tests/test_decision_signal_service.py \
  tests/test_portfolio_decision_gate.py \
  tests/test_decision_quality_service.py -q
```

Expected: FAIL on the new contract assertions.

**Step 3: Update both analysis prompt paths**

Add the two-axis object only when a `portfolio_context` is present. Preserve the existing canonical `action` for compatibility; derive it from the final gated position/incremental decision only after the two-axis object validates.

Do not ask the model for position percentages unless `risk_budget_evaluated=true` and the existing sizing contract supplies valid units. The prompt must label any analyst-created threshold as provisional.

**Step 4: Extract and gate the context**

In `decision_signal_extractor.py`, normalize `dashboard.portfolio_decision` and place the sanitized object in signal metadata. In `PortfolioDecisionGate`, apply existing blockers independently:

- blocked add/buy -> `incremental_action=wait`;
- blocked reduce/exit -> keep the current position action non-executable and expose blockers;
- quiet hold remains `position_action=hold` without inventing add permission.

Do not collapse the two axes back into one enum for quality evaluation.

**Step 5: Freeze context after gated signal persistence**

In `DecisionSignalService.create_gated_signal()`, after `create_signal()` succeeds, call `DecisionQualityService.freeze_context()` only when a portfolio decision object and a valid 64-character snapshot hash exist. Pass the already-built frozen snapshot; do not replay the portfolio again.

The quality-sidecar write is best-effort and logged. It must not roll back a successfully saved `DecisionSignal`, but the response metadata must expose `quality_context_status=failed` rather than silently pretending the context exists.

**Step 6: Add material deduplication tests**

Prove that identical decisions reuse one context, while changed axes, benchmark, trigger/invalidation, cutoff, or snapshot evidence create a distinct material event. Repeated daily display refreshes must not increase the evaluable sample count.

**Step 7: Run focused tests**

Expected: PASS.

**Step 8: Checkpoint**

If commit authorization exists:

```bash
git add src/analyzer.py src/agent/executor.py src/services/decision_signal_extractor.py \
  src/services/portfolio_decision_gate.py src/services/decision_signal_service.py \
  src/services/decision_quality_service.py tests/test_decision_signal_extractor.py \
  tests/test_decision_signal_service.py tests/test_portfolio_decision_gate.py \
  tests/test_decision_quality_service.py
git commit -m "feat: freeze two-axis portfolio decisions"
```

### Task 4: Extend Human Feedback Without Rewriting AI Context

**Files:**
- Modify: `api/v1/schemas/decision_signals.py`
- Modify: `api/v1/endpoints/decision_signals.py`
- Modify: `src/services/decision_signal_outcome_service.py`
- Modify: `src/repositories/decision_signal_outcome_repo.py`
- Modify: `tests/test_decision_signal_outcome_service.py`
- Modify: `tests/test_decision_signal_outcome_api.py`
- Modify: `tests/test_api_schema_pydantic.py`

**Step 1: Write failing service and API tests**

Extend shadow feedback with:

```json
{
  "human_decision": "modify",
  "human_position_action": "hold",
  "human_incremental_action": "no_add",
  "actual_position_action": "hold",
  "actual_incremental_action": "no_add",
  "decision_reason_code": "valuation_not_attractive"
}
```

Assert:

- `accept` may default human axes to the frozen AI axes;
- `modify` requires both human axes;
- `veto` requires a reason code or note;
- `no_action` is not treated as agreement;
- subsequent feedback may update human/actual fields but cannot mutate the AI context, snapshot hash, benchmark, or recommendation cutoff.

**Step 2: Run focused tests and verify failure**

```bash
.venv/bin/python -m pytest \
  tests/test_decision_signal_outcome_service.py \
  tests/test_decision_signal_outcome_api.py \
  tests/test_api_schema_pydantic.py -q
```

Expected: FAIL on missing fields and validation.

**Step 3: Implement schemas and service validation**

Add strict Pydantic literals for both axes and bounded public-text validation for the reason. Read the frozen context server-side; never accept client-supplied AI actions or benchmark identity.

Keep existing usefulness feedback optional but distinct from decision agreement.

**Step 4: Run tests**

Expected: PASS and existing legacy feedback tests remain unchanged.

**Step 5: Checkpoint**

If commit authorization exists:

```bash
git add api/v1/schemas/decision_signals.py api/v1/endpoints/decision_signals.py \
  src/services/decision_signal_outcome_service.py src/repositories/decision_signal_outcome_repo.py \
  tests/test_decision_signal_outcome_service.py tests/test_decision_signal_outcome_api.py \
  tests/test_api_schema_pydantic.py
git commit -m "feat: capture two-axis human decisions"
```

### Task 5: Implement The 5/20/60-Bar Quality Engine

**Files:**
- Modify: `src/repositories/stock_repo.py`
- Modify: `src/services/decision_quality_service.py`
- Modify: `src/repositories/decision_quality_repo.py`
- Create: `tests/test_decision_quality_outcomes.py`

**Step 1: Write failing deterministic outcome tests**

Use fixed daily bars for the instrument and benchmark. Cover:

- 5/20/60 exact forward trading bars;
- benchmark fixed in context;
- excess return;
- MFE and MAE from forward high/low;
- `HOLD` and `EXIT` normalized action returns;
- `REDUCE` without exposure and `ADD_IN_BATCHES` without a triggered tranche returning `decision_value_status=unable`;
- missing anchor/benchmark/forward bars;
- no use of bars at or after an unavailable horizon;
- adjusted-price/corporate-action marker mismatch failing closed.

Use these formulas, rounded only at serialization:

```text
stock_return = (end_close / start_price - 1) * 100
benchmark_return = (benchmark_end / benchmark_start - 1) * 100
excess_return = stock_return - benchmark_return
MFE = (max_high / start_price - 1) * 100
MAE = (min_low / start_price - 1) * 100

HOLD normalized return = stock_return
EXIT normalized return = 0
decision value vs HOLD = normalized action return - stock_return
hindsight regret = max(stock_return, 0) - normalized action return
```

Do not calculate normalized action return for `REDUCE` or `ADD_IN_BATCHES` until an exposure/tranche execution contract is available.

**Step 2: Run the test and verify failure**

```bash
.venv/bin/python -m pytest tests/test_decision_quality_outcomes.py -q
```

Expected: FAIL because the quality evaluator is not implemented.

**Step 3: Add exact paired-bar repository access**

Add a method that returns instrument and benchmark bars under the same cutoff/horizon contract. Do not silently substitute the nearest date independently for one side. Preserve explicit unable reasons such as:

- `missing_context`;
- `missing_anchor_price`;
- `missing_benchmark_identity`;
- `missing_benchmark_anchor`;
- `insufficient_forward_bars`;
- `corporate_action_adjustment_unknown`;
- `exposure_contract_missing`.

**Step 4: Implement versioned quality outcomes**

Use `decision-quality-v1`, leaving `decision-signal-v1` unchanged. Snapshot context dimensions into each outcome so later registry or signal changes cannot rewrite historical statistics.

**Step 5: Run focused and regression tests**

```bash
.venv/bin/python -m pytest \
  tests/test_decision_quality_outcomes.py \
  tests/test_decision_signal_outcome_service.py \
  tests/test_backtest_service.py -q
```

Expected: PASS.

**Step 6: Checkpoint**

If commit authorization exists:

```bash
git add src/repositories/stock_repo.py src/services/decision_quality_service.py \
  src/repositories/decision_quality_repo.py tests/test_decision_quality_outcomes.py
git commit -m "feat: evaluate portfolio decision quality"
```

### Task 6: Add Quality, Attribution, And Weekly Review APIs

**Files:**
- Modify: `api/v1/schemas/decision_signals.py`
- Modify: `api/v1/endpoints/decision_signals.py`
- Modify: `docs/architecture/api_spec.json`
- Create: `tests/test_decision_quality_api.py`
- Modify: `tests/test_decision_signal_docs.py`

**Step 1: Write failing API tests**

Add contracts for:

```text
GET  /api/v1/decision-signals/{signal_id}/quality
POST /api/v1/decision-signals/quality/outcomes/run
GET  /api/v1/decision-signals/quality/stats
PUT  /api/v1/decision-signals/{signal_id}/attributions/{horizon}
GET  /api/v1/decision-signals/quality/weekly-review
```

Define static `/quality/...` routes before `/{signal_id}` routes to avoid path capture.

Assert explicit `unable_reasons`, sample size, instrument concentration, and horizon maturity. A zero-sample response must be an empty state, not misleading zero performance.

**Step 2: Run tests and verify failure**

```bash
.venv/bin/python -m pytest tests/test_decision_quality_api.py tests/test_decision_signal_docs.py -q
```

Expected: FAIL with 404 or missing schema definitions.

**Step 3: Implement API schemas and endpoints**

The attribution PUT accepts only category, status, summary, evidence, counterexamples, and user note. It may confirm or reject a proposal but cannot edit the frozen context or outcome.

The weekly review response includes:

- material decisions only;
- triggered/expired conditions when evidenced;
- mature and pending horizons;
- AI-human disagreements;
- confirmed attribution counts;
- candidate patterns with sample size and counterexamples;
- no automatic personal-rule activation.

**Step 4: Update the checked-in API specification**

Regenerate `docs/architecture/api_spec.json` from the runtime FastAPI schema, then verify the resulting diff is limited to the new contracts:

```bash
.venv/bin/python -c 'import json; from pathlib import Path; from api.app import create_app; Path("docs/architecture/api_spec.json").write_text(json.dumps(create_app().openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")'
```

**Step 5: Run API tests**

Expected: PASS.

**Step 6: Checkpoint**

If commit authorization exists:

```bash
git add api/v1/schemas/decision_signals.py api/v1/endpoints/decision_signals.py \
  docs/architecture/api_spec.json tests/test_decision_quality_api.py \
  tests/test_decision_signal_docs.py
git commit -m "feat: expose decision quality review APIs"
```

### Task 7: Build The Portfolio Decision Review UI

**Files:**
- Modify: `apps/dsa-web/src/types/decisionSignals.ts`
- Modify: `apps/dsa-web/src/api/decisionSignals.ts`
- Modify: `apps/dsa-web/src/api/__tests__/decisionSignals.test.ts`
- Create: `apps/dsa-web/src/components/portfolio/PortfolioDecisionReview.tsx`
- Create: `apps/dsa-web/src/components/portfolio/__tests__/PortfolioDecisionReview.test.tsx`
- Modify: `apps/dsa-web/src/pages/PortfolioPage.tsx`
- Modify: `apps/dsa-web/src/pages/__tests__/PortfolioPage.test.tsx`
- Modify: `apps/dsa-web/src/i18n/uiText.ts`
- Modify: `apps/dsa-web/src/locales/featureText.ts`

**Step 1: Write failing API mapping tests**

Test snake/camel mapping for context, both action axes, horizon outcomes, attribution, and weekly review. Preserve unknown enum values as visible degraded text rather than silently mapping them to a valid action.

**Step 2: Write failing component tests**

Cover:

- one-screen expanded decision with both axes;
- `WAIT/INSUFFICIENT_EVIDENCE` and blocker display;
- 5/20/60 pending/completed/unable states;
- benchmark/excess/MFE/MAE values;
- `ACCEPT/MODIFY/VETO/NO_ACTION` flow;
- reason required for modify/veto;
- stale request suppression when switching holdings;
- no action button that implies order execution;
- mobile wrapping without overlapping labels or controls.

**Step 3: Run tests and verify failure**

```bash
cd apps/dsa-web
npm ci
npm test -- \
  src/api/__tests__/decisionSignals.test.ts \
  src/components/portfolio/__tests__/PortfolioDecisionReview.test.tsx \
  src/pages/__tests__/PortfolioPage.test.tsx
```

Expected: FAIL because the client contract/component does not exist.

**Step 4: Implement the client and review component**

Keep the holdings table scannable. Quiet rows show only the compact two-axis summary and next review point. Open the existing details surface for material exceptions; do not add nested cards or a new navigation page.

Use existing icon/button, badge, alert, typography, and i18n patterns. Keep all user-visible enums localized. Do not expose raw evidence JSON by default.

**Step 5: Run focused tests**

Expected: PASS.

**Step 6: Run Web lint and build**

```bash
cd apps/dsa-web
npm run lint
npm run build
```

Expected: both commands exit 0.

**Step 7: Perform visual verification**

Start the existing local backend and Web dev server, then verify desktop and mobile states with Playwright. Capture screenshots outside the repository for the eventual PR description. Verify no overlap, no false executable wording, and a visible unavailable state.

**Step 8: Checkpoint**

If commit authorization exists:

```bash
git add apps/dsa-web/src/types/decisionSignals.ts apps/dsa-web/src/api/decisionSignals.ts \
  apps/dsa-web/src/api/__tests__/decisionSignals.test.ts \
  apps/dsa-web/src/components/portfolio/PortfolioDecisionReview.tsx \
  apps/dsa-web/src/components/portfolio/__tests__/PortfolioDecisionReview.test.tsx \
  apps/dsa-web/src/pages/PortfolioPage.tsx apps/dsa-web/src/pages/__tests__/PortfolioPage.test.tsx \
  apps/dsa-web/src/i18n/uiText.ts apps/dsa-web/src/locales/featureText.ts
git commit -m "feat: add portfolio decision review loop"
```

### Task 8: Add Confirmed Learning Patterns

**Files:**
- Modify: `src/services/decision_quality_service.py`
- Modify: `src/repositories/decision_quality_repo.py`
- Modify: `api/v1/schemas/decision_signals.py`
- Modify: `apps/dsa-web/src/components/portfolio/PortfolioDecisionReview.tsx`
- Create: `tests/test_decision_quality_learning.py`
- Modify: `apps/dsa-web/src/components/portfolio/__tests__/PortfolioDecisionReview.test.tsx`

**Step 1: Write failing aggregation tests**

Assert that candidate patterns:

- use confirmed attributions only;
- display eligible sample count and counterexamples;
- do not merge different product types or horizons without a visible segment;
- remain `observed` until explicit user approval;
- never mutate decision scoring, prompts, or risk policy.

**Step 2: Run tests and verify failure**

```bash
.venv/bin/python -m pytest tests/test_decision_quality_learning.py -q
```

Expected: FAIL because no learning aggregation exists.

**Step 3: Implement descriptive aggregation**

Aggregate confirmed attribution and AI-human comparison rows into read-only candidate patterns. Use exact counts and rates; do not introduce an arbitrary weighted personal score. Keep single-instrument dominance and correlated repeated-event warnings visible.

**Step 4: Add weekly case-card UI**

Show the seven approved questions, confirmed attribution, contrary evidence, and candidate-rule status. Approval changes only the learning-sidecar status; it does not alter risk policy or automatically affect recommendations in v1.

**Step 5: Run backend and frontend focused tests**

Expected: PASS.

**Step 6: Checkpoint**

If commit authorization exists:

```bash
git add src/services/decision_quality_service.py src/repositories/decision_quality_repo.py \
  api/v1/schemas/decision_signals.py tests/test_decision_quality_learning.py \
  apps/dsa-web/src/components/portfolio/PortfolioDecisionReview.tsx \
  apps/dsa-web/src/components/portfolio/__tests__/PortfolioDecisionReview.test.tsx
git commit -m "feat: summarize confirmed decision lessons"
```

### Task 9: Document, Verify, And Start Shadow Operation

**Files:**
- Modify: `docs/decision-signals.md`
- Modify: `docs/portfolio-research-workflow.md`
- Modify: `docs/full-guide.md`
- Modify: `docs/CHANGELOG.md`
- Review: `docs/INDEX.md`
- Review: `docs/INDEX_EN.md`

**Step 1: Update focused documentation**

Document:

- two independent action axes;
- 5/20/60 roles;
- benchmark and counterfactual limits;
- quality `unable` reasons;
- human feedback semantics;
- attribution and candidate-rule confirmation;
- no automatic execution or risk-policy mutation;
- 20-day operational gate and 60-bar long-term evidence limit.

Add one flat `[Unreleased]` entry in the required format. Do not expand `README.md`. Evaluate whether English topic documentation needs synchronization and record the reason if it is deferred.

**Step 2: Run focused backend verification**

```bash
.venv/bin/python -m pytest \
  tests/test_portfolio_decision_quality_schema.py \
  tests/test_decision_quality_repo.py \
  tests/test_decision_quality_service.py \
  tests/test_decision_quality_outcomes.py \
  tests/test_decision_quality_api.py \
  tests/test_decision_quality_learning.py \
  tests/test_decision_signal_outcome_service.py \
  tests/test_decision_signal_outcome_api.py -q
```

Expected: PASS.

**Step 3: Run the repository backend gate**

```bash
./scripts/ci_gate.sh
```

Expected: exit 0. If unrelated pre-existing failures remain, capture exact failures and rerun the closest affected tests; do not report the gate as passed.

**Step 4: Run Web verification**

```bash
cd apps/dsa-web
npm run lint
npm run build
```

Expected: both exit 0.

**Step 5: Run static integrity checks**

From repository root:

```bash
python -m py_compile \
  src/schemas/portfolio_decision_quality.py \
  src/repositories/decision_quality_repo.py \
  src/services/decision_quality_service.py
git diff --check
```

Expected: both exit 0.

**Step 6: Perform a real local shadow smoke test**

Using service/API paths only:

1. Select one verified held ordinary instrument and one special product if available.
2. Generate a frozen research snapshot.
3. Create a portfolio decision signal without placing an order.
4. Confirm the quality context uses the same snapshot hash and fixed benchmark.
5. Submit human feedback and verify the AI context remains unchanged.
6. Run only mature outcomes and verify pending/unable horizons remain explicit.
7. Verify the ledger transaction/cash counts are unchanged.

Do not seed or repair real portfolio data as part of the smoke test without separate authorization.

**Step 7: Prepare prospective-validation notes**

Record the shadow start date, engine versions, eligible material events, missing evidence, and maturity dates. Do not claim return improvement at launch.

**Step 8: Final checkpoint**

Review the complete diff against the approved design. Confirm the PR description will include affected-page screenshots and exact verification results. If commit authorization exists:

```bash
git add docs/decision-signals.md docs/portfolio-research-workflow.md docs/full-guide.md \
  docs/CHANGELOG.md docs/architecture/api_spec.json
git commit -m "docs: document portfolio decision quality loop"
```

Do not push or create a PR without separate explicit authorization.
