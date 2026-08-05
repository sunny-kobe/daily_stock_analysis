# 版本化组合行情证据设计

## 结论

新增独立的只追加行情证据账本，保留 `stock_daily` 作为通用缓存。资料准备、冻结
snapshot 和绑定分析必须引用同一个 evidence batch；旧缓存不得被删除、覆盖或补写来源。

## 根因

`stock_daily` 以 `(code, date)` 唯一，只能保存同一标的同一交易日的一种值和来源。
真实 A2 验收需要把旧裸 provider 缓存与新 provider/复权身份同时保留，因此 17 条持仓
全部触发 `position_existing_bar_conflict`，无法形成第一条可复盘建议。

## 方案

- 新增 `portfolio_market_evidence_bars`，一行属于一个内容寻址的 `batch_hash`。
- batch 保存完整 OHLCV、来源、来源契约版本、复权身份、抓取时间和逐行 hash。
- 唯一键为 `(batch_hash, code, date)`；SQLite 禁止 UPDATE/DELETE。
- 相同内容重复准备幂等；来源修订产生新 batch，不修改旧 batch。
- snapshot 只读取 `captured_at <= cutoff` 的最新完整 batch，并把 `batch_hash` 写入
  position/benchmark 证据及 snapshot hash。
- 绑定分析通过 snapshot 中的 batch hash 读取同一组历史 bar；禁止回退到网络或
  `stock_daily`。未绑定的普通分析保持原行为。

## 数据边界

允许新增：版本化行情证据、合格建议、质量上下文、不可变 evidence sidecar、报告和
本地验收 manifest。

禁止修改：旧 `stock_daily`、持仓、批次、现金、交易、标的登记、风险政策、策略阶段、
人工反馈、Prompt 和策略规则。

## 失败与回滚

batch 字段不完整、日期重复、hash 不一致、cutoff 后写入或绑定 hash 不存在时失败关闭。
回滚只停用新读取路径；追加证据保留审计，不回填旧表，不删除证据。

## 验收标准

- 真实 17 条持仓全部完成 baseline 对账，每条显示建议或明确资料不足。
- 至少一条 same-trace 建议同时具有完整 quality context 和 immutable evidence sidecar。
- 相同 cutoff、snapshot hash 和策略版本两次产生相同语义结果，重复运行不增加新样本。
- 控制真源内容 hash 不变，旧 `stock_daily` 零删除零改写，`PRAGMA quick_check=ok`。
