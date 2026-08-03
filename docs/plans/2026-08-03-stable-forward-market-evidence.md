# Stable Forward Market Evidence Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make current full-portfolio research produce durable, evaluable forward recommendations by preparing stable position and benchmark bars with compatible adjustment identities, while keeping bound analysis read-only for `stock_daily`.

**Architecture:** Keep the existing modular monolith and evidence preparation endpoint. Add explicit provider routing only inside `PortfolioResearchEvidenceService`, preserve verified legacy rows without rewriting them, block recommendation creation when a position and its benchmark use different adjustment identities, and propagate the existing frozen research context into all market-cache write decisions. Do not add a table, migration, provider fallback, strategy rule, or trading behavior.

**Tech Stack:** Python, FastAPI, SQLAlchemy, pandas, pytest

---

## Contract

- **Source of truth:** DSA portfolio ledger/control plane; `cn=000300`, `hk=HSI`, `us=SPY`; Tencent `qfq` for CN and Yahoo `adjusted` for HK/US.
- **In scope:** deterministic benchmark routing, strict legacy overlap verification, recommendation-time adjustment matching, and read-only `stock_daily` behavior for snapshot-bound analysis.
- **Non-goals:** schema changes, old-row rewriting, strategy or prompt changes, automatic feedback, automatic promotion, orders, or historical replay.
- **Primary risks:** partial writes after a conflict, silent provider fallback, stale legacy rows being treated as qualified, and deep analysis overwriting explicit source labels.
- **Done when:** focused tests and repository gates pass; a fresh API-only run yields a deterministic result per holding; control-plane tables are byte-for-byte unchanged; at least one complete saved recommendation exists before calling the run the first forward sample batch.

### Task 1: Route benchmark bars and bridge verified legacy rows

**Files:**
- Modify: `src/services/portfolio_research_evidence_service.py`
- Modify: `data_provider/tencent_fetcher.py`
- Modify: `data_provider/yfinance_fetcher.py`
- Test: `tests/test_portfolio_research_evidence_service.py`
- Test: the existing focused Tencent/YFinance fetcher test modules discovered with `rg -n "TencentFetcher|YfinanceFetcher" tests`

**Step 1: Write failing provider-routing tests**

Add focused tests proving:

- CN benchmark calls an injected `TencentFetcher` with `sh000300`, stores as `000300`, and labels the row `TencentFetcher|adjustment=qfq`.
- HK benchmark calls an injected `YfinanceFetcher` with `^HSI`, stores as `HSI`, and labels the row `YfinanceFetcher|adjustment=adjusted`.
- SPY calls Yahoo unchanged.
- A fixed benchmark provider failure returns `benchmark_market_data_unavailable` and never calls `DataFetcherManager` fallback.
- `TencentFetcher` preserves an explicit `sh`/`sz` prefix, while ordinary stock-code conversion keeps its existing behavior.
- `YfinanceFetcher` preserves native caret index symbols instead of appending `.SZ`.

Use constructor injection for the two benchmark fetchers so tests exercise real routing without network access.

**Step 2: Run the routing tests and verify RED**

Run the exact test node IDs added in Step 1 with:

```bash
.venv/bin/python -m pytest <node-ids> -q
```

Expected: failures show the current manager route is used, `sh000300` is converted to `sz000300`, or `^HSI` receives an invalid suffix.

**Step 3: Implement the minimum fixed routing**

In `PortfolioResearchEvidenceService`, define an internal route contract equivalent to:

```python
BENCHMARK_ROUTES = {
    "cn": {"storage_code": "000300", "fetch_code": "sh000300", "provider": "tencent"},
    "hk": {"storage_code": "HSI", "fetch_code": "^HSI", "provider": "yfinance"},
    "us": {"storage_code": "SPY", "fetch_code": "SPY", "provider": "yfinance"},
}
```

