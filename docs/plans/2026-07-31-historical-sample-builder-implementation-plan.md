# 真实历史样本构建与持有基线实施计划

> 执行要求：按 TDD 顺序逐项实现，每个红测先确认失败，再做最小实现。核心样本构建文件只允许一个执行窗口写入，完成后由调度窗口统一审查和验证。

**Goal：** 将版本化的本地冻结原始数据确定性地转换为合格样本、逐条排除记录和“继续持有”基线成绩卡，为后续真实数据导入建立可信入口。

**Architecture：** 新增纯计算的历史样本构建服务。它只读取调用方传入的冻结 JSON，不访问网络、当前数据库、当前产品表或当前研究数据；先生成样本资格清单，再将合格事件交给现有回测服务。真实数据文件与测试用合成 fixture 必须明确分开，测试结果不得被表述为真实策略成绩。

**Tech Stack：** Python 3、pytest、现有 `StrategyRegistryService` canonical JSON/hash、现有 `PortfolioStrategyBacktestService`。

---

## 切片契约

- Goal：建立可重复、可审计的历史样本入口，并输出继续持有基线成绩卡。
- Source of truth：调用方提供的版本化冻结 JSON；其中每项价格、基准、FX、产品身份和复权信息都必须带来源时间与内容哈希。
- In scope：样本构建、资格检查、逐条排除原因、样本集合哈希、继续持有基线成绩卡、CLI 输出、专题文档和 changelog。
- Non-goals：从当前数据库倒推历史、联网抓取、AI 历史重放、候选策略、每日调度、API/Web、Schema/迁移、真实交易。
- Risks：偷用未来数据、错误开盘价、自然日代替交易 bar、复权混用、benchmark/FX 错配、重复样本、合成数据冒充真实成绩。
- Done when：相同冻结输入产生相同哈希；只用截点后的第一根可交易开盘价；5/20/60 按有效 bar；历史回放零网络；缺证据逐条排除；成绩卡只使用合格事件并包含事件集合哈希；测试 fixture 明示 synthetic；focused tests 与相关回归通过。

## Task 1：锁定冻结原始输入和资格清单契约

**Files：**

- Create: `tests/test_portfolio_strategy_historical_sample_service.py`
- Create: `tests/fixtures/strategy_validation/synthetic_frozen_historical_source_v1.json`
- Create: `src/services/portfolio_strategy_historical_sample_service.py`

**Step 1：添加首批失败测试**

至少包含：

- `test_build_event_uses_first_tradable_open_strictly_after_cutoff`
- `test_build_event_requires_5_20_60_valid_trading_bars`
- `test_build_manifest_records_candidate_and_exclusion_reasons`
- `test_semantic_duplicate_decisions_are_excluded`
- `test_same_frozen_input_produces_same_source_and_event_set_hash`
- `test_builder_makes_no_network_or_database_calls`

fixture 只能包含合成的冻结原始 bars 和证据元数据，不得预填 execution、horizon 结果或“真实收益”标签。

**Step 2：运行红测**

```bash
.venv/bin/python -m pytest tests/test_portfolio_strategy_historical_sample_service.py -q
```

预期：因服务不存在或契约未实现而失败。

**Step 3：实现最小纯计算服务**

服务输入必须声明 schema/version、source snapshot hash、decision cutoff、市场/标的/产品身份、复权身份、标的 bars、固定 benchmark 身份及 bars、FX、成本/交易单位证据和 development/validation period。

输出必须包含：

- `source_snapshot_hash`
- `candidate_count`
- `eligible_events`
- `eligible_event_ids`
- `eligible_event_set_hash`
- `excluded_events`，每条带稳定 reason code
- `dataset_hash`

资格规则：执行价只取截点后第一根可交易 bar 的 open；5/20/60 只计有效交易 bar；benchmark 与 FX 必须按同一执行时点对齐；复权身份和产品证据必须匹配；禁止以不同 event_id 绕过语义去重。

**Step 4：运行 focused tests**

```bash
.venv/bin/python -m pytest tests/test_portfolio_strategy_historical_sample_service.py -q
```

预期：全部通过。

## Task 2：让现有回测只消费可证明的合格事件

