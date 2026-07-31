# Portfolio Research Orchestrator V2 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Restore and upgrade the global `portfolio-research-orchestrator` skill so future portfolio-analysis requests automatically use the DSA-first layered workflow and return one consolidated decision summary.

**Architecture:** Keep the canonical skill under CcSwitch and expose it to Codex through a symlink. Put the hot-path routing and safety rules in `SKILL.md`, detailed decision/output contracts in one reference file, and a deterministic contract test in `scripts/`.

**Tech Stack:** Markdown skill instructions, YAML agent metadata, Python standard-library contract checks, SQLite CcSwitch registry.

---

### Task 1: Initialize The Missing Skill And Establish A Failing Contract Test

**Files:**
- Create: `/Users/lan/.cc-switch/skills/portfolio-research-orchestrator/SKILL.md`
- Create: `/Users/lan/.cc-switch/skills/portfolio-research-orchestrator/agents/openai.yaml`
- Create: `/Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/test_contract.py`
- Create: `/Users/lan/.cc-switch/skills/portfolio-research-orchestrator/references/contracts.md`

**Step 1: Initialize the missing skill skeleton**

Run `init_skill.py` with `scripts,references` resources and interface metadata. This is scaffolding only; do not restore old helper behavior from memory.

**Step 2: Write the contract test**

The standard-library test must assert that the skill contains these exact contract concepts:

- DSA ledger is the sole holdings truth.
- Every non-zero holding receives a DSA baseline.
- Deep research is exception-based and must be action-changing.
- Required analysis tasks finish before one consolidated summary.
- Both `position_action` and `incremental_action` are present.
- Missing evidence produces `WAIT` or `INSUFFICIENT_EVIDENCE`.
- No broker, order, scheduler, live runner, automatic multi-agent run, or invented sizing.
- Human feedback cannot rewrite the AI recommendation or frozen context.
- 5/20/60 outcomes remain explicit and `PROVISIONAL` until mature.

**Step 3: Run the test and confirm RED**

Run:

```bash
python3 /Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/test_contract.py
```

Expected: non-zero exit because the skeleton does not yet satisfy the workflow contract.

### Task 2: Implement The Layered Portfolio Analysis Skill

**Files:**
- Modify: `/Users/lan/.cc-switch/skills/portfolio-research-orchestrator/SKILL.md`
- Modify: `/Users/lan/.cc-switch/skills/portfolio-research-orchestrator/references/contracts.md`

**Step 1: Write the minimal hot-path instructions**

Define repository verification, DSA baseline coverage, exception routing, task waiting, unified summary, human confirmation, and explicit quality review. Keep `SKILL.md` concise and imperative.

**Step 2: Write the detailed reference contract**

Document product-specific gates, evidence precedence, the two action axes, required per-position fields, portfolio-level summary order, and fail-closed behavior.

**Step 3: Run the contract test and confirm GREEN**

Run the same `test_contract.py`; expected exit code 0 with all checks reported as passing.

### Task 3: Generate Current Codex Metadata

**Files:**
- Modify: `/Users/lan/.cc-switch/skills/portfolio-research-orchestrator/agents/openai.yaml`

**Step 1: Read the current metadata schema reference**

Read `skill-creator/references/openai_yaml.md` before generating metadata.

**Step 2: Generate metadata from the finalized skill**

Use `generate_openai_yaml.py` with:

- display name: `Portfolio Research Orchestrator`
- short description: Chinese DSA-first portfolio analysis and unified decision summary
- default prompt: analyze all current holdings, wait for required work, and return one consolidated evidence-backed summary without trading

**Step 3: Inspect generated metadata**

Confirm the default prompt names `$portfolio-research-orchestrator` and does not imply automation, brokerage, or guaranteed returns.

### Task 4: Register And Expose The Skill Through CcSwitch

**Files:**
- Modify: `/Users/lan/.cc-switch/cc-switch.db`
- Create: `/Users/lan/.codex/skills/portfolio-research-orchestrator` symlink

**Step 1: Compute the finalized content hash**

Hash the canonical skill files deterministically.

**Step 2: Upsert the local registry row**

Create or update `local:portfolio-research-orchestrator` with the canonical directory, current description, content hash, timestamps, and `enabled_codex=1`.

**Step 3: Create the Codex discovery link**

Ensure the Codex path is a symlink resolving to the CcSwitch canonical directory. Do not replace a non-symlink path without inspecting it first.

### Task 5: Validate Installation And Trigger Semantics

**Files:**
- Verify only; no additional files expected.

**Step 1: Run the system skill validator**

```bash
python3 /Users/lan/.codex/skills/.system/skill-creator/scripts/quick_validate.py /Users/lan/.cc-switch/skills/portfolio-research-orchestrator
```

Expected: `Skill is valid!`

**Step 2: Re-run the contract test**

Expected: all contract checks pass.

**Step 3: Verify all CcSwitch layers**

Confirm canonical `SKILL.md`, registry `enabled_codex=1`, and a resolving Codex symlink.

**Step 4: Perform a static prompt audit**

Check that representative prompts for full-portfolio analysis, single-position add decisions, and 5/20/60 quality review match the description and required output contract. Do not launch an external worker or real analysis.

**Step 5: Check repository scope**

Run `git diff --check` in DSA. Do not commit, push, or modify unrelated user work.