Call only the routed provider for benchmark bars. Keep ordinary position fetches on the existing manager. Make the smallest symbol-normalization changes needed for explicit Tencent exchange prefixes and Yahoo caret symbols.

**Step 4: Write failing legacy compatibility tests**

Add tests covering all of these cases before changing comparison behavior:

- Same base provider plus exact finite equality for `open/high/low/close/volume/amount/pct_chg`, with at least one new date: ready; legacy rows unchanged; new rows have explicit adjustment labels.
- Only matching legacy dates and no new explicit date: insufficient.
- Different base provider, any field difference, missing-value semantic difference, `NaN`, or infinity: conflict.
- A conflict writes zero new rows, including rows earlier in the fetched frame.
- When local history is below the analysis minimum, preparation requests about 260 trading days; otherwise it uses the short incremental window.

**Step 5: Run the legacy tests and verify RED**

```bash
.venv/bin/python -m pytest tests/test_portfolio_research_evidence_service.py -q
```

Expected: the newly added cases fail because current comparison requires exact full `data_source`, treats non-finite values as `None`, and uses a fixed ten-day window.

**Step 6: Implement strict two-phase verification and save**

Compare all fetched overlaps before calling `save_dataframe`. Accept an exact legacy source only when its base provider equals the fetched provider and every required field is finite and equal. Never update a legacy row. Save only absent dates with the explicit adjustment label, then require the final target date to carry that explicit label. Compute the larger initial request only when stored history is below the existing Agent history threshold.

**Step 7: Run Task 1 GREEN tests**

```bash
.venv/bin/python -m pytest tests/test_portfolio_research_evidence_service.py -q
.venv/bin/python -m pytest <focused-fetcher-test-files> -q
```

Expected: all selected tests pass with no network access.

### Task 2: Block recommendations with incompatible adjustment identities

**Files:**
- Modify: `src/services/portfolio_research_snapshot_service.py`
- Test: `tests/test_portfolio_research_snapshot_service.py`
- Test: `tests/test_portfolio_research_baseline_service.py`
- Test: `/Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/test_acceptance_runner.py`

**Step 1: Write failing snapshot tests**

Create two snapshots with otherwise complete evidence:

```python
# mismatch
position["adjustment_identity"] = "qfq"
benchmark["adjustment_identity"] = "adjusted"

# match
position["adjustment_identity"] = benchmark["adjustment_identity"] = "qfq"
```

Assert the mismatch produces a position-scoped blocker:

```python
{
    "code": "benchmark_adjustment_identity_mismatch",
    "scope": "position",
    "account_id": account_id,
    "market": "cn",
    "symbol": "600519",
    "benchmark_symbol": "000300",
}
```

Also assert `completeness == "INSUFFICIENT_EVIDENCE"`, the matching case has no such blocker, baseline retains the blocker on the correct position row, and the runner does not count the mismatch as `new_saved`.

**Step 2: Run the new tests and verify RED**

```bash
.venv/bin/python -m pytest tests/test_portfolio_research_snapshot_service.py tests/test_portfolio_research_baseline_service.py -q
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest /Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/test_acceptance_runner.py -q
```

Expected: mismatch assertions fail because the snapshot currently checks benchmark availability but not equality with each position.

**Step 3: Implement the hard gate**

After both position and benchmark payloads are built, map benchmark by market and add the blocker for each position whose non-empty adjustment identity differs. Preserve existing missing-identity blockers. Keep `symbol` as the held symbol and add `benchmark_symbol` separately so per-position baseline filtering cannot drop it.

**Step 4: Run Task 2 GREEN tests**

Repeat the Step 2 commands. Expected: all selected tests pass and the runner reports no saved recommendation for an ineligible row.

### Task 3: Make snapshot-bound detailed analysis read-only for market bars

