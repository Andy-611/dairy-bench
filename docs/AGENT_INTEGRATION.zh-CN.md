# Agent 接入

## V3 Agent 契约

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
| `place_order(side, product, quantity, limit_price)` | 提交全额担保的限价单 |
| `replace_order(order_id, quantity, limit_price)` | 原子替换自有订单，并失去原时间优先级 |
| `cancel_order(order_id)` | 撤销自有挂单并释放剩余冻结资产 |
| `set_retail_price(product, unit_price)` | 设置零售消费者价格 |
| `wait(until?)` | 等待指定时刻或下一个相关事件 |

牧场可以生产和交易原奶；加工厂可以转换并交易原奶或盒装奶；零售商可以交易盒装奶
并设置消费者价格。

`place_order` 和 `replace_order` 的 `quantity` 必须为正数，并且是 `0.0001` 的整数倍
（最多四位小数）。引擎会整条拒绝非法命令而不是四舍五入；没有有意义的数量时，Agent
应选择 `wait`，不要提交尘埃订单。

OpenAI 使用一次必选、不可并行的 Responses API 函数工具调用；Codex 使用严格的
结构化输出 envelope。两者都按照同一个带判别字段的 Pydantic `CompanyCommand`
联合类型校验。缺失、多条、未知、未授权或参数错误的调用会成为明确的协议拒绝，且不
修改经济状态。

## Agent 可以看到什么

`AgentTurn` 只包含 Runtime 授权给当前企业的事实：

- 模拟时间、状态版本、当日 Turn 序号与上限、强类型唤醒原因及因果引用；
- 可用现金和库存，以及适用时的零售价；
- 自有挂单冻结的现金和 FEFO 库存；
- 自有未完成订单，包括剩余数量和优先序号；
- 与自身业务有关的匿名 `MarketView`：最优买卖价、买卖各前三档聚合深度、最近
  成交价和当日成交量；
- 已保证的待到货商品、数量、准确到货时间及数量守恒的到期日分桶；
- 当前生产或加工任务，以及当日剩余作业产能；
- 企业可见领域事件和上一条命令结果；
- 当前企业独立且有预算上限的记忆上下文。

Agent 看不到公开订单簿中其他企业的身份，也看不到其他企业的私有资产、记忆、Prompt
或 Provider trace。引擎而不是 Prompt 才是事实来源。

## 市场与时间语义

两个商品市场都采用连续、全额担保的限价订单簿：

1. 订单数量必须为正数且是 `0.0001` 的整数倍；精度非法的挂单或改单整条拒绝，不做
   四舍五入。
2. 买单冻结 `quantity * limit_price`，卖单冻结真实 FEFO 批次。
3. 可用现金或库存不足时，整张新订单被拒绝。
4. 只要 `best_bid >= best_ask`，新订单立即交叉撮合。
5. 更优价格优先；同价按持久化的到达优先级排序。
6. 成交价取静止在订单簿中的 maker 订单价格。
7. 成交可只消耗订单的一部分。剩余部分保留优先级；显式 Replace 会获得新订单 ID
   和新优先级。
8. 买方价差资金立即释放；撤单或 19:00 关市会释放全部未成交冻结资产。

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
Provider 请求由三层上下文构成：

1. 当前权威 `AgentTurn` 事实；
2. 最近的完整 Turn/Command/Outcome 循环；
3. 更早完整循环的确定性长期摘要。

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

## 运行 Codex Agent

从仓库根目录运行：

```powershell
.\start.cmd
```

启动器设置 `CODEX_HOME=.dairy-bench/codex`，与个人
`%USERPROFILE%\.codex` 隔离。九家公司各有独立的 `AsyncCodex` Runtime，每个
Turn 使用隔离 thread。Benchmark Runtime 为只读模式，并关闭 shell、搜索、插件、
Codex memory 和多 Agent 功能。

每个 Turn 完成后，Dairy Bench 导出公开推理摘要和最终结构化输出；仅在导出成功后
归档源 session：

```text
run_artifacts/<run_id>/
|-- reasoning/day-001__farm_a__turn-0001.md
`-- final_outputs/day-001__farm_a__turn-0001.json
```

项目不会读取或存储 `auth.json`、隐藏思维链或加密推理内容。

## 运行 OpenAI Agent

```powershell
cd backend
$env:OPENAI_API_KEY="your OpenAI API key"

# 可选
$env:DAIRY_BENCH_OPENAI_MODEL="gpt-5.6-terra"
$env:DAIRY_BENCH_OPENAI_REASONING_EFFORT="medium"
$env:DAIRY_BENCH_OPENAI_MAX_OUTPUT_TOKENS="2048"
$env:DAIRY_BENCH_OPENAI_TIMEOUT_SECONDS="60"
$env:DAIRY_BENCH_OPENAI_MAX_ATTEMPTS="3"

python -m uvicorn company_bench.web:create_app --factory --host 127.0.0.1 --port 8000
```

只有后端读取 `OPENAI_API_KEY`；密钥不会进入浏览器、Journal 或数据库。可用
`DAIRY_BENCH_OPENAI_BASE_URL` 指向兼容服务。

## 失败语义

| 情况 | 结果 |
|---|---|
| 模型命令缺失或无效 | 协议拒绝；经济状态不变；可在下一虚拟分钟尝试修正 |
| 角色、担保、所有权、产能或时间规则失败 | 强类型引擎拒绝；运行继续 |
| 认证失败或重试耗尽的 Provider 故障 | 整个运行失败，不产生误导性分数 |
| Journal 故障或 Runtime 不变量被破坏 | 当前事务回滚，运行失败 |

## 审计接口

```text
GET /api/run-jobs
GET /api/run-jobs/{run_id}
GET /api/runs/{run_id}/timeline?day={day}
GET /api/runs/{run_id}/timeline/{entry_id}
GET /api/runs/{run_id}/turns
GET /api/runs/{run_id}/invocations
GET /api/runs/{run_id}/invocations/{invocation_id}/artifacts
```

`turns` 是权威业务 Journal；`invocations` 审计 Provider 调用、延迟和 token；
`timeline` 是可读的因果投影；导出的 `artifacts` 只是源证据，不会触发模型调用。
Run History 对所有生命周期状态开放；即使没有最终 Episode 或得分，也不会隐藏已经提交
的 Journal、错误和 Checkpoint。Exact Replay 仍是独立的 completed Run 确定性验证。

## 添加其他 Provider

V3 Adapter 实现：

```python
class CommandGateway(Protocol):
    async def generate_command(
        self,
        request: CommandModelRequest,
    ) -> CommandModelResult: ...

    async def close(self) -> None: ...
```

`PolicyFactory` 为每家公司创建独立 Gateway。Adapter 把输出校验为已授权的
`CompanyCommand`，将内容错误映射为 `ModelOutputError`，将基础设施错误映射为
`ModelInfrastructureError`。它不能访问 `EconomyEngine` 或其他公司的状态。
