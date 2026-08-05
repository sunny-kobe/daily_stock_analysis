# 组合证据新鲜度与复用设计

## 目标

让 `POST /api/v1/portfolio/research-evidence/prepare` 在不放宽证据门槛的前提下，
复用已经冻结且仍符合当前交易日要求的行情 batch，避免同一天重复访问外部数据源。

## 根因

当前 prepare 每次都会抓取全部持仓和 benchmark。它只排除 `as_of` 当日 bar，
却不验证返回的最后一根 bar 是否正好对应上一根已完成交易日。因此旧 bar 可能先被
prepare 标记为 ready，随后才在 snapshot 中被判 stale；重复调用还会再次走同一失败源。

## 方案选择

- 只按 `as_of` 或抓取时间复用：速度快，但可能复用过期 bar，不采用。
- 只优化数据源熔断：能减少冷启动等待，但不能修复错误的 ready 判定，不先采用。
- 统一交易日新鲜度后复用 immutable batch：同时修复正确性和重复抓取，采用。

## 数据流

1. 根据持仓市场和 `as_of` 计算上一根已完成交易日。
2. 从 `portfolio_market_evidence_bars` 读取 cutoff 前最新完整 batch。
3. 仅当 batch 的最后日期、schema 版本、固定 benchmark 来源和价格都匹配时复用。
4. 不匹配时按现有 provider 路径抓取并追加 batch。
5. 新抓结果仍旧过期时返回明确的 `*_market_data_stale`，不得标记 ready。
6. 复权身份未知仍返回 `*_adjustment_identity_unknown`，不得因缓存复用而放宽。

## 边界

- 不改持仓、现金、交易、风险、策略、Prompt、成本或 benchmark。
- 不新增并发、重试或数据源 fallback。
- 不复用 legacy `stock_daily`。
- 交易日历无法证明目标日期时不复用，并保持失败关闭。

## 验证

- 同一 `as_of` 连续 prepare：第二次不调用行情 provider，batch hash 与记录数不变。
- 旧 bar 不得标 ready，也不得阻止后续重新抓取。
- 固定 benchmark 来源不匹配时不得复用。
- 运行 evidence service、snapshot、API、交易日历回归测试。
- 真实 API-only baseline 对比耗时、prepare 计数和数据库副作用。
