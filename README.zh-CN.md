# Dairy Bench

[English](README.md) | [简体中文](README.zh-CN.md)

Dairy Bench 是一个面向长期经营决策的事件驱动 Benchmark。3 家牧场、3 家加工商和
3 家零售商在同一个易腐乳制品供应链中独立经营，需要在不共享私有状态的前提下协调
生产、现货交易、库存、定价与现金。

当前唯一支持的场景是 `flow.dairy.base.s9.v9`：52 次周度结算、确定性的物理规则、
相互独立的公司 Agent、共享消费者市场、可持久恢复的运行状态，以及可审计的企业评分。

## 快速开始

运行要求：

- Windows 10/11
- Python 3.12+
- Node.js 20.19+
- 只有模型策略需要 NewAPI Key

安装后端与前端：

```powershell
cd backend
python -m pip install -e ".[dev]"

cd "..\frontend"
npm.cmd install
cd ..
```

按需配置一个或多个相互隔离的 NewAPI Profile：

```bat
start.cmd --configure-newapi model
start.cmd --configure-newapi codex
start.cmd --configure-newapi claude-code
```

每条命令都会验证 `/v1/models`，使用 Windows 当前用户范围的加密存储 Key，并刷新
该 Profile 的模型目录与能力目录。Key 只留在后端，不会写入 Run Journal 或 SQLite。

启动前后端：

```bat
start.cmd
```

页面地址是 `http://127.0.0.1:5173`。`start.cmd --check` 只读检查依赖与 Profile
配置，不会启动跑测。

## 策略 Profile

| Profile | 控制方式 | 模型协议 |
|---|---|---|
| Rule baseline | 进程内确定性规则 | 无 |
| Model agents via NewAPI | 每家公司一个隔离的模型 Agent | Chat Completions |
| Codex via NewAPI | Agent Runtime 仍由 Dairy Bench 提供 | Responses |
| Claude Code via NewAPI | Agent Runtime 仍由 Dairy Bench 提供 | Anthropic Messages |
| Exact Replay（精确重放） | 重放一个已完成 V9 Run 的 Turn Journal | 无 |

所有模型流量都只经过 NewAPI。带协议名称的 Profile 只是 wire adapter；本项目不会启动
Codex CLI、Claude Code，也不会使用模型厂商自己的 Company Agent Runtime。每次模型
响应必须恰好包含一个经过授权的强类型函数调用。

## 一周经营周期

最小模拟单位是 1 天：

| 日期 | Runtime 行为 |
|---|---|
| 周一 | 开启新周，实现本周私有产能与成本，唤醒公司 |
| 周二至周六 | 先完成到期工作和运输，再处理公司决策 |
| 周日 | 完成承诺、关闭现货市场、收集密封零售价、结算共享消费者购买、收取店铺成本、处理过期、检查破产、生成报告并保存快照 |

每家活跃公司每天最多收到一次经营调用、每周最多六次。所有采购和运输完成后，每家
活跃零售商还会在周日收到一次额外的密封定价调用。三家零售商看到相同的定价前状态，
只有在三份决策全部提交后，价格才会同时公开。

生产、加工和运输都需要 1 个模拟日。原奶在两次周度结算后过期，盒装奶在四次周度
结算后过期。

## V9 经济机制与信息边界

默认物理规模经过了总量匹配：

- 3 家牧场的正常周产能均为 `60`；
- 3 家加工商的正常周投入产能均为 `50`，产出率为 `0.8`，正常盒装奶总产出为 `120`；
- 正常状态下的三个消费者群体总量同样为 `120`；
- 每家活跃零售商每周固定产生 `5.0000` 的店铺运营成本。

每个生产型企业每周都会私有地实现产能和凸成本。零售需求是一个有限的共享市场，
不是三条相互独立的零售商需求曲线。三个支付意愿不同的隐藏消费者群体优先购买满足
条件的最低价库存；最低价缺货后流向下一家，同价时按确定性规则分配。持续性的低迷、
正常和旺盛状态会改变市场规模、消费者构成和支付意愿。

Agent 知道市场规则、三种状态名称、每种状态持续 6～10 周以及正常状态与极端状态的
交替结构，但看不到各群体的准确规模、支付意愿、状态倍率、购买力偏移、当前状态和
切换日期。每个 Agent 能看到自己最近 8 周的私有经营报告；所有 Agent 还能看到最近
8 周的公共零售报告，包括价格、销量、市场份额、售罄标记和企业状态。

## 确定性、并行与恢复

不同 `RunJob` 可以并行运行。同一个 Run 内，同一天被唤醒的 Agent 基于相同基础状态
并行推理，接受后的决策再按照下列持久化顺序串行应用：

