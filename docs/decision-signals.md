# DecisionSignal 决策信号专题

本页收口 #1390 P7，说明 DSA 如何把个股分析、Agent、告警和组合风险中的 AI 建议沉淀为可查询、可反馈、可后验评估的 `DecisionSignal` 资产。它是报告之上的结构化索引，不替代 Markdown 报告、`operation_advice`、三态 `decision_type`、告警规则或真实交易系统。

## 能力边界

- `DecisionSignal` 只记录建议、证据摘要、风险、观察条件、生命周期和来源，不执行下单或调仓。
- 写入失败、提取失败、告警信号关联失败和通知发送失败都不阻断主分析、告警触发或报告保存。
- #1756 只将 `decision_profile` 字段化并修正 server-side filter、去重、续期和 active 失效语义；不新增环境变量、config registry 项或 `.env.example` 内容。
- 当前没有 `DECISION_SIGNAL_*` 开关；信号功能的关闭或回滚通过 revert 对应代码完成。

## 字段与枚举

核心字段由 `api/v1/schemas/decision_signals.py` 定义，主要包括：

- 身份与来源：`stock_code`、`stock_name`、`market`、`source_type`、`source_agent`、`source_report_id`、`trace_id`、`decision_profile`、`trigger_source`。
- 建议语义：`action`、`action_label`、`confidence`、`score`、`horizon`、`market_phase`、`plan_quality`、`status`。
- 计划与解释：`entry_low`、`entry_high`、`stop_loss`、`target_price`、`invalidation`、`watch_conditions`、`reason`、`risk_summary`、`catalyst_summary`。
- 证据与质量：`evidence`、`data_quality_summary`、`metadata`。
- 生命周期：`expires_at`、`created_at`、`updated_at`。

枚举取值：

| 字段 | 取值 |
| --- | --- |
| `market` | `cn`、`hk`、`us`、`jp`、`kr`、`tw` |
| `source_type` | `analysis`、`agent`、`alert`、`market_review`、`manual` |
| `market_phase` | `premarket`、`intraday`、`lunch_break`、`closing_auction`、`postmarket`、`non_trading`、`unknown` |
| `action` | `buy`、`add`、`hold`、`reduce`、`sell`、`watch`、`avoid`、`alert` |
| `horizon` | `intraday`、`1d`、`3d`、`5d`、`10d`、`20d`、`swing`、`long` |
| `decision_profile` | `conservative`、`balanced`、`aggressive`；数据库 `NULL` 表示 legacy / unknown |
| `plan_quality` | `complete`、`partial`、`minimal`、`unknown` |
| `status` | `active`、`expired`、`invalidated`、`closed`、`archived` |

Web 展示必须把这些 wire value 映射为当前 UI 语言的用户可读标签；API 响应继续保留原始枚举值。

## Canonical 评分与 action 口径

个股分析、技术评分 fallback、报告展示 fallback 与 `DecisionSignal` 提取共用 `decision-scale-v1` 口径。`decision_type` 只保留 `buy|hold|sell` 兼容统计；更细的可执行语义以八态 `action` 为准。

- 用户侧可见面存在两类字段：`operation_advice` 保留文本口径（如“持有观察”），`action` 作为统一 8 态决策口径（如 `hold/watch/reduce`）用于风控、回测与列表展示。新生成或最终保存前重算的个股报告应优先让两者保持一致；历史记录或兼容载荷仍出现语义冲突时，默认以 `action` 为列表、回测、DecisionSignal 等结构化展示的优先字段，`operation_advice` 仅作说明文本保留。

| score | signal key | `action` | legacy `decision_type` | 语义 |
| --- | --- | --- | --- | --- |
| 80-100 | `strong_buy` | `buy` | `buy` | 强烈买入，高胜率机会，可执行买入/加仓计划 |
| 60-79 | `buy` | `buy` | `buy` | 偏积极机会，允许少量待确认项 |
| 40-59 | `watch` | `watch` | `hold` | 信号分歧或确认不足，等待触发条件 |
| 20-39 | `reduce` | `reduce` | `sell` | 风险明显抬升，优先降低暴露 |
| 0-19 | `sell` | `sell` | `sell` | 趋势或风险显著恶化，优先退出 |

