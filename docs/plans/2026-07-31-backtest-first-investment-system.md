# Backtest-First Investment Decision System Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deliver one simple user-facing investment instruction per holding and an auditable strategy-version scorecard backed by deterministic, leakage-resistant historical testing.

**Architecture:** Preserve the existing two-axis portfolio contract, DSA ledger, frozen decision-quality context, and legacy backtest compatibility. Add a small deterministic projection for user-facing instructions, repair outcome integrity, then add immutable strategy versions and validation runs consumed by a bounded no-network replay service. Extend the existing Backtest page rather than creating a parallel application.

**Tech Stack:** Python 3.11, SQLAlchemy/SQLite, FastAPI/Pydantic, pytest, React 19, TypeScript, Vitest, existing DSA portfolio and decision-quality modules.

---

## Scope And Guardrails

In scope:

- simple Chinese instructions for existing holdings;
- immutable strategy version records;
- immutable historical validation runs;
- 5/20/60-bar metrics separated by horizon, market, product, action, and market regime;
- explicit fees, tax, slippage, FX, execution-price and benchmark assumptions;
- strategy scorecards in the existing `/backtest` page;
- manual-only validation-stage transitions.

Out of scope:

- opportunity discovery;
- machine learning or automatic weighting;
- automatic strategy promotion;
- broker/order integration;
- scheduler or live execution;
- multi-agent orchestration;
- generic experiment-platform abstractions.

The accepted current database is the baseline. New tables are additive. Existing holdings, trades, DecisionSignals and legacy backtest rows are never rewritten or copied into a second ledger.

## Task 1: Add One User-Facing Instruction Projection

**Files:**

- Create: `src/schemas/portfolio_instruction.py`
- Test: `tests/test_portfolio_instruction.py`
- Modify: `src/services/portfolio_research_baseline_service.py`
- Modify: `src/services/decision_quality_service.py`
- Modify: `api/v1/schemas/portfolio.py`
- Modify: `tests/test_portfolio_research_baseline_service.py`
- Modify: `tests/test_decision_quality_api.py`
- Create: `apps/dsa-web/src/utils/portfolioInstruction.ts`
- Test: `apps/dsa-web/src/utils/__tests__/portfolioInstruction.test.ts`
- Modify: `apps/dsa-web/src/types/decisionSignals.ts`
- Modify: `apps/dsa-web/src/components/portfolio/PortfolioDecisionReview.tsx`
- Modify: `apps/dsa-web/src/components/portfolio/__tests__/PortfolioDecisionReview.test.tsx`
- Modify: `apps/dsa-web/src/i18n/uiText.ts`

### Step 1: Write failing backend mapping tests

Cover the exact contract:

```python
@pytest.mark.parametrize(
    ("position_action", "incremental_action", "blocked", "expected"),
    [
        ("hold", "add_in_batches", False, "add"),
        ("hold", "wait", False, "hold"),
        ("hold", "no_add", False, "hold"),
        ("reduce", "no_add", False, "reduce"),
        ("exit", "no_add", False, "exit"),
        ("hold", "add_in_batches", True, "insufficient"),
    ],
)
def test_project_holding_instruction(...):
    ...

def test_rejects_contradictory_reduce_and_add():
    with pytest.raises(ValueError, match="contradictory_portfolio_actions"):
        project_holding_instruction("reduce", "add_in_batches", blocked=False)
```

Run:

```bash
.venv/bin/python -m pytest tests/test_portfolio_instruction.py -q
```

Expected: FAIL because `src.schemas.portfolio_instruction` does not exist.

### Step 2: Implement and expose the canonical backend projection

Define an internal `HoldingInstruction` literal with `add|hold|reduce|exit|insufficient`. The function must not infer sizing, must not change the stored two-axis decision, and must reject contradictory action pairs. Add the computed `user_instruction` to portfolio baseline items and decision-quality detail responses without removing either internal axis.

Add API regression assertions that both paths return the same instruction for the same internal pair and blocker state.

