# HK07709 Precision Portfolio Analysis Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a fail-closed, best-fit Agent analysis profile for HK07709 with deterministic 100-share execution guidance.

**Architecture:** A server-side policy resolver enriches portfolio analysis context and selects targeted skills. Agent prompts consume those constraints, and decision-signal extraction emits an execution plan in metadata only when evidence passes a deterministic gate. The React signal view renders the plan without changing persistence schemas.

**Tech Stack:** Python, FastAPI, pytest, React, TypeScript, Vitest.

---

### Task 1: Portfolio Analysis Policy

**Files:**
- Create: `src/services/portfolio_analysis_policy.py`
- Test: `tests/test_portfolio_analysis_policy.py`

**Steps:**

1. Write failing tests for HK07709 product identity, underlying `000660.KS`, targeted skills, 100-share lot, and unknown-symbol fallback.
2. Run `python -m pytest tests/test_portfolio_analysis_policy.py -q` and confirm failure.
3. Implement the immutable policy resolver with no configuration or persistence side effects.
4. Re-run the focused test and confirm pass.

### Task 2: Portfolio Task Propagation

**Files:**
- Modify: `api/v1/endpoints/portfolio.py`
- Test: `tests/test_portfolio_pr2.py`

**Steps:**

1. Add failing endpoint tests asserting HK07709 task submission receives targeted `skills` plus the position policy context.
2. Run the focused endpoint tests and confirm failure.
3. Enrich `_resolve_position_analysis_context` and pass explicit skills into `submit_tasks_batch`.
4. Re-run the endpoint tests and confirm pass.

### Task 3: Agent Prompt Constraints

**Files:**
- Modify: `src/agent/executor.py`
- Test: `tests/test_agent_executor.py`

**Steps:**

1. Add a failing test that the prompt contains position quantity, 100-share lot, leveraged-product identity, underlying code, and fail-closed instruction.
2. Run the focused test and confirm failure.
3. Add a compact portfolio execution section to `_build_user_message`.
4. Re-run the focused test and confirm pass.

### Task 4: Deterministic Execution Plan

**Files:**
- Modify: `src/services/decision_signal_extractor.py`
- Test: `tests/test_decision_signal_extractor.py`

**Steps:**

1. Add failing tests for fresh evidence plus `sell` producing 100/0, stale evidence producing blocked 0/100, and `reduce` being non-executable for a single lot.
2. Run the focused extractor tests and confirm failure.
3. Implement finite-number validation, evidence blockers, and metadata fields: `execution_status`, `suggested_trade_quantity`, `remaining_quantity`, `trade_lot_size`, and `execution_blockers`.
4. Re-run extractor tests and confirm pass.

### Task 5: Decision-Signal Presentation

**Files:**
- Modify: `apps/dsa-web/src/components/decision-signals/DecisionSignalDisplay.tsx`
- Modify: `apps/dsa-web/src/i18n/uiText.ts`
- Test: `apps/dsa-web/src/components/decision-signals/__tests__/DecisionSignalDisplay.test.tsx`

**Steps:**

1. Add failing component tests for executable 100-share and blocked plans, plus a legacy metadata case.
2. Run the focused Vitest file and confirm failure.
3. Parse the opaque metadata defensively and render execution status, quantity, remaining quantity, lot size, and blockers.
4. Re-run the component tests and confirm pass.

### Task 6: Documentation And Verification

**Files:**
- Modify: `docs/CHANGELOG.md`

**Steps:**

1. Add one flat `[Unreleased]` improvement entry.
2. Run focused Python tests for Tasks 1-4.
3. Run `python -m py_compile` for changed Python files.
4. Run the focused Web test, `npm run lint`, and `npm run build` in `apps/dsa-web`.
5. Run `git diff --check`.
6. Restart the existing local service without terminating unrelated processes.
7. Submit a forced HK07709 portfolio analysis for account 5 and verify explicit skills, Agent execution, diagnostics, and the 100-share execution plan.

No commit step is included because repository rules require explicit user confirmation before committing.