如果 `score >= 60` 但最终 `action` 是 `hold/watch`，或 `score < 40` 但最终 `action` 仍是 `hold/watch`，必须有明确 guardrail 解释，例如 `dashboard.decision_stability.reason`、`dashboard.decision_score_calibration.guardrail_reason` 或 `metadata.guardrail_reason`。风控降级会保留 `raw_score`、`adjusted_score`、`raw_action`、`final_action` 和原因；没有明确原因的中性动作在 DecisionSignal 提取时会按 canonical score 对齐为 `buy/reduce/sell`。

## 生命周期、去重与状态

`src/services/decision_signal_service.py` 是信号生命周期的主入口：

- `horizon` 和 `expires_at` 显式传入时优先。
- 未传 `horizon` 时，`alert` 或盘前/盘中/午间休市/集合竞价阶段默认 `intraday`，盘后、非交易时段、未知阶段或缺少阶段时默认 `3d`。
- `intraday` 过期时间优先读取低敏 `metadata.market_phase_summary.minutes_to_close/minutes_to_open`；缺失时按市场 fallback TTL。
- `expired`、`invalidated`、`closed`、`archived` 不能通过 `PATCH /status` 直接恢复为 `active`。
- 同源去重优先使用 `(source_report_id, source_type, market, stock_code, decision_profile, action, horizon, market_phase)`；没有 report 但有 `trace_id` 时使用 trace 维度。
- `decision_profile` 参与信号身份：`NULL` 只与 `NULL` 匹配，非空 profile 只与相同 profile 匹配。Exact dedup、relaxed dedup、horizon/phase fill、expired refresh、active invalidation 和 stale backfill invalidation 都遵循该 same-profile 语义。
- 新的相反 active 信号只会把同 profile 的旧 active 信号标记为 `invalidated`，并把失效来源写入 metadata。不同非 `NULL` profile 可并存，即使 action 相反。
- Expired duplicate refresh 不会改写 `decision_profile`，只能刷新同 profile 记录。

## API

当前公开接口由 `api/v1/endpoints/decision_signals.py` 和 `docs/architecture/api_spec.json` 描述：

- `POST /api/v1/decision-signals`：创建或按同源键去重，返回 `{ item, created }`。
- `GET /api/v1/decision-signals`：分页查询，支持市场、股票、动作、阶段、`decision_profile`、来源、状态、时间范围和持仓过滤。省略或传空 `decision_profile` 不加 profile 条件，返回所有 profile；`decision_profile=unknown` 查询 `NULL` 行；合法 profile 精确匹配。
- `GET /api/v1/decision-signals/{signal_id}`：查询单条。
- `PATCH /api/v1/decision-signals/{signal_id}/status`：更新状态和可选 metadata。
- `GET /api/v1/decision-signals/latest/{stock_code}`：查询股票最新 active 信号。
- `POST /api/v1/decision-signals/outcomes/run`：显式触发后验评估。
- `GET /api/v1/decision-signals/outcomes`、`GET /api/v1/decision-signals/outcomes/stats`、`GET /api/v1/decision-signals/{signal_id}/outcomes`：查询后验结果与统计。
- `GET/PUT /api/v1/decision-signals/{signal_id}/feedback`：查询或写入 useful / not useful 反馈。
- `GET/PUT /api/v1/decision-signals/{signal_id}/shadow-feedback`：查询或写入前瞻验证记录；首次冻结推荐上下文，后续只更新人工结果。
- `GET /api/v1/decision-signals/{signal_id}/quality`：查询冻结的持仓双轴决策、5/20/60 日结果和归因。
- `POST /api/v1/decision-signals/quality/outcomes/run`：显式评估已成熟的持仓质量窗口；不包含 scheduler。
- `GET /api/v1/decision-signals/quality/stats`：查询当前质量引擎的成熟样本统计；零样本返回空状态。
- `PUT /api/v1/decision-signals/{signal_id}/attributions/{horizon}`：人工提交或确认单个窗口的归因。
- `GET /api/v1/decision-signals/quality/weekly-review`：查询周复盘 case、分歧和仅供观察的候选模式。
- `PUT /api/v1/decision-signals/{signal_id}/execution-links/{trade_id}`、`GET /api/v1/decision-signals/{signal_id}/execution-links`：把冻结建议归因到已存在的 DSA trade；只写 sidecar，不改交易账本。同日且缺少真实成交时刻时保持 `same_day_unknown`。
- `POST /api/v1/decision-signals/strategy-validation/shadow-comparisons`：记录 champion/challenger 对同一冻结输入的不可改写比较；不会创建 production DecisionSignal。
- `GET /api/v1/decision-signals/strategy-validation/shadow-review`、`GET /api/v1/decision-signals/strategy-validation/review-summary`：读取影子周复盘与只读治理摘要。
- `POST /api/v1/decision-signals/strategy-validation/reviews`、`POST /api/v1/decision-signals/strategy-validation/rollbacks`：显式人工审批、拒绝、退役或切换 validation-side selected reference；不会自动激活 daily runtime。
- `POST /api/v1/decision-signals/reassess`：基于来源历史报告预览不同决策风格下的信号，不写库。