Run the focused backend test again. Expected: PASS.

### Step 3: Write failing Web mapping and component tests

Assert that the normal review header shows exactly one primary label:

```text
加仓 | 持有 | 减仓 | 清仓 | 资料不足
```

Assert that the internal labels “现有仓位” and “新增资金” are absent from the default view and the modify form. Keep both axes in the API request used for feedback; do not change the backend contract in this task.

Run:

```bash
cd apps/dsa-web && npm test -- --run src/utils/__tests__/portfolioInstruction.test.ts src/components/portfolio/__tests__/PortfolioDecisionReview.test.tsx
```

Expected: FAIL because the projection and new labels do not exist.

### Step 4: Implement simple presentation and reverse mapping for feedback

Display the backend-provided `userInstruction` as a single action badge plus reason, risk, condition and review point already present in the quality payload. The frontend utility only maps a user's simple modification back to the required two-axis API payload:

- `add -> hold + add_in_batches`;
- unchanged `hold` preserves the current `hold + wait|no_add` pair;
- newly selected `hold -> hold + no_add`;
- `reduce -> reduce + no_add`;
- `exit -> exit + no_add`.

Keep advanced/internal values out of the ordinary UI. Do not add a new card or route.

Run the focused Web tests again. Expected: PASS.

### Step 5: Verify Task 1

```bash
.venv/bin/python -m pytest tests/test_portfolio_instruction.py tests/test_portfolio_research_baseline_service.py -q
.venv/bin/python -m pytest tests/test_decision_quality_api.py -q
cd apps/dsa-web && npm test -- --run src/utils/__tests__/portfolioInstruction.test.ts src/components/portfolio/__tests__/PortfolioDecisionReview.test.tsx src/pages/__tests__/PortfolioPage.test.tsx
```

Do not commit without explicit user authorization.

## Task 2: Repair Decision-Outcome Integrity Before Strategy Testing

**Files:**

- Modify: `src/schemas/portfolio_decision_quality.py`
- Modify: `src/repositories/decision_quality_repo.py`
- Modify: `src/services/decision_quality_service.py`
- Modify: `src/storage.py`
- Test: `tests/test_portfolio_decision_quality_schema.py`
- Test: `tests/test_decision_quality_repo.py`
- Test: `tests/test_decision_quality_service.py`
- Test: `tests/test_decision_quality_outcomes.py`
- Test: `tests/test_decision_quality_learning.py`

### Step 1: Add regression tests for immutable completed outcomes

Create a completed outcome, change the supplied market bars, evaluate the same `(signal_id, horizon, engine_version)` again, and assert that the stored result remains unchanged. A correction must require a new engine or data revision identifier.

Run:

```bash
.venv/bin/python -m pytest tests/test_decision_quality_repo.py tests/test_decision_quality_outcomes.py -q
```

Expected: at least one new test FAILS because current upsert behavior can update an existing row.

### Step 2: Add regression tests for execution timing

For an intraday decision cutoff, assert that the anchor is the first tradable bar after the cutoff. The cutoff date's not-yet-visible close must never be used.

Expected: FAIL against the current calendar-date anchor behavior.

### Step 3: Add regression tests for sample identity and horizon separation

Assert that:

- display-only snapshot refreshes do not create an independent material sample;
- a real action, evidence, benchmark or policy change does create a new sample;
- 5d, 20d and 60d aggregates remain separate;
- weekly review accepts an explicit start/end window and excludes older rows.

Run:

```bash
.venv/bin/python -m pytest tests/test_portfolio_decision_quality_schema.py tests/test_decision_quality_service.py tests/test_decision_quality_learning.py -q
```

Expected: FAIL for the new integrity cases.

### Step 4: Implement the minimum integrity fixes

- replace completed-outcome upsert with create-or-return-existing semantics;
- add explicit `data_revision` identity where a corrected result is required;
- anchor evaluation to the first tradable bar after the decision cutoff;
- remove display-only snapshot refresh identity from material sample counting while retaining the frozen hash for audit;
- require horizon and date-window filters in aggregate review.

