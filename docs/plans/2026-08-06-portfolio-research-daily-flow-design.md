# 最小可用日常持仓研究流程设计

## 目标

把当前面向全组合验收的持仓研究链路收敛为可日常使用的 scope-first 流程，同时保留 ledger 唯一真相源、冻结 cutoff、逐行失败关闭、产品专用证据、双轴动作和人工确认。

## 用户流程

1. 用户先选择账户和持仓；“全部持仓”也是一个明确 scope。
2. 系统只为 scope 内正持仓准备行情、固定 benchmark、FX 和产品约束证据。
3. 系统用相同 scope 和 cutoff 构建只读 snapshot，并把 scope 纳入 snapshot hash。
4. baseline 只提供筛选摘要；异常、冲突或用户显式选择的行才进入 detailed analysis。
5. 每行独立计算 readiness。范围外标的和其他失败行不阻断已经合格的 scope 行。
6. detailed result 在展示前校验账户、symbol、verified name、产品类型、benchmark、cutoff、双轴动作和风险预算。
7. A 股 14:45 或其他市场近收盘时，只刷新会改变执行判断的价格、交易状态、spread、成交量/VWAP 等执行证据，不刷新 ledger、研究论点或静态产品身份。
8. 用户最终执行 `accept|modify|veto|no_action`；系统不访问 broker、不生成订单。

## 契约设计

- 新增 `PortfolioResearchScopeItem(account_id, market, symbol)`，所有 scope item 去重并规范化。
- `POST /api/v1/portfolio/research-evidence/prepare` 接受可选 scope；省略时兼容现有全持仓流程。
- `GET /api/v1/portfolio/research-snapshot` 接受重复 `scope` 查询值，格式为 `account_id:market:symbol`。
- baseline 和 position analysis request 携带同一 scope；服务重建 snapshot 时使用相同 scope，确保 hash 可重现。
- snapshot 输出显式 `scope` 和 `scope_hash`；scope 只筛选 ledger 正持仓，不接受调用方注入数量、成本或现金。
- baseline 展示名只使用冻结 instrument registry 的 verified name；静态名称只能作为无 registry 时的诊断信息，不能覆盖 verified identity。
- readiness 对外收敛为 `ready|insufficient|failed|awaiting_confirmation`；trace、hash、manifest、quality sidecar 等仍保留在 audit detail。
- 新增 execution-check request/response，只返回逐行 execution readiness、发生变化的证据和 blocker，不生成新 DecisionSignal。

## 产品证据

- A 股 benchmark 固定为 `000300`，继续校验来源与复权身份。
- QDII 要求同 cutoff NAV/IOPV、溢折价、FX、spread 和 tracking evidence；任一缺失只关闭该行。
- daily-reset 产品要求 verified product terms、daily reset、leverage factor、底层身份、底层同 cutoff 行情和路径/再平衡风险声明；任一缺失只关闭该行。
- `risk_budget_evaluated=false` 时可保留无 sizing 的研究摘要，但禁止数量、比例和仓位建议。

## 状态与持久化边界

- ledger、现金、交易、instrument registry 和 risk policy 仍是只读输入。
- evidence prepare 可继续追加 market evidence batch 和 FX cache。
- detailed analysis 可继续写报告、DecisionSignal 和审计 sidecar。
- execution-check 不写 ledger、DecisionSignal 或研究 sidecar；只读或复用执行证据缓存。
- 人工确认只追加 shadow-feedback，不改写冻结 AI 推荐。

## 失败处理

- scope 中不存在或非正持仓的行返回明确 blocker，不静默扩展到全组合。
- 单行 identity、freshness、benchmark 或产品证据失败只影响该行。
- exact trace 只证明同轮关联；语义 identity 不一致仍失败关闭。
- 14:45 证据变化到足以改变动作时，状态回到待人工确认；无变化时保留原研究结论。

## 验证

- 后端单元测试覆盖 scope 解析、scope hash、逐行隔离、510980 名称串位、513870 QDII、HK07709 daily-reset、risk budget sizing 和 execution-check。
- runner 测试覆盖范围外不足不阻断、exact trace 不替代语义校验。
- Web 测试覆盖 scope 选择、任务轮询、逐行状态、14:45 复核、隐藏审计字段和人工确认。
- 最终执行 focused pytest、相关 Web vitest、lint、build 和 `./scripts/ci_gate.sh`。