这些接口继承现有 `/api/v1/*` 管理员鉴权；`ADMIN_AUTH_ENABLED=true` 时需要有效管理员会话 Cookie。

## Reassess preview

`reassess` 第一版只做 preview，不创建或更新 `DecisionSignal`。

请求只支持：

```json
{
  "source_report_id": 123,
  "decision_profile": "aggressive",
  "persist": false
}
```

契约边界：

- `source_report_id` 是唯一事实来源，重评估只读取对应持久化历史报告快照。
- 不支持 `signal_id`，也不接受客户端提交 `action`、`score`、`confidence`、价格、metadata 或 guardrail 结果；额外字段会被请求校验拒绝。
- `persist=true` 当前固定返回 HTTP 400，错误码为 `unsupported_operation`。保存重评估结果留给 #1757。
- 重评估不会静默抓取实时行情，也不会用当前市场数据补齐历史快照。
- 历史报告不存在、非个股报告或快照缺少结构化决策输入时，分别返回明确错误。
- data quality 会归一为 `high`、`medium`、`low`、`poor`、`unknown`，guardrail 只使用归一化后的等级。
- `guardrail_result` 是机器审计数据，记录 raw/final action、是否通过、violations 和 adjustments；`warnings` 是用户可读摘要，测试和客户端逻辑应优先依赖稳定 `code`。
- blocked preview 仍是 HTTP 200，UI 必须突出 `blocked_reason`，不能把它当作普通可执行信号。
- aggressive 不是模型采样温度语义，也不会自动生成三套 profile 信号。

## Web 展示

Web 入口位于 `/decision-signals`：