Use additive SQLite columns and the existing schema-initialization pattern. Do not rewrite accepted database rows.

### Step 5: Run all decision-quality tests

```bash
.venv/bin/python -m pytest \
  tests/test_portfolio_decision_quality_schema.py \
  tests/test_decision_quality_repo.py \
  tests/test_decision_quality_service.py \
  tests/test_decision_quality_outcomes.py \
  tests/test_decision_quality_learning.py \
  tests/test_decision_quality_api.py -q
```

Expected: PASS.

Do not commit without explicit user authorization.

## Task 3: Add Immutable Strategy Versions And Validation Runs

**Files:**

- Create: `src/schemas/strategy_validation.py`
- Create: `src/repositories/strategy_validation_repo.py`
- Create: `src/services/strategy_registry_service.py`
- Modify: `src/storage.py`
- Test: `tests/test_strategy_validation_schema.py`
- Test: `tests/test_strategy_validation_repo.py`
- Test: `tests/test_strategy_registry_service.py`

### Step 1: Write failing schema tests

Define a minimal version manifest containing:

```python
{
    "strategy_key": "portfolio-current-policy",
    "version": "1.0.0",
    "name": "当前持仓策略",
    "change_summary": "冻结当前规则作为比较基线",
    "changed_dimension": "baseline",
    "markets": ["cn", "hk", "us"],
    "instrument_types": ["equity", "etf", "qdii", "adr_ads", "daily_leveraged_product"],
    "horizons": ["5d", "20d", "60d"],
    "evaluation_mode": "forward_only",
    "policy": {},
    "cost_model": {},
    "benchmark_policy": {},
    "status": "draft",
}
```

Allow only `historical_and_forward` or `forward_only` for `evaluation_mode`. Reject unknown fields, missing scope, multiple changed dimensions, absent cost assumptions, or an unsupported status.

Run:

```bash
.venv/bin/python -m pytest tests/test_strategy_validation_schema.py -q
```

Expected: FAIL because the schema does not exist.

### Step 2: Write failing repository tests

Add tests for three additive tables:

- `portfolio_strategy_versions` with unique `(strategy_key, version)`;
- `portfolio_strategy_validation_runs` with immutable protocol, dataset hash, engine version, status and result payload.
- `portfolio_strategy_transitions` with append-only from/to status, human reason and timestamp.

Assert that an existing strategy version and a completed validation run cannot be updated in place.

Expected: FAIL because the models and repository do not exist.

### Step 3: Implement models and repository

Follow the current SQLAlchemy metadata and `schema_migrations` baseline pattern. Store structured payloads as canonical JSON with a SHA-256 manifest hash. Do not add a second holdings, cash or trades table.

### Step 4: Write failing lifecycle tests

Allowed manual lifecycle:

```text
draft -> backtest_running -> backtest_failed
draft -> backtest_running -> simulation -> small_capital -> active -> retired
```

Require a completed qualifying run before entering `simulation`, a completed forward observation before `small_capital`, and an explicit human reason for every transition. Reject automatic promotion flags.

### Step 5: Implement `StrategyRegistryService`

The service creates immutable versions and append-only transition records. It must never activate a strategy as a side effect of saving a test result.

### Step 6: Verify Task 3

```bash
.venv/bin/python -m pytest \
  tests/test_strategy_validation_schema.py \
  tests/test_strategy_validation_repo.py \
  tests/test_strategy_registry_service.py \
  tests/test_storage.py -q
```

Expected: PASS.

Do not commit without explicit user authorization.

## Task 4: Build The Bounded Historical Strategy Validator

**Files:**

