# DSA Portfolio Control Plane Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make DSA the only writable portfolio control plane and route daily research from its ledger through deterministic identity, risk, freshness, and human-confirmation gates.

**Architecture:** Add stable instrument and risk-policy tables beside the replayed ledger, expose a pure holdings universe and frozen research snapshot, then apply one fail-closed gate before actionable signals are persisted or shown. Preserve existing watchlist behavior as an explicit compatibility mode and keep external workers behind artifact-only routing contracts.

**Tech Stack:** Python 3, SQLAlchemy, SQLite, FastAPI/Pydantic, pytest/unittest, React/TypeScript, Vitest, Tailwind, existing DSA services and repositories.

---

### Task 1: Persist Instrument Identity And Portfolio Risk Policy

**Files:**
- Modify: `src/storage.py`
- Modify: `src/repositories/portfolio_repo.py`
- Test: `tests/test_storage.py`
- Test: `tests/test_portfolio_service.py`

**Steps:**
1. Add failing storage tests for `portfolio_instruments` and singleton `portfolio_risk_policy` table creation and round trips.
2. Run the focused tests and confirm failures are caused by missing models/repository methods.
3. Add the two SQLAlchemy models, constraints, validation constants, and repository CRUD methods.
4. Ensure no default risk-policy row or inferred instrument row is inserted during database initialization.
5. Re-run focused tests and confirm they pass.

### Task 2: Add Domain Services And Validation

**Files:**
- Create: `src/services/portfolio_instrument_service.py`
- Create: `src/services/portfolio_risk_policy_service.py`
- Test: `tests/test_portfolio_instrument_service.py`
- Test: `tests/test_portfolio_risk_policy_service.py`

**Steps:**
1. Write failing tests for canonical symbol/market identity, enum validation, product-specific required fields, singleton policy validation, and partial-update behavior.
2. Run the tests and verify expected failures.
3. Implement service validation and repository orchestration without provider-side auto-verification.
4. Re-run the focused tests.

### Task 3: Derive The Daily Universe From The Ledger

**Files:**
- Create: `src/services/portfolio_universe_service.py`
- Modify: `src/config.py`
- Modify: `main.py`
- Modify: `src/core/pipeline.py`
- Modify: `.env.example`
- Test: `tests/test_portfolio_universe_service.py`
- Test: `tests/test_config_env_compat.py`
- Test: `tests/test_main_cli.py` or the closest existing main scheduling test

**Steps:**
1. Write failing tests for `portfolio_holdings`, `watchlist`, and `union`, including deduplication and canonical ordering.
2. Add a failing test proving ledger read failure/empty ledger does not fall back to `STOCK_LIST` in holdings mode.
3. Add `ANALYSIS_UNIVERSE_SOURCE` with compatibility default `watchlist`.
4. Implement the pure cached-position resolver and wire it into batch/scheduled analysis.
5. Re-run the focused tests.

### Task 4: Produce A Frozen Read-Only Research Snapshot

**Files:**
- Create: `src/services/portfolio_research_snapshot_service.py`
- Modify: `api/v1/schemas/portfolio.py`
- Modify: `api/v1/endpoints/portfolio.py`
- Test: `tests/test_portfolio_research_snapshot_service.py`
- Test: `tests/test_portfolio_api.py`

**Steps:**
1. Write failing tests for deterministic hashing, sorted canonical content, blockers, and privacy exclusions.
2. Add a test that captures database row counts before/after the service and GET endpoint and proves no writes occur.
3. Implement the service, response schemas, and `/portfolio/research-snapshot` endpoint.
4. Re-run focused service/API tests.

### Task 5: Generalize HK07709 Into Registry-Driven Analysis Policy

**Files:**
- Modify: `src/services/portfolio_analysis_policy.py`
- Modify: `api/v1/endpoints/portfolio.py`
- Modify: `src/agent/executor.py`
- Modify: `tests/test_portfolio_analysis_policy.py`
- Modify: `tests/test_portfolio_api.py`
- Modify: `tests/test_agent_executor.py`

**Steps:**
1. Add failing tests for equity, ETF, QDII, ADR/ADS, and daily-reset registry records.
2. Prove unknown/provisional records do not silently receive executable product policy.
3. Replace symbol-only resolution with registry-backed policy generation while retaining the HK07709 compatibility adapter.
4. Re-run focused tests and confirm the existing HK07709 behavior remains covered.

### Task 6: Apply The Fail-Closed Decision Gate

**Files:**
- Create: `src/services/portfolio_decision_gate.py`
- Modify: `src/services/decision_signal_extractor.py`
- Modify: `src/services/decision_signal_service.py`
- Test: `tests/test_portfolio_decision_gate.py`
- Modify: `tests/test_decision_signal_extractor.py`