- 默认查询 `status=active`。
- 页面顶部提供页面级“当前股票”主路径，独立于高级列表筛选。用户提交主股票、选择自动补全候选或点击候选 chip 后，latest active 与时间线共用同一个已应用股票上下文；只修改输入草稿不会触发 latest 或时间线查询。
- 当前股票候选优先展示最近分析过的股票；如果没有历史候选，或历史候选加载失败，则降级展示股票索引中 active 且 popularity 较高的热门股票。候选只作为手动点击入口，页面加载时不会自动提交查询；历史和股票索引都不可用时仅显示无候选降级文案。
- 当前股票上下文会显示已应用的代码、名称和可推导市场，并提供清空入口。清空会让 latest 与时间线回到引导态，不影响高级列表筛选或列表来源详情抽屉。
- 支持按市场、股票代码、动作、市场阶段、来源、来源报告 ID 和状态进行高级列表筛选；这些筛选不等同于当前股票上下文，也不会污染 latest active 查询。
- 单支股票信号时间线复用现有 `GET /api/v1/decision-signals` list API，不新增 timeline endpoint。时间线必须先应用非空当前股票后才会查询；没有当前股票时只显示引导态，不拉取 market-only 或 global timeline。
- 时间线只支持 `30d`、`90d`、`180d` 三个时间范围，默认 `90d`；每次最多请求 100 条。若返回 `total > items.length`，Web 会显示“仅展示最近 100 条信号，请缩小时间范围”，避免静默展示不完整轨迹。
- 时间线筛选保留独立的 market、range、status 表单和查询按钮。选择新当前股票时，如果能推导市场，只在这一次初始化时间线 market；用户之后可以手动改 market，查询以按钮提交时的表单快照为准。
- 时间线 status filter 只支持 `all` 与 `active`：`all` 不传 `status`，`active` 传 `status=active`。P1 不提供 terminal status filter，也不做前端 terminal 过滤。
- 时间线支持 profile filter，复用 list API 的 server-side `decision_profile` 查询；`unknown` 只用于筛选和展示 legacy `NULL` 行。普通高级列表不新增 profile filter。
- 信号表现统计保持全局已复盘 outcome 口径，不等于当前可见信号数量，也不随当前股票或高级列表筛选变化；当已复盘样本数为 0 时，Web 显示零样本空状态而不是一组 `0/-` 指标。
- Web 展示优先读取正式 `decision_profile` 字段，只有字段缺失时才回退 legacy metadata；历史缺失或非法 profile 的信号显示为 `unknown`，不会误标为 `balanced`。
- market filter 在 API / 服务层与 Web 前端均已支持 `cn/hk/us/jp/kr/tw`；`jp/kr/tw` 的前端本地化标签均已补齐，`tw` 信号可经 API 正常写入、按 `market=tw` 查询，并可在 Web DecisionSignal 页面通过市场筛选项选择台股（tw）；告警（大盘红绿灯）市场支持 `cn/hk/us/jp/kr`。
- 详情抽屉展示动作、状态、评分、置信度、周期、计划质量、市场阶段、价格计划、风险、观察条件、证据、数据质量和 metadata。
- 详情抽屉或已有来源报告 ID 的页面上下文可以发起 reassess preview；没有可用来源报告 ID 时入口禁用。Preview 不加入列表、latest 或时间线，也不提供保存按钮。
- Web 只能把信号标记为 `closed`、`invalidated` 或 `archived`，不提供 terminal 状态恢复为 active。
- 历史报告详情不再内嵌展示报告绑定的 `source_type=analysis` 信号，也不会因打开报告详情触发 `source_report_id` 信号查询；需要查看报告来源信号时统一进入 `/decision-signals` 页面按来源报告 ID 精确筛选，或打开 `/decision-signals?sourceReportId=<recordId>` deep link。该筛选和 deep link 都会使用 `source_type=analysis + source_report_id` 的精确查询，以保留旧报告的 best-effort 懒回填入口。
- 持仓页异步查询每个唯一持仓的 latest active 信号，单只查询失败只显示降级提示，不阻断组合快照或其他持仓信号。

所有用户可见枚举必须使用 i18n 标签；技术 ID、股票代码、API 字段名、env key、URL 示例可以保留英文。

## Decision profile identity

#1756 后 `decision_profile` 是 `decision_signals` 的正式 nullable 字段，同时 metadata 保留兼容字段：

- `decision_profile=balanced`
- `profile_source=auto_default`：普通新分析生成路径。
- `profile_source=backfill_defaulted`：历史报告 lazy backfill 路径。
- `profile_policy_version=decision-profile-v1`
- `signal_generation_version=legacy-report-extractor-v1`
- `decision_signal_metadata_version=decision-signal-metadata-v1`

- 新写入时，顶层合法 `decision_profile` 优先；顶层显式 `null`、空值或非法值直接拒绝。顶层缺失时才 fallback 合法 `metadata.decision_profile`；二者都缺失或 metadata profile 非法时默认写入 `balanced`。
- 新写入会同步 `metadata.decision_profile` 为正式字段值，避免双源冲突；metadata 省略或显式 `null` 均按无 metadata 处理，object 会浅复制，非 object 会被拒绝。
- PATCH metadata 省略时保留原值，显式 `null` 时清空为 SQL `NULL`，object 时整包替换。正式 profile 非 `NULL` 时会覆盖 metadata 中的冲突值；正式 profile 为 legacy `NULL` 时会移除请求 object 中的 profile key，且不会提升正式字段。
- 自动失效写入同样遵循正式字段权威语义：正式 profile 非 `NULL` 时同步 metadata profile；legacy `NULL` 时只追加失效信息，保留原 legacy metadata，不注入或删除 profile。
- Legacy / unknown 只用数据库 `NULL` 表示。`profile_policy_version` 只表示默认 profile metadata contract version，不代表已经实现独立 profile policy engine、scoring engine 或多 profile 生成。P1/P2 不写入 `scoring_version` 或 `scoring_breakdown`；这些字段如需引入，应由后续 reassess / scoring issue 定义。
- Lazy backfill 语义：省略 profile 保留旧的 `source_type=analysis + source_report_id` 懒回填；`decision_profile=balanced` 可生成 balanced 回填；`decision_profile=unknown`、`conservative`、`aggressive` 不自动创建行。

