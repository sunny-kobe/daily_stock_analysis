# Portfolio Evidence Freshness Reuse Implementation Plan

**Goal:** Align prepare freshness with the market calendar and reuse only qualifying immutable evidence batches.

**Architecture:** `PortfolioResearchEvidenceService` resolves the expected last completed daily bar for each market. `_prepare_bar` first attempts an exact eligible batch read, otherwise keeps the existing fetch and append path; both paths share the same final validation.

**Tech Stack:** Python, FastAPI, SQLAlchemy, pandas, exchange-calendars, pytest

---

### Task 1: Lock the behavior with failing tests

**Files:**
- Modify: `tests/test_portfolio_research_evidence_service.py`

1. Add a test proving two same-`as_of` prepares reuse position and benchmark batches without new provider calls or rows.
2. Add a test proving a stale fetched bar is reported as `*_market_data_stale` rather than ready.
3. Run the focused tests and confirm RED for the missing reuse/freshness behavior.

### Task 2: Implement the shared freshness predicate

**Files:**
- Modify: `src/services/portfolio_research_evidence_service.py`

1. Pass market identity into `_prepare_bar`.
2. Resolve the expected completed daily-bar date through the existing trading calendar.
3. Read the latest immutable batch and accept it only when date, schema, source and price match.
4. Apply the same date check to newly fetched batches and return a stale blocker on mismatch.
5. Run the focused tests through GREEN.

### Task 3: Update contracts and run regression checks

**Files:**
- Modify: `docs/portfolio-research-workflow.md`
- Modify: `docs/CHANGELOG.md`

1. Document freshness-qualified reuse and fail-closed stale results.
2. Run evidence service, repository, snapshot, API and trading-calendar tests.
3. Run `python -m py_compile` for changed Python files and `git diff --check`.

### Task 4: Real API-only proof

1. Back up SQLite and verify `PRAGMA quick_check`.
2. Start a fresh single-runtime API-only service with scheduler suppressed.
3. Run baseline twice for the same effective evidence date.
4. Compare duration, provider calls, evidence row counts, ready/insufficient counts and protected tables.
5. Stop the temporary service. Do not start broker, orders, scheduler, automatic multi-agent execution or outcome evaluation.

The shared worktree already contains the user's uncommitted evidence implementation, so this plan does not create intermediate commits that would absorb unrelated work.
