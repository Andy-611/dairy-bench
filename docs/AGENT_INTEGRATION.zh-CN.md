# Agent 接入

## V4 Agent 契约

Dairy Bench 运行九个独立企业 Agent：三家牧场、三家加工厂和三家零售商。Scheduler
每次唤醒 Agent 时只执行一个封闭、可审计的决策循环：

```text
唤醒
  -> AgentTurn（权威的企业私有观察）
  -> 恰好一条 CompanyCommand
  -> EconomyEngine 校验并返回 CommandOutcome
  -> 不可变 TurnRecord
```

Agent 不提交整日计划，也不能自行提供身份、时间、状态版本、命令 ID 或 Turn ID；这些
字段由 `EpisodeRuntime` 写入 `CommandEnvelope`。

## 结构化命令格式

模型必须选择一条且仅一条符合企业角色的命令：

| 命令 | 含义 |
|---|---|
| `produce(product, quantity)` | 启动一项牧场生产作业 |
| `transform(input_product, output_product, input_quantity)` | 启动一项加工转换作业 |
| `set_quote_ladder(product, side, levels)` | 原子设置最多三档目标价格与数量 |
| `set_retail_price(product, unit_price)` | 设置零售消费者价格 |
| `wait(until?)` | 等待指定时刻或下一个相关事件 |

牧场可以生产和交易原奶；加工厂可以转换并交易原奶或盒装奶；零售商可以交易盒装奶
并设置消费者价格。

`set_quote_ladder` 的每档都包含 `quantity` 和 `limit_price`。一个阶梯最多三档且价格
不得重复；所有数量和价格都必须是 `0.0001` 的整数倍（最多四位小数），并且每档
数量必须为正数。引擎会整条拒绝非法命令而不是四舍五入。`levels` 为空表示撤掉该
企业在对应产品和方向上的
全部报价。

该命令描述目标状态，而不是一串交易所操作。Agent 必须按价格从高到低提交买方目标，
按价格从低到高提交卖方目标；顺序错误的阶梯会被拒绝。市场再确定性对账：

1. 价格和数量都完全相同的档位保持不变，保留订单身份和优先级；
2. 剩余目标中与旧档同价的档位执行 Replace；
3. 其余旧档与目标档按本侧价格优先顺序配对执行 Replace；
4. 没有目标与之配对的旧档执行 Cancel，没有旧档与之配对的目标档执行 Place。

每个 Replace 或 Place 档位都是一张拥有新身份和新优先级的独立订单。由于档位不包含
模型提供的身份，系统无法区分“改价”与“删除后新增”的主观意图；两者都会失去旧
优先级，并由上述确定性配对形成审计结果。一次 `set_quote_ladder` 即完成整组对账，
只消耗一个 Agent Turn。

成功后的 `CommandOutcome.quote_ladder_result` 会报告每个目标档位的 `keep`、`replace`
或 `place` 动作、结果订单 ID 与优先级、立即撮合后的剩余数量，以及单独 Cancel 的订单
ID；成交事件和计划到货仍使用 Outcome 原有的事件字段。

所有模型运行统一使用 NewAPI 的 Chat Completions 接口，并要求恰好一个已授权函数调用。
Adapter 优先设置 `tool_choice="required"`；若 Provider 明确拒绝这个可选提示，则只省略
该字段重试，同时保留完整 tools schema 和同样的输出校验。所有请求的 `max_tokens`
固定为 131,072，不进行阶梯式预算增长，也不提供环境变量覆盖。无论模型属于哪个家族，
输出都按同一个带判别字段的 Pydantic `CompanyCommand` 联合类型校验。缺失、多条、
未知、未授权或参数错误的调用都不能改变经济状态。

## Agent 可以看到什么

`AgentTurn` 只包含 Runtime 授权给当前企业的事实：

- 模拟时间、状态版本、当日 Turn 序号与上限、强类型唤醒原因及因果引用；
- 可用现金和库存，以及适用时的零售价；
- 预留现金、统一口径的 `marked_surplus`，以及按产品和到期日分桶、分别列出可用量与
  挂单预留量的自有现货库存；
- 自有未完成订单，包括剩余数量、优先序号和同价 FIFO 队列前方的未成交总量；
- 与自身业务有关的匿名 `OrderBookView`：全部买卖价位的聚合数量与活跃订单数，以及
  最近成交价和当日成交量；`bids[0]` 与 `asks[0]` 分别是最优买价和最优卖价；
- 已保证的待到货商品、数量、准确到货时间及数量守恒的到期日分桶；
- 当前生产或加工任务，以及权威的当日作业状态；
- 企业可见领域事件和上一条命令结果。

Provider 输入还包含强类型 `decision_constraints` 投影。它始终显式序列化营业时间与
运行时限；对于生产企业，还会显式给出当日已用和剩余作业产能。这些值均由
`AgentTurn` 即时派生，不是第二份经济状态。Provider 输入会把该投影和 Turn 与当前
企业独立且有预算上限的记忆上下文组合起来。