## 市场结构 metadata

普通个股分析和 Agent 个股分析如果携带 `market_structure_context`，自动提取 `DecisionSignal` 时会把以下低敏字段追加到 metadata：

- `market_structure_version`
- `market_theme_version`
- `stock_market_position_version`
- `market_structure_status`
- `primary_theme`
- `theme_phase`
- `stock_role`
- `market_structure_risk_tags`

这些字段只用于解释信号所处题材背景，不参与 `action`、`score`、`horizon`、同源去重键或生命周期计算。它们也不是题材龙头证明；当 `market_structure_risk_tags` 或缺失证据显示成分股、leader stocks 不完整时，客户端和后验分析应按降级题材证据处理。

快照字段中的 `provider` / `dataset` 来自市场结构抽取链路元数据，属于运行后持久化证据，不参与 LLM provider/model 路由、`base URL` 解析、`.env` 写回或配置迁移；可核验范围见 `src/schemas/market_structure.py`。

## 告警、通知与组合风险

- 股票级真实告警触发会优先关联同标的 latest active 信号，并把低敏 `decision_signal_summary` 写入 `alert_triggers.diagnostics`。
- 没有 active 信号时，告警 worker 只创建最小 `source_type=alert/action=alert` 信号。
- 告警信号的 `trace_id=alert-rule-<hash>` 只用于同源重试的 best-effort 去重，不覆盖 active 信号本体。
- 通知只引用公开摘要字段：`action`、`horizon`、`reason`、`watch_conditions`、`risk_summary`、`source_report_id`。
- 通知中的 `reason` 在脱敏后完整展示，避免固定字符数在句中截断；`watch_conditions` 和 `risk_summary` 仍保持紧凑摘要上限。
- 通知不得输出 signal `metadata`、`evidence`、raw diagnostics、webhook URL、token 或 cookie。
- `GET /api/v1/portfolio/risk` 的 `decision_signal_risk` 只统计当前持仓中的 active `sell/reduce/alert` 信号，查询失败时 fail-open。

更多告警和通知细节见 `docs/alerts.md` 与 `docs/notifications.md`。

## 后验评估与反馈

P5 通过 sidecar 表保存用户反馈和后验结果，不扩展 `decision_signals` 主表：

- `decision_signal_feedback` 保存每个信号最新的 `useful|not_useful` 反馈、可选原因/备注和来源；前瞻记录还冻结 snapshot hash、evidence sources、gated recommendation、推荐时间、证据过期、延迟和 token，并保存人工判断、实际动作与修正时间。
- `decision_signal_outcomes` 按 `(signal_id, horizon, engine_version)` 幂等保存后验评估结果。
- 当前 `engine_version=decision-signal-v1`。
- 后验评估只支持日线可验证的 `1d/3d/5d/10d/20d`；`intraday/swing/long`、非方向动作、缺价和 forward bars 不足会写入 `eval_status=unable` 与明确 `unable_reason`。
- 没有持仓质量上下文的 legacy 信号，首次 shadow 写入仍必须有真实 usefulness feedback、frozen snapshot hash、证据来源、人工判断、实际动作、修正分钟数、延迟和 token，且信号必须有 `expires_at`。
- 已有持仓质量上下文的信号由服务端引用冻结 snapshot；首次写入只要求人工决策。usefulness 可以暂缺：为兼容旧 SQLite `NOT NULL` 列，内部保存 `unrated`，公开 API 始终序列化为 `feedback_value=null`，`unrated` 不是公开枚举。
- 评估时冻结 action、market、market_phase、source_type、source_agent、plan_quality、data_quality_level、holding_state 等统计维度，历史统计不依赖后续 live join。

## 持仓决策质量闭环

持仓分析不再把“现有仓位怎么处理”和“新增资金是否投入”压缩成一个 action：

| 轴 | 枚举 | 含义 |
| --- | --- | --- |
| `position_action` | `hold`、`reduce`、`exit` | 只评价已经持有的暴露 |
| `incremental_action` | `add_in_batches`、`wait`、`no_add` | 只评价尚未投入的新增资金 |

