# 每日全持仓模拟可用版实施计划

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** 在持仓页提供可信的每日全持仓模拟工作流，并完成第一批受控前向建议记录。

**Architecture:** 复用现有 prepare、research snapshot、baseline、bound position analysis、quality 和 feedback API。先修复证据资格的一致性，再新增 Web 编排组件；共享 runner 负责首次全持仓过程验收，不新增数据库表或自动任务。

**Tech Stack:** Python 3.11、FastAPI/Pydantic、SQLAlchemy/SQLite、React 19、TypeScript、Vitest、pytest、Playwright。

---

## Task 1：关闭证据与评价一致性缺口

**Files:**
- Modify: `src/services/decision_evidence_snapshot_service.py`
- Modify: `src/services/decision_quality_service.py`
- Modify: `src/services/decision_signal_service.py`
- Test: `tests/test_decision_evidence_snapshot_service.py`
- Test: `tests/test_decision_quality_outcomes.py`
- Test: `tests/test_decision_signal_service.py`

1. 先增加失败测试：缺少任一 `5d/20d/60d` 置信度时 evidence status 为 `insufficient_evidence`。
2. 运行精确测试并确认因当前 evidence validator 未校验而失败。
3. 在 `_validate_decision` 中校验周期集合，不猜测置信度。
4. 增加失败测试：`context_status != complete` 时 `evaluate_outcome` 返回 `unable/context_not_evaluable`，不读取 forward bars。
5. 实现最小 outcome gate 并重跑测试。
6. 增加失败测试：`portfolio_context.benchmark` 或模型 benchmark 与 snapshot 不同时，最终决策仍使用 snapshot benchmark。
7. 移除调用方 benchmark 覆盖，只允许 snapshot benchmark；缺失时保留 missing blocker。
8. 运行三个 focused test 文件。

## Task 2：让 baseline 只复用证据完整的旧建议

**Files:**
- Modify: `src/services/portfolio_research_snapshot_service.py`
- Modify: `src/services/portfolio_research_baseline_service.py`
- Test: `tests/test_portfolio_research_snapshot_service.py`
- Test: `tests/test_portfolio_research_baseline_service.py`

1. 先增加失败测试：snapshot 的 active signal 含最小 evidence 摘要，但不含正文和 hash 细节。
2. 失败测试覆盖旧 signal 无 sidecar、sidecar 资料不足、完整 sidecar 三条路径。
3. 通过现有 repository/service 读取摘要，不回填旧记录。
4. baseline 增加资格判断；缺 sidecar 或非 complete 时返回 `资料不足`。
5. 重跑 snapshot、baseline 和 portfolio API focused tests。

## Task 3：核对行情准备窗口内全部重叠 bar

**Files:**
- Modify: `src/services/portfolio_research_evidence_service.py`
- Test: `tests/test_portfolio_research_evidence_service.py`

1. 增加失败测试：最新日一致但更早重叠日 OHLC 或来源冲突时，该标的为 insufficient，数据库旧行不变。
2. 确认测试因当前只核对最新日而失败。
3. 对 fetched window 的全部重叠日期使用同一 canonical 比较；任一冲突停止该标的写入。
4. 覆盖无冲突幂等、单标的失败不阻断其他标的。

## Task 4：补齐 Web API contract

**Files:**
- Modify: `apps/dsa-web/src/types/portfolio.ts`
- Modify: `apps/dsa-web/src/api/portfolio.ts`
- Test: `apps/dsa-web/src/api/__tests__/portfolio.test.ts`（若不存在则在现有 portfolio API 测试位置新增）

1. 先写失败测试，固定 `prepareResearchEvidence()`、`buildResearchBaseline()` 和 bound `analyzePosition()` 的请求路径与 snake_case payload。
2. 新增 additive TypeScript response types，不改变后端 Schema。
3. 实现 API client 并运行 focused tests。

## Task 5：新增持仓页“今日持仓计划”

**Files:**
- Create: `apps/dsa-web/src/components/portfolio/PortfolioDailyPlan.tsx`
- Create: `apps/dsa-web/src/components/portfolio/__tests__/PortfolioDailyPlan.test.tsx`
- Modify: `apps/dsa-web/src/pages/PortfolioPage.tsx`
- Modify: `apps/dsa-web/src/pages/__tests__/PortfolioPage.test.tsx`
- Modify: `apps/dsa-web/src/i18n/uiText.ts`

1. 先写失败组件测试：按钮严格按 prepare -> snapshot -> baseline 顺序执行。
2. 覆盖全部持仓计数、简单中文指示、资料不足、重试和 coverage mismatch。
3. 实现无自动运行的页面组件；不显示内部 blocker/hash/英文状态。
4. 详细分析按钮携带同一 `snapshotHash/cutoff/accountId`。
5. 修改现有持仓行“分析”行为：未生成今日计划时提示先生成计划，不提交未绑定任务。
6. 运行组件、页面和 API focused tests。

## Task 6：同步共享每日 review runner

**Files:**
- Modify: `/Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/portfolio_review.py`
- Modify: `/Users/lan/.cc-switch/skills/portfolio-research-orchestrator/scripts/test_portfolio_review.py`
- Modify: `/Users/lan/.cc-switch/skills/portfolio-research-orchestrator/SKILL.md`（仅在命令或状态契约变化时）

1. 先写失败测试：prepare 必须先于 snapshot preflight。
2. 失败测试覆盖 baseline 缺 sidecar 不得 `TERMINAL_COMPLETE`。
3. 深挖 exact-trace signal 后读取 quality detail，只有 evidence snapshot complete 才标记新建议已保存。
4. manifest 区分 reference/new_saved/insufficient/awaiting_confirmation。
5. 运行 skill contract、runner tests 和 `quick_validate.py`；通过 CCSwitch 真源同步规则检查。

## Task 7：仓库验证与浏览器验收

1. 运行 Tasks 1-5 focused backend tests，确认通过。
2. 运行相关 Web tests、`npm run lint`、`npm run build`。
3. 使用临时数据库运行 `./scripts/ci_gate.sh`。
4. 启动 API-only 服务和 Web dev server，桌面与 390px 验收今日计划、资料不足和 bound analysis 请求。
5. 检查控制台错误、网络请求、布局重叠和文案泄漏。

## Task 8：真实首次全持仓前向模拟

1. 记录真实数据库 SHA-256、大小、mtime、`quick_check` 和关键表行数，创建带时间戳的只读备份。
2. 仅启动 `AGENT_ARCH=single`、scheduler suppressed 的 API-only 服务。
3. runner 先 prepare/preflight；若 identity、产品、benchmark、FX 或 runtime gate 失败，修复真源或返回资料不足，不绕过。
4. 显式运行全持仓 acceptance；每个非零持仓行必须终态，exact trace 对应新 signal 或明确失败。
5. 逐行核对 evidence/quality 状态，记录人工确认入口；不替用户自动接受建议。
6. 运行前后核对持仓、现金、交易、风险政策、策略阶段完全不变；只报告新增缓存、signal、evidence、quality 和 artifact。

## Task 9：文档、提交与发布

**Files:**
- Modify: `docs/plans/2026-07-31-investment-system-validation-status.md`
- Modify: `docs/portfolio-research-workflow.md`
- Modify: `docs/strategy-validation.md`
- Modify: `docs/CHANGELOG.md`

1. 用中文记录真实可用流程、运行结果、资料不足项和下周操作步骤；不扩写 README。
2. 运行 `git diff --check`，精确排除用户现有 `.agents/`、`.playwright-cli/` 和未纳入本轮的设计草稿。
3. 提交并推送 `main`；核对远端 SHA。