Agent 看不到公开订单簿中其他企业的身份，也看不到其他企业的私有资产、记忆、Prompt
或 Provider trace。引擎而不是 Prompt 才是事实来源。

## 市场与时间语义

两个商品市场都采用连续、全额担保的限价订单簿：

1. 每档数量和价格都必须是 `0.0001` 的整数倍，且数量必须为正数；任意一档非法都会
   使整个阶梯被拒绝，不做四舍五入。
2. 市场在暂存事务中完成整组对账；未变化档位保持原冻结，待变化旧档只在该事务内
   释放冻结。
3. 全部目标买档合计需要按四位小数、半偶舍入后的 `quantity * limit_price` 足额现金
   担保；全部目标卖档合计
   需要真实 FEFO 库存担保。任一资源不足都会完整恢复原阶梯。
4. Place 或 Replace 档位按目标价格从最优到最差进入撮合器；只要
   `best_bid >= best_ask` 就持续成交。
5. 更优价格优先；同价按每张订单持久化的独立优先序号排序。
6. 成交价取静止在订单簿中的 maker 订单价格。
7. 成交可只消耗订单的一部分，剩余部分保留自己的优先级；Replace 档位获得新订单
   ID 和新优先级。
8. 买方价差资金立即释放；空目标阶梯或 19:00 关市会释放对应的全部未成交冻结资产。

所有持久化经济值共用同一个四位小数精度。部分成交后，引擎会重新计算剩余数量对应的
舍入后冻结额，并按现金守恒推导退款；若订单、成交或作业成本舍入为零，整条命令会
原子回滚。若一笔有效成交留下的余量舍入为零，或在各笔金额独立按四位结算后无法继续
全额担保，引擎只自动撤回该余量并释放对应资产，已经完成的成交不回滚。

每笔成交都让卖方立即收款，并安排买方在 30 个虚拟分钟后自动到货。在到货前，这些
批次只作为待到货可见，不能加工、出售或再次冻结。这不是物流工作流：系统没有发货
命令、路线、承运商、运力、延迟、失败或托管状态。

`produce` 和 `transform` 同样在恰好 30 个虚拟分钟后完成。每家公司同时最多有一项
物理作业，但该作业不会阻塞市场、等待或零售价命令。启动作业时即消耗现金；加工还会
消耗输入库存，产出只在完成时变为可用。

营业窗口为 `[09:00, 19:00)`。命令没有 30 分钟经济冷却；Runtime 限制每家公司每个
虚拟分钟最多决策一次，并设置每日每家公司 25 Turn 的硬上限。19:00 先处理到期作业
和到货，再关市，最后执行消费者购买。此前已承诺的任务最晚可在 19:29 完成，19:30
日结。

`wait` 是注意力计划，不是轮询动作。它最多可声明三个针对 Agent 可见匿名
`best_bid` / `best_ask` 的价格条件，多个条件固定为 OR；也可给出最多 120 分钟后的
当日绝对兜底时间。省略时间时，只要仍早于 19:00，Runtime 就安排 120 分钟后的默认
复查。Alert 为一次性，只在同一分钟全部命令提交后判断，命中则于下一分钟唤醒。重复、
不可见、提交时已经为真的条件，以及非法时间，会使整条命令被拒绝且不改变经济状态。
自有成交、作业完成和到货也会唤醒受影响企业；普通订单簿变化不再广播，挂单没有额外
复查定时器。达到每日上限时，系统写入明确且不改变状态的审计步骤，并停止当日后续
Agent 调用；此后的每个 Wake 仍会连同强类型因果信号写入 Journal。

## 确定性并发

同一分钟被唤醒的所有 Agent 都观察同一个基础状态版本，Provider 请求并发执行，但
完成速度不决定经济优先级。应用命令前，Runtime 按以下键对企业排序：

```text
SHA256(seed | absolute_minute | company_id)
```

随后命令串行提交，全局 `apply_sequence` 被持久化。Provider 延迟、重试和 token
使用量只属于调用审计指标。这使运行可以复现并公平比较，而不会把网络延迟误当作经营
能力。

## 企业私有记忆

每家公司拥有独立的 `ConversationMemory`、模型客户端和 Gateway 生命周期。一次
Provider 请求由四层上下文构成：

1. 当前权威 `AgentTurn` 事实；
2. 由该 Turn 派生的显式 `decision_constraints`；
3. 最近的完整 Turn/Command/Outcome 循环；
4. 更早完整循环的确定性长期摘要。

记忆按 token 预算压缩，而不是固定保留最近七天。压缩不会拆开命令和结果，也不产生
额外模型调用。默认在约 12,288 个估算 token 时开始压缩，完整 Prompt 另有 16,384
token 上限。如果仅当前权威事实就无法装入，请求会明确失败，而不会隐藏业务事实。

记忆用于辅助 Agent 推理，但不是经济事实权威。即使 Prompt 已压缩，完整
`TurnRecord` 仍永久保留在不可变 Journal 中。