- Create: `src/services/portfolio_strategy_backtest_service.py`
- Create: `strategies/portfolio_hold_baseline_v1.json`
- Create: `strategies/portfolio_current_policy_v1.json`
- Create: `tests/fixtures/strategy_validation/minimal_portfolio_events.json`
- Create: `tests/test_portfolio_strategy_backtest_service.py`
- Create: `scripts/portfolio_strategy_backtest.py`
- Create: `tests/test_portfolio_strategy_backtest_cli.py`

### Step 1: Write failing no-leakage tests

The validator must fail closed when:

- any required event lacks its decision-time cutoff;
- identity, product type, adjusted-price identity, benchmark or FX is missing;
- a bar at or before an intraday cutoff is used as execution;
- current news, current registry or any network/data-provider method is called;
- a required fee, tax, slippage or product cost is missing;
- a historical event contains only free-text AI analysis without replayable structured inputs.

Run:

```bash
.venv/bin/python -m pytest tests/test_portfolio_strategy_backtest_service.py -q
```

Expected: FAIL because the validator does not exist.

### Step 2: Freeze the comparison baseline and current policy honestly

Create two immutable manifests:

- `portfolio_hold_baseline_v1` is a deterministic `hold + no_add` comparison baseline and may use `historical_and_forward` validation;
- `portfolio_current_policy_v1` records the currently active DSA decision path and must remain `forward_only` unless every rule and input is demonstrably reproducible at the historical cutoff.

The current-policy scorecard must say “历史回测不可用，等待模拟样本” when its AI/news evidence layer cannot be replayed. It must never borrow the hold-baseline result or legacy analysis-record hit rate.

### Step 3: Write failing deterministic-result tests

For the same strategy manifest, frozen event dataset and engine version, assert byte-equivalent canonical results and the same result hash across repeated runs.

### Step 4: Write failing metric-separation tests

Require separate buckets for:

- 5d, 20d and 60d;
- market;
- product type;
- displayed instruction;
- market regime;
- development period and untouched validation period.

Each bucket reports sample count, action-defined win rate, net return after cost, benchmark excess, maximum drawdown, average gain/loss, turnover, total cost and unable count. Never aggregate incompatible products into one performance conclusion.

### Step 5: Implement the minimal validator

The service consumes only a frozen JSON-compatible dataset and a validated strategy manifest. It evaluates deterministic policy rules, applies the frozen execution and cost convention, and writes one immutable validation run through `StrategyRegistryService`.

AI/news-dependent rules must be marked `forward_only`; the historical report lists them as not tested instead of substituting current evidence.

### Step 6: Add the CLI

Contract:

```bash
.venv/bin/python scripts/portfolio_strategy_backtest.py \
  --dataset tests/fixtures/strategy_validation/minimal_portfolio_events.json \
  --strategy <strategy-manifest.json> \
  --output <run-report.json>
```

The CLI must not start DSA, use the network, mutate holdings, or change strategy status.

### Step 7: Verify Task 4

```bash
.venv/bin/python -m pytest \
  tests/test_portfolio_strategy_backtest_service.py \
  tests/test_portfolio_strategy_backtest_cli.py -q
```

Expected: PASS.

Do not commit without explicit user authorization.

## Task 5: Add Strategy Validation APIs

**Files:**

- Create: `api/v1/schemas/strategy_validation.py`
- Create: `api/v1/endpoints/strategy_validation.py`
- Modify: `api/v1/endpoints/__init__.py`
- Modify: `api/v1/router.py`
- Modify: `api/v1/schemas/__init__.py`
- Test: `tests/test_strategy_validation_api.py`
- Modify: `docs/architecture/api_spec.json`

### Step 1: Write failing API contract tests

Add read endpoints:

```text
GET /api/v1/strategy-validation/strategies
GET /api/v1/strategy-validation/strategies/{strategy_key}/versions/{version}
GET /api/v1/strategy-validation/runs/{run_id}
```

Add explicit human-write endpoints:

```text
POST /api/v1/strategy-validation/strategies
POST /api/v1/strategy-validation/runs
POST /api/v1/strategy-validation/strategies/{strategy_key}/versions/{version}/transition
```