**Files:**
- Modify: `src/core/pipeline.py`
- Modify: `src/services/history_loader.py`
- Modify: `src/agent/tools/data_tools.py`
- Test: `tests/test_portfolio_api.py`
- Test: `tests/test_pipeline_fetch_error.py`
- Test: `tests/test_pipeline_market_phase_context.py`
- Test: `tests/test_data_tools_daily_history_cache.py`
- Test: `tests/test_agent_frozen_context.py`

**Step 1: Write failing binding-propagation test**

Extend the existing bound position-analysis API test to assert the queued `portfolio_context` contains the existing frozen snapshot envelope with `schema_version`, `snapshot_hash`, and `cutoff`. Do not add a request field, API production change, second boolean flag, or user configuration switch.

**Step 2: Write failing Pipeline tests**

For a Pipeline created with a snapshot-bound `portfolio_context`, assert:

- `fetch_and_save_stock_data` does not fetch or call the lower storage save path.
- `_ensure_agent_history` does not fetch or save missing history.
- `_get_analysis_context_with_market_fallback` does not enter the JP/KR/TW fetch/save fallback.
- analysis continues using the already prepared DB rows; insufficient rows fail explicitly instead of silently refreshing.

Add matching tests showing unbound legacy analysis retains current fetch-and-cache behavior.

**Step 3: Write failing Agent tool test**

Run `get_daily_history` with the frozen context and a non-DB provider result. Assert it may return read results according to the existing tool contract but never calls `save_daily_data`. Add the unbound control case. Also prove the existing Agent runner `copy_context()` path carries the cache policy into single and parallel tool calls and that Pipeline resets it in `finally`.

**Step 4: Run Task 3 tests and verify RED**

```bash
.venv/bin/python -m pytest tests/test_portfolio_api.py -q
.venv/bin/python -m pytest tests/test_pipeline_fetch_error.py tests/test_pipeline_market_phase_context.py tests/test_data_tools_daily_history_cache.py tests/test_agent_frozen_context.py -q
```

Expected: bound analysis still reaches one or more cache write calls.

**Step 5: Implement one derived read-only policy**

Derive the policy only from the existing frozen research snapshot envelope. Add a default-false `ContextVar` helper beside the existing history-loader context, set/reset it around `process_single_stock`, and rely on the Agent runner's existing `copy_context()` behavior. Guard all four write paths without changing unbound callers: `fetch_and_save_stock_data`, `_ensure_agent_history`, `_get_analysis_context_with_market_fallback`, and Agent `get_daily_history`. Do not add a public setting or schema field.

**Step 6: Run Task 3 GREEN tests**

Repeat Step 4. Expected: bound tests prove zero `stock_daily` writes and unbound compatibility tests remain green.

### Task 4: Integrated verification and first forward sample run

**Files:**
- Modify: `docs/CHANGELOG.md`
- Modify: `docs/plans/2026-07-31-investment-system-validation-status.md`
- Modify: `docs/portfolio-research-workflow.md`
- Modify: `docs/strategy-validation.md`
- Do not stage: `.agents/`, `.playwright-cli/`, `docs/plans/2026-07-31-prospective-decision-evidence-design.md`, `docs/plans/2026-07-31-prospective-decision-evidence-implementation-plan.md`

**Step 1: Run deterministic focused and repository checks**

```bash
.venv/bin/python -m pytest tests/test_portfolio_research_evidence_service.py tests/test_portfolio_research_snapshot_service.py tests/test_portfolio_research_baseline_service.py tests/test_portfolio_api.py -q
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
  /Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/test_contract.py \
  /Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/test_portfolio_review.py \
  /Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/test_acceptance_runner.py -q
./scripts/ci_gate.sh
cd apps/dsa-web && npm run lint && npm run build
```

Record exact counts and failures. Do not call the broader gate passed if a command was skipped.

**Step 2: Create a formal database backup and control-table hashes**

Resolve the configured database path without printing secrets. Use SQLite backup/CLI facilities to create a dated backup outside tracked paths. Record row counts and deterministic content hashes for holdings, cash, trades, instruments, risk policy, strategy stage, human feedback, decision signals, evidence/quality records, and `stock_daily`.

