# Agent 接入说明

## 1. 本项目中的 Agent 是什么

鲜奶场景固定有 6 家公司：

```text
牧场 A、牧场 B、加工厂 A、加工厂 B、零售商 A、零售商 B
```

每家公司恰好对应一个 `LlmCompanyPolicy`，也就是一个独立 Agent。每个 Agent 每天只做一件事：

```text
接收本公司的 CompanyObservation
→ 调用一次模型
→ 返回符合本公司角色的 CompanyDecision
```

因此一局总计 `6 × 30 = 180` 次模型调用。本项目不实现“一个公司内部再放多个 Agent”的组织结构。

六个 Agent 各自保存最近 7 天的结构化经营记忆，并分别拥有独立的
`ModelGateway`。Codex 模式下每家公司拥有一个独立 `AsyncCodex` runtime；
OpenAI API 模式下每家公司拥有一个独立 `AsyncOpenAI` Client。它们不共享
对话、连接池、调用状态或公司私有状态。

## 2. 使用 Codex Agent（无需 API Key）

### 2.1 登录与验证

官方 SDK 会自动安装匹配版本的 Codex runtime，并复用 `CODEX_HOME` 下已有的
ChatGPT 登录。默认目录为 `%USERPROFILE%\.codex`，但 Dairy Bench 不会直接读取
或复制 `auth.json`。

首次使用时完成一次登录：

```powershell
codex login
codex login status
```

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

脚本默认使用 `gpt-5.6-terra` 和 `high` 推理强度。如需修改，可直接编辑
`start.cmd` 顶部的三个环境变量。

如需单独验证认证和结构化输出，仍可在 `backend` 下运行
`python scripts\codex_smoke.py`。它只执行牧场 A 的一天决策，因此会产生一次真实
模型调用。

每家公司拥有一个长期存活到本局结束的 Codex runtime，但每天创建一个新的
持久化 thread。thread 不跨天复用，因此最近 7 天记忆仍由 Benchmark 显式管理，
不会引入隐藏的跨天上下文；`ephemeral=False` 只负责让 Codex 在
`CODEX_HOME/sessions` 中保存原始 Session。runtime 使用独立临时目录、只读沙箱和
`deny_all` 审批，并关闭 shell、搜索、插件、记忆和多 Agent 功能。

每个 Turn 完成后，Dairy Bench 会直接读取该 Thread 对应的 Session 文件，并导出：

```text
run_artifacts/<run_id>/
├── reasoning/day-001__farm_a.md
└── final_outputs/day-001__farm_a.json
```

推理文件只保存 Codex 公开提供的英文 reasoning summary；最终输出文件保存格式化后的
原始结构化 JSON。系统不会复制或尝试解密 `encrypted_content`。默认请求
`detailed` 推理摘要。

### 2.2 打开前端

`start.cmd` 会分别打开前后端终端，并自动打开 `http://127.0.0.1:5173`。选择
“Codex 公司 Agent”，输入随机种子并运行。停止时在两个服务终端中分别按
`Ctrl+C`。

## 3. 使用 OpenAI API Agent

### 3.1 安装并配置后端

```powershell
cd "G:\Project in DeepWisdom\Multi Agent Company Bench\Dairy Bench\backend"
python -m pip install -e ".[dev]"

$env:OPENAI_API_KEY="你的 OpenAI API Key"

# 以下均为可选配置
$env:DAIRY_BENCH_OPENAI_MODEL="gpt-5.6-terra"
$env:DAIRY_BENCH_OPENAI_REASONING_EFFORT="medium"
$env:DAIRY_BENCH_OPENAI_MAX_OUTPUT_TOKENS="2048"
$env:DAIRY_BENCH_OPENAI_TIMEOUT_SECONDS="60"
$env:DAIRY_BENCH_OPENAI_MAX_ATTEMPTS="3"

python -m uvicorn company_bench.web:create_app --factory --host 127.0.0.1 --port 8000
```

`OPENAI_API_KEY` 必须在启动 Uvicorn **之前**设置。若后端已经启动，设置环境变量后需要重启后端。

如果使用兼容的自定义 API 地址，还可以设置：

```powershell
$env:DAIRY_BENCH_OPENAI_BASE_URL="https://你的兼容服务/v1"
```

### 3.2 启动前端

```powershell
cd "G:\Project in DeepWisdom\Multi Agent Company Bench\Dairy Bench\frontend"
npm.cmd install
npm.cmd run dev
```

打开 `http://127.0.0.1:5173`：

1. 在“公司决策方式”选择“OpenAI 公司 Agent”；
2. 输入随机种子；
3. 点击“运行 30 天”；
4. 等待进度到 `30 / 30`，随后查看成绩、公司结果和事件。

若该选项显示“未配置”，访问 `http://127.0.0.1:8000/api/policy-profiles` 检查后端是否识别到 API Key。浏览器不会接收、保存或转发 API Key。

## 4. 一次决策如何流动

