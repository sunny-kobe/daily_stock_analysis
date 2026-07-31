# Portfolio Point-in-Time Eligibility Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make portfolio research snapshots honestly prospective-only, freeze active DecisionSignals into the snapshot identity, and fail closed before bound baseline or analysis submission when temporal eligibility is not proven.

**Architecture:** Extend the existing `portfolio-research-snapshot-v1` response with a nested point-in-time eligibility block and frozen active signals. Keep the current DSA ledger, instrument registry, and risk policy as truth sources; compute deterministic temporal blockers without adding storage. The baseline consumes only frozen signals, while API gates reject a matching but temporally ineligible snapshot.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, SQLAlchemy, pytest, React, TypeScript, Vitest, Testing Library.

**Execution boundary:** Work in the current checkout because it contains the user's in-progress portfolio-control-plane changes. Do not create a new worktree, commit, push, merge, rebase, modify CCSwitch skills, or touch portfolio data.

---

### Task 1: Lock the backend point-in-time response contract

**Files:**
- Modify: `tests/test_portfolio_research_snapshot_service.py`
- Modify: `api/v1/schemas/portfolio.py`

**Step 1: Write failing service tests**

Add focused tests proving:

```python
assert snapshot["point_in_time"]["scope"] == "current_prospective"
assert snapshot["point_in_time"]["historical_replay_eligible"] is False
assert snapshot["point_in_time"]["prospective_decision_eligible"] is True
assert snapshot["point_in_time"]["blockers"] == []
assert set(snapshot["point_in_time"]["source_cutoffs"]) == {
    "accounts",
    "position_cache",
    "daily_snapshots",
    "instrument_registry",
    "risk_policy",
    "decision_signals",
}
```

Add a second test with a cutoff earlier than current source rows and assert `prospective_decision_eligible=false`, `historical_replay_eligible=false`, and stable `*_after_cutoff` blockers.

**Step 2: Verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider \
  tests/test_portfolio_research_snapshot_service.py \
  -k 'point_in_time or canonical_hashed' -vv
```

Expected: FAIL because `point_in_time` does not exist.

**Step 3: Add Pydantic response models**

Add `PortfolioPointInTimeEligibility` with literal scope, two eligibility booleans, nullable source-cutoff mapping, and blocker list. Add `point_in_time` plus frozen `decision_signals` to `PortfolioResearchSnapshotResponse`.

**Step 4: Keep tests red until the service implements the fields**

Run the same command. Expected: service tests still fail on missing runtime payload, while schema import succeeds.

### Task 2: Compute temporal eligibility and freeze active signals

**Files:**
- Modify: `src/services/portfolio_research_snapshot_service.py`
- Modify: `tests/test_portfolio_research_snapshot_service.py`

**Step 1: Add failing active-signal tests**

Use the isolated database to create a relevant active DecisionSignal. Assert the snapshot contains only the public baseline fields and that changing its structured portfolio decision changes `snapshot_hash`.

Add tests for:

- active signal `created_at` or `updated_at` after cutoff;
- present source rows with missing cutoff metadata;
- more signals than the supported capture limit;
- snapshot construction remains read-only.

**Step 2: Verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider \
  tests/test_portfolio_research_snapshot_service.py -vv
```

Expected: FAIL on missing frozen signals and temporal blockers.

**Step 3: Implement the minimum service behavior**

Add dependency-injected signal loading tied to the snapshot repository database. Build sorted frozen signal payloads for held identities, compute per-source maximum cutoffs, and add temporal blockers when present rows lack timestamps or exceed cutoff. Always set `historical_replay_eligible=false`.

Do not infer historical state, mutate records, refresh caches, or add persistence.

**Step 4: Verify GREEN**

Run the full snapshot test file. Expected: all tests pass.

### Task 3: Make baseline consume only frozen signals

**Files:**
- Modify: `src/services/portfolio_research_baseline_service.py`
- Modify: `tests/test_portfolio_research_baseline_service.py`

**Step 1: Write failing tests**

Add a snapshot fixture containing `decision_signals`. Instantiate the production-default baseline service and assert it selects the frozen account-specific signal. Add a tripwire loader that raises if current signals are queried after snapshot freeze.

Add a second test proving a signal absent from the snapshot is not discovered from current state.

**Step 2: Verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider \
  tests/test_portfolio_research_baseline_service.py \
  -k 'frozen_signal or current_signal' -vv
```

Expected: FAIL because the baseline currently calls `DecisionSignalService.list_signals`.

**Step 3: Implement minimum behavior**

Use `snapshot["decision_signals"]` when no explicit test adapter is supplied. Retain dependency injection only for existing isolated unit tests; production must not install the live loader by default.

**Step 4: Verify GREEN**

Run the full baseline service test file. Expected: all tests pass and the deep-analysis tripwire remains untouched.

### Task 4: Enforce the API gate and drift ordering

**Files:**
- Modify: `api/v1/endpoints/portfolio.py`
- Modify: `tests/test_portfolio_api.py`

**Step 1: Write failing API tests**

Add tests for both bound endpoints:

```text
matching hash + prospective_decision_eligible=false
-> HTTP 409
-> error=research_snapshot_not_point_in_time_eligible
-> baseline service/task submission not called
```

Retain existing tests proving hash mismatch returns `research_snapshot_mismatch` first. Add a successful matching eligible snapshot case.

**Step 2: Verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider \
  tests/test_portfolio_api.py \
  -k 'research_snapshot or research_baseline or position_analysis_binds' -vv
```

Expected: new ineligible cases fail because the endpoints currently continue submission.

