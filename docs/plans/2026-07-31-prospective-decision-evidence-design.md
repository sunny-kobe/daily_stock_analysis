# 新决策证据账本设计

## 结论

下一阶段先完成一件事：从现在开始，每条重要持仓建议都保存一份不可改写的决策时证据。未来评价 5/20/60 个交易日结果时，只能使用这份当时已经可见的证据，不能用今天的数据补写过去。

本切片只建立证据账本和明确的失败状态，不承诺当前全部持仓都已经具备完整证据。真实输入缺少 benchmark 行情、FX、复权身份、产品条款、交易单位或成本时，页面显示“资料不足”，该记录不得进入有效回测样本。

## 方案比较

### 方案一：独立证据表，推荐

新增 `decision_signal_evidence_snapshots`，一条新建议最多对应一条证据快照。结构化输入、来源证据、策略版本和各自哈希都在新表中冻结；旧建议不回填。

优点：不可改写边界清楚，可建立唯一约束和索引，未来结果证据可以继续使用独立追加表，不污染现有可变的建议元数据。缺点：首次正常启动会新增表和 SQLite 防改写 trigger。

### 方案二：扩充现有质量上下文

给 `decision_signal_quality_contexts` 增加策略、结构化输入和完整证据字段。

不采用。真实数据库已有 101 行旧记录，新增字段容易让旧记录与新完整记录混在一起，也会把轻量评价索引和大体积证据正文耦合在同一张表。

### 方案三：写入本地 JSON 文件

把内容寻址 JSON 保存为 append-only artifact，再在建议元数据中保存 hash。

不采用。文件和数据库建议无法共享事务，查询与备份更困难，建议元数据本身又会被续期、失效和质量流程修改，不能作为长期真源。

## 实施契约

- Goal：让上线后的新持仓建议保存可验证、不可原地改写的决策证据，并向用户显示“已保存”或“资料不足”。
- Source of truth：DSA 持仓、现金、交易、风险政策；已核验的标的与产品资料；决策时冻结的价格、FX、benchmark、复权、交易单位和成本；版本化策略 manifest。
- In scope：新增证据快照表、严格 schema、幂等只增 repository、证据组装服务、策略版本绑定、建议写入集成、质量详情 API 摘要、持仓复盘状态展示、临时数据库迁移验证。
- Non-goals：不回填旧 101 条记录；不采集未来 5/20/60 bar；不建立第二本持仓账；不自动改变策略阶段；不产生订单；不补齐所有外部数据源；不实现机会发现。
- Risks：首次真实启动会同时创建尚未落库的策略三表和新证据表；现有建议与质量上下文不是同一事务；当前研究快照缺少部分完整证据，初期可能大量显示“资料不足”。
- Done when：完整冻结输入可幂等写入并校验 hash；同一建议内容变化被拒绝；数据库层拒绝 UPDATE/DELETE；缺证据明确保存 unable reasons；旧记录保持 missing/excluded；API 和页面显示简单状态；相同输入产生相同 hash；所有验证只使用临时数据库。

## 数据模型

`decision_signal_evidence_snapshots` 最小字段：

- `id`
- `event_id`，唯一、确定性生成
- `signal_id`，唯一
- `account_id`
- `market`
- `stock_code`
- `decision_cutoff`
- `strategy_key`
- `strategy_version`
- `strategy_manifest_hash`
- `evidence_schema_version`
- `structured_inputs_json`
- `decision_input_hash`
- `evidence_json`
- `evidence_hash`
- `evidence_status`
- `unable_reasons_json`
- `created_at`

`structured_inputs_json` 只保存确定性决策输入；AI 自由文本、新闻正文和来源材料放在 `evidence_json`，不能成为可执行政策。两部分分别使用 canonical JSON 和 SHA-256。

证据正文最少覆盖：决策时持仓、现金、风险限制、价格、标的身份、产品条款、复权身份、benchmark、FX、交易单位、成本、支持与反对证据、触发条件和失效条件。

## 不可改写与失败行为

- 相同 `signal_id`、相同 canonical 内容重试时返回原记录。
- 相同 `signal_id` 但任一冻结字段变化时返回 `decision_evidence_immutable`。
- repository 只提供 insert/get/list，不提供 update/delete。
- SQLite 使用幂等 `BEFORE UPDATE` 和 `BEFORE DELETE` trigger 阻止直接 SQL 改写。
- 建议先落库、证据后落库。证据写入失败时建议仍可作为普通建议存在，但必须显示“资料不足”，且永远不能进入合格评价样本；相同请求允许重试补齐缺失 sidecar。
- 旧建议不生成 sidecar，不使用当前 registry、行情或 benchmark 回填，统一视为 `legacy_evidence_snapshot_missing`。

## 用户界面

持仓复盘只显示简单状态：

- `complete` -> `复盘资料：已保存`
- `insufficient_evidence` / `failed` / `missing` -> `复盘资料：资料不足`

页面可显示策略名称和版本；缺失原因使用中文映射。内部字段名、hash 和原始证据正文不默认铺在主界面。

## 后续依赖

证据账本完成后，下一切片按缺失原因补齐真实输入来源，然后开始每日全持仓模拟。5/20/60 个交易日自然成熟后，再新增独立的结果证据表，保存执行 bar、标的/benchmark bar、复权身份和每行来源 hash；结果不得回写本表。