## Journal、Checkpoint 与 Replay

Turn Journal 记录精确观察、Runtime 绑定的命令、结果、`apply_sequence`、观察哈希、
因果引用以及协议错误。System Step 记录作业完成、到货、关市、消费者购买和日结及其
经济影响。

每个稳定虚拟时间 bucket 结束后，新 Journal 条目与替换后的 `RunCheckpoint` 在同一
事务中提交。Checkpoint 包含强类型经济状态、订单簿与冻结资产、待完成作业与到货、
Scheduler、企业可用时间、事件、快照、记忆、游标和固定策略指纹。

恢复只能从该原子边界继续，并拒绝 Provider、模型、Prompt、场景或配置漂移。精确
Replay 不创建 Provider Gateway：它校验每次观察哈希，重现记录的命令或协议拒绝，
比较每个结果与 System Step，并最终要求事件、快照和得分完全相同。

## 通过 NewAPI 运行模型 Agent

NewAPI 是唯一模型调用边界。模型目录可以包含 GPT、Claude、Gemini、DeepSeek，或任意
能够通过通用接口返回函数调用的模型家族。

```bat
start.cmd --configure-newapi
start.cmd
```

第一条命令会隐藏输入密钥，通过固定的
`https://newapi.deepwisdom.ai/v1/models` 校验并获取完整模型列表，然后把凭据以当前
Windows 用户的 DPAPI 加密格式保存在 `.dairy-bench/credentials`，同时保存不含密钥
的模型目录。正常启动时，密钥只解密到后端子进程环境；浏览器只能收到模型目录。UI
选中的模型会写入 `RunJob` 和 Policy 审计元数据，恢复运行仍使用同一模型。替换密钥
或刷新模型列表时重新执行配置命令；若后端已运行，配置后需要重启。

每家公司拥有独立的 `NewApiModelGateway` 和 HTTP Client。调用统一进入
`/v1/chat/completions`，不存在模型厂商专用 SDK 或回退路径。模型目录本身不能证明
工具调用兼容性。Gateway 首次要求必选工具调用；若 Provider 明确拒绝 `tool_choice`，
它会记住该能力，并在重试和后续调用中只省略这个字段。若所选模型在固定的 131,072
Token 预算内仍未返回函数调用，系统会先记录调用审计，再以明确的兼容性错误结束运行。

## 失败语义

| 情况 | 结果 |
|---|---|
| 模型命令无效 | 协议拒绝；经济状态不变；可在下一虚拟分钟尝试修正 |
| 所选模型无法返回必选函数调用 | 记录兼容性错误并立即结束运行 |
| 角色、担保、所有权、产能或时间规则失败 | 强类型引擎拒绝；运行继续 |
| 认证或永久 Provider 配置故障 | 整个运行失败，不产生误导性分数 |
| DNS、连接、超时、408、409、429 或重试耗尽的 5xx 故障 | 中断运行并保留 Checkpoint |
| Journal 故障或 Runtime 不变量被破坏 | 当前事务回滚，运行失败 |

## 审计接口

```text
GET /api/run-jobs?limit={limit}&offset={offset}
GET /api/run-jobs/{run_id}
POST /api/run-jobs/{run_id}/stop
POST /api/run-jobs/{run_id}/resume
GET /api/runs/{run_id}/timeline?day={day}
GET /api/runs/{run_id}/timeline/{entry_id}
GET /api/runs/{run_id}/turns
GET /api/runs/{run_id}/invocations
```

`turns` 是权威业务 Journal；`invocations` 审计 Provider 调用、延迟和 token；
`timeline` 是可读的因果投影。
页面的 **All Runs** 包含所有已持久化状态，并可读取它们已提交的时间线；
**Completed Run Replay** 下拉框只列出 completed Run，启动后才执行确定性验证。
`stopped`、`interrupted` 和拥有 Checkpoint 的 `failed` Run 都支持沿用原 run ID 显式恢复；
只有 `interrupted` 会在后端重启时自动恢复。未完成 Run 没有最终分数，也不会进入
Completed Run Replay 来源。

## 模型适配边界

V4 Adapter 实现：

```python
class CommandGateway(Protocol):
    async def generate_command(
        self,
        request: CommandModelRequest,
    ) -> CommandModelResult: ...

    async def close(self) -> None: ...
```

`AgentFactory` 为每家公司创建独立 NewAPI Gateway。Adapter 把输出校验为已授权的
`CompanyCommand`，将内容错误映射为 `ModelOutputError`，将兼容性错误映射为
`ModelCompatibilityError`，将认证等永久配置故障映射为 `ModelConfigurationError`，
并将 DNS、连接、超时、限流或 5xx 等临时故障映射为 `ModelInfrastructureError`。
它不能访问 `EconomyEngine` 或其他公司的状态。支持新模型时应把模型接入 NewAPI，
而不是新增直连 Provider Adapter。