**Steps:**
1. Write one failing test per hard blocker: identity, price, FX, risk policy, QDII premium, ADR conversion, daily reset/underlying, and lot size.
2. Add tests proving missing news alone does not block a quiet hold but blocks an event-dependent actionable signal.
3. Implement a pure gate returning `raw_action`, `final_action`, blockers, required capability, and completeness label.
4. Integrate it before persistence/display metadata; workers cannot clear hard blockers.
5. Re-run focused tests.

### Task 7: Make Risk GET Paths Pure

**Files:**
- Modify: `src/services/portfolio_risk_service.py`
- Modify: `api/v1/endpoints/portfolio.py`
- Modify: `tests/test_portfolio_service.py`
- Modify: `tests/test_portfolio_api.py`

**Steps:**
1. Add failing tests proving GET risk does not add daily snapshots.
2. Add a failing test for incomplete drawdown history returning an unavailable block and explicit limitation.
3. Separate explicit snapshot backfill from risk calculation and use pure mode in GET/research paths.
4. Re-run focused risk/API tests, including existing sector timeout tests.

### Task 8: Expose Management APIs And Web Controls

**Files:**
- Modify: `api/v1/schemas/portfolio.py`
- Modify: `api/v1/endpoints/portfolio.py`
- Modify: `apps/dsa-web/src/api/portfolio.ts`
- Modify: `apps/dsa-web/src/pages/PortfolioPage.tsx`
- Modify: `apps/dsa-web/src/pages/__tests__/PortfolioPage.test.tsx`
- Modify: `apps/dsa-web/src/i18n/uiText.ts`
- Modify: `apps/dsa-web/src/locales/featureText.ts`

**Steps:**
1. Write failing API tests for instrument/risk-policy CRUD, validation errors, additive snapshot fields, and privacy.
2. Implement the API endpoints and schemas.
3. Write failing Web tests for identity status, conditional product fields, risk policy editing, and blocker display.
4. Implement compact un-nested portfolio controls using existing components and preserve current realtime-refresh behavior.
5. Run focused Vitest tests, lint, and build.

### Task 9: Add Artifact-Only Worker Routing

**Files:**
- Create: `src/services/portfolio_research_routing.py`
- Modify: `src/services/portfolio_research_snapshot_service.py`
- Test: `tests/test_portfolio_research_routing.py`
- Later modify after DSA verification: `/Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/preflight.py`
- Later modify after DSA verification: `/Users/lan/.cc-switch/skills/portfolio-research-orchestrator/references/contracts.md`

**Steps:**
1. Add failing routing tests proving routine holdings stop after DSA and each exception selects at most one capability.
2. Add tests for `not_required`, `offered_pending_confirmation`, and blocked required evidence.
3. Implement pure routing output with frozen input hash and exact research question.
4. After DSA verification, use `ccswitch-skill-sync` before changing the installed skill; add snapshot/preflight contract checks without holdings persistence or worker startup.
5. Run the skill helper tests and `quick_validate.py` if present.

### Task 10: Close The Prospective Feedback Loop

**Files:**
- Modify: `src/services/decision_signal_outcome_service.py`
- Modify: `api/v1/endpoints/decision_signals.py`
- Modify: corresponding decision-signal schemas
- Test: `tests/test_decision_signal_outcome_service.py`
- Test: `tests/test_decision_signal_outcome_api.py`

**Steps:**
1. Write failing tests for frozen snapshot hash, human decision, actual manual action, correction minutes, evidence expiry, and immutable recommendation-time fields.
2. Add additive fields/metadata without rewriting historical signals.
3. Implement manual/shadow update APIs; do not add a scheduler.
4. Re-run focused outcome tests.

### Task 11: Document, Configure, And Verify End To End

**Files:**
- Modify: `.env.example`
- Modify: `docs/analysis-context-pack.md`
- Create: `docs/portfolio-research-workflow.md`
- Modify: `docs/CHANGELOG.md`

**Steps:**
1. Document source-of-truth boundaries, setup order, failure states, rollback, and the four minimal workflows.
2. Set this installation's local `ANALYSIS_UNIVERSE_SOURCE=portfolio_holdings` only after the pure resolver tests pass.
3. Run focused backend tests after every task, then `python -m py_compile` on changed Python files.
4. Run Web focused tests, `npm run lint`, and `npm run build`.
5. Run `./scripts/ci_gate.sh` and report any unrelated dirty-worktree failures separately.
6. Exercise the new service/API against a temporary copy of the real database; verify the real database is unchanged.
7. Perform a requirement-by-requirement completion audit before claiming the architecture is implemented.

No task performs broker authorization, order placement, scheduler startup, live-runner startup, automatic worker fan-out, git commit, tag, push, or PR creation.
