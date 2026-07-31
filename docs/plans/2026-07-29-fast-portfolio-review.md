# Fast Portfolio Review Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a two-stage full-portfolio review that creates a deterministic baseline for every holding, asks the user which `name（symbol）` candidates to deepen, and runs news/LLM analysis only for selected symbols.

**Architecture:** A new repository service and API build a snapshot-bound, cache-only baseline without entering `StockAnalysisPipeline` or any market-data provider. A separate personal-skill runner checkpoints the baseline and later reuses the existing detailed position-analysis endpoint for explicit selections. Existing process acceptance remains compatible.

**Tech Stack:** Python 3, FastAPI, Pydantic, pytest/unittest, existing DSA data providers and portfolio services.

---

### Task 1: Define the baseline service contract

**Files:**
- Create: `src/services/portfolio_research_baseline_service.py`
- Create: `tests/test_portfolio_research_baseline_service.py`

**Step 1: Write failing coverage and no-LLM tests**

Create fixtures containing two accounts, repeated symbols, verified instruments, risk blockers, cached positions, and active DecisionSignals. Assert that the wished-for service:

- returns one row per positive account-position;
- fetches quote/history evidence once per `market:symbol`;
- preserves account-specific rows and existing two-axis actions;
- never invokes task queue, news, analyzer, or LLM collaborators;
- emits `display_label=name（symbol）` and `selection_key=market:symbol`.

**Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_portfolio_research_baseline_service.py -q
```

Expected: collection/import failure because `PortfolioResearchBaselineService` does not exist.

**Step 3: Implement the minimum pure orchestration service**

Add dependency-injected collaborators for snapshot rows, cache-only name resolution, frozen-snapshot quotes, local DB daily-bar loading, active signals, and technical analysis. Keep shared market evidence keyed by `market:symbol`; construct account-specific rows afterward.

Do not generate `add_in_batches`, `reduce`, or `exit` from a trend score. Preserve a valid active signal; otherwise use explicitly labeled conservative `hold/wait` with blockers.

**Step 4: Run the focused tests and verify GREEN**

Run the same command and require all tests to pass.

**Step 5: Add failing exception-routing tests**

Cover stale/missing quote, insufficient history, signal evidence blockers, risk breach, QDII required evidence, daily-reset product, quiet holding, stable ordering, and at-most-five recommended candidates.

**Step 6: Implement deterministic candidate routing**

Return every exception with reasons and priority, while marking only the first five as `recommended=true`. Explicit user selection remains outside this selector.

**Step 7: Run focused tests**

Run the test file again and require all tests to pass.

### Task 2: Add the snapshot-bound batch API

**Files:**
- Modify: `api/v1/schemas/portfolio.py`
- Modify: `api/v1/endpoints/portfolio.py`
- Modify: `tests/test_portfolio_api.py`

**Step 1: Write failing schema/API tests**

Add tests for:

- required paired SHA-256 hash and timezone-aware cutoff;
- HTTP 409 `research_snapshot_mismatch` before baseline calculation;
- successful response with all rows and candidates;
- baseline service receiving the frozen snapshot;
- no call to `get_task_queue` or the detailed analysis endpoint;
- partial row evidence remaining HTTP 200 and visible.

**Step 2: Run focused API tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_portfolio_api.py -q
```

Expected: new route returns 404 or missing schema symbols.

**Step 3: Implement Pydantic request/response schemas and endpoint**

Add `PortfolioResearchBaselineRequest` and typed response models. Implement `POST /api/v1/portfolio/research-baseline` by rebuilding the requested snapshot, comparing the hash, and calling the baseline service. Preserve existing `/positions/{symbol}/analysis` behavior.

**Step 4: Run API tests and verify GREEN**

Run the focused API test file and require all tests to pass.

### Task 3: Wire strict cache-only market evidence

**Files:**
- Modify: `src/services/portfolio_research_baseline_service.py`
- Modify: `src/services/portfolio_service.py` only if a public read-only batch quote adapter is necessary
- Modify: `tests/test_portfolio_research_baseline_service.py`

**Step 1: Write failing cache-boundary tests**

Assert quotes come only from the frozen snapshot, history comes only from the local DB cache, no provider prefetch or cache warmup is started, and missing cached evidence degrades only the affected symbol.

**Step 2: Run tests and verify RED**

Run the baseline service tests and confirm the expected calls are absent.

**Step 3: Implement cache-only adapters**

