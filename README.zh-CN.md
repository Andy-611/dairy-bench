# Dairy Bench

Dairy Bench 是一个事件驱动的多 Agent Benchmark，其中四家农场、四家
加工商和四家零售商共同经营一条易腐乳制品供应链。每家公司都由一个独立
Agent 控制。

一个 episode 持续 30 个模拟日。决策单位不是每日计划，而是一个原子公司
Turn：

```text
唤醒 → AgentTurn → 一个 CompanyCommand → 引擎结果 → Journal → 下一次唤醒
```

只有确定性的经济引擎可以改变现金、库存、订单或交易结果。Agent 只能提交
强类型的业务命令。

## 已实现的内容

- 十二家异构公司、原奶和瓶装奶、FEFO 库存以及两个现货市场。
- 每家公司都可以使用独立的规则、Codex、OpenAI 或精确 Replay Agent。
- 虚拟分钟时钟、固定市场清算时间、事件驱动唤醒、同一时刻并发推理以及
  确定性的串行命令提交。
- 每个 Turn 恰好执行一个强类型原子命令。OpenAI 使用原生函数工具；
  Codex 使用等价的严格结构化 Adapter。
- 十二个 Agent 各自拥有独立的 token 预算记忆、模型 Gateway 和模型客户端
  生命周期。
- 不可变的 Turn Journal、原子 checkpoint、崩溃恢复以及无需模型调用的
  Replay。
- 后台执行、30 天进度轮询、明确的失败状态以及完整的 provider 调用审计。
- 确定性经济系统上的效率、公平性、履约率和浪费指标。
- SQLite 持久化、FastAPI 后端以及英文 React 仪表板。
- 以 Turn 为中心的 Operations Replay，将唤醒、观察、命令、结果、经济
  影响、系统步骤和源运行 trace 连接在一起。

```text
React → FastAPI → RunCoordinator → EpisodeRuntime → Scheduler + EconomyEngine
                         │                └── Evaluator
                         ├── PolicyFactory
                         │   ├── BaselineCompanyAgent
                         │   ├── LlmCompanyAgent × 12 → Gateway × 12
                         │   └── ReplayCompanyAgent × 12
                         └── LifecycleRepository → Journal + Checkpoint + SQLite
```

## 环境要求

- Python 3.12+
- Node.js 20.19+
- 只有在使用相应 Agent 时，才需要 Codex 登录或 OpenAI API key

只需安装一次依赖：

```powershell
cd "G:\Project in DeepWisdom\Multi Agent Company Bench\Dairy Bench\backend"
python -m pip install -e ".[dev]"

cd "..\frontend"
npm.cmd install
```

从仓库根目录启动两个应用：

```powershell
cd "G:\Project in DeepWisdom\Multi Agent Company Bench\Dairy Bench"
.\start.cmd
```

启动器会启动 FastAPI 和 Vite，然后打开 `http://127.0.0.1:5173`。
规则基线不需要模型凭据即可运行。

## Codex Agent

`start.cmd` 使用仓库自己的 `.dairy-bench/codex` 目录，不会把 Benchmark
session 写入 `%USERPROFILE%\.codex`。第一次启动时，可能会要求你为这个
隔离的 Codex home 完成 `codex login`。

每家公司都会获得一个独立的 Codex Runtime，每个公司 Turn 都使用一个隔离
thread。上下文由确定性摘要和固定 token 预算下的近期完整
Turn/Command/Outcome 循环组成。

每一份可读的公开推理摘要和最终结构化输出都会导出到：

```text
run_artifacts/<run_id>/
├── reasoning/
└── final_outputs/
```

Replay 期间仍然可以审计源 trace。它们会被标记为源运行用量，绝不会被计为
Replay 自身发起的模型调用。

## OpenAI Agent

在启动后端的同一个 PowerShell session 中设置 API key：

```powershell
$env:OPENAI_API_KEY="your-key"
$env:DAIRY_BENCH_OPENAI_MODEL="gpt-5.6-terra"  # 可选

python -m uvicorn company_bench.web:create_app --factory --host 127.0.0.1 --port 8000
```

API key 只由后端读取。它绝不会发送到浏览器，也不会存入 Benchmark 数据库。

模型调用量取决于唤醒次数和每个 Agent 的 `wait` 决策，同时受到配置中每家
公司每日 Turn 上限的约束。无效的模型输出会成为明确的协议拒绝，不会改变
经济状态；在正常命令时长后，系统会通过 `CONTINUE` 再次唤醒 Agent，让它
可以修正输出。认证失败、网络故障或 provider 故障在重试耗尽后会使运行
失败，而不是产生误导性的分数。

## 数据与验证

默认数据库为：

```text
backend/data/dairy_bench.sqlite3
```

可以使用 `DAIRY_BENCH_DB` 覆盖数据库路径。可以使用
`DAIRY_BENCH_ARTIFACTS_DIR` 覆盖产物目录。

运行验证：

```powershell
cd "G:\Project in DeepWisdom\Multi Agent Company Bench\Dairy Bench\backend"
python -m pytest -q
python -m ruff check .

cd "..\frontend"
npm.cmd run build
```

## 主要 HTTP 接口

- `GET /api/policy-profiles` —— 列出可用的策略模式和模型配置。
- `POST /api/runs` —— 创建后台运行并返回 `202 RunJob`。
- `GET /api/run-jobs/{run_id}` —— 读取生命周期状态和天数进度。
- `GET /api/runs/{run_id}` —— 读取一个已完成的 episode。
- `GET /api/runs/{run_id}/timeline?day={day}` —— 读取一天的
  Operations Replay。
- `GET /api/runs/{run_id}/timeline/{entry_id}` —— 读取一个详细的 Turn
  或系统步骤条目。
- `GET /api/runs/{run_id}/turns` —— 读取不可变的原始 Turn Journal。
- `GET /api/runs/{run_id}/invocations` —— 读取 provider 调用审计。
- `GET /api/runs/{run_id}/invocations/{invocation_id}/artifacts` ——
  读取导出的 Codex 公开推理摘要和最终输出。
- `GET /api/runs` —— 列出已完成的运行。

## 设计文档

- [Agent 接入](docs/AGENT_INTEGRATION.md)
- [V2 架构与不变量](docs/V2_ARCHITECTURE.md)
- [历史 V1 MVP 框架](docs/MVP_FRAMEWORK.md)
- [历史 V1 场景目录](docs/SCENARIO_CATALOG_V1.md)
