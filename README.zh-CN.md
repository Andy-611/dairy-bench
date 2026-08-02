# Dairy Bench

Dairy Bench 是一个事件驱动的多 Agent 易腐乳制品供应链 Benchmark。三家牧场、
三家加工厂和三家零售商共享两个现货市场，九家企业各由一个独立 Agent 控制。

默认场景为 `flow.dairy.base.s9.v3`。一个 episode 持续 30 个模拟日，每次决策只
提交一条强类型原子命令：

```text
唤醒 -> AgentTurn -> 一条 CompanyCommand -> EconomyEngine -> Journal -> 下次唤醒
```

只有确定性的经济引擎可以修改现金、库存、订单、作业、在途交付和交易结果。自然语言
永远不能直接促成经济结算。

## V3 概览

- 原奶和盒装奶采用全额担保的连续限价订单簿。订单交叉时立即按价格优先、同价时间
  优先撮合，成交价取 maker 订单价格，并支持部分成交；Agent 可以挂单、改单和撤单。
- 买单冻结按限价计算的全部现金，卖单冻结真实的 FEFO 库存批次。资产不足时整单
  拒绝，不产生虚假流动性。
- 订单数量采用固定的 `0.0001` 市场步长；非正数、尘埃数量或超过四位小数的挂单、
  改单请求会整单拒绝，不做静默四舍五入。
- 同一分钟的 Agent 请求并发执行，命令随后按照持久化的
  `SHA256(seed | minute | company)` 顺序串行提交。Provider 响应延迟只用于审计，
  不影响经济结果。
- `produce` 和 `transform` 独占企业的物理资源 30 个虚拟分钟；市场命令和零售价
  命令没有经济冷却，但每家企业每个虚拟分钟最多决策一次。
- 成交后卖方立即收款，买方在 30 分钟后自动收到货物；不引入手动发货、路线、承运商
  或托管工作流。
- Agent 可看到匿名盘口、自有订单、可用及冻结资产、待到货、当前作业和当日剩余产能。
- 每家企业拥有独立的 token 预算记忆；不可变 Journal、原子 Checkpoint、崩溃恢复和
  精确 Replay 是长期事实权威。

每日时间表：

| 时间 | 事件 |
|---|---|
| 09:00 | 两个市场开市并唤醒全部企业 |
| 09:00-18:59 | 接受企业决策并连续撮合订单 |
| 19:00 | 先完成到期作业和到货，再关闭订单簿、释放 DAY 订单冻结，最后执行消费者购买 |
| 19:00-19:29 | 只处理此前已经承诺的作业完成和到货 |
| 19:30 | 处理库存过期并提交日终快照 |

完整契约见 [V3 架构与不变量](docs/V3_ARCHITECTURE.zh-CN.md) 和
[Agent 接入](docs/AGENT_INTEGRATION.zh-CN.md)。

## 系统概览

```text
React -> FastAPI -> RunCoordinator -> EpisodeRuntime -> Scheduler + EconomyEngine
                         |                 `-> Evaluator
                         |-> PolicyFactory -> CompanyAgent x 9
                         `-> LifecycleRepository -> Journal + Checkpoint + SQLite
```

Agent 可使用规则基线、Codex、OpenAI 或精确 Replay。OpenAI 使用原生函数工具，
Codex 使用等价的严格结构化输出 Adapter；所有 Provider 最终都校验为同一个 Pydantic
命令联合类型。

## 环境与启动

- Python 3.12+
- Node.js 20.19+
- 仅在使用对应 Agent 时需要 Codex 登录或 OpenAI API key

从仓库根目录安装一次依赖：

```powershell
cd backend
python -m pip install -e ".[dev]"

cd "..\frontend"
npm.cmd install
cd ..
```

启动 FastAPI 和 Vite：

```powershell
.\start.cmd
```

启动器会打开 `http://127.0.0.1:5173`。规则基线不需要模型凭据。

`Run History` 可直接打开 completed、failed、interrupted 或仍在运行的持久化 Run，
查看已有轨迹、错误和最后 Checkpoint。当前 Run 与天数写入 `?run=...&day=...`，刷新、
前进和后退都会保留页面状态。`Exact Replay` 是独立功能：它只对 completed Run
无模型调用地重新执行，并验证结果是否精确一致。

## 模型 Agent

`start.cmd` 使用仓库自己的 `.dairy-bench/codex`，不会写入
`%USERPROFILE%\.codex`。每家公司拥有独立 Runtime 和私有记忆；每个 Codex Turn
使用隔离 thread。公开推理摘要和最终结构化输出导出到：

```text
run_artifacts/<run_id>/
|-- reasoning/
`-- final_outputs/
```

使用 OpenAI Agent 时，在启动后端的同一个 PowerShell 中设置：

```powershell
$env:OPENAI_API_KEY="your-key"
$env:DAIRY_BENCH_OPENAI_MODEL="gpt-5.6-terra"  # 可选

python -m uvicorn company_bench.web:create_app --factory --host 127.0.0.1 --port 8000
```

密钥不会进入浏览器、Journal 或 Benchmark 数据库。Provider 基础设施故障会使运行
失败，而不会伪造经济动作；无效结构化输出会成为明确的协议拒绝，且不修改经济状态。

## 数据与验证

默认数据库为 `backend/data/dairy_bench.sqlite3`。可用 `DAIRY_BENCH_DB` 覆盖数据库
路径，用 `DAIRY_BENCH_ARTIFACTS_DIR` 覆盖产物目录。

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
- `GET /api/runs/{run_id}/invocations/{invocation_id}/artifacts`
- `GET /api/runs`

## 设计文档

- [V3 架构与不变量](docs/V3_ARCHITECTURE.zh-CN.md)
- [V3 Agent 接入](docs/AGENT_INTEGRATION.zh-CN.md)
- [历史 V2 架构](docs/V2_ARCHITECTURE.md)
- [历史 V1 MVP 框架](docs/MVP_FRAMEWORK.md)
- [历史 V1 场景目录](docs/SCENARIO_CATALOG_V1.md)
