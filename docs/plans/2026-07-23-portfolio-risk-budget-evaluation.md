# Portfolio Risk Budget Evaluation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deterministically evaluate the saved DSA portfolio risk policy in native currency buckets and fail closed on missing evidence.

**Architecture:** Extend the existing instrument metadata contract with verified sector exposures, then evaluate current cached positions, latest account cash, and persisted daily snapshots inside `PortfolioResearchSnapshotService`. Keep the endpoint read-only; explicit DSA refresh services remain the only way to materialize derived snapshots.

**Tech Stack:** Python 3.11, SQLAlchemy models already present in DSA, pytest/unittest.

---

### Task 1: Validate Sector Evidence

**Files:**
- Modify: `src/services/portfolio_instrument_service.py`
- Modify: `tests/test_portfolio_instrument_service.py`

**Steps:**
1. Write failing tests for a valid weighted `risk_sector` block and malformed/missing evidence.
2. Run the focused tests and confirm the expected failures.
3. Add normalization and validation without changing the database schema.
4. Run the focused tests and confirm they pass.

### Task 2: Evaluate Risk Budgets By Currency

**Files:**
- Modify: `src/services/portfolio_research_snapshot_service.py`
- Modify: `tests/test_portfolio_research_snapshot_service.py`

**Steps:**
1. Write failing tests for complete evaluation, threshold breaches, missing sector evidence, and incomplete drawdown history.
2. Run the focused tests and confirm the placeholder implementation fails them.
3. Implement currency-bucket cash, single-position, sector, high-risk-product, and drawdown calculations.
4. Preserve hard blockers and expose explicit evidence blocker codes.
5. Run the focused tests and confirm they pass.

### Task 3: Refresh Current DSA Evidence

**Files:**
- Modify through service APIs only: DSA instrument metadata and derived portfolio snapshots.

**Steps:**
1. Update held instruments with verified `risk_sector` metadata through `PortfolioInstrumentService`.
2. Materialize the drawdown window through the explicit DSA risk service.
3. Refresh the current derived snapshot through `PortfolioService`; do not mutate trades or cash events.
4. Build the frozen research snapshot and inspect every evidence blocker.

### Task 4: Document And Verify

**Files:**
- Modify: `docs/portfolio-research-workflow.md`
- Modify: `docs/CHANGELOG.md`

**Steps:**
1. Update the workflow contract from permanent placeholder to currency-bucket evaluation semantics.
2. Add one flat `[Unreleased]` changelog entry.
3. Run focused tests, Python compilation, `git diff --check`, and a real read-only frozen snapshot build.
4. Do not commit or push without explicit user authorization.
