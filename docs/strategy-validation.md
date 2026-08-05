# 策略回测与验证

这套流程只回答两个问题：一套固定规则过去表现怎样，以及它是否有资格进入下一阶段。它不会自动改变持仓、分配资金或生成订单。

## 主线

```text
固定策略 -> 历史回测 -> 模拟观察 -> 小额验证 -> 正式使用或停用
```

1. **固定策略**：保存策略名称、版本、改了什么、适用市场和产品、成本与基准。相同版本不可覆盖。
2. **历史回测**：只使用版本化的冻结原始数据。它先生成候选记录和资格清单，再把可用样本交给继续持有基线。
3. **模拟观察**：记录之后真实出现的 5/20/60-bar 结果，不投入资金。
4. **小额验证**：只有模拟样本合格后，才可由人批准进入小额真实周期。
5. **正式使用或停用**：仍由人根据样本、风险和失败场景决定；系统不会自动晋升。

## 操作指示

普通界面只显示一个明确动作：

| 显示 | 含义 |
| --- | --- |
| 加仓 | 保留现有仓位，并允许分批投入新增资金 |
| 持有 | 保留现有仓位，但当前不追加资金 |
| 减仓 | 降低已有仓位，不追加资金 |
| 清仓 | 退出已有仓位，不追加资金 |
| 资料不足 | 关键证据不完整，不能形成可执行建议 |

后端仍保留两项内部字段，用于准确记录已有仓位和新增资金的不同约束。普通用户无需分别操作这两个字段。

## 怎么看成绩

Web 的“回测”页面默认打开“策略成绩”。每个结果必须按以下范围分开显示，不能把不同周期或不同产品混成一个结论：

- 5、20、60 个交易 bar；
- A 股、港股、美股；
- 股票、ETF、QDII、ADR/ADS、每日重置产品；
- 显示动作、市场环境；
- 开发期和未参与调参的验证期。

每组至少显示样本数、胜率定义、扣除成本后的收益、相对基准收益、最大回撤、平均盈利/亏损、换手、总成本和无法评价的数量。页面中的“历史分析记录”是旧兼容结果，不代表完整策略表现。

## 必须停止的情况

缺少以下任一项时，历史回测整次返回“资料不足”，不输出部分表现结论：

- 决策截止时间或标的身份；
- 产品类型或复权口径；
- 决策时已知的 benchmark、FX 或结构化输入；
- 截止时间之后的可交易开盘价；
- 手续费、税费、滑点、汇兑和产品成本假设。

包含当前新闻或 AI 证据、但无法恢复历史时点输入的策略必须标记为“只做模拟观察”。当前策略使用这一限制，不借用持有基线或旧分析命中率作为历史成绩。

## 只做模拟观察的策略

`forward_only` 策略不会产生合格的历史回测记录。即使一次记录明确标记
`historical_status=not_available`，也不能满足历史回测策略进入模拟阶段的门槛。

这类策略从“待回测”开始，由人填写理由后选择“开始模拟观察”。这是从当天起
记录真实可用输入和后续结果，不表示已经完成或通过历史回测；系统不会自动改变
阶段。进入小额验证前，仍必须有合格的前向观察记录和人工批准。

## 新建议的复盘资料

从证据账本上线后，新持仓建议会绑定固定的策略版本，并把当时可见的持仓、现金、风险、价格、标的、产品、复权、benchmark、FX、交易单位、成本和研究证据保存到 `decision_signal_evidence_snapshots`。记录创建后不能更新或删除；相同内容可以安全重试，内容发生变化会被拒绝。

持仓复盘只显示两个简单状态：

- **复盘资料：已保存**：关键资料齐全，可以等待 5/20/60 个交易 bar 后评价。
- **复盘资料：资料不足**：缺少关键资料或写入失败，本条建议不计入有效策略成绩。

旧建议不使用今天的资料回填，统一保持“资料不足”。证据账本不会自动改变策略阶段，也不会修改持仓、现金、交易或生成订单。当前系统提供显式的资料准备入口来采集策略规定的 benchmark、FX 和复权行情；产品身份、交易单位和风险政策仍必须来自已核验的现有真源。任一项缺失或过期时显示“资料不足”，这属于安全行为，不代表策略失败。

资料准备只接受该市场上一根已经结束的交易日数据，不把盘中同日 close 或更早的旧 bar 当作合格收盘价。`portfolio-research-evidence-prepare-v2` 将完整行情窗口追加到版本化 evidence batch，并复用日期、schema 和固定 benchmark 来源完全匹配的现有 batch；`stock_daily` 旧缓存不会被复用、覆盖或补写来源。benchmark 超过 72 小时或 FX 超过 7 天时不能冻结为完整记录。

2026-07-31 的第一次真实全持仓模拟中，17 条持仓全部因重叠日线冲突或 `000300/HSI` 基准缺失停在“资料不足”。这次结果只证明失败关闭生效，不是策略亏损、回测失败或有效前向样本；没有保存新建议，也没有开始计算 5/20/60 日成绩。下一步先修复稳定行情证据，再重新建立第一批样本。

