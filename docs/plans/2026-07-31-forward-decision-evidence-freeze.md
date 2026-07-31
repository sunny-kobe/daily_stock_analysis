# 前向决策证据冻结实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让从现在开始的每条持仓建议都绑定固定策略和当时可见的完整输入，证据不足时只显示“资料不足”，证据齐全时可以在未来按 5/20/60 个交易 bar 原样评价。

**Architecture:** 保留 `decision_signals`、持仓账本和现有质量上下文作为各自真源，新增一张一对一、不可更新的证据 sidecar。行情准备是显式的数据采集步骤，决策写入只消费冻结快照；任何单个标的失败只影响该标的，不阻断其他持仓。

**Tech Stack:** Python 3.11、SQLAlchemy/SQLite、FastAPI/Pydantic、pytest、现有 DSA portfolio/DecisionSignal/strategy validation 模块。

---

## Contract

### Goal

新 DecisionSignal 可以证明：使用了哪个策略版本、当时有哪些结构化输入、关键证据来自哪里、为什么可评价或为什么只能显示“资料不足”。

### Source of truth

- 持仓、现金、交易和账户：现有 DSA ledger/replay。
- 标的身份、产品类型和交易单位：`portfolio_instruments`。
- 风险限制：`portfolio_risk_policy` 和冻结风险预算结果。
- 策略规则、成本和 benchmark：`strategies/portfolio_current_policy_v1.json` 对应的不可改写策略版本。
- 决策时行情、FX 和研究输入：本轮生成的冻结 evidence bundle。

### In scope

- additive `decision_signal_evidence_snapshots` 表；
- 固定 `portfolio-current-policy@1.0.0` 及 manifest hash；
- canonical structured inputs 和 `decision_input_hash`；
- canonical evidence bundle 和 `evidence_bundle_hash`；
- `000300 / HSI / SPY` benchmark 身份；
- 标的、benchmark、复权、FX、产品、交易单位和成本的证据 envelope；
- 单标的 `complete / insufficient_evidence` 和中文“可评价 / 资料不足”映射；
- 显式、无订单能力的市场证据准备入口；
- 新记录接入现有 quality context 和后续评价链路；
- 旧记录继续排除，不补历史。

### Non-goals

- 不回填旧 101 条候选；
- 不自动运行 5/20/60 日到期任务；
- 不自动晋升、切换或调权策略；
- 不接券商、不生成订单、不改变持仓/现金/交易；
- 不做机会发现、机器学习或多 Agent 产品架构；
- 不重写当前 DecisionSignal、质量评价或回测模块。

### Risks

- 真实数据库尚未初始化 `portfolio_strategy_*` 新表；首次运行会发生 additive schema 变更。
- 现有 `stock_daily` 来源字符串没有稳定复权身份，旧行情不能改写后冒充合格证据。
- benchmark 当前使用市场情绪指数，必须与策略 benchmark 分离。
- 网络数据源可能部分失败或返回不可识别复权口径，必须逐标的失败关闭。
- signal、quality context 和 evidence sidecar 当前不是一个事务；sidecar 写入失败时不得留下“complete”用户状态。

### Done when

- 新持仓决策一对一保存不可改写 evidence sidecar；
- 相同输入幂等，任一已冻结字段变化返回 `immutable_evidence_snapshot_changed`；
- 策略、输入、证据 hash 均可重算验证；
- benchmark 只取策略规定的 `000300 / HSI / SPY`；
- 关键证据缺失时该持仓显示“资料不足”，其他持仓仍可继续；
- 完整证据记录可进入前向 eligible 集合，旧记录仍为 `legacy_evidence_snapshot_missing`；
- focused tests、完整后端门禁和真实数据库副本迁移验收通过；
- 文档和 `docs/CHANGELOG.md` 与实际行为一致。

## Task 1: Add The Immutable Evidence Snapshot Contract

**Files:**