Assert that create operations are idempotent by manifest hash, completed runs are immutable, transitions require a human reason, and there is no endpoint for automatic promotion or order generation.

Run:

```bash
.venv/bin/python -m pytest tests/test_strategy_validation_api.py -q
```

Expected: FAIL because the router does not exist.

### Step 2: Implement additive Pydantic schemas and endpoints

Return both internal status and simple Chinese display status. Keep validation blockers and unable reasons visible. Never return a global score that can hide a failed hard requirement.

### Step 3: Update the checked-in API specification

Regenerate or update `docs/architecture/api_spec.json` through the repository's existing API-spec workflow. Do not hand-edit generated sections when a generator is available.

### Step 4: Verify Task 5

```bash
.venv/bin/python -m pytest tests/test_strategy_validation_api.py tests/test_api_schema_pydantic.py -q
```

Expected: PASS.

Do not commit without explicit user authorization.

## Task 6: Turn The Backtest Page Into A Strategy Scorecard

**Files:**

- Create: `apps/dsa-web/src/types/strategyValidation.ts`
- Create: `apps/dsa-web/src/api/strategyValidation.ts`
- Create: `apps/dsa-web/src/components/backtest/StrategyScorecard.tsx`
- Create: `apps/dsa-web/src/components/backtest/__tests__/StrategyScorecard.test.tsx`
- Modify: `apps/dsa-web/src/pages/BacktestPage.tsx`
- Modify: `apps/dsa-web/src/pages/__tests__/BacktestPage.test.tsx`
- Modify: `apps/dsa-web/src/i18n/uiText.ts`
- Modify: `apps/dsa-web/src/locales/featureText.ts`

### Step 1: Write failing API-adapter tests

Test snake-case to camel-case mapping for strategy versions, run metrics, unable reasons and user-facing status labels.

Run:

```bash
cd apps/dsa-web && npm test -- --run src/api/__tests__/strategyValidation.test.ts
```

Expected: FAIL because the API adapter does not exist.

### Step 2: Write failing scorecard tests

The first view must show:

- strategy name and version;
- exactly what changed;
- current stage;
- sample count;
- win rate with its action definition;
- net return after costs;
- benchmark excess;
- maximum drawdown;
- average gain/loss;
- total costs;
- worst period and failure scenarios;
- visible “资料不足” reasons.

The page must label legacy analysis-record results as “历史分析记录，不能代表完整策略表现”. It must not show internal `champion`, `challenger`, `position_action` or `incremental_action` terms.

Expected: FAIL against the current page.

### Step 3: Implement the scorecard in the existing route

Use a compact strategy list and one unframed detail section. Keep the legacy results available in a secondary tab for compatibility. Do not create a dashboard of decorative cards.

### Step 4: Add manual transition controls

Only show transitions allowed by the API. Require a reason for entering simulation, small-capital validation, active use or retirement. Never expose automatic promotion.

### Step 5: Verify Task 6

```bash
cd apps/dsa-web && npm test -- --run \
  src/api/__tests__/strategyValidation.test.ts \
  src/components/backtest/__tests__/StrategyScorecard.test.tsx \
  src/pages/__tests__/BacktestPage.test.tsx
cd apps/dsa-web && npm run lint && npm run build
```

Expected: tests, lint and build PASS.

Do not commit without explicit user authorization.

## Task 7: Document The Workflow And Run Full Verification

**Files:**

- Create: `docs/strategy-validation.md`
- Modify: `docs/INDEX.md`
- Modify: `docs/INDEX_EN.md`
- Modify: `docs/CHANGELOG.md`
- Modify only if needed for behavior parity: `docs/decision-signals.md`

### Step 1: Document the user workflow

Document in simple Chinese:

```text
固定策略 -> 历史回测 -> 模拟观察 -> 小额验证 -> 正式使用或停用
```