两轴独立门禁；不能从 legacy `buy/add/hold` 推断 `add_in_batches`。`ADD_IN_BATCHES` 只有在 DSA 风险预算、现金、标的身份和既有 sizing/unit contract 都充分时才可采用，否则必须是 `WAIT`/`INSUFFICIENT_EVIDENCE`，且系统不制造仓位比例或建议数量。

质量窗口固定为：

- `5d`：短期执行与时机观察，不能替代长期 thesis 判断。
- `20d`：shadow 运行的首个操作性评估门；成熟前不得声称设计改善了收益。
- `60d`：较长期 thesis 与决策价值证据；60 个对齐交易 bar 未成熟前，长期结论继续标为 `PROVISIONAL`。

每个 material event 会冻结 snapshot hash、产品类型、双轴 AI 建议、5/20/60 置信度、基准身份、cutoff 和版本。产品类型来自同一冻结 research snapshot，属于 material fingerprint，并原样写入 outcome 供学习分组；缺失时不能回退当前 registry。人工反馈只写入 `accept|modify|veto|no_action` 及人工双轴/原因：`accept` 默认采用冻结双轴且不得提交冲突值；`modify` 必须提交两个人工轴；`veto` 必须有 reason 或 note；`no_action` 不计为同意。任何反馈都不能改写 AI 原始建议、冻结上下文或已经计算的 outcome。

由持仓分析 API 创建的 DecisionSignal 会复用请求绑定的完整 research snapshot。`account_id`、标的身份、产品类型、`frozen_snapshot_hash`、固定 benchmark、cutoff、evidence version、decision profile 与 `portfolio-decision-v1` 由 DSA 在持久化前确定性写入，不依赖模型重复生成；snapshot 不提供的 benchmark 仍保留为缺证据并 fail closed，不进行猜测。

结果只使用决策日同日锚点、标的与固定 benchmark 完全对齐的前瞻交易 bar，以及一致且可识别的复权标记。它会计算标的/基准/超额收益、MFE 和 MAE，但不把相关性当因果，也不把“事后持有”反事实当作可交易收益。`reduce` 或 `add_in_batches` 缺少冻结 exposure/tranche contract 时，`decision_value_status=unable`，不会制造反事实仓位。

质量上下文或 outcome 的明确不可评估原因包括：

- 冻结上下文：`account_id_missing`、`instrument_identity_missing`、`instrument_type_missing`、`frozen_snapshot_hash_missing`、`frozen_snapshot_hash_invalid`、`position_action_missing`、`incremental_action_missing`、`benchmark_identity_missing`、`evidence_cutoff_missing`、`evidence_version_missing`、`decision_profile_missing`、`decision_version_missing`、`invalidation_missing`、`next_review_missing`、`confidence_horizons_incomplete`。
- 前瞻结果：`missing_context`、`instrument_type_missing`、`missing_benchmark_identity`、`missing_anchor_price`、`missing_benchmark_anchor`、`insufficient_forward_bars`、`corporate_action_adjustment_unknown`、`horizon_not_mature`、`exposure_contract_missing`。

缺少 account、标的身份、snapshot hash、两轴动作或 cutoff 时，无法形成稳定的 material-event 身份：对应 blocker 保留在 `DecisionSignal.metadata`，状态为 `insufficient_evidence`，不写入伪造或半成品 sidecar。每个 signal 保留自己的不可变冻结上下文用于审计；重复刷新可共享同一 material fingerprint，但统计必须按 fingerprint 去重为一个 effective sample，不能把后续 signal id 计成独立样本。

归因类别固定为 `fact_error|evidence_error|thesis_error|valuation_error|timing_error|risk_error|execution_error|unattributed`，状态为 `proposed|confirmed|rejected`。学习汇总只使用 `confirmed` 归因，并按 category、horizon 和 instrument type 分组，保留精确样本数、反例、单标的集中和重复事件警告。候选模式状态始终为 `observed`，`automatic_activation=false`；不会自动修改评分、Prompt、风险策略或执行动作。所有成熟表现统计仍为 `PROVISIONAL`。