2026-08-03 已完成固定 benchmark、复权一致性、冻结分析只读保护和只追加行情 evidence batch。snapshot 将 position/benchmark batch hash 纳入冻结输入，绑定 pipeline 与 Agent 历史工具只读 exact batch。在新的真实验收产生资料完整建议之前，策略成绩仍没有新增有效样本。

## 本地确定性回测

历史样本只接受本地冻结 JSON，流程是：

```text
冻结原始数据 -> 候选记录 -> 可用样本 / 排除原因 -> 继续持有基线成绩
```

当前数据库可以先只读导出真实候选及证据缺口：

```bash
.venv/bin/python scripts/portfolio_strategy_export_source.py \
  --database data/stock_analysis.db \
  --frozen-at 2026-07-31T18:00:00+08:00 \
  --reporting-currency CNY \
  --output /tmp/portfolio-strategy-real-candidates.json
```

这个命令使用 SQLite `mode=ro`，不会初始化 `DatabaseManager`、迁移或写入主数据库业务内容，也不会联网读取当前 registry、行情或研究快照补历史。读取 WAL 模式数据库时，SQLite 可能维护 `-shm` 协调文件，因此这里不承诺文件系统零副作用；主数据库内容、大小和 mtime 必须在验收前后保持不变。`frozen_at` 必须是已经发生的时间，未来时间会被拒绝。导出结果中的“真实候选”只表示数据库确实存在这些决策记录；缺少策略绑定、冻结输入、历史身份、benchmark、FX、复权、成本或后续 bar 时，候选仍必须被资格构建器排除，不能称为真实历史成绩。

先构建资格清单：

```bash
.venv/bin/python scripts/portfolio_strategy_build_dataset.py \
  --source /path/to/versioned-frozen-source.json \
  --output /tmp/portfolio-strategy-dataset.json
```

再运行继续持有基线：

```bash
.venv/bin/python scripts/portfolio_strategy_backtest.py \
  --dataset /tmp/portfolio-strategy-dataset.json \
  --strategy strategies/portfolio_hold_baseline_v1.json \
  --output /tmp/portfolio-strategy-report.json
```

两个命令默认只读本地输入，只写指定的 JSON 输出文件；不写数据库、不启动分析服务、不访问网络，也不修改持仓、现金或交易。`--real-performance` 只能用于明确标记为 `synthetic=false` 且 `dataset_hash` 完整校验通过的数据；测试数据、缺少分类或 hash 不一致时命令失败关闭，不输出真实成绩声明。

构建器和回测器分别保护三层内容：

- `source_snapshot_hash`：冻结原始输入的内容 hash，覆盖候选、价格 bar、身份、FX、成本和冻结时间；调用方声明的 hash 不一致会被拒绝。
- `dataset_hash`：资格清单和可用样本的内容 hash；回测前必须重新验证，篡改任一事件字段会使整次回测成为“资料不足”。
- `eligible_event_set_hash`：按排序后的唯一样本编号计算，保证不同策略只能比较同一批可用样本。

资格检查使用以下固定口径：决策截止时间后的首个明确可交易开盘价作为执行价；执行后第 5、20、60 个有效 bar 作为结果期限；同一账户、策略版本、动作、输入和标的在同一本地日期内没有实质变化时只保留一条样本。`reporting_currency` 和 FX 只作为完整性证据，收益仍按标的本币计算，不做跨币种汇总；FX pair 必须与标的币种和报告币种一致，同币种汇率必须为 1。benchmark 币种必须等于标的币种。

每日重置产品与普通股票、ETF、QDII、ADR/ADS 隔离处理。每日重置产品缺少 reset frequency、底层身份或各自决策时点证据时，逐条排除；不得用普通产品证据补齐。

`tests/fixtures/strategy_validation/synthetic_frozen_historical_source_v1.json` 只用于实现验证，不能据此声称真实表现。2026-07-31 已从当前数据库只读导出第一份真实候选 source：101 条候选全部因决策时证据不完整被排除，继续持有基线返回“资料不足”。这证明真实资格链路会失败关闭，不代表已经获得真实胜率、收益、最大回撤或相对基准成绩。

## API

- `GET /api/v1/strategy-validation/strategies`
- `GET /api/v1/strategy-validation/strategies/{strategy_key}/versions/{version}`
- `GET /api/v1/strategy-validation/runs/{run_id}`
- `POST /api/v1/strategy-validation/strategies`
- `POST /api/v1/strategy-validation/runs`
- `POST /api/v1/strategy-validation/strategies/{strategy_key}/versions/{version}/transition`
- `GET /api/v1/decision-signals/{signal_id}/quality`：返回复盘资料状态和策略版本摘要，不返回完整证据正文。

阶段变更必须提交人工理由。API 不提供自动晋升或订单入口。