Explain action labels, scorecard metrics, failure behavior, AI/news forward-only limits, and manual confirmation boundaries. Add a short English index entry; a full English duplicate is not required unless an existing paired topic document is introduced.

### Step 2: Add one flat changelog entry

Under `[Unreleased]`, add independent flat entries using the repository's required format. Do not add category headings.

### Step 3: Run focused backend verification

```bash
.venv/bin/python -m py_compile \
  src/schemas/portfolio_instruction.py \
  src/schemas/strategy_validation.py \
  src/repositories/strategy_validation_repo.py \
  src/services/strategy_registry_service.py \
  src/services/portfolio_strategy_backtest_service.py \
  api/v1/schemas/strategy_validation.py \
  api/v1/endpoints/strategy_validation.py \
  scripts/portfolio_strategy_backtest.py

.venv/bin/python -m pytest \
  tests/test_portfolio_instruction.py \
  tests/test_portfolio_decision_quality_schema.py \
  tests/test_decision_quality_repo.py \
  tests/test_decision_quality_service.py \
  tests/test_decision_quality_outcomes.py \
  tests/test_decision_quality_learning.py \
  tests/test_strategy_validation_schema.py \
  tests/test_strategy_validation_repo.py \
  tests/test_strategy_registry_service.py \
  tests/test_portfolio_strategy_backtest_service.py \
  tests/test_portfolio_strategy_backtest_cli.py \
  tests/test_strategy_validation_api.py -q
```

Expected: PASS.

### Step 4: Run the repository backend gate

```bash
./scripts/ci_gate.sh
```

Expected: PASS. Report the exact pass/fail totals and any warnings.

### Step 5: Run full Web verification

```bash
cd apps/dsa-web && npm test
cd apps/dsa-web && npm run lint
cd apps/dsa-web && npm run build
```

Expected: PASS.

### Step 6: Perform browser acceptance

Start the supported API-only application and Web dev server without scheduler, broker, orders, external workers or automatic multi-agent execution. Verify the exact modified checkout is served.

At desktop and mobile widths confirm:

- Portfolio review shows one simple instruction and no internal two-axis terminology by default;
- Backtest opens on strategy scorecards;
- failed and unable runs remain visibly blocked;
- legacy analysis results carry the compatibility warning;
- long strategy names and failure reasons do not overlap controls.

Capture screenshots outside the repository for delivery evidence.

### Step 7: Inspect the final diff

```bash
git diff --check
git status --short
git diff -- \
  src/schemas/portfolio_instruction.py \
  src/schemas/strategy_validation.py \
  src/repositories/decision_quality_repo.py \
  src/repositories/strategy_validation_repo.py \
  src/services/decision_quality_service.py \
  src/services/strategy_registry_service.py \
  src/services/portfolio_strategy_backtest_service.py \
  api/v1/endpoints/strategy_validation.py \
  apps/dsa-web/src/components/portfolio/PortfolioDecisionReview.tsx \
  apps/dsa-web/src/pages/BacktestPage.tsx \
  docs/strategy-validation.md \
  docs/CHANGELOG.md
```

Confirm no unrelated refactor, portfolio mutation, automatic promotion, order path, dependency change, root configuration change or remote Git operation was introduced.

Do not commit, push, merge, rebase or create a PR without explicit user authorization.

## Execution Order

Execute Tasks 1 through 7 serially. Tasks 2 through 5 touch shared schema, persistence and API contracts and are not suitable for parallel edits. Stop after any failed integrity gate; historical profit or a passing UI test cannot override a data-leakage, benchmark, identity, cost or result-immutability failure.

The first useful checkpoint is Tasks 1 and 2: simple user instructions plus trustworthy outcome evaluation. The first complete product slice is Tasks 1 through 6: one honestly labeled current-policy version, one deterministic hold-baseline validation run and one user-visible scorecard. This proves the validation path; it does not misstate the AI/news policy as historically validated. Simulation automation and small-capital workflow are intentionally deferred to the next approved slice.
