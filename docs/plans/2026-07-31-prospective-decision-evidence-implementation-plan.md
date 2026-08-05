# 新决策证据账本实施计划

> 实施状态：核心切片已完成，等待全量门禁、浏览器验收和真实数据库不变性复核后发布。

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal：** 为上线后的新持仓建议保存不可改写的决策时证据，并在持仓复盘中显示简单、可信的证据状态。

**Architecture：** 新增独立 additive sidecar，不修改或回填旧建议。服务使用 canonical JSON 和 SHA-256 冻结策略、结构化输入和来源证据；repository 与 SQLite trigger 共同保证不可原地改写。现有质量详情 API 只返回小型摘要，Web 不展示内部枚举和完整证据正文。

**Tech Stack：** Python 3.11、SQLAlchemy、SQLite、Pydantic、FastAPI、React、TypeScript、Vitest、pytest。

---

## 契约

- Source of truth：当前 DSA 决策调用显式传入的冻结研究快照、上下文快照和版本化策略 manifest。
- In scope：schema、additive table、migration marker、SQLite immutability trigger、repository、service、决策写入集成、质量详情 API、持仓复盘状态、测试和专题文档。
- Non-goals：不回填旧记录，不采集未来 bar，不改变策略阶段，不生成订单，不修改持仓/现金/交易。
- Done when：完整与缺失证据两条路径均可复现；不可改写在 repository 和直接 SQL 两层生效；用户看到“已保存/资料不足”；所有数据库测试使用临时路径。

### Task 1：锁定证据 schema 与 canonical hash

**Files:**
- Create: `src/schemas/decision_evidence_snapshot.py`
- Test: `tests/test_decision_evidence_snapshot_schema.py`

1. 先写失败测试，覆盖完整记录、缺失证据、未知字段、时间截点、canonical hash 和未来证据拒绝。
2. 运行：`.venv/bin/python -m pytest tests/test_decision_evidence_snapshot_schema.py -q`，确认因模块缺失失败。
3. 实现最小 Pydantic schema 和 hash helper。
4. 重跑 focused test，确认通过。

### Task 2：新增 additive 表、迁移标记和数据库防改写

**Files:**
- Modify: `src/storage.py`
- Test: `tests/test_storage.py`

1. 先写失败测试，覆盖新表、唯一约束、幂等冷启动、并发初始化、迁移记录以及直接 `UPDATE/DELETE` 被 trigger 拒绝。
2. 运行相关 test node，确认失败。
3. 新增 `PortfolioDecisionEvidenceSnapshotRecord`、独立 schema version marker 和幂等 SQLite triggers。
4. 在临时数据库连续初始化两次并并发初始化，确认无重复表、trigger 或 migration row。

### Task 3：实现只增 repository 与证据服务

**Files:**
- Create: `src/repositories/decision_evidence_snapshot_repo.py`
- Create: `src/services/decision_evidence_snapshot_service.py`
- Test: `tests/test_decision_evidence_snapshot_repo.py`
- Test: `tests/test_decision_evidence_snapshot_service.py`

1. 先写失败测试，覆盖首次写入、相同内容幂等、不同内容拒绝、策略 manifest 精确绑定、完整/资料不足、每日重置产品和 benchmark/FX/复权/成本缺失。
2. 实现 insert/get/list，不提供 update/delete。
3. 服务默认读取 `strategies/portfolio_current_policy_v1.json`，通过 `StrategyRegistryService.create_version` 幂等登记 draft 版本，但绝不改变阶段。
4. 结构化输入和来源证据分别 canonicalize/hash，构建确定性 `event_id`。

### Task 4：接入新建议写入并保持 fail-closed

**Files:**
- Modify: `src/services/decision_signal_service.py`
- Test: `tests/test_decision_signal_service.py`

1. 先写失败测试，证明 `create_gated_signal` 会为有 `portfolio_decision` 的新建议写证据 sidecar。
2. 覆盖完整证据、资料不足、重复请求、sidecar 异常、旧普通 signal 和不同内容重试。
3. sidecar 失败不得回滚或伪造为完整；返回元数据只保存状态摘要，完整正文只在 sidecar。
4. 保留现有 opposing-signal invalidation 与 quality-context 行为。

### Task 5：在质量详情 API 返回证据摘要

**Files:**
- Modify: `src/services/decision_quality_service.py`
- Modify: `api/v1/schemas/decision_signals.py`
- Test: `tests/test_decision_quality_service.py`
- Test: `tests/test_decision_quality_api.py`

1. 先写失败测试，覆盖新记录的 `complete/insufficient_evidence` 和旧记录的 `missing`。
2. `GET /api/v1/decision-signals/{signal_id}/quality` 增加可选 `evidence_snapshot` 摘要：展示状态、策略名称/版本、缺失原因和创建时间，不返回原始正文。
3. 保持旧客户端兼容；旧记录不触发回填。

### Task 6：持仓复盘显示简单证据状态

**Files:**
- Modify: `apps/dsa-web/src/types/decisionSignals.ts`
- Modify: `apps/dsa-web/src/components/portfolio/PortfolioDecisionReview.tsx`
- Modify: `apps/dsa-web/src/i18n/uiText.ts`
- Test: `apps/dsa-web/src/components/portfolio/__tests__/PortfolioDecisionReview.test.tsx`

1. 先写失败测试，覆盖“复盘资料：已保存”“复盘资料：资料不足”、策略版本和缺失原因。
2. 复用现有质量请求，不新增页面或导航。
3. 页面不显示 `complete`、`insufficient_evidence`、hash 等内部术语。

### Task 7：文档与整体验证

**Files:**
- Modify: `docs/plans/2026-07-31-investment-system-validation-status.md`
- Modify: `docs/strategy-validation.md`
- Modify: `docs/CHANGELOG.md`

1. 更新真实能力、限制、下一依赖和中文用户文案；不修改 README。
2. focused backend：运行 Tasks 1-5 的精确测试文件。
3. focused Web：运行 `PortfolioDecisionReview` 测试。
4. 临时数据库执行 `./scripts/ci_gate.sh`；真实数据库前后校验 SHA-256、大小、mtime 和 `PRAGMA quick_check`。
5. 运行 Web lint、完整测试和 build；浏览器验收桌面与 390px 手机宽度。
6. 最终检查 `git diff --check`、工作区范围和真实数据库未变化。
