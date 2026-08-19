# Dairy Bench Frontend

[简体中文](README.zh-CN.md)

The frontend is a React and TypeScript observer for Dairy Bench. It creates and
controls runs, follows their lifecycle, and renders evaluation and timeline
projections returned by FastAPI. Economic state transitions, persistence,
NewAPI calls, and credentials remain exclusively on the backend.

## Source layout

```text
src/
|-- app/          application composition, workspace state, and global styles
|-- features/     run controls, evaluation, market, and timeline views
`-- shared/       typed API boundary, formatting, labels, and reusable UI
```

`/api/policy-profiles` is the source of truth for selectable policies and model
catalogs. API payloads are decoded from `unknown` into typed view models;
malformed fields produce an explicit contract error instead of entering the UI.

The selected run and simulation week are stored in the `run` and `week` URL
parameters. Run history includes every persisted lifecycle state. Only stopped
and interrupted runs expose resume controls; completed and failed runs are
terminal.

## Local development

Requires Node.js 20.19 or newer and the Dairy Bench FastAPI service at
`http://127.0.0.1:8000`.

```powershell
cd frontend
npm.cmd install
npm.cmd run dev
```

Open `http://127.0.0.1:5173`. Vite proxies `/api` to the backend.

## Verification

```powershell
npm.cmd run build
```

The build runs strict TypeScript checking before producing the Vite bundle.
