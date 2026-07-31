# 真实候选 source 只读导出器实施计划

> **执行要求：** 使用 TDD，先验证测试因能力缺失而失败，再做最小实现。

**Goal：** 从当前 DSA SQLite 基线只读导出版本化的真实候选 source JSON，并让现有资格构建器如实生成可用样本或排除原因。

**Architecture：** 新增独立的只读导出服务，直接使用 SQLite URI `mode=ro` 和 `PRAGMA query_only=ON`，不初始化 `DatabaseManager`。导出器只映射决策时已经持久化的字段；策略绑定、结构化输入、历史身份、benchmark、FX、复权和成本证据缺失时保留为空并记录审计缺口，禁止用当前 registry、当前研究快照或在线数据回填。

**Tech Stack：** Python 标准库 `sqlite3` / `json` / `hashlib`、pytest、现有历史样本构建与回测服务。

---

## 契约

- Goal：取得第一份真实、版本化候选 source 和资格清单。
- Source of truth：`decision_signal_quality_contexts`、对应 `decision_signals` 和数据库内原始时间戳；现有冻结 source v1 契约负责最终资格判断。
- In scope：只读 SQLite 导出、候选稳定编号、原始行内容 hash、逐条缺口、冻结时间过滤、确定性 source hash、CLI、focused tests、真实库只读 smoke。
- Non-goals：补造策略版本或输入 hash、读取当前 registry 补历史、联网补行情、写数据库、产生真实成绩、修改策略阶段、API/Web、自动调度、订单。
- Risks：普通数据库初始化产生迁移副作用；晚到记录破坏复现；缺失字段被错误填充；真实候选被误称为真实成绩；个人数据被提交。
- Done when：同一数据库状态和冻结参数重复导出相同 hash；现有真实库得到稳定候选数和逐条缺口；资格构建器得到 0 条可用样本并明确排除；主数据库 hash、大小和 mtime 及 Git 用户改动保持不变。

## Task 1：先固定只读导出服务行为

**Files:**

- Create: `tests/test_portfolio_strategy_source_export_service.py`
- Create: `src/services/portfolio_strategy_source_export_service.py`

1. 用最小临时 SQLite 建立 `decision_signal_quality_contexts` 和 `decision_signals`，写入一条有真实动作但缺策略绑定和输入 hash 的记录。
2. 添加失败测试，要求：只读 URI、冻结时间过滤、稳定 candidate ID、`synthetic=false`、缺口列表、不能出现事后生成的 `decision_input_hash` 或 `structured_inputs`。
3. 运行：

```bash
.venv/bin/python -m pytest tests/test_portfolio_strategy_source_export_service.py -q
```

预期：因导出服务尚不存在而失败。

4. 最小实现服务，只查询所需两张表；对缺表、无效 JSON、无时区或未来的 `frozen_at` 和数据库写模式失败关闭。
5. 重跑 focused tests，预期通过。

## Task 2：增加 CLI 和端到端资格检查

**Files:**

- Create: `tests/test_portfolio_strategy_source_export_cli.py`
- Create: `scripts/portfolio_strategy_export_source.py`

1. 先添加失败测试，覆盖 CLI 参数、JSON 输出、候选/缺口统计和重复导出 hash 一致。
2. 增加端到端测试：导出 source 后交给 `PortfolioStrategyHistoricalSampleService`，缺策略绑定的候选必须稳定排除，不能成为 eligible event。
3. 运行失败测试并确认失败原因来自 CLI 缺失。
4. 最小实现 CLI：必填 `--database`、`--frozen-at`、`--reporting-currency`、`--output`；只写指定输出文件。
5. 重跑两组 focused tests。

## Task 3：真实库只读 smoke 与文档同步

**Files:**

- Modify: `docs/strategy-validation.md`
- Modify: `docs/plans/2026-07-31-investment-system-validation-status.md`
- Modify: `docs/CHANGELOG.md`

1. 记录数据库文件 SHA-256、mtime 和 Git 状态。
2. 使用固定冻结时间导出到临时目录：

```bash
.venv/bin/python scripts/portfolio_strategy_export_source.py \
  --database data/stock_analysis.db \
  --frozen-at 2026-07-31T18:00:00+08:00 \
  --reporting-currency CNY \
  --output /tmp/dsa-real-candidate-source-20260731.json
```

3. 运行现有资格构建器：

```bash
.venv/bin/python scripts/portfolio_strategy_build_dataset.py \
  --source /tmp/dsa-real-candidate-source-20260731.json \
  --output /tmp/dsa-real-candidate-dataset-20260731.json
```

4. 验收预期：真实候选存在、可用样本为 0、每条都有稳定排除原因；不得运行 `--real-performance` 宣称成绩。
5. 再次核对数据库 SHA-256、mtime、`PRAGMA quick_check` 和 Git 状态。
6. 更新中文文档，明确“真实候选资格清单”不等于“真实历史成绩”。`[Unreleased]` 只追加扁平条目。

## Task 4：整体验证

```bash
.venv/bin/python -m pytest \
  tests/test_portfolio_strategy_source_export_service.py \
  tests/test_portfolio_strategy_source_export_cli.py \
  tests/test_portfolio_strategy_historical_sample_service.py \
  tests/test_portfolio_strategy_build_dataset_cli.py \
  tests/test_portfolio_strategy_backtest_service.py \
  tests/test_portfolio_strategy_backtest_cli.py -q

.venv/bin/python -m py_compile \
  src/services/portfolio_strategy_source_export_service.py \
  scripts/portfolio_strategy_export_source.py

./scripts/ci_gate.sh
git diff --check
```

若完整 gate 与 focused 结果不一致，停止提交并先定位原因。真实 source 和 dataset 只保存在 `/tmp` 或被忽略的本地数据目录，不提交个人持仓数据。
