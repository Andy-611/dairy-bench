# Dairy Bench

Dairy Bench 是一个事件驱动的多 Agent 易腐乳制品供应链 Benchmark。三家牧场、
三家加工厂和三家零售商共享两个现货市场，九家企业各由一个独立 Agent 控制。

默认场景为 `flow.dairy.base.s9.v5`。一个 episode 持续 30 个模拟日，每次决策只
提交一条强类型原子命令：

```text
唤醒 -> AgentTurn -> 一条 CompanyCommand -> EconomyEngine -> Journal -> 下次唤醒
```

只有确定性的经济引擎可以修改现金、库存、订单、作业、在途交付和交易结果。自然语言
永远不能直接促成经济结算。

## V4 概览

- 原奶和盒装奶采用全额担保的连续限价订单簿。订单交叉时立即按价格优先、同价时间
  优先撮合，成交价取 maker 订单价格，并支持部分成交；Agent 一次模型调用即可为一个
  产品和方向设置最多三档、各自独立的目标报价。
- 买单冻结按限价计算的全部现金，卖单冻结真实的 FEFO 库存批次。资产不足时整单
  报价阶梯原子拒绝，不产生虚假流动性，也不会留下只更新一部分的中间状态。
- 所有经济 Decimal 共用 `0.0001` 这一种精度。Agent 提交的数量和价格若超过四位
  小数，会被整条命令拒绝且不会静默舍入；派生金额采用半偶舍入，派生实物数量向下
  取整，舍入后为零的订单、成交或作业成本会被原子拒绝。
- 同一分钟的 Agent 请求并发执行，命令随后按照持久化的
  `SHA256(seed | minute | company)` 顺序串行提交。Provider 响应延迟只用于审计，
  不影响经济结果。
- `produce` 和 `transform` 独占企业的物理资源 30 个虚拟分钟；市场命令和零售价
  命令没有经济冷却，但每家企业每个虚拟分钟最多决策一次。
- 成交后卖方立即收款，买方在 30 分钟后自动收到货物；不引入手动发货、路线、承运商
  或托管工作流。
- Agent 可看到全部匿名聚合价位、含排队量的自有订单、按到期日拆分的可用及冻结资产、
  待到货、当前作业和当日剩余产能。
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

完整契约见 [V4 架构与不变量](docs/ARCHITECTURE.zh-CN.md) 和
[Agent 接入](docs/AGENT_INTEGRATION.zh-CN.md)。

## 系统概览

```text
React -> FastAPI -> RunCoordinator -> EpisodeRuntime -> Scheduler + EconomyEngine
                         |                 `-> Evaluator
                         |-> AgentFactory -> CompanyAgent x 9
                         `-> RunStore -> Journal + Checkpoint + SQLite
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

页面会自动打开刚提交或仍在运行的 Run。当前 Run 与天数写入
`?run=...&day=...`，刷新、前进和后退都会保留页面状态。选择 `Exact Replay`
后，随机种子会替换为按提交时间从新到旧排列的全部 completed Run 下拉框；选择来源
即可打开其持久化结果与时间线，启动 Replay 则不会调用模型，并会验证结果是否精确一致。

Run 仍处于活动状态时，同一个主按钮会变为 `Stop run`。停止是永久操作：
已经提交的 Journal、Checkpoint 和时间线证据仍可读取，但该 Run 不会生成最终分数、
不会恢复，也不会成为 Exact Replay 来源。

## 模型 Agent

`start.cmd` 使用仓库自己的 `.dairy-bench/codex`，不会写入
`%USERPROFILE%\.codex`。每家公司拥有独立 Runtime 和私有记忆；每个 Codex Turn
使用隔离 thread。公开推理摘要和最终结构化输出导出到：

```text
.dairy-bench/artifacts/<run_id>/
|-- reasoning/
`-- final_outputs/
```

使用 OpenAI Agent 时，在启动后端的同一个 PowerShell 中设置：

```powershell
$env:OPENAI_API_KEY="your-key"
$env:DAIRY_BENCH_OPENAI_MODEL="gpt-5.6-terra"  # 可选

python -m uvicorn company_bench.web.app:create_app --factory --app-dir src --host 127.0.0.1 --port 8000
```

密钥不会进入浏览器、Journal 或 Benchmark 数据库。Provider 基础设施故障会使运行
失败，而不会伪造经济动作；无效结构化输出会成为明确的协议拒绝，且不修改经济状态。

## 数据与验证

所有可变运行状态统一存放在 Git 忽略的项目目录 `.dairy-bench/`：数据库位于
`data/runs.sqlite3`，Agent 证据位于 `artifacts/`，隔离的 Codex 状态位于
`codex/`，加密的 NewAPI 凭据位于 `credentials/`。如确有需要，只用
`DAIRY_BENCH_HOME` 覆盖整个运行目录。

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

## 设计文档

- [V4 架构与不变量](docs/ARCHITECTURE.zh-CN.md)
- [V4 Agent 接入](docs/AGENT_INTEGRATION.zh-CN.md)