```text
React 创建 RunJob
→ RunCoordinator 在后台执行
→ PolicyFactory 为 6 家公司创建 6 个 LlmCompanyPolicy
→ 每日同时收集 6 个 CompanyDecision
→ EconomyEngine 统一结算交易、生产、销售和过期
→ SQLite 保存运行状态、最终结果和模型审计
```

模型只能看到 `CompanyObservation` 和本公司最近 7 天的记忆。它看不到其他公司的现金、库存或私有记忆，也不能直接修改经济状态。

模型响应使用严格 Structured Outputs。不同角色只允许返回对应结构：

- 牧场：`FarmDecision`；
- 加工厂：`ProcessorDecision`；
- 零售商：`RetailerDecision`；
- 任意角色都可以返回 `NoOpDecision`。

## 5. 为什么任务是异步的

规则策略几乎瞬间完成，但 180 次真实模型调用可能持续较久。因此：

```text
POST /api/runs
→ 立即返回 HTTP 202 和 run_id
→ GET /api/run-jobs/{run_id} 轮询 queued/running/completed/failed
→ completed 后 GET /api/runs/{run_id}
```

前端已经封装了这段轮询，不需要手动操作。直接调用 API 时，请按上述顺序读取。

当前版本会在后端重启后把未完成任务从第 1 天重新执行；尚未提供逐日精确断点续跑，因此重启可能产生额外模型调用。已完成运行和审计记录不会受影响。

创建 OpenAI 运行：

```json
{
  "mode": "openai",
  "seed": 42
}
```

创建 Codex 运行：

```json
{
  "mode": "codex",
  "seed": 42
}
```

创建规则基线：

```json
{
  "mode": "baseline",
  "seed": 42
}
```

精确回放：

```json
{
  "mode": "replay",
  "source_run_id": "run_..."
}
```

回放自动继承来源运行的 seed，并逐公司、逐日使用原有决策，适合验证经济引擎和结果持久化。

## 6. 失败语义

两类失败必须区分：

| 情况 | 处理方式 | 原因 |
|---|---|---|
| 模型返回缺字段、错误角色或不合法数值 | 该公司当天改为 `NoOpDecision`，记录 `PolicyFailedEvent`，整局继续 | 这是 Agent 决策质量的一部分 |
| API Key 错误、网络超时、限流重试耗尽、服务端故障 | 整个 `RunJob` 变为 `failed`，不生成 Benchmark 成绩 | 这不是 Agent 的经营能力，不能污染分数 |

每次模型调用都会记录：

- 公司、天数和当时的 `CompanyObservation`；
- provider、model、prompt 版本与哈希；
- 成功、Agent 输出错误或基础设施错误；
- 决策、请求/响应 ID、重试次数、延迟和 token 用量。

通过以下接口查看：

```text
GET /api/runs/{run_id}/invocations
GET /api/runs/{run_id}/invocations/{invocation_id}/artifacts
```

第二个接口只读取项目内已导出的文件，不会再次调用模型或消耗 Token。前端
`AUDIT TRAIL` 在事件 Card 展开时才懒加载相关公司的当日轨迹；交易事件同时关联
卖方和买方。

系统只保存 Codex 公开提供的推理内容，不保存模型隐藏思维链，也不会把 API Key、
`auth.json` 或其他 Codex 登录凭证写入审计记录。

## 7. 更换模型服务

经济引擎只依赖 `CompanyPolicy.decide()`，LLM 策略只依赖 `ModelGateway`：

```python
class ModelGateway(Protocol):
    async def generate(
        self,
        request: ModelRequest,
        output_type: type[DecisionModel],
    ) -> ModelResult: ...

    async def close(self) -> None: ...
```

接入其他模型提供方时，实现这个接口，再通过 `PolicyFactory(gateway_factory=...)` 注入即可。`PolicyFactory` 会为每家公司调用一次 factory，因此 factory 每次必须返回全新的 Gateway，不能复用同一个 Client。新 Adapter 应继续满足三条规则：

1. 把供应商响应校验成角色对应的强类型 Decision；
2. 内容不合法时抛出 `ModelOutputError`；
3. 网络、认证和服务故障时抛出 `ModelInfrastructureError`。

这样更换模型不会修改 `EconomyEngine`、评分器或前端。

## 8. 验证

普通测试不会访问真实 OpenAI 网络。它通过 fake Codex runtime 和
`httpx.MockTransport` 分别验证 Codex 与 Responses API 的结构化输出契约：

```powershell
cd "G:\Project in DeepWisdom\Multi Agent Company Bench\Dairy Bench\backend"
New-Item -ItemType Directory -Force ".tmp" | Out-Null
python -m pytest --basetemp ".tmp\pytest"
python -m ruff check .
```

关键自动化检查包括：

- 6 个独立 Agent 在 30 天内恰好产生 180 次调用；
- Agent 输出错误只降级一个公司日；
- 基础设施错误使任务失败且不保存成绩；
- OpenAI Structured Outputs 的请求与解析契约；
- 精确回放；
- SQLite v1 到 v2 的无损升级。