**Step 3: Implement a shared endpoint guard**

After hash equality, inspect `point_in_time.prospective_decision_eligible`. Raise the stable conflict error before baseline construction or task submission. Do not change unbound manual analysis in this slice.

**Step 4: Verify GREEN**

Run the same focused API selection and then all `tests/test_portfolio_api.py`.

### Task 5: Expose eligibility in the Portfolio Control Plane

**Files:**
- Modify: `apps/dsa-web/src/types/portfolio.ts`
- Modify: `apps/dsa-web/src/api/portfolio.ts`
- Modify: `apps/dsa-web/src/components/portfolio/PortfolioControlPlane.tsx`
- Modify: `apps/dsa-web/src/i18n/uiText.ts`
- Modify: `apps/dsa-web/src/pages/__tests__/PortfolioPage.test.tsx`

**Step 1: Write failing Web tests**

Extend the complete mocked snapshot response. Assert the real Control Plane renders:

- `current_prospective`;
- `Prospective ready` or `Prospective not ready`;
- historical replay unavailable;
- at least one source cutoff;
- temporal blocker text when present.

**Step 2: Verify RED**

Run:

```bash
cd apps/dsa-web
npm test -- --run src/pages/__tests__/PortfolioPage.test.tsx
```

Expected: FAIL because the eligibility UI is absent.

**Step 3: Implement typed mapping and restrained UI**

Map `point_in_time` and `decision_signals` from snake_case API data. Add a compact unframed eligibility section inside the existing risk surface; do not add nested cards or action controls.

**Step 4: Verify GREEN**

Run the same Vitest file. Expected: all tests pass.

### Task 6: Synchronize documentation and OpenAPI

**Files:**
- Modify: `docs/portfolio-research-workflow.md`
- Modify: `docs/CHANGELOG.md`
- Modify: `docs/architecture/api_spec.json`
- Modify: `tests/test_api_schema_pydantic.py`

**Step 1: Add or update contract assertions**

Assert the runtime OpenAPI response contains `PortfolioPointInTimeEligibility` and the snapshot response requires `point_in_time`.

**Step 2: Update docs**

Document prospective-only scope, historical replay rejection, frozen active signals, exact 409 error, and the remaining unbound-manual-analysis boundary. Add one flat `[修复]` entry under `[Unreleased]`. Do not update README.

**Step 3: Regenerate OpenAPI**

Run:

```bash
.venv/bin/python -c 'import json; from pathlib import Path; from api.app import create_app; Path("docs/architecture/api_spec.json").write_text(json.dumps(create_app().openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")'
```

**Step 4: Verify docs/schema contract**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider \
  tests/test_api_schema_pydantic.py tests/test_portfolio_api.py -q
```

### Task 7: Run integration and compatibility verification

**Files:** no additional production files.

**Step 1: Focused backend suite**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider \
  tests/test_portfolio_research_snapshot_service.py \
  tests/test_portfolio_research_baseline_service.py \
  tests/test_portfolio_api.py \
  tests/test_api_schema_pydantic.py -q
```

**Step 2: Shared-runner compatibility**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider \
  /Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/test_contract.py \
  /Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/test_portfolio_review.py \
  /Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/test_acceptance_runner.py -q
```

**Step 3: Repository backend gate**

```bash
./scripts/ci_gate.sh
```

Before and after this command, hash `data/stock_analysis.db` and any WAL/SHM files. Stop and report if repository tests mutate the real database.

**Step 4: Web checks**

```bash
cd apps/dsa-web
npm run lint
npm run build
```

**Step 5: Final static audit**

```bash
git diff --check
git status --short
git diff -- \
  src/services/portfolio_research_snapshot_service.py \
  src/services/portfolio_research_baseline_service.py \
  api/v1/schemas/portfolio.py \
  api/v1/endpoints/portfolio.py \
  apps/dsa-web/src/types/portfolio.ts \
  apps/dsa-web/src/api/portfolio.ts \
  apps/dsa-web/src/components/portfolio/PortfolioControlPlane.tsx \
  apps/dsa-web/src/i18n/uiText.ts \
  docs/portfolio-research-workflow.md \
  docs/CHANGELOG.md
```

Confirm no portfolio data, configuration, dependencies, lockfiles, CI, shared skills, or unrelated files changed.

## Dependencies After Phase 1A

1. **Phase 1B: revisioned point-in-time truth.** Design append-only revisions/tombstones for ledger corrections, instrument identity, risk policy, DecisionSignal state, benchmark mapping, market bars, FX, and adjustment identity. This requires separate database/storage approval.
2. **Phase 2: daily full-holdings decision MVP.** Require eligible frozen inputs for every non-zero row; complete triggers, invalidation, next-review, opposing evidence, and portfolio impact; then make snapshot binding mandatory for the supported daily workflow.
3. **Phase 3: opportunity discovery.** Add a separate deterministic universe and observation funnel, primary-source validation, at most three finalists, and portfolio-fit gates without creating a second holdings ledger.
4. **Phase 4: immutable decision and outcome loop.** Replace mutable feedback upserts with append-only feedback events, bind actual trades, and finish 5/20/60-bar revision identity, cost, benchmark, calibration, and error attribution.
5. **Phase 5: real strategy validation.** Build eligible replay datasets from revisioned artifacts, fail closed on incomplete paired sets, bind governance evidence to stored validation runs, and reuse the safe replay/outcome work from the strategy worktree only after a separate integration audit.
6. **Phase 6: advanced optimization.** Consider ranking calibration or machine learning only after enough independent qualified samples; never auto-promote or generate orders.
