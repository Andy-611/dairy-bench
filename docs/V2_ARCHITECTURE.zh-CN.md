# Dairy Bench V2 架构

V2 用一个事件驱动的公司闭环 Turn 取代了每日公司计划：

```text
唤醒 → AgentTurn → 一个 CompanyCommand → CommandOutcome → TurnRecord
```

一天仍然定义市场开市、两次清算、消费者销售、过期和评分，但不再定义
Agent 可以思考多少次。

## 深模块

### `EpisodeRuntime`

Runtime 只暴露一个主要操作 `run()`，同时负责完整的模拟协议：

- 推进一个单调递增的虚拟分钟时钟；
- 在同一分钟内，先于公司 Turn 执行系统事件；
- 为每家公司维护独立的 `available_at`；
- 延迟并合并公司忙碌期间到达的唤醒；
- 基于同一个基础 `state_version` 构建同一分钟的所有观察；
- 并发询问各公司 Agent；
- 按照由 seed 决定的确定性顺序提交命令；
- 原子地追加 Journal 并替换 checkpoint；以及
- 评估已经完成的 episode。

### `EconomyEngine`

引擎是现金、库存、订单、生产、交易和零售结果的唯一写入者。Agent 和
模型 provider 可以提出命令，但不能直接调用状态修改方法。

`EconomyState` 是可序列化的日内状态，包含公司账户、长期有效订单、已用
产能、零售价格、市场状态和当日事件。V1 的 `step()` 路径仍然保留，但只
用于读取和验证遗留场景。

### `ConversationMemory`

每家公司都拥有一个独立的记忆实例：

- 当前经营事实从 `EconomyState` 中实时投影；
- 近期历史保留完整的 Turn/Command/Outcome 循环；
- 容量由 token 预算控制，而不是由固定天数控制；
- 旧循环会变成携带 `source_hash` 的确定性摘要；
- 完整的 provider 请求具有独立的预估 prompt-token 上限；
- 摘要帮助 Agent 推理，但绝不会成为权威经济状态。

### `RunTimelineProjector`

Projector 是 Operations Timeline 背后的读取侧模块。它把权威 Turn Journal、
系统步骤、provider 审计、Replay 血缘和导出的公开产物连接成一条强类型
时间线。浏览器不会重建因果关系，也不会根据时间戳推断模型调用。

Projector 还会将源运行和 Replay 的核算分开：Replay 不会调用 provider，
而源运行的 trace 和 token 使用量仍然作为来源信息保留。

## 时间协议

默认的 `flow.dairy.base.s12.v2` 场景安排如下：

| 时间 | 系统步骤 |
|---|---|
| 09:00 | 开市并唤醒所有公司 |
| 11:00 | 清算原奶市场 |
| 16:00 | 清算瓶装奶市场 |
| 19:00 | 执行消费者销售、过期处理和日终快照 |

一条普通命令会占用公司 30 个虚拟分钟。系统不会轮询 `wait`。公司只会在
明确指定的截止时间、下一次开市或其他实质性系统事件发生时被唤醒。每家公司
每天最多拥有 20 个 Turn。

每家公司只有一条有效的未来行动链。在冷却期间收到的外部唤醒会被移动到
`available_at`，并与已有唤醒合并。当另一个事件更早唤醒公司时，之前的
wait 定时器或 continuation 定时器会被取消。

在同一个虚拟分钟 bucket 内：

1. 首先执行系统步骤；
2. 合并同一家公司的多个唤醒原因；
3. 每个 Agent 都观察同一个基础状态版本；
4. 并发执行模型查询；以及
5. 按稳定、可重放的顺序提交命令。

因此，provider 的响应速度不会改变经济结果。

## 命令协议

每个 Turn 只允许一个经过角色授权的原子命令：

- `Produce`
- `Transform`
- `PlaceOrder`
- `CancelOrder`
- `SetRetailPrice`
- `Wait`

Runtime 负责绑定 `turn_id`、`company_id`、`sim_time` 和
`state_version`。模型不能提供或伪造这些字段。

OpenAI Adapter 使用 provider 原生函数工具，并设置：

```text
tool_choice = required
parallel_tool_calls = false
max_tool_calls = 1
```

