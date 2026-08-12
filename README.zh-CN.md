# Dairy Bench

Dairy Bench 是一个乳制品供应链多公司 Benchmark：3 家牧场、3 家加工厂和
3 家零售商在同一易腐经济中独立经营。默认场景是
`flow.dairy.base.s9.v6`，一个 Episode 包含 52 个经营周。

每次决策只经过一条强类型边界：

```text
Wake -> AgentTurn -> 一个 CompanyDecision -> EconomyEngine -> Journal -> 下次 Wake
```

只有 `EconomyEngine` 能修改现金、库存、订单、生产任务、运输和交易结果；模型文本
不能直接执行经济结算。

## 52 周日历

最小模拟粒度为 1 天。UI 提供 52 个周按钮；选中某周后显示 Monday 至 Sunday
七个时间帧。

| 日期 | 运行规则 |
|---|---|
| 周一 | 开启新周，按公式实现本周私有产能与成本，唤醒公司 |
| 周二至周六 | 先完成到期生产/运输，再执行公司决策 |
| 周日 | 完成到期承诺、关闭批发市场、一次性结算消费者购买、处理过期、保存周快照 |

周日不调用 Agent。每家公司每天最多行动一次、每周最多六次。生产原奶、加工盒装奶、
交易运输均耗时 1 天；原奶和盒装奶分别在 2 周、4 周后过期。

产能和需求并不是固定实现值。牧场正常产能 `60`、加工厂正常产能 `50`、零售商基础
需求 `40` 仍是原有确定性公式的输入：产能与单位成本每公司每周实现一次，潜在需求
每零售商每周实现一次，并在周日一次性购买。

## 三种策略模式

- **Rule baseline**：确定性规则，不调用模型。
- **Model agents via NewAPI**：每家公司使用隔离 Agent；只要支持统一函数调用契约，
  NewAPI 目录中的任意模型家族都可使用。
- **Completed Run Replay**：不调用模型，精确重放已完成 Run 的 Turn Journal，并检测
  observation/outcome 漂移。

所有模型调用只经过 NewAPI；项目没有 Codex、OpenAI Company Agent、Claude 或其他
模型厂商的直连接口。

## 并行与确定性

多个 `RunJob` 可以并行执行。同一个 Run 内，同一天被唤醒的 Agent 基于同一个状态
并行推理，之后按以下持久化顺序串行应用决策：

```text
SHA256(seed | absolute_day | company_id)
```

因此模型返回速度不会改变经济结果。默认最多并行 100 个 Run、100 个 NewAPI 请求；
如网关限制更低，可设置：

```powershell
$env:DAIRY_BENCH_MAX_CONCURRENT_RUNS = "20"
$env:DAIRY_BENCH_MAX_CONCURRENT_NEWAPI_REQUESTS = "50"
```

## 安装与运行

需要 Python 3.12+、Node.js 20.19+；只有模型模式需要 NewAPI Key。

```powershell
cd backend
python -m pip install -e ".[dev]"

cd "..\frontend"
npm.cmd install
cd ..
```

配置或更换 NewAPI Key：

```bat
start.cmd --configure-newapi
```

该命令会验证 `/v1/models`、用 Windows 当前用户加密保存凭证，并刷新本地模型与能力
目录。修改凭证后应重启正在运行的后端。

启动：

```bat
start.cmd
```

页面地址为 `http://127.0.0.1:5173`；`start.cmd --check` 只检查依赖和配置。

NewAPI Adapter 只接受恰好一个授权函数调用，禁止并行工具调用，并允许一次结构化修复。
模型最大输出长度会校准一次并缓存，之后直接使用已确认上限，不做阶梯增长。Key 只存在
于后端进程中。

## Run、恢复与数据

当前视图写入 `?run=...&week=...`。停止或中断的 Run 会保留 Journal 与原子 Checkpoint，
并能用相同 Run ID 恢复；失败 Run 为终态。违反模型协议的 Episode 可以保留诊断分数，
但不会进入 Benchmark 排名。

运行数据位于 Git 忽略目录：

```text
.dairy-bench/
|-- credentials/
|   |-- newapi-model-capabilities.json
|   `-- newapi-models.json
`-- data/runs-v6.sqlite3
```

## 验证

```powershell
cd backend
python -m pytest -q -p no:cacheprovider
python -m ruff check src tests

cd "..\frontend"
npm.cmd run build
```

完整机制见 [架构与不变量](docs/ARCHITECTURE.zh-CN.md) 与
[Agent 接入契约](docs/AGENT_INTEGRATION.zh-CN.md)。
