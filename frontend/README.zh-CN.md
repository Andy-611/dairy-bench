# Dairy Bench 前端

[English](README.md)

前端是 Dairy Bench 的 React + TypeScript 观察界面，负责创建和控制 Run、跟踪
生命周期，并展示 FastAPI 返回的评估与时间线投影。经济状态变更、持久化、NewAPI
调用和凭证始终只属于后端。

## 源码结构

```text
src/
|-- app/          应用组装、工作区状态与全局样式
|-- features/     Run 控制、评估、市场与时间线界面
`-- shared/       强类型 API 边界、格式化、标签与复用 UI
```

`/api/policy-profiles` 是可选策略和模型目录的唯一事实来源。所有 API Payload 都从
`unknown` 解码为强类型 View Model；字段不符合契约时会明确报错，不会进入 UI。

当前选中的 Run 和模拟周分别保存在 URL 的 `run`、`week` 参数中。Run 历史会展示
所有已持久化的生命周期状态；只有 stopped 和 interrupted Run 提供恢复按钮，
completed 与 failed Run 都是终态。

## 本地开发

需要 Node.js 20.19 或更高版本，并确保 Dairy Bench FastAPI 服务运行在
`http://127.0.0.1:8000`。

```powershell
cd frontend
npm.cmd install
npm.cmd run dev
```

打开 `http://127.0.0.1:5173`；Vite 会把 `/api` 代理到后端。

## 验证

```powershell
npm.cmd run build
```

构建会先执行严格 TypeScript 检查，再生成 Vite Bundle。
