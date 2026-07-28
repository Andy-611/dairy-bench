# Dairy Bench

Dairy Bench 是 Multi-Agent Company Bench 的第一个可运行场景：2 家牧场、2 家加工厂和 2 家零售商共同经营一条鲜牛奶产业链。

这里的 “Multi-Agent” 指多家公司相互交易和竞争，**每家公司只由一个独立 Agent 控制**，不实现公司内部多 Agent 团队。一局持续 30 天：

```text
原奶生产 → 原奶交易 → 加工盒装奶 → 盒装奶交易
→ 消费者购买 → 库存过期 → 效率/公平评分 → SQLite 留档
```

核心原则：

> Agent 只能提交经营决策；只有经济引擎能够改变现金、库存和交易结果。

## 已实现

- 6 家异质企业、两种易腐商品、FEFO 库存和两级现货市场；
- 每家公司一个独立决策器，可选择规则基线、Codex Agent、OpenAI Agent 或历史回放；
- 6 个 Agent 分别拥有独立记忆、`ModelGateway` 和模型客户端；
- 后台运行、30 天进度轮询、失败状态和完整模型调用审计；
- 确定性经济引擎，以及效率、公平、缺货和浪费等指标；
- SQLite v2 持久化、FastAPI API 和 React 中文仪表盘。

```text
React → FastAPI → RunCoordinator → DairyBenchmark → EconomyEngine
                         │              └─ Evaluator
                         ├─ PolicyFactory
                         │   ├─ BaselinePolicy
                         │   ├─ LlmCompanyPolicy × 6 → ModelGateway × 6 → Codex / OpenAI
                         │   └─ ReplayPolicy × 6
                         └─ LifecycleRepository → SQLite
```

## 本地运行

要求 Python 3.12+、Node.js 20.19+。

依赖只需安装一次：

```powershell
cd "G:\Project in DeepWisdom\Multi Agent Company Bench\Dairy Bench\backend"
python -m pip install -e ".[dev]"

cd "..\frontend"
npm.cmd install
```

以后从项目根目录直接运行：

```powershell
cd "G:\Project in DeepWisdom\Multi Agent Company Bench\Dairy Bench"
.\start.cmd
```

`start.cmd` 会分别打开后端和前端终端，等待 3 秒后打开
`http://127.0.0.1:5173`。默认启用 `gpt-5.6-terra` 的 `high` 推理模式。停止时在
两个服务终端中分别按 `Ctrl+C`；如需更换模型，修改 `start.cmd` 顶部的环境变量。

不配置或不选择模型也仍可运行“规则基线”。

## 接入 Codex Agent（无需 API Key）

先在普通 PowerShell 中确认 Codex 已使用你的 ChatGPT 账号登录：

```powershell
codex login
codex login status
```

默认认证目录是 `%USERPROFILE%\.codex`。后端和 Codex 必须由同一个 Windows
用户启动；项目不会读取、复制或保存 `auth.json`。

登录成功后，直接从项目根目录启动：

```powershell
cd "G:\Project in DeepWisdom\Multi Agent Company Bench\Dairy Bench"
.\start.cmd
```

刷新前端，选择“Codex 公司 Agent”即可运行。六家公司各自拥有一个独立
`AsyncCodex` runtime；每次公司日决策使用一个新的持久化 thread，历史信息仍只由
Benchmark 明确提供的最近 7 天记忆决定。Codex 原始 Session 保留在
`CODEX_HOME/sessions`，公开轨迹和最终输出同时导出到项目根目录的
`run_artifacts/<run_id>/`。

若要使用专门的、与日常 Codex 配置隔离的认证目录，可在登录和启动后端前同时设置：

```powershell
$env:CODEX_HOME="$env:LOCALAPPDATA\DairyBench\codex"
$env:DAIRY_BENCH_CODEX_HOME=$env:CODEX_HOME
codex login
```

## 接入 OpenAI API Agent

在**启动后端的同一个 PowerShell 终端**先设置 API Key：

```powershell
$env:OPENAI_API_KEY="你的 OpenAI API Key"

# 可选；不设置时默认使用 gpt-5.6-terra
$env:DAIRY_BENCH_OPENAI_MODEL="gpt-5.6-terra"

python -m uvicorn company_bench.web:create_app --factory --host 127.0.0.1 --port 8000
```

刷新页面，在“公司决策方式”中选择“OpenAI 公司 Agent”，输入 seed，再点击“运行 30 天”。API Key 只由后端读取，不会发送到浏览器或写入数据库。

一局包含 `6 家公司 × 30 天 = 180` 次模型决策，因此会比规则基线慢，并产生相应 API 费用。模型返回的业务内容不合法时，仅将该公司当日决策降级为 `NoOp`；认证、网络或服务端故障会把整局标记为 `failed`，避免生成失真的 Benchmark 成绩。

完整配置、调用流程和审计说明见 [Agent 接入说明](docs/AGENT_INTEGRATION.md)。

## 数据与验证

默认数据库位于 `backend/data/dairy_bench.sqlite3`，可用 `DAIRY_BENCH_DB` 指定其他路径。
Codex 导出目录默认为 `run_artifacts`；需要迁移时可设置
`DAIRY_BENCH_ARTIFACTS_DIR`。

```powershell
cd "G:\Project in DeepWisdom\Multi Agent Company Bench\Dairy Bench\backend"
New-Item -ItemType Directory -Force ".tmp" | Out-Null
python -m pytest --basetemp ".tmp\pytest"
python -m ruff check .

cd "..\frontend"
npm.cmd run build
```

主要 API：

- `GET /api/policy-profiles`：读取可用决策模式及后端模型配置；
- `POST /api/runs`：创建后台任务，立即返回 `202 RunJob`；
- `GET /api/run-jobs/{run_id}`：读取状态和 30 天进度；
- `GET /api/runs/{run_id}`：读取已完成的完整结果；
- `GET /api/runs/{run_id}/invocations`：读取逐公司、逐日模型审计记录；
- `GET /api/runs/{run_id}/invocations/{invocation_id}/artifacts`：读取已导出的
  Codex 公开推理轨迹与最终输出；
- `GET /api/runs`：读取历史运行摘要。

其他设计说明：

- [Agent 接入说明](docs/AGENT_INTEGRATION.md)
- [MVP 框架设计](docs/MVP_FRAMEWORK.md)
- [第一版场景目录](docs/SCENARIO_CATALOG_V1.md)
