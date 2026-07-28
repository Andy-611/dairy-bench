# Dairy Bench 前端

单页 React + TypeScript 仪表盘。页面只负责创建运行、轮询进度和展示后端 `EpisodeResult`，不执行经济结算，也不接触模型 API Key。

## 本地运行

需要 Node.js 20.19+。先在 `backend/` 启动 FastAPI（默认 `http://127.0.0.1:8000`），再执行：

```powershell
cd "G:\Project in DeepWisdom\Multi Agent Company Bench\Dairy Bench\frontend"
npm.cmd install
npm.cmd run dev
```

访问 `http://127.0.0.1:5173`。Vite 会把 `/api` 代理到后端。

页面提供三种运行方式：

- **规则基线**：无需 API Key，适合验证环境；
- **OpenAI 公司 Agent**：需要后端在启动前设置 `OPENAI_API_KEY`；
- **精确回放**：填写一条已完成运行的 Run ID，重放其 180 条决策。

提交后，页面会轮询 `RunJob` 并显示当前天数；只有任务完成后才读取完整结果。模型选项是否可用、使用哪个模型，全部以后端 `/api/policy-profiles` 为准。

## 构建检查

```powershell
npm.cmd run build
```

前端会把后端可能以字符串返回的 `Decimal` 转换为 JavaScript `number`；字段缺失或类型不符时会报告响应契约错误。