- Create: `src/schemas/decision_evidence_snapshot.py`
- Create: `src/repositories/decision_evidence_snapshot_repo.py`
- Modify: `src/storage.py`
- Test: `tests/test_decision_evidence_snapshot_schema.py`
- Test: `tests/test_decision_evidence_snapshot_repo.py`

### Step 1: Write failing schema tests

覆盖以下契约：

```python
payload = {
    "schema_version": "decision-evidence-snapshot-v1",
    "signal_id": 101,
    "strategy_key": "portfolio-current-policy",
    "strategy_version": "1.0.0",
    "strategy_manifest_hash": "a" * 64,
    "decision_cutoff": "2026-07-31T08:00:00Z",
    "reporting_currency": "CNY",
    "structured_inputs": {"account_id": 1, "market": "cn", "symbol": "600519"},
    "evidence_bundle": {"benchmark": {"symbol": "000300"}},
    "readiness_status": "insufficient_evidence",
    "blockers": ["benchmark_bar_missing"],
    "snapshot_hash": "b" * 64,
}
```

断言 canonical JSON 与 hash 稳定；拒绝未知字段、无时区 cutoff、非法 hash、`complete` 但仍有 blocker。

Run:

```bash
.venv/bin/python -m pytest tests/test_decision_evidence_snapshot_schema.py -q
```

Expected: FAIL，因为 schema 尚不存在。

### Step 2: Write failing repository tests

断言：

- `signal_id` 唯一；
- 首次创建成功；
- 完全相同内容重复创建返回 existing；
- 任一字段变化返回 `immutable_evidence_snapshot_changed`；
- 模型没有 `updated_at`；
- repository 不提供 update/delete 方法。

Run:

```bash
.venv/bin/python -m pytest tests/test_decision_evidence_snapshot_repo.py -q
```

Expected: FAIL，因为 model/repository 尚不存在。

### Step 3: Implement the minimum additive model and repository

表字段固定为：`id`、`signal_id`、`quality_context_id`、`schema_version`、`strategy_key`、`strategy_version`、`strategy_manifest_hash`、`decision_cutoff`、`reporting_currency`、`structured_inputs_json`、`decision_input_hash`、`evidence_bundle_json`、`evidence_bundle_hash`、`readiness_status`、`blockers_json`、`snapshot_hash`、`created_at`。

不增加 `updated_at`，不修改旧表，不回填旧行。

### Step 4: Verify Task 1

```bash
.venv/bin/python -m pytest \
  tests/test_decision_evidence_snapshot_schema.py \
  tests/test_decision_evidence_snapshot_repo.py \
  tests/test_storage.py -q
```

Expected: PASS。

## Task 2: Build And Validate Decision-Time Evidence

**Files:**

- Create: `src/services/decision_evidence_snapshot_service.py`
- Modify: `src/services/strategy_registry_service.py`
- Modify: `src/services/portfolio_research_snapshot_service.py`
- Test: `tests/test_decision_evidence_snapshot_service.py`
- Modify: `tests/test_portfolio_research_snapshot_service.py`

### Step 1: Write failing strategy-binding tests

断言服务只接受 registry 中与 manifest hash 完全一致的策略版本；缺版本、hash 漂移、市场/产品不在策略范围时返回稳定 blocker，不接受调用方或 AI 自报的策略内容。

### Step 2: Write failing evidence-envelope tests

每个 envelope 至少包含：`schema_version`、`as_of`、`source`、`source_version`、`source_hash` 和业务字段。

完整性逐项检查：

- instrument identity 与 verified 产品证据；
- daily reset 产品的 reset frequency 与 underlying；
- 标的和 benchmark 的复权身份；
- 策略指定 benchmark 身份及 decision-time bar；
- FX pair/rate；同币种必须显式 `1.0`；
- lot size 和 manifest 成本模型；
- decision-time price/input bars；
- 所有证据 `as_of <= decision_cutoff`；
- 未来 bar 不得进入 structured inputs。

### Step 3: Implement canonical builder

