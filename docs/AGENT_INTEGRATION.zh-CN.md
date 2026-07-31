# Agent 接入

## V2 Agent 契约

Dairy Bench 运行十二家公司，并为每家公司提供一个独立 Agent。Agent 不再提交
一份完整的每日计划。每当 Scheduler 唤醒它时，它都会完成一个闭环：

```text
唤醒
  → AgentTurn（当前事实、可见事件、私有记忆）
  → 一个原子 CompanyCommand
  → EconomyEngine 验证和 CommandOutcome
  → 不可变的 TurnRecord
```

一天限定市场开市、清算、消费者销售、过期和评分，但不限定 provider 调用。

默认系统时间表如下：

| 时间 | 事件 |
|---|---|
| 09:00 | 开市并唤醒所有公司 |
| 11:00 | 清算原奶市场 |
| 16:00 | 清算瓶装奶市场 |
| 19:00 | 执行消费者销售、过期处理和日终快照 |

一条普通命令会占用公司 30 个虚拟分钟。在冷却期间收到的唤醒会被延迟到
`available_at` 并合并，从而为每家公司保留一条行动链。默认的每日上限是
每家公司 20 个 Turn。

## 模型提交格式

模型必须选择一个且仅一个经过其公司角色授权的命令：

- `produce`
- `transform`
- `place_order`
- `cancel_order`
- `set_retail_price`
- `wait`

由 Runtime 而不是模型绑定 `turn_id`、`company_id`、模拟时间和
`state_version`。

OpenAI Adapter 使用 Responses API 函数工具，并设置：

```text
tool_choice = required
parallel_tool_calls = false
max_tool_calls = 1
```

Codex Adapter 使用由同一个 Pydantic 命令联合类型支持的严格结构化输出
envelope。两条 provider 路径都会规范化为 `CompanyCommand`。缺少调用、
存在多个调用、工具未知或参数无效都会成为明确的协议拒绝，并且不会修改
经济状态。协议拒绝会消耗一个 Turn 和正常的 30 分钟时长，随后安排
`CONTINUE`，让 Agent 可以看到错误并提交修正。

## Agent 记忆

每家公司都拥有独立的 `ConversationMemory`。它的请求上下文分为三层：

1. 从 `EconomyState` 投影得到的当前现金、库存、零售价格和长期有效订单；
2. 自上一个 Turn 以来公司可见的事件，以及上一次结果；
3. 近期完整的 Turn/Command/Outcome 循环和确定性的长期摘要。

旧循环按照预估 token 数量压缩，绝不会把一条命令和它的结果拆开。默认记忆
预算在大约 12,288 tokens 时开始压缩，而完整请求具有独立的 16,384
预估上限。如果仅当前权威事实就超过上限，该 Turn 会明确失败，而不是静默
删除业务事实。

摘要不需要额外的模型调用，也不是经济事实来源。完整的 `TurnRecord` 对象会
永久保留在 Journal 中；provider thread 不是 Benchmark 的记忆权威。

## Journal、恢复和 Replay

Turn Journal 存储：

- 完整的 Agent 观察；
- 由 Runtime 绑定的命令；
- 引擎结果和全局 `apply_sequence`；
- 观察哈希；以及
- 任何明确的协议错误。

每个稳定的时间 bucket 结束后，新的 Journal 记录和替换后的
`RunCheckpoint` 会在同一个事务中提交。checkpoint 包含经济状态、
Scheduler、待处理事件、公司可用时间、固定策略元数据、Agent 记忆、游标、
事件、快照和 episode 最初的开始时间。

恢复操作从该原子边界继续执行。它会拒绝 provider、模型、prompt 或配置指纹
漂移，而不是在同一个运行中混用不同策略。

Replay 会验证每一个观察哈希，重现源命令或源协议拒绝，并比较每一个结果。
随后，它会验证源数据流已经耗尽，而且最终事件、快照和分数完全相等。Replay
不会创建 provider Gateway，模型调用次数为零。

`source_run_id` 会保留在策略元数据和时间线来源信息中。Operations Replay
UI 可以展示导出的源 trace 和源 token 使用量，但会把它们标记为源证据，
而不是 Replay 活动。

## 运行 Codex Agent

从仓库根目录运行：

```powershell
cd "G:\Project in DeepWisdom\Multi Agent Company Bench\Dairy Bench"
.\start.cmd
```

启动器设置 `CODEX_HOME=.dairy-bench/codex`，与用户个人的
`%USERPROFILE%\.codex` 隔离。第一次启动时可能会要求单独登录。后端验证会
拒绝直接使用个人 Codex home。

十二家公司都会获得独立的 `AsyncCodex` Runtime，而且每个 Turn 都使用一个
隔离 thread。Runtime 为只读模式，使用 `deny_all` 审批，并禁用 shell、
搜索、插件、Codex memory 和多 Agent 功能。

一个 Turn 完成后，Dairy Bench 会：