Codex Adapter 在一个严格的结构化 envelope 中使用同一个 Pydantic 命令
联合类型。两条路径都只产生一个 `CompanyCommand`。没有工具调用、存在多个
调用、工具未知或参数无效都会成为明确的协议拒绝；它们绝不会被静默转换成
一次成功的 `wait`。协议拒绝不会改变经济状态，但会消耗正常的 Turn 时长，
并安排 `CONTINUE`，让 Agent 可以修正输出；该行为仍受每日 Turn 上限和
营业时间边界约束。

## Journal、checkpoint 和 Replay

V2 Turn Journal 记录：

- 完整的 `AgentTurn` 观察；
- 由 Runtime 绑定的 `CommandEnvelope`；
- 引擎产生的 `CommandOutcome`；
- 观察哈希；
- 基础状态版本和结果状态版本；
- 全局应用顺序；以及
- 没有生成有效命令时的原始协议错误。

带版本的 `RunCheckpoint` 包含：

- `EconomyState`；
- `SchedulerCheckpoint`；
- 每家公司的 `PolicyDescriptor` 和 `AgentCheckpoint`；
- 已提交的 Turn、事件和快照；
- 公司 Turn/Event 游标和 `available_at` 值；以及
- episode 最初的 `started_at`。

新的 Journal 记录和替换后的 checkpoint 在同一个 Repository 事务中
提交。因此，恢复操作会把世界、时钟和 Agent 记忆恢复到同一个原子边界。
恢复前，Runtime 会比较 provider、模型、prompt 版本和配置指纹，防止同一个
episode 在没有提示的情况下混用不同策略。

Replay Agent 按顺序消费源 Turn Journal。它会验证当前观察哈希，返回源命令
或源协议拒绝，并比较每一个结果。评分前，它会验证源数据流已经耗尽，而且最终
事件、快照和分数完全相同。它不会创建模型 Gateway，也不会调用 provider。
任何经济、可见性或调度漂移都会立即导致失败。

## Operations Timeline

Operations Timeline 以 Turn 为中心，而不是以事件为中心。它把一天组织成多个
虚拟分钟时刻，并展示：

- 该分钟发生的系统步骤；
- 合并后的唤醒原因；
- 基于同一基础版本的并发观察；
- 确定性的命令应用顺序；
- 强类型的接受或拒绝结果；
- 直接经济影响以及下一次计划唤醒；以及
- provider trace 或源运行 Replay 来源信息。

命令的直接影响嵌套在对应 Turn 下面。市场清算、消费者销售、过期和日终状态
变化显示为系统步骤。这样可以避免把同一个经济行为展示两次。

默认展示所有没有效果的 wait，并保留完整审计记录；公司、状态和命令筛选仍然生效。
条目详情采用延迟获取，因此
30 天运行不需要预先加载所有 trace payload。

## 信息边界

公司观察只包含：

- 自己的现金、聚合库存和零售价格；
- 自己的长期有效订单；
- 公开的场景规则和上一天的市场摘要；
- 自上一个 Turn 之后该公司可见的事件；以及
- 自己的上一次结果和私有记忆。

私有事件只返回给受影响的公司。交易只对买方和卖方可见。其他公司的现金、
库存、命令、provider transcript 和记忆绝不会进入当前 Agent 的输入。

## 必须满足的不变量

- 模拟时间单调递增；冷却不能把事件安排到过去。
- 一家公司在给定时刻最多有一个尚未完成的 Turn。
- 一个 Turn 最多应用一个命令。
- 同一个时间 bucket 中的所有命令共享同一个基础 `state_version`。
- 只有 `EconomyEngine` 可以修改经济状态。
- 相同 seed 和命令流产生完全相同的事件、快照和分数。
- Replay 不调用任何 provider。
- checkpoint 恢复结果与不中断执行一致。
- 记忆压缩绝不会拆开一个 Command/Outcome 循环。
- 一家公司不能观察另一家公司的私有事实。
- 时间线投影绝不会调用模型或修改模拟。

Scheduler、Runtime、Memory、Repository、Projector 和端到端测试共同强制
执行这些边界。
