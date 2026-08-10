# Dairy Bench

Dairy Bench 是一个事件驱动的多 Agent 易腐乳制品供应链 Benchmark。三家牧场、三家
加工厂和三家零售商共享两个现货市场，每家企业由独立 Agent 控制。

默认场景为 `flow.dairy.base.s9.v5`，每个 Episode 持续 30 个模拟日。所有决策都经过
同一条强类型边界：

```text
唤醒 -> AgentTurn -> 一条 CompanyCommand -> EconomyEngine -> Journal -> 下次唤醒
```

只有 `EconomyEngine` 可以修改现金、库存、订单、作业、交付或成交结果；模型文本不能
直接促成经济结算。

## 策略模式

Dairy Bench 只保留三种模式：

- **Rule baseline**：确定性规则，不调用模型。
- **Model agents via NewAPI**：每家公司拥有独立 Agent 和 NewAPI HTTP Client。模型
  目录可以包含任意家族，但所选模型必须能通过 NewAPI 通用 Chat Completions 接口返回
  函数调用。
- **Completed Run Replay**：不调用模型，重放已完成 Run 的 Turn Journal，并检查观察与结果漂移。

项目不再保留任何模型厂商直连接口；所有模型运行统一通过唯一的 NewAPI Adapter，
并校验为同一个 Pydantic 命令联合类型。

## V4 概览

- 原奶和盒装奶采用全额担保的连续限价订单簿。
- 报价阶梯最多三档，整组原子更新，并遵守价格—时间优先级。
- FEFO 库存冻结和统一的 `0.0001` 经济精度。
- 同一分钟并发模型推理，随后按持久化的确定性顺序串行应用命令。
- 生产、加工和到货均为 30 个虚拟分钟的事件。
- 每家企业拥有私有记忆；Journal、Checkpoint、恢复与确定性重放保持权威性。

每日时间表：

| 时间 | 事件 |
|---|---|
| 09:00 | 市场开市并唤醒全部企业 |
| 09:00–18:59 | 接受企业决策并连续撮合 |
| 19:00 | 完成到期事项、关闭订单簿，再执行消费者购买 |
| 19:00–19:29 | 处理此前已承诺的作业完成和到货 |
| 19:30 | 处理库存过期并提交日终快照 |

完整契约见 [V4 架构与不变量](docs/ARCHITECTURE.zh-CN.md) 和
[Agent 接入](docs/AGENT_INTEGRATION.zh-CN.md)。

## 安装与启动

要求：

- Python 3.12+
- Node.js 20.19+
- 只有模型运行需要 NewAPI key

首次安装：

```powershell
cd backend
python -m pip install -e ".[dev]"

cd "..\frontend"
npm.cmd install
cd ..
```

在仓库根目录配置或替换 NewAPI key：

```bat
start.cmd --configure-newapi
```

该命令会通过 `/v1/models` 校验 key，将凭据按当前 Windows 用户加密保存，并把完整的
非敏感模型目录写入 `.dairy-bench/credentials/`。更换 key 或刷新模型列表时重新执行
即可；该操作会使旧路由验证得到的模型能力失效。若后端已运行，配置后需要重启后端。

启动项目：

```bat
start.cmd
```

启动器会打开 `http://127.0.0.1:5173`。`start.cmd --check` 只检查本地依赖和 NewAPI
配置，不启动服务。

UI 的模型下拉框来自 NewAPI 模型目录。由于 `/v1/models` 本身不能证明工具调用兼容性，
系统仍要求模型恰好返回一个已授权函数调用。Adapter 优先发送 `tool_choice="required"`；
只有 Provider 明确拒绝该参数时（例如部分 thinking 模型）才会省略它重试。模型第一次
提交 Run 前，系统会用一条短请求确认文档候选上限，或从 NewAPI 的明确参数拒绝中提取
准确上限，并写入本地版本化能力目录。运行时直接把确认值作为 `max_tokens`，不做阶梯式
增长。系统不会回退到自由文本或另一个 Provider。

解密后的 key 只存在于后端进程环境，不进入浏览器、Journal 或 SQLite 数据库。

## Run 与数据

当前 Run 和日期保存在 `?run=...&day=...`。**All Runs** 包含所有持久化状态及其已提交
时间线；**Completed Run Replay** 只选择 completed 来源并继承其 seed。stopped、
interrupted，以及拥有 Checkpoint 的 failed Run 都会沿用原 run ID 断点续跑；只有
interrupted 会在后端重启时自动恢复。未完成 Run 不生成最终分数，也不能成为 Replay 来源。

所有可变状态位于 Git 忽略的 `.dairy-bench/`：

```text
.dairy-bench/
|-- credentials/
|   |-- newapi-model-capabilities.json
|   `-- newapi-models.json
`-- data/runs.sqlite3
```

仅在必要时使用 `DAIRY_BENCH_HOME` 覆盖根目录。

## 验证

```powershell
cd backend
python -m pytest -q
python -m ruff check .

cd "..\frontend"
npm.cmd run build
```

## 主要 HTTP 接口

- `GET /api/policy-profiles`
- `POST /api/runs`
- `GET /api/run-jobs`
- `GET /api/run-jobs/{run_id}`
- `GET /api/runs/{run_id}`
- `GET /api/runs/{run_id}/timeline?day={day}`
- `GET /api/runs/{run_id}/timeline/{entry_id}`
- `GET /api/runs/{run_id}/turns`
- `GET /api/runs/{run_id}/invocations`
