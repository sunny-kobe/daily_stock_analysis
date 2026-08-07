# Portfolio Research Daily Flow Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development for every behavior change.

**Goal:** Implement the approved scope-first daily portfolio research flow with per-row readiness, stronger product identity gates, an execution-only near-close check, and a complete Web confirmation loop.

**Architecture:** Extend the existing portfolio research services and routes with an optional normalized scope while keeping no-scope callers backward compatible. Keep research correctness gates in the backend, expose a compact user readiness model, and retain hashes, traces, manifests, and quality sidecars as audit-only data.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy, pytest, React 19, TypeScript, Vitest.

---

### Task 1: Scope contract and frozen snapshot

**Files:**
- Modify: `api/v1/schemas/portfolio.py`
- Modify: `api/v1/endpoints/portfolio.py`
- Modify: `src/services/portfolio_research_snapshot_service.py`
- Modify: `src/services/portfolio_research_evidence_service.py`
- Test: `tests/test_portfolio_research_snapshot_service.py`
- Test: `tests/test_portfolio_research_evidence_service.py`
- Test: `tests/test_portfolio_api.py`

1. Add failing tests for normalized scope, duplicate removal, unknown/non-positive holdings, and no-scope compatibility.
2. Run the focused tests and confirm failures are caused by the absent scope contract.
3. Add `PortfolioResearchScopeItem` and optional scope fields.
4. Filter only repository-derived positions and accounts; include normalized scope in snapshot/hash.
5. Re-run focused tests to GREEN.

### Task 2: Scope-local consolidation and canonical identity

**Files:**
- Modify: `src/services/portfolio_research_baseline_service.py`
- Modify: `/Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/portfolio_review.py`
- Modify: `/Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/portfolio_acceptance.py`
- Test: `tests/test_portfolio_research_baseline_service.py`
- Test: `/Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/test_portfolio_review.py`
- Test: `/Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/test_acceptance_runner.py`

1. Add RED cases for 510980 verified-name mismatch and an unselected insufficient row.
2. Make frozen registry name authoritative.
3. Calculate `consolidated_ready` only over the requested scope; report full coverage separately.
4. Add semantic name/product checks after exact-trace lookup.
5. Re-run runner and baseline suites to GREEN.

### Task 3: Product-specific evidence gates

**Files:**
- Modify: `src/services/portfolio_research_evidence_service.py`
- Modify: `src/services/portfolio_decision_gate.py`
- Modify: `src/services/decision_evidence_snapshot_service.py`
- Modify: `api/v1/schemas/portfolio.py`
- Test: `tests/test_portfolio_research_evidence_service.py`
- Test: `tests/test_portfolio_decision_gate.py`
- Test: `tests/test_decision_evidence_snapshot_service.py`

1. Add RED cases for partial QDII evidence and incomplete HK07709 terms/underlying evidence.
2. Emit structured `product_evidence` with explicit required fields and blockers.
3. Keep missing providers fail closed without synthesizing values.
4. Require complete product evidence before actionable DecisionSignal readiness.
5. Verify ordinary ETF/equity rows remain unaffected.

### Task 4: Near-close execution check

**Files:**
- Create: `src/services/portfolio_research_execution_service.py`
- Modify: `api/v1/schemas/portfolio.py`
- Modify: `api/v1/endpoints/portfolio.py`
- Test: `tests/test_portfolio_research_execution_service.py`
- Test: `tests/test_portfolio_api.py`

1. Define RED tests proving only scoped rows are checked and research identity remains unchanged.
2. Implement an execution-check response with current price/trading status/spread-volume evidence when available, changed fields, blockers, and `requires_reconfirmation`.
3. Do not write ledger, DecisionSignal, report, or quality state.
4. Verify `risk_budget_evaluated=false` never returns size or percentage.

### Task 5: Web daily workflow

**Files:**
- Modify: `apps/dsa-web/src/types/portfolio.ts`
- Modify: `apps/dsa-web/src/api/portfolio.ts`
- Modify: `apps/dsa-web/src/components/portfolio/PortfolioDailyPlan.tsx`
- Modify: `apps/dsa-web/src/pages/PortfolioPage.tsx`
- Modify: relevant locale files
- Test: existing portfolio API/component/page test files

1. Add RED tests for scope selection, v2 schema, task polling/result readiness, per-row failure isolation, execution-check, and audit disclosure.
2. Add compact user states and keep internal IDs hidden by default.
3. Complete human confirmation through the supported shadow-feedback endpoint.
4. Avoid request waterfalls where independent evidence can be loaded concurrently without violating cutoff ordering.
5. Run focused Vitest, lint, and build.

### Task 6: Documentation and final verification

**Files:**
- Modify: `docs/portfolio-research-workflow.md`
- Modify: `docs/CHANGELOG.md`

1. Update the user workflow, API contract, failure semantics, 14:45 behavior, and audit disclosure.
2. Run focused backend and runner tests.
3. Run Web tests, lint, and build.
4. Run `./scripts/ci_gate.sh`.
5. Inspect `git diff --check`, changed files, and current dirty-worktree ownership before reporting completion.