**Files：**

- Modify: `src/services/portfolio_strategy_backtest_service.py`
- Modify: `tests/test_portfolio_strategy_backtest_service.py`

**Step 1：添加失败测试**

至少包含：

- `test_rejects_execution_without_first_eligible_bar_provenance`
- `test_rejects_future_or_unhashed_point_in_time_evidence`
- `test_rejects_unaligned_benchmark_fx_or_adjustment_identity`
- `test_rejects_daily_reset_product_without_required_evidence`
- `test_result_contains_eligible_event_set_hash`

**Step 2：运行红测**

```bash
.venv/bin/python -m pytest tests/test_portfolio_strategy_backtest_service.py -q
```

**Step 3：最小实现**

保留现有 `hold + position_fraction=1.0` 范围。回测服务校验构建器提供的来源、执行 bar、horizon bar、复权、benchmark、FX、产品和事件集合哈希；任何 hard gate 失败时整次运行 `unable`，不得输出部分成绩。

**Step 4：运行 focused tests**

```bash
.venv/bin/python -m pytest \
  tests/test_portfolio_strategy_historical_sample_service.py \
  tests/test_portfolio_strategy_backtest_service.py -q
```

## Task 3：提供本地可执行入口和清晰结果

**Files：**

- Create: `scripts/portfolio_strategy_build_dataset.py`
- Create: `tests/test_portfolio_strategy_build_dataset_cli.py`
- Modify: `scripts/portfolio_strategy_backtest.py`
- Modify: `tests/test_portfolio_strategy_backtest_cli.py`

**Step 1：添加失败测试**

覆盖：读取冻结 source JSON、输出 dataset/manifest、显示候选/合格/排除数量与原因、将 dataset 交给继续持有基线、拒绝 synthetic fixture 被标为真实成绩。

**Step 2：运行红测**

```bash
.venv/bin/python -m pytest \
  tests/test_portfolio_strategy_build_dataset_cli.py \
  tests/test_portfolio_strategy_backtest_cli.py -q
```

**Step 3：实现最小 CLI**

CLI 只做本地文件读取、纯计算和 JSON 输出。默认不写数据库；只有显式调用现有回测持久化入口时才写隔离验证数据库。对 synthetic 输入，用户可见状态必须是“测试数据，不代表真实成绩”。

**Step 4：运行 focused tests**

重复 Step 2 命令，预期全部通过。

## Task 4：同步专题文档并完成验收

**Files：**

- Modify: `docs/strategy-validation.md`
- Modify: `docs/plans/2026-07-31-investment-system-validation-status.md`
- Modify: `docs/CHANGELOG.md`

文档必须使用简单中文表达：候选记录、可用样本、排除原因、样本编号、测试数据、真实成绩。内部字段名可保留在字段契约中，不作为主界面术语。

**Focused verification：**

```bash
.venv/bin/python -m pytest \
  tests/test_portfolio_strategy_historical_sample_service.py \
  tests/test_portfolio_strategy_backtest_service.py \
  tests/test_portfolio_strategy_build_dataset_cli.py \
  tests/test_portfolio_strategy_backtest_cli.py -q
python -m py_compile \
  src/services/portfolio_strategy_historical_sample_service.py \
  src/services/portfolio_strategy_backtest_service.py \
  scripts/portfolio_strategy_build_dataset.py \
  scripts/portfolio_strategy_backtest.py
git diff --check
```

**Broader verification：**

```bash
./scripts/ci_gate.sh
```

Web 基线独立验证：

```bash
cd apps/dsa-web
npm test -- src/components/alerts/__tests__/AlertRuleForm.test.tsx
npx vitest run --silent
npm run lint
npm run build
```

## 本切片后的精确依赖

1. 使用明确授权的真实、版本化 source JSON 运行构建器，得到第一份真实资格清单；缺少历史身份的候选记录保持排除。
2. 用该资格清单运行继续持有基线，冻结 development/validation 分区和成绩卡。
3. 开始每日全持仓模拟记录，等待真实 5/20/60 bar 成熟。
4. 样本足够后，只新增一个改变单项规则的候选策略，并强制共用同一个 `eligible_event_set_hash`。
