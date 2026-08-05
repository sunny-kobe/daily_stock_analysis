# Versioned Portfolio Market Evidence Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove the real full-portfolio sample blocker by storing immutable versioned market-bar batches and binding analysis to the exact batch frozen in the research snapshot.

**Architecture:** Add a dedicated append-only SQLAlchemy model and repository without changing `stock_daily`. Evidence preparation writes content-addressed batches; snapshot and bound analysis resolve only the exact batch hash, while legacy unbound analysis keeps its existing cache behavior.

**Tech Stack:** Python, FastAPI, SQLAlchemy, pandas, pytest, SQLite

---

### Task 1: Append-only evidence storage

**Files:**
- Modify: `src/storage.py`
- Create: `src/repositories/portfolio_market_evidence_repo.py`
- Test: `tests/test_portfolio_market_evidence_repo.py`

1. Write tests for content-addressed batch insertion, idempotency, revised same-date coexistence,
   exact batch reads, cutoff selection, invalid rows, and SQLite update/delete rejection.
2. Run the focused tests and confirm RED because the model/repository do not exist.
3. Add the model, indexes, immutable SQLite guards, schema marker, and minimum repository.
4. Run the focused tests and confirm GREEN.

### Task 2: Evidence preparation and snapshot binding

**Files:**
- Modify: `src/services/portfolio_research_evidence_service.py`
- Modify: `src/services/portfolio_research_snapshot_service.py`
- Test: `tests/test_portfolio_research_evidence_service.py`
- Test: `tests/test_portfolio_research_snapshot_service.py`
- Test: `tests/test_portfolio_research_baseline_service.py`

1. Write RED tests proving a conflicting legacy `stock_daily` row remains unchanged while the new
   batch becomes ready, and snapshot position/benchmark records reference its exact hash.
2. Replace evidence preparation's `stock_daily` insert with append-only batch persistence.
3. Make snapshot choose only batches captured by cutoff and include batch identity in its hash.
4. Run focused tests through GREEN; retain fail-closed adjustment and freshness gates.

### Task 3: Exact frozen history for bound analysis

**Files:**
- Modify: `src/services/history_loader.py`
- Modify: `src/core/pipeline.py`
- Modify: `src/agent/tools/data_tools.py`
- Test: `tests/test_data_tools_daily_history_cache.py`
- Test: `tests/test_pipeline_fetch_error.py`
- Test: `tests/test_pipeline_market_phase_context.py`
- Test: `tests/test_agent_frozen_context.py`

1. Write RED tests proving the batch hash propagates through tool threads and every bound history
   consumer reads that batch without network or legacy cache fallback.
2. Add scoped ContextVars for the frozen code/batch identity and reset them in `finally`.
3. Use exact batch frames for bound readiness, trend analysis, analysis context, and Agent tools.
4. Preserve all unbound behavior and run the focused suite through GREEN.

### Task 4: Verification, documentation, and real acceptance

**Files:**
- Modify: `docs/portfolio-research-workflow.md`
- Modify: `docs/strategy-validation.md`
- Modify: `docs/CHANGELOG.md`
- Modify: `docs/plans/2026-07-31-investment-system-validation-status.md`

1. Run focused evidence/snapshot/API/runner tests, then `PATH="$PWD/.venv/bin:$PATH" ./scripts/ci_gate.sh`.
2. Create a SQLite online backup outside the repository and hash protected control tables.
3. Start a fresh single-agent API-only runtime on a free port with scheduler suppressed.
4. Prepare evidence, freeze one cutoff/hash, run the all-position baseline twice against that exact
   identity, then deepen every eligible holding through the supported runner path.
5. Require at least one complete saved suggestion, semantic repeatability, complete reconciliation,
   unchanged protected tables, unchanged legacy `stock_daily`, and `PRAGMA quick_check=ok`.
6. Record exact evidence and stop. Do not start simulation observation or outcome evaluation.

No commits, pushes, strategy changes, feedback writes, orders, or automatic promotion are authorized.