1. 从 `CODEX_HOME/sessions` 读取原始 session JSONL；
2. 导出公开推理摘要和最终结构化输出；以及
3. 仅在成功导出后归档 session。

```text
run_artifacts/<run_id>/
├── reasoning/day-001__farm_a__turn-0001.md
└── final_outputs/day-001__farm_a__turn-0001.json
```

项目绝不会读取、复制或存储 `auth.json`。它不会暴露隐藏的思维链，也不会
解密 `encrypted_content`。

归档的源 session 默认保留 60 天，同时至少保留最近三次运行。清理操作只会
删除满足以下条件的 session：位于隔离归档内、具有严格的 Dairy Bench 标题、
超过 `DAIRY_BENCH_CODEX_SESSION_RETENTION_DAYS`，并且不在
`DAIRY_BENCH_CODEX_SESSION_MIN_RUNS` 的保护范围内。导出的
`run_artifacts` 是长期审计记录，不受源 session 保留规则约束。

## 运行 OpenAI Agent

```powershell
cd "G:\Project in DeepWisdom\Multi Agent Company Bench\Dairy Bench\backend"
$env:OPENAI_API_KEY="your OpenAI API key"

# 可选
$env:DAIRY_BENCH_OPENAI_MODEL="gpt-5.6-terra"
$env:DAIRY_BENCH_OPENAI_REASONING_EFFORT="medium"
$env:DAIRY_BENCH_OPENAI_MAX_OUTPUT_TOKENS="2048"
$env:DAIRY_BENCH_OPENAI_TIMEOUT_SECONDS="60"
$env:DAIRY_BENCH_OPENAI_MAX_ATTEMPTS="3"

python -m uvicorn company_bench.web:create_app --factory --host 127.0.0.1 --port 8000
```

只有后端会读取 `OPENAI_API_KEY`；它绝不会进入浏览器、Journal 或数据库。
可以使用 `DAIRY_BENCH_OPENAI_BASE_URL` 配置与 OpenAI 兼容的服务。

## Runtime 和失败语义

```text
React
  → FastAPI / RunCoordinator
  → EpisodeRuntime
  → 并发执行 CompanyAgent.act()
  → 按确定性顺序执行 EconomyEngine.apply_batch()
  → Turn Journal + Checkpoint
  → Evaluator
```

| 情况 | 结果 |
|---|---|
| 模型命令缺失或无效 | 协议拒绝；经济状态不变；正常命令时长后触发 `CONTINUE` |
| 角色、现金、库存或状态规则验证失败 | 强类型引擎拒绝；运行继续 |
| 认证失败、重试耗尽的网络故障、限流或 provider 中断 | 整个运行失败；不生成分数 |
| Journal 故障或 Runtime 不变量遭到破坏 | 当前事务回滚，运行失败 |

同一分钟的所有 Agent 都观察同一个基础 `state_version`。它们的请求可以按
任意顺序完成，但命令会按照由 seed 决定的稳定顺序应用。

## 审计接口

```text
GET /api/runs/{run_id}/timeline?day={day}
GET /api/runs/{run_id}/timeline/{entry_id}
GET /api/runs/{run_id}/turns
GET /api/runs/{run_id}/invocations
GET /api/runs/{run_id}/invocations/{invocation_id}/artifacts
```

`turns` 是权威业务 Journal。`invocations` 审计 provider 调用、token 和
延迟。`timeline` 投影具有因果关系且便于人类阅读的时刻。`artifacts` 返回
之前导出的 Codex 公开文件，绝不会调用模型。

`invocation_id` 标识一次真实的 provider 调用；`domain_turn_id` 标识一个
确定性的经济 Turn。如果进程在收到 provider 响应之后、提交 checkpoint
之前停止，恢复操作会创建新的 invocation ID，同时保留先前的调用及其审计
数据。

## 添加其他 provider

V2 Adapter 需要实现：

```python
class CommandGateway(Protocol):
    async def generate_command(
        self,
        request: CommandModelRequest,
    ) -> CommandModelResult: ...

    async def close(self) -> None: ...
```

`PolicyFactory` 会为每家公司创建一个新的 Gateway。Adapter 将 provider
输出验证为经过授权的 `CompanyCommand`，把内容错误映射为
`ModelOutputError`，并把网络、认证和服务故障映射为
`ModelInfrastructureError`。它不能访问 `EconomyEngine` 或其他公司的
状态。

## 验证

测试不需要访问真实模型：

```powershell
cd "G:\Project in DeepWisdom\Multi Agent Company Bench\Dairy Bench\backend"
python -m pytest --basetemp "..\.tmp\pytest"
python -m ruff check .

cd "..\frontend"
npm.cmd run build
```

测试覆盖范围包括：唤醒合并、冷却、单工具协议、协议拒绝 Replay、provider
完成顺序无关性、信息隔离、SQLite 原子写入、精确 checkpoint 恢复、零调用
Replay、时间线来源信息以及强类型 API 解析。