**Step 3: Start a fresh isolated API-only runtime**

Use a free port other than 8123 and the established single-agent, scheduler-suppressed command. Verify listener PID cwd, JSON `/openapi.json`, canonical portfolio routes, and scheduler-disabled status before any runner action. Never terminate or reuse PID 61019.

**Step 4: Run the full-portfolio workflow**

Run the canonical `portfolio_review.py` against the fresh API. Reconcile all non-zero ledger positions. Prepare evidence, freeze a new snapshot, build baseline, and explicitly select all eligible rows for detailed analysis. Do not create human feedback, promote a strategy, allocate cash, or produce orders.

**Step 5: Wait for terminal results and prove persistence boundaries**

Require every row to end as either user-visible “已保存” or “资料不足” with a concrete blocker. Count new signals only when decision evidence and quality context are complete. After stopping only the runtime started in Step 3, compare control-table hashes: holdings, cash, trades, instruments, risk policy, strategy stage, and human feedback must be unchanged. Any `stock_daily` changes must be only newly prepared explicit-source bars; any new decision rows must match eligible selected holdings.

**Step 6: Update Chinese documentation**

Document the real run date, input count, eligible count, saved count, insufficient reasons, provider/adjustment policy, immutable boundaries, exact verification commands, and remaining 5/20/60-bar waiting periods. Keep `[Unreleased]` flat in `docs/CHANGELOG.md`; do not expand README.

**Step 7: Final review, commit, and push**

Run `git diff --check`, inspect the complete scoped diff, and perform final spec and code-quality review. Stage only A2 code/tests/docs, create English commit messages without `Co-Authored-By`, and push `main` to `origin/main` only after all required checks pass. Keep the four explicitly excluded local paths unstaged.

**Step 8: Synchronize the shared skill registry only when safe**

Use `PYTHONDONTWRITEBYTECODE=1` for shared-skill checks. Do not change the CC Switch registry while CC Switch is running. If it remains active, report registry hash synchronization as the only deferred operational item rather than terminating it.

---

## 2026-08-03 实施与实跑结果

- Task 1-3 已完成：固定三地 benchmark 路由和复权身份；旧行情只做严格重叠校验且不覆盖；复权不一致在建议保存前失败；绑定冻结 snapshot 的详细分析不写 `stock_daily`，并严格使用冻结 cutoff。
- 验证结果：A2 专项 `198 passed`；Task 3 focused `53 passed`；共享 runner `31 passed, 4 subtests passed`；全仓后端 `5546 passed, 4 deselected, 500 subtests passed`；Web lint/build 和 `git diff --check` 通过。完整门禁使用 `PATH="$PWD/.venv/bin:$PATH" ./scripts/ci_gate.sh` 通过。
- 正式实跑覆盖 5 个账户、17 个非零持仓，baseline 对账通过；证据准备为 `ready=0 / insufficient=17`，因此合格详细分析行数为 0，没有新增建议、证据快照、质量记录或人工反馈。
- 根因已进一步收敛：17 条持仓都发生 `position_existing_bar_conflict`，其中 A 股和美股 16 条还发生 `benchmark_existing_bar_conflict`。`HSI` 固定路由成功，只新增 260 条 `YfinanceFetcher|adjustment=adjusted` 行情。
- 实跑前后账户、持仓、批次、现金、交易、标的登记、风险政策、策略记录、人工反馈、建议和分析历史逐表完全一致；旧 `stock_daily` 行全部保留。数据库 `PRAGMA quick_check=ok`。
- A2 达成“稳定失败关闭和不可改写边界”，但没有达成“产生第一批前向样本”。精确下一依赖是只追加的行情证据版本：同日旧缓存与新来源版本必须可以并存，冻结建议必须引用精确版本，不能依赖单版本 `stock_daily` 的最新值。