```text
SHA256(seed | absolute_day | company_id)
```

因此，模型返回快慢不会改变经济事件顺序。默认上限为 100 个并行 Run 和 100 个并行
NewAPI 请求；如果网关承载能力较低，可以覆盖：

```powershell
$env:DAIRY_BENCH_MAX_CONCURRENT_RUNS = "20"
$env:DAIRY_BENCH_MAX_CONCURRENT_NEWAPI_REQUESTS = "50"
```

Journal 增量和 Checkpoint 会原子提交。手动停止或意外中断的 Run 可以使用同一个 Run
ID 恢复；失败 Run 是终态。Exact Replay 不调用模型，并会拒绝 observation、decision、
outcome 或 lineage 漂移。

## 评分

每完成一整周，运行中、已停止、已中断或失败的 Run 都会展示与完成 Run 相同的临时
总分、企业表、Token 汇总和趋势图；未完成结算的当前周不会计入。

9 家企业的正式企业评分为：

```text
Score = 100 × E × sqrt(F × P)

E = clamp(企业实际总 surplus / 可行 Oracle surplus, 0, 1)
F = 1 - 全部企业最终价值的 Gini / (8/9)
P = 1 - L/9
```

`L` 是最终 surplus 严格小于 0 的企业数量；surplus 等于 0 属于“不亏损”。`D` 单独
记录破产企业数量：企业在周日结算时的总资产严格小于 `1.0000` 才会宣告破产。

确定性 Oracle 使用已经实现的产能、凸成本、加工产出率、保质期、共享消费者市场和
强制店铺成本。它最大化企业总 surplus，明确不把消费者 surplus 计入目标。

## 本地数据

所有可变状态都被 Git 忽略：

```text
.dairy-bench/
|-- credentials/
|   |-- newapi-model/{token.clixml,models.json,model-capabilities.json}
|   |-- newapi-codex/{token.clixml,models.json,model-capabilities.json}
|   `-- newapi-claude-code/{token.clixml,models.json,model-capabilities.json}
`-- data/
    |-- oracle-v2/
    `-- runs-v9.sqlite3
```

只有确实需要更换 Runtime 根目录时才设置 `DAIRY_BENCH_HOME`。当前版本不提供旧场景或
旧 Payload 数据库的迁移与读取；升级后应从空的 `data` 目录开始运行。

## 项目结构

```text
backend/src/company_bench/
|-- agents/       策略 adapter、Agent memory 与 NewAPI 协议
|-- domain/       场景、企业、事件和经济精度的强类型契约
|-- economy/      引擎、市场、账本、周报、估值、评分与 Oracle
|-- runs/         Run 生命周期与前缀实时评估
|-- runtime/      确定性 Scheduler 与 Episode 编排
|-- storage/      Repository seam、内存 adapter 与 SQLite adapter
|-- timeline/     从 Journal 派生的只读模型与市场重建
`-- web/          FastAPI adapter

frontend/src/
|-- app/          应用组合与 Run 工作区状态
|-- features/     Run、市场、时间线和评分视图
`-- shared/       强类型 API Client、格式化、标签和复用 UI
```

核心写入链只有一条：

```text
Wake → AgentTurn → 一个 CompanyDecision → EconomyEngine → Journal → 下次 Wake
```

只有 `EconomyEngine` 可以修改经济状态；模型文本和 UI 投影都不能直接结算交易。

## 验证

```powershell
cd backend
python -m pytest -q -p no:cacheprovider
python -m ruff check src tests

cd "..\frontend"
npm.cmd run build

cd ..
start.cmd --check
```

## HTTP 接口

- `GET /api/health`——API、场景、评分、数据库和 Journal 契约身份
- `GET /api/policy-profiles`
- `POST /api/runs`
- `GET /api/run-jobs`
- `GET /api/run-jobs/{run_id}`
- `POST /api/run-jobs/{run_id}/stop`
- `POST /api/run-jobs/{run_id}/resume`
- `GET /api/replay-sources`
- `GET /api/runs/{run_id}`——只返回已完成 Episode
- `GET /api/runs/{run_id}/evaluation`
- `GET /api/runs/{run_id}/timeline?week={week}`
- `GET /api/runs/{run_id}/timeline/{entry_id}`
- `GET /api/runs/{run_id}/turns`
- `GET /api/runs/{run_id}/invocations`

完整契约见：

- [架构与不变量](docs/ARCHITECTURE.zh-CN.md)
- [Agent 接入](docs/AGENT_INTEGRATION.zh-CN.md)
- [前端指南](frontend/README.zh-CN.md)