`structured_inputs` 只保存确定性决策输入和 evidence hash 引用；新闻、AI 文本和原始研究内容放在单独的 source envelope，不作为可执行政策。相同冻结输入和版本必须得到相同 `decision_input_hash` 与 `evidence_bundle_hash`。

### Step 4: Align portfolio benchmark identity

`PortfolioResearchSnapshotService` 从当前策略 manifest 读取 benchmark policy，持仓研究快照只返回 `cn=000300`、`hk=HSI`、`us=SPY`。市场情绪指数继续服务市场环境分析，但不得进入策略评价 benchmark 字段。

### Step 5: Verify Task 2

```bash
.venv/bin/python -m pytest \
  tests/test_decision_evidence_snapshot_service.py \
  tests/test_portfolio_research_snapshot_service.py \
  tests/test_strategy_registry_service.py -q
```

Expected: PASS。

## Task 3: Integrate Evidence With Portfolio Decision Writes

**Files:**

- Modify: `src/services/decision_signal_extractor.py`
- Modify: `src/services/decision_signal_service.py`
- Modify: `src/services/decision_quality_service.py`
- Modify: `src/schemas/portfolio_decision_quality.py`
- Modify: `src/services/portfolio_research_baseline_service.py`
- Test: `tests/test_decision_signal_extractor.py`
- Test: `tests/test_decision_signal_service.py`
- Test: `tests/test_decision_quality_service.py`
- Test: `tests/test_portfolio_research_baseline_service.py`

### Step 1: Write failing integration tests

覆盖：

- 绑定 snapshot 的 portfolio analysis 显式传入 strategy key/version；
- signal 保存后立即写 evidence sidecar；
- sidecar `complete` 才允许 quality context 为 `complete`；
- sidecar 缺项时保留 signal，但 quality/user instruction 为 `insufficient_evidence`；
- sidecar 写入异常时 metadata 最终为 failed/资料不足，不得残留 complete；
- 一个标的失败不影响第二个标的创建自己的 signal/evidence；
- 非 portfolio alert/legacy signal 不被强制伪造策略证据。

### Step 2: Implement the minimum write orchestration

先纯计算 evidence 和 blockers，再保存 signal，随后写 quality context 与 immutable sidecar。暂不进行大范围事务重构；以 evidence sidecar 是否存在且 hash 校验通过作为唯一完整性门槛。

### Step 3: Keep simple user status

普通持仓输出只映射为“加仓、持有、减仓、清仓、资料不足”。新增内部字段可以进入调试/API metadata，但不在普通界面展示策略枚举或双轴术语。

### Step 4: Verify Task 3

```bash
.venv/bin/python -m pytest \
  tests/test_decision_signal_extractor.py \
  tests/test_decision_signal_service.py \
  tests/test_decision_quality_service.py \
  tests/test_portfolio_research_baseline_service.py -q
```

Expected: PASS。

## Task 4: Prepare Current Market Evidence Without Trading Side Effects

**Files:**

- Create: `src/services/portfolio_research_evidence_service.py`
- Modify: `src/storage.py`
- Modify: `api/v1/endpoints/portfolio.py`
- Modify: `api/v1/schemas/portfolio.py`
- Test: `tests/test_portfolio_research_evidence_service.py`
- Modify: `tests/test_portfolio_api.py`

### Step 1: Write failing source-identity tests

新保存的 daily bars 必须把已知来源映射为明确复权身份：Efinance/Akshare/Tencent 为 `qfq`，Yfinance 为 `adjusted`；未知来源保持 unknown blocker，旧行不回填。

### Step 2: Write failing evidence-preparation tests

显式准备入口只允许：

- 读取当前非零持仓；
- 获取持仓和 `000300/HSI/SPY` 的必要行情；
- 刷新当前 FX；
- 保存行情/FX 缓存和每项结果；
- 按标的返回成功或 blocker；
- 数据源失败时继续其他标的。

禁止：修改持仓、现金、交易、风险政策、策略阶段或产生订单。

