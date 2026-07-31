# HK07709 Precision Portfolio Analysis Design

## Goal

Run HK07709 portfolio analysis through a best-fit Agent profile while refusing to emit an executable trade when critical evidence is incomplete. Any executable quantity must respect the actual 100-share holding and a 100-share trading lot.

## Decision Rules

- Treat HK07709 as a daily-reset 2x leveraged product linked to SK Hynix (`000660.KS`), not as an ordinary operating company.
- Use a targeted strategy set: leveraged-product risk, event-driven, expectation repricing, and trend confirmation. Do not activate every strategy.
- Inject the actual position quantity, average cost, trade lot, product type, leverage, daily-reset flag, and underlying code into the Agent prompt.
- A signal is executable only when the current quote is available and fresh, daily bars are available, the Agent run succeeded, and the required product/underlying evidence is not marked missing or failed.
- Missing, stale, conflicting, or unsupported critical evidence must produce `execution_status=blocked`, `suggested_trade_quantity=0`, and explicit blockers. It must not masquerade as a precise buy or sell instruction.
- For a 100-share holding with a 100-share lot:
  - `sell` means sell 100 shares and leave 0.
  - `hold` means trade 0 and retain 100.
  - `reduce` is not executable as a partial sale; it remains a watch/hold plan until a full-lot exit condition is met.
  - `buy` or `add` may recommend one 100-share lot only when the evidence gate passes.

## Architecture

Add a small server-side portfolio analysis policy resolver so every caller receives the same HK07709 behavior. The portfolio endpoint attaches the resolved policy and targeted skills to the existing task queue context. Agent prompt construction serializes the portfolio execution constraints. Decision-signal extraction derives a deterministic execution plan into the existing opaque `metadata` object, avoiding a database migration.

The Web UI reads that metadata and presents the suggested quantity, remaining quantity, lot size, execution status, and blockers. Existing signals without the metadata remain backward compatible.

## Error Handling

- Invalid or non-finite position and lot quantities block execution.
- A stale or unavailable portfolio price blocks execution even if the LLM returns `sell`.
- Unknown products keep current behavior and do not inherit HK07709-specific rules.
- Agent or provider degradation remains visible in diagnostics; the execution plan records blockers rather than hiding them with a fallback action.
- No code path creates a trade or calls a broker API.

## Verification

- Unit-test policy resolution and exact 100-share semantics.
- API-test portfolio task submission propagates targeted skills and policy context.
- Unit-test prompt injection and decision-signal evidence gating.
- Component-test execution-plan rendering and legacy-signal compatibility.
- Run focused Python tests, Web lint/build, `py_compile`, and `git diff --check`.
- Restart the local service, submit a forced HK07709 portfolio analysis, and verify task skills, Agent diagnostics, evidence gate, and displayed quantity.

## Boundaries

- No database schema or migration.
- No real order execution.
- No global activation of all strategies.
- Preserve unrelated and pre-existing worktree changes.