当前策略验证 outcome 使用 `decision-quality-v2`。Observation anchor 是 cutoff 前最后一根完全已知 bar，shadow execution anchor 是 cutoff 后第一根可交易 bar，实际执行 anchor 只接受已确认 execution link。完成结果按 `engine_version + data_revision_hash + input_bar_hash` 不可就地覆盖；修订数据产生新 revision。统计必须显式按 horizon、market、product type、strategy version 和 outcome engine 分组，同时区分 event count 与 material fingerprint 去重后的 effective sample count。

Execution link 不复制交易。服务只读核对 DSA `portfolio_trades` 的 account、canonical instrument、方向、数量、价格、费用与税费，并冻结 recommendation-time position quantity。`created_at` 只是记录时间；缺少真实成交时刻或人工先后确认时不能用于 actual-execution return。无 link 也不等于 `HOLD`，实际动作保持空值。

前瞻 shadow 已于 `2026-07-25` 以 `decision-quality-v1`、`portfolio-decision-v1` 和 `portfolio-research-snapshot-v1` 启动。只有冻结上下文完整且 material-event fingerprint 唯一的事件才进入 eligible 样本；重复刷新不增加样本数。启动时所有 5/20/60 窗口均未成熟，实际成熟日期由标的与固定 benchmark 对齐后的真实交易 bar 决定，不预填日历日期。完整启动记录、当前缺失证据和 `PROVISIONAL` 边界见 [DSA 组合研究控制面](portfolio-research-workflow.md#shadow-启动记录2026-07-25)。

策略版本、point-in-time replay、purged walk-forward、prospective shadow 与人工 promotion/rollback 的完整操作契约见 [组合策略验证闭环](portfolio-strategy-validation.md)。

## 脱敏与低敏边界

信号写入和状态更新使用 `src/utils/sanitize.py` 中的 `sanitize_decision_signal_text()` 与 `sanitize_decision_signal_payload()`：

- 文本字段、JSON 字段和展示型短文本写入前会脱敏。
- 覆盖敏感 key、Bearer、Authorization/Cookie header 或赋值、token-like 字符串、webhook URL、URL userinfo，以及带敏感 query/fragment 参数的 URL。
- 普通证据 URL 会保留，保证来源可追溯。
- `trace_id` 是同源去重身份字段；如果包含会被脱敏的 credential，API 会拒绝请求，而不是保存被 redaction 破坏后的身份值。
- Web 的 JSON 展示只显示后端已脱敏数据，不应重新拼接 raw diagnostics 或配置值。

P7 的全局验收是确认信号池、通知摘要和 Web 展示不泄露 token、cookie、webhook URL、API key、邮箱密码等敏感信息。

## 迁移与回滚

#1756 对 SQLite 执行非破坏性 migration。

迁移说明：

- 升级后无需新增 `.env`、`.env.example` 或 Web 设置项。
- Existing SQLite 只在缺列时 `ALTER TABLE ADD COLUMN decision_profile`、nullable shadow-feedback 列和 quality-context `instrument_type` 列，不会 drop/rebuild 既有表，也不会删除旧 index、feedback 或 quality row。旧 quality row 无法从 hash 可靠反推 recommendation-time 产品类型，因此保持 `NULL` 并在 outcome 返回 `instrument_type_missing`。
- Migration 会幂等创建 profile-aware indexes，并 row-by-row 防御解析 `metadata_json`：仅合法 `metadata.decision_profile` 回填到正式字段；invalid JSON、非 object 或非法 profile 保持 `NULL`。启动日志会记录 backfilled、invalid JSON、non-object、invalid profile 和 skipped existing profile 统计，这些统计只用于诊断，不阻断启动。
- 旧历史报告不会批量回填。只有显式调用信号列表接口或在 Web AI 建议页按来源报告 ID 触发精确查询 `source_type=analysis + source_report_id` 且无命中时，才会 best-effort 懒回填。
- 已存在的 `decision_signals`、feedback 和 outcome 数据保持兼容。

回滚说明：

- 当前没有 `DECISION_SIGNAL_*` 开关；关闭信号提取/写入的回滚方式是 revert 相关代码。
- 回滚后，普通报告保存、告警触发、通知发送和组合风险主流程仍按既有路径运行。
- 回滚不会自动删除历史 `decision_signals`、`decision_signal_feedback` 或 `decision_signal_outcomes` 数据；如需清理，应由维护者单独制定数据清理策略。