Reuse the frozen position quote fields, cache-only name lookup, local DB daily bars, and `StockTrendAnalyzer`. Record quote/history source and cutoff separately from the frozen holdings snapshot. Do not call provider prefetch methods or persist any portfolio or market-data cache.

**Step 4: Run tests and verify GREEN**

Run the focused service and API tests.

### Task 4: Add the two-stage daily runner

**Files:**
- Create: `/Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/portfolio_review.py`
- Create: `/Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/test_portfolio_review.py`

**Step 1: Write failing runner tests**

Cover:

- `baseline` calls preflight plus batch baseline and makes zero detailed-analysis POSTs;
- manifest state is `awaiting_selection` and atomically checkpointed;
- all rows and candidates use `name（symbol）`;
- `deepen --select market:symbol` submits only selected symbols;
- a non-recommended but valid holding can be explicitly selected;
- unknown keys fail before any POST;
- one symbol across accounts is submitted serially;
- the original hash/cutoff is reused and drift fails closed;
- exact-trace DecisionSignals are collected;
- baseline and deepened records remain separate;
- zero selections can finalize the baseline.

**Step 2: Run runner tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest /Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/test_portfolio_review.py -q
```

Expected: runner import fails because the file does not exist.

**Step 3: Implement the minimum runner**

Reuse the acceptance runner's HTTP transport, JSON validation, exact-trace collection, timeout semantics, and atomic manifest writes without changing its default full-acceptance behavior. Keep stage selection explicit; do not use stdin prompts.

**Step 4: Run runner and legacy acceptance tests**

Run:

```bash
.venv/bin/python -m pytest \
  /Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/test_portfolio_review.py \
  /Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/test_acceptance_runner.py -q
```

Require all tests to pass.

### Task 5: Update user-visible contracts and docs

**Files:**
- Modify: `/Users/lan/.cc-switch/skills/portfolio-research-orchestrator/SKILL.md`
- Modify: `/Users/lan/.cc-switch/skills/portfolio-research-orchestrator/references/contracts.md`
- Create: `/Users/lan/.cc-switch/skills/portfolio-research-orchestrator/references/daily-review-runner-design.md`
- Modify: `docs/portfolio-research-workflow.md`
- Modify: `docs/CHANGELOG.md`

**Step 1: Add failing contract checks**

Extend the skill contract tests to require:

- daily review uses deterministic baseline plus user-selected deepening;
- full process acceptance remains separate;
- output always uses `name（symbol）`;
- unselected holdings never trigger news or LLM tasks;
- baseline and deepened evidence remain separate.

**Step 2: Run contract tests and verify RED**

Run:

```bash
.venv/bin/python /Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/test_contract.py
```

Expected: new contract assertions fail.

**Step 3: Update skill and repository documentation**

Document the two stages, commands, manifest semantics, failure behavior, name formatting, and the distinction between daily review and explicit process acceptance. Add one flat `[Unreleased]` changelog entry.

**Step 4: Run contract checks and CcSwitch validation**

Run:

```bash
.venv/bin/python /Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/test_contract.py
python /Users/lan/.cc-switch/skills/quick_validate.py
```

Verify the CcSwitch row has `enabled_codex=1` and `~/.codex/skills/portfolio-research-orchestrator` resolves to the canonical directory.

### Task 6: Integration and performance verification

**Files:**
- Modify only files required by failures found in this task.

**Step 1: Run focused deterministic verification**

```bash
.venv/bin/python -m pytest \
  tests/test_portfolio_research_baseline_service.py \
  tests/test_portfolio_api.py \
  /Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/test_portfolio_review.py \
  /Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/test_acceptance_runner.py -q
```

**Step 2: Run repository gate appropriate to changed Python/API/docs files**

```bash
./scripts/ci_gate.sh
```

If unrelated dirty-worktree failures exist, record the exact failing tests and rerun all affected focused tests.

**Step 3: Run live API-only baseline timing**

Start only the supported API runtime when unavailable:

```bash
AGENT_ARCH=single RUNTIME_SCHEDULER_SUPPRESS_START=true .venv/bin/python main.py --serve-only --host 127.0.0.1 --port 8123
```

Run the new baseline stage against an explicit validated base URL. Record wall time, row count, candidate labels, data blockers, and confirm zero detailed-analysis task submissions.

**Step 4: Stop only the process started for this task**

Do not terminate any pre-existing user process.

**Step 5: Review the final diff and worktree overlap**

Run `git diff --check`, inspect every touched file, and ensure no user changes were overwritten. Do not commit, push, merge, or modify remote state without explicit authorization.