### Step 3: Implement a bounded prepare endpoint

新增显式 `POST /api/v1/portfolio/research-evidence/prepare`。它只准备下一次冻结 snapshot 所需的市场证据，不自动提交分析、不自动运行策略、不自动交易。

### Step 4: Verify Task 4

```bash
.venv/bin/python -m pytest \
  tests/test_portfolio_research_evidence_service.py \
  tests/test_portfolio_api.py -q
```

Expected: PASS。

## Task 5: Export New Eligible Candidates And Document The Daily Workflow

**Files:**

- Modify: `src/services/portfolio_strategy_source_export_service.py`
- Modify: `tests/test_portfolio_strategy_source_export_service.py`
- Modify: `tests/test_portfolio_strategy_source_export_cli.py`
- Modify: `docs/portfolio-research-workflow.md`
- Modify: `docs/strategy-validation.md`
- Modify: `docs/plans/2026-07-31-investment-system-validation-status.md`
- Modify: `docs/CHANGELOG.md`

### Step 1: Write failing exporter tests

LEFT JOIN 新 sidecar：

- 旧记录返回 `legacy_evidence_snapshot_missing`，不回填；
- 新记录重算两个 JSON hash；
- 只有 `complete` 且 hash/策略/benchmark 对齐才移除 recommendation-time gaps；
- 未来 5/20/60 bars 未成熟时继续排除，不提前生成成绩。

### Step 2: Implement the minimum exporter bridge

不改变历史样本构建器契约，不读取当前 registry/新闻/FX 补旧记录。新记录只把已经冻结的 source body 交给后续成熟度检查。

### Step 3: Document the user workflow

文档使用简单中文：

```text
准备今日证据 -> 查看全部持仓建议 -> 选择需要深挖的持仓 -> 记录人的决定
            -> 等待 5/20/60 个交易 bar -> 查看策略表现
```

明确“资料不足”只影响当前持仓，未成熟不等于失败，任何真实动作仍由人确认。

### Step 4: Focused verification

```bash
.venv/bin/python -m py_compile \
  src/schemas/decision_evidence_snapshot.py \
  src/repositories/decision_evidence_snapshot_repo.py \
  src/services/decision_evidence_snapshot_service.py \
  src/services/portfolio_research_evidence_service.py

.venv/bin/python -m pytest \
  tests/test_decision_evidence_snapshot_schema.py \
  tests/test_decision_evidence_snapshot_repo.py \
  tests/test_decision_evidence_snapshot_service.py \
  tests/test_decision_signal_service.py \
  tests/test_decision_quality_service.py \
  tests/test_portfolio_research_snapshot_service.py \
  tests/test_portfolio_research_evidence_service.py \
  tests/test_portfolio_strategy_source_export_service.py \
  tests/test_portfolio_strategy_source_export_cli.py -q
```

Expected: PASS。

### Step 5: Real-database-copy migration acceptance

复制真实数据库到临时目录，初始化新代码并验证：

- 新增策略表和 evidence sidecar；
- 旧 143 个 signals、101 个 quality contexts 及持仓/现金/交易行数不变；
- 新 sidecar 初始为 0 行；
- `PRAGMA quick_check=ok`；
- 不把旧记录补成 eligible。

不得用测试直接初始化真实数据库。

### Step 6: Repository-wide verification

```bash
./scripts/ci_gate.sh
git diff --check
git status --short
```

根据 API 改动风险，再运行受影响 Web contract tests；本切片不新增普通界面控件时不做无关前端重构。

## Execution Order

Tasks 1-5 串行执行。Task 1-3 共享持久化和 DecisionSignal 写入链路，不并行修改；Task 4 可在 Task 3 契约稳定后实现；Task 5 只在前四项 focused tests 全部通过后开始。

实现阶段每个任务都必须遵循：先写 focused failing test，确认按预期失败，再做最小实现，再重跑 focused tests。最终提交只精确暂存本切片文件，排除 `.agents/` 和 `.playwright-cli/`。
