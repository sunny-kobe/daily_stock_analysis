# 组合策略验证闭环

本页定义 DSA 如何把冻结持仓建议、人工判断、真实交易、5/20/60-bar 结果、历史 replay、walk-forward OOS、prospective shadow 与人工治理串成可审计闭环。它是验证与学习系统，不是交易系统。

## 不可变边界

- DSA ledger 是账户、持仓、现金、成本和交易的唯一真源；验证表不复制第二本账。
- `position_action=hold|reduce|exit` 与 `incremental_action=add_in_batches|wait|no_add` 独立保存和评价。
- 缺少 identity、point-in-time evidence、benchmark、复权身份、execution ordering 或 risk budget 时返回 `unable`、`WAIT` 或 `INSUFFICIENT_EVIDENCE`。
- 历史 replay 禁止网络请求，也不能用当前 registry、新闻、基本面或模型输出补历史 cutoff。
- 完成的 outcome、validation event 和 shadow comparison 不可就地改写。数据修订或引擎变化产生新 version/revision。
- 没有 broker、订单、scheduler、live runner、自动 multi-agent 或自动 promotion。

## 验证阶段

```text
Phase 0 数据与评估完整性
  -> Phase 1 冻结决策与真实交易 attribution
  -> Phase 2 成熟 5/20/60 outcome
  -> Phase 3 point-in-time historical replay
  -> Phase 4 60-bar purged/embargo walk-forward OOS
  -> Phase 5 prospective champion/challenger shadow
  -> Phase 6 人工 promotion、reject 或 rollback
```

任一阶段的 leakage、identity、benchmark、adjustment、immutability 或 hard-gate 失败都会阻断下一阶段。

## 版本与数据契约

`strategies/validation/champion-v1.json` 冻结当前基线的 policy、Prompt template、model descriptor、generation config、代码提交、snapshot schema、decision contract、outcome engine、benchmark mapping 与 cost model 身份。`PROVISIONAL_BASELINE` 不是已证明可靠或已激活的 champion。

主要 append-only sidecar：

- `decision_signal_execution_links`
- `portfolio_strategy_versions`
- `portfolio_validation_runs`
- `portfolio_validation_events`
- `portfolio_rule_candidates`
- `portfolio_shadow_comparisons`
- `portfolio_strategy_governance_events`
- `portfolio_strategy_selections`

策略状态为 `draft -> shadow -> champion -> retired`，候选规则状态为 `observed -> proposed -> approved|rejected`。进入 `champion` 或 `approved` 必须是显式人工调用；批准本身不接入 production runtime。

## Outcome 与 execution attribution

`decision-quality-v2` 分开三种锚点：

- observation anchor：decision cutoff 前最后一根完全已知 bar；
- shadow execution anchor：cutoff 后第一根可交易 bar；
- actual execution anchor：已确认 execution link 的真实成交时间/价格。

Intraday cutoff 不得使用当日最终收盘。市场日历或 session phase 不能证明时返回 `execution_anchor_unverified`。Outcome 保存 `data_revision_hash`、`input_bar_hash` 和 `computed_at`；相同输入返回原结果，修订输入创建新 revision。

Execution link 只引用已存在的 DSA trade。服务会核对 account、canonical market/symbol、side、quantity、price、fee/tax 和时序，不修改 ledger。交易只有日期而没有真实成交时刻时保持 `same_day_unknown`；只有用户明确确认成交发生在 signal 之后，才能用于 actual-execution attribution。多个已确认交易可以共同满足 staged decision；无 link 时实际动作保持空值。

## 手工运行

以下命令只读或只写 validation sidecar，不启动服务、scheduler、broker、worker 或外部研究：

```bash
.venv/bin/python scripts/portfolio_strategy_validation.py preflight --as-of 2026-07-30
.venv/bin/python scripts/portfolio_strategy_validation.py build-dataset --from YYYY-MM-DD --to YYYY-MM-DD --output <artifact.json>
.venv/bin/python scripts/portfolio_strategy_validation.py replay --dataset <artifact.json> --strategy <manifest.json> --output <run.json>
.venv/bin/python scripts/portfolio_strategy_validation.py report --run <run.json>
```

Replay 只接受完整冻结输入，并校验未来证据、当前 registry 替换、事后 benchmark、数据源网络调用、bar 充分性和复权身份。Walk-forward 使用 expanding train windows、未触碰 validation/test windows 和 60-trading-bar purge/embargo；所有策略比较必须使用相同 event IDs。

报告按 horizon、market、product type、action axis、strategy version 与 engine 分段，至少展示 eligible/effective sample、unable rate、excess return、decision value vs HOLD、MFE、MAE、drawdown、turnover、costs、opportunity cost、human override value、instrument/regime concentration 和 confidence interval。普通股票、ETF、QDII 与每日重置产品不能合并成一个表现结论。

## Shadow 与人工治理

Champion 与 challenger 必须读取同一 frozen snapshot/evidence artifact，输出保存在 validation sidecar，不写入 production DecisionSignal，也没有 order capability。每日记录 eligible 与 abstention，周复盘只比较 paired disagreement 和成熟结果，不在同一窗口调参。

人工 review evidence summary 必须包括：

- historical OOS 与 prospective shadow；
- hard-gate failures；
- sample concentration；
- cost 与 drawdown delta；
- mature horizons；
- reason、approver 和 rollback target。

缺 20d 成熟证据或存在 hard-gate failure 时不能 approve。缺 60d 时 `long_term_improvement_status=PROVISIONAL`。Rollback 只切换 validation-side selected strategy reference，保留全部 DecisionSignal、feedback、trade、outcome、run、manifest 和治理历史。

## API 与 Web

- `PUT/GET /api/v1/decision-signals/{signal_id}/execution-links[/{trade_id}]`
- `POST /api/v1/decision-signals/strategy-validation/shadow-comparisons`
- `GET /api/v1/decision-signals/strategy-validation/shadow-review`
- `GET /api/v1/decision-signals/strategy-validation/review-summary`
- `POST /api/v1/decision-signals/strategy-validation/reviews`
- `POST /api/v1/decision-signals/strategy-validation/rollbacks`

持仓页的策略验证面板是只读视图，展示 champion/challenger、OOS/shadow、hard gates、集中度、成本、回撤、unable、成熟 horizon 和 rollback target。页面不提供下单、自动激活、批准或执行回滚控件。

## 运行节奏与完成定义

- 每日：baseline -> selected deepening -> frozen decision -> feedback -> optional trade link。
- 每周：trigger/expiry -> mature outcomes -> attribution -> candidate observations。
- 每月：replay diagnostics、walk-forward 与数据质量审计。
- 每个 candidate：frozen OOS protocol -> shadow run -> human continue/reject/approve/rollback。

只有版本和 protocol 在看到结果前冻结、所有 identity/leakage/benchmark/adjustment/immutability gate 通过、paired OOS 不由单一标的/制度/阈值驱动、shadow 与 OOS 同向、20-bar after-cost value 改善且成本/MAE/drawdown 不显著恶化、人工明确批准并指定 rollback target 时，candidate 才能进入有限人工 review。否则结果是 `CONTINUE_SHADOW`、`REJECT` 或 `INSUFFICIENT_EVIDENCE`。

## 回滚

关闭 validation/review 入口并选择前一版本 manifest。保留所有历史 sidecar 与 manifest。删除 schema/data、改写 DSA ledger、risk policy、Prompt 或 scheduler 是独立高风险操作，不属于本回滚流程。
