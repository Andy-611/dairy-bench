# Dairy Bench Frontend

A React and TypeScript dashboard that creates runs, monitors progress, and
renders backend projections. Economic settlement and NewAPI credentials remain
on the backend.

The dashboard exposes exactly three policy modes:

- **Rule baseline** — deterministic and credential-free.
- **Model agents via NewAPI** — selects any configured NewAPI model.
- **Completed Run Replay** — reproduces a completed source Turn Journal without model
  calls.

**All Runs** browses every persisted lifecycle state. Stopped, interrupted, and
checkpointed failed runs expose an explicit resume action without changing the
run ID.

`/api/policy-profiles` is the source of truth for availability and the model
catalog. Every API response is parsed from `unknown` into typed view models;
malformed fields produce an explicit contract error.

## Local development

Requires Node.js 20.19 or newer and FastAPI running at
`http://127.0.0.1:8000`.

```powershell
cd frontend
npm.cmd install
npm.cmd run dev
```

Open `http://127.0.0.1:5173`. Vite proxies `/api` to the backend. The selected
run and day are preserved in the `run` and `day` URL parameters.

## Production build

```powershell
npm.cmd run build
```
