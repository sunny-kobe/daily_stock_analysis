# 稳定前向行情证据设计

## 结论

当前全持仓模拟已经能在关键证据不完整时安全停止，但第一次真实运行中 17 个持仓全部显示“资料不足”，没有形成可等待 5/20/60 个交易 bar 的新建议。本切片只修复行情证据链，让新建议从创建时起就具备可持续评价的价格和 benchmark 身份。

本设计不放宽 hard gate，不覆盖旧行情，不新增数据库表，也不改变策略、持仓、现金、交易、人工反馈或交易边界。

## 根因

1. 旧 `stock_daily.data_source` 只保存 `YfinanceFetcher` 或 `TencentFetcher`，新证据链要求显式的 `|adjustment=adjusted` 或 `|adjustment=qfq`。当前实现对整个重叠窗口做精确来源比较，因此旧标签与新标签必然冲突。
2. `000300` 和 `^HSI` 经过通用日线路由后会被 Yahoo 错误转换为 `.SZ` 标的，无法按策略规定的 benchmark 身份保存。
3. A 股持仓使用腾讯 `qfq`，若 `000300` 使用 Yahoo `adjusted`，建议创建时可能看似完整，但 5/20/60 日结果会因持仓与 benchmark 复权口径不一致而永久无法评价。
4. 普通 Pipeline 和 Agent history 工具会用裸 provider 名 UPSERT `stock_daily`，可能把资料准备阶段写入的显式来源重新覆盖成裸标签。

## Goal

让每日全持仓模拟能够稳定准备三地持仓与固定 benchmark 行情，并保证保存的新建议在后续 5/20/60-bar 评价时仍具有一致、可验证的复权身份。

## Source of truth

- 持仓、现金、交易、标的身份和风险政策继续只来自 DSA ledger/control plane。
- 当前策略 benchmark 固定为 `cn=000300`、`hk=HSI`、`us=SPY`。
- A 股日线证据使用腾讯 `qfq`；港股和美股日线证据使用 Yahoo `adjusted`。
- `stock_daily` 旧行保持原样；只有本切片之后的新日期可以写入显式复权来源。

## In scope

### 1. 固定 benchmark 路由

- `000300`：provider code 为 `sh000300`，只使用 `TencentFetcher`，保存 code 仍为 `000300`，复权身份为 `qfq`。
- `HSI`：provider code 为 `^HSI`，只使用 `YfinanceFetcher`，保存 code 仍为 `HSI`，复权身份为 `adjusted`。
- `SPY`：provider code 与保存 code 均为 `SPY`，只使用 `YfinanceFetcher`，复权身份为 `adjusted`。
- 固定来源失败时直接显示“资料不足”，不切换到复权口径不同的 provider。

### 2. Legacy 重叠兼容

旧裸标签只允许作为一次过渡校验：

- 只接受精确的 `YfinanceFetcher` 或 `TencentFetcher`；
- fetched provider 必须与旧行相同；
- `open/high/low/close/volume/amount/pct_chg` 必须逐字段相等且为合法有限值；
- 任一字段不同、缺失语义不同、出现 `NaN/inf` 或 provider 不同都视为冲突；
- 旧行不更新、不补标签、不删除；
- 必须至少存在一个更新日期的显式来源 bar，才能把本次价格证据标为 ready；最新目标日期仍为旧裸行时继续显示“资料不足”。

为避免详细分析再次补写历史，首次资料准备在本地历史不足时按需获取最多约 260 个交易日；后续只做短窗口增量准备。

### 3. 推荐时复权一致性门禁

冻结 snapshot 在创建建议前逐持仓比较：

```text
position.adjustment_identity == benchmark.adjustment_identity
```

任一方缺失或二者不一致时，加入持仓级 hard blocker，snapshot 保持 `INSUFFICIENT_EVIDENCE`，不得创建完整 evidence snapshot、quality context 或 `new_saved` 建议。

### 4. 绑定分析的行情缓存只读边界

绑定 `research_snapshot_hash/research_cutoff` 的详细分析只能读取资料准备阶段冻结的 `stock_daily`：

- 普通日线刷新、`_ensure_agent_history` 和 Agent history 工具不得在该次分析中 UPSERT `stock_daily`；
- 若准备后的历史仍不足，详细结果保持“资料不足”，不能退回普通写缓存路径；
- 未绑定 snapshot 的 legacy 手工分析入口保持原行为，本切片不改变其数据刷新语义。

## Non-goals

- 不新增或迁移行情表。
- 不回填、重写或删除旧 `stock_daily`。
- 不改变 benchmark 集合、策略规则、Prompt、评分或策略阶段。
- 不自动写人工反馈，不自动晋升策略，不生成订单。
- 不把当前行情倒灌为历史 point-in-time 证据。

## 用户可见行为

普通界面继续只显示“已保存”或“资料不足”。内部 provider、复权枚举、hash 和 hard blocker 仍用于审计，不要求用户操作。

- 资料完整：保存一条新建议，并等待人的决定和 5/20/60 个交易 bar。
- provider 不可用、历史冲突、benchmark 缺失或复权不一致：显示“资料不足”，不保存有效策略样本。

## 风险与控制

- 固定来源可能因单一 provider 故障降低可用率；这是有意的失败关闭，不能用口径不同的数据源提高表面成功率。
- 260 日首次准备可能较慢；仅在历史不足时触发，后续使用短窗口增量。
- 绑定分析只读可能暴露现有缓存不足；结果必须明确失败，不得恢复写入。
- `stock_daily` 是共享表；所有新增测试必须证明旧行零修改、冲突时零部分写入、控制真源零变化。

## Done when

- 三个固定 benchmark 使用正确 provider code、保存身份和复权口径。
- legacy 相同行可以过渡，真实冲突继续失败且旧行不变。
- snapshot 在建议创建前阻止持仓/benchmark 复权不一致。
- 绑定详细分析的三个行情写入口均不会修改 `stock_daily`。
- focused tests、后端门禁、API/Web 相邻验证通过。
- 新正式备份上重跑全持仓模拟，每行得到“已保存”或有真实原因的“资料不足”；至少有资料完整行形成新建议时，才称为第一批前向样本。
- 实跑前后持仓、现金、交易、标的身份、风险政策和策略阶段逐表内容一致。
