# Portfolio Research Orchestrator V2 Design

## Goal

Restore the missing `portfolio-research-orchestrator` personal skill and make it the default workflow for portfolio analysis requests. Every run must use the DSA ledger as holdings truth, complete a DSA baseline for every non-zero position, deepen research only where new evidence can change the action, and return one consolidated Chinese summary after all required work finishes.

## Trigger And Scope

The skill should trigger for requests such as:

- 分析今天全部持仓并统一总结
- 看一下今天所有持仓，更新行情和风险
- 某只持仓现在能不能加仓、继续持有或需要减仓
- 运行持仓决策质量复盘

It must not trigger automatic trading, broker access, order creation, schedulers, live runners, or unrequested multi-agent execution.

## Workflow

1. Resolve and verify the DSA repository and supported API/service path.
2. Read all non-zero positions from the DSA ledger and freeze one analysis cutoff.
3. Run a DSA baseline for every position, including identity, product type, price freshness, FX, concentration, risk policy, and current DecisionSignal.
4. Select deep-research exceptions only when stale evidence, material movement, a risk gate, a new event, or an explicit user question could change the conclusion.
5. Use the minimum relevant evidence route for each exception. External systems remain optional evidence providers and cannot override DSA blockers.
6. Wait for required analysis tasks to finish, then produce one consolidated summary. Do not make the user collect results position by position.
7. Keep human confirmation explicit through `accept|modify|veto|no_action`. Never rewrite the immutable AI recommendation or frozen context.
8. Run 5/20/60-bar outcomes only through the explicit endpoint when the corresponding horizon is mature. Keep return-improvement claims `PROVISIONAL` until evidence gates mature.

## Output Contract

For every non-zero holding, report:

- stock name, code, account, and product type;
- `position_action=hold|reduce|exit`;
- `incremental_action=add_in_batches|wait|no_add`;
- dominant reason plus supporting and opposing evidence;
- risk and evidence blockers;
- observable trigger, invalidation condition, and next review point;
- whether the conclusion is baseline-only, deepened, or `INSUFFICIENT_EVIDENCE`.

Lead with portfolio-level exceptions and decisions. Keep quiet unchanged holdings concise. Never manufacture prices, positions, returns, confidence, benchmarks, or sizing.

## Files

Canonical skill:

- `~/.cc-switch/skills/portfolio-research-orchestrator/SKILL.md`
- `~/.cc-switch/skills/portfolio-research-orchestrator/agents/openai.yaml`
- `~/.cc-switch/skills/portfolio-research-orchestrator/references/contracts.md`

Retain deterministic helpers only when they still match current repository behavior. Do not restore obsolete scripts by memory alone.

## Validation

- Run the system skill validator.
- Verify the CcSwitch registry row is `local:portfolio-research-orchestrator` with `enabled_codex=1`.
- Verify `~/.codex/skills/portfolio-research-orchestrator` resolves to the canonical CcSwitch directory.
- Run a static trigger/output-contract audit against representative portfolio, single-stock, and quality-review prompts.
- Do not run a real portfolio analysis, external worker, scheduler, broker action, or multi-agent forward test as part of skill installation.
