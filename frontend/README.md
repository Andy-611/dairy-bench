# Dairy Bench Frontend

A single-page React and TypeScript dashboard. It creates runs, polls progress,
and renders backend projections. Economic settlement and model credentials stay
on the backend.

V3 displays the continuous, fully collateralized spot market and its intraday
lifecycle: markets open at 09:00, close before consumer sales at 19:00, and the
day closes at 19:30. Operation and delivery completions appear as distinct
system transitions; Agent detail includes reserved assets, live market depth,
incoming deliveries, and the active operation.

## Local development

Requires Node.js 20.19 or newer. Start FastAPI from `backend/` (default:
`http://127.0.0.1:8000`). From the repository root, run:

```powershell
cd frontend
npm.cmd install
npm.cmd run dev
```

Open `http://127.0.0.1:5173`. Vite proxies `/api` to the backend.

The dashboard supports four policy modes:

- **Rule baseline**: deterministic and requires no model credentials.
- **Codex company agents**: one isolated Codex runtime per company.
- **OpenAI company agents**: requires `OPENAI_API_KEY` on the backend.
- **Exact replay**: replays the Turn Journal of a completed source run without
  making model calls.

After submission, the page polls `RunJob`, automatically selects it, and reports
the current day. Full results are loaded only after completion. Exact Replay
uses `/api/replay-sources` to list every completed run from newest to oldest;
selecting a source also opens its persisted results and operations timeline.
The selected run and day live in the `run` and `day` URL parameters, so refresh
and browser navigation restore the same view. `/api/policy-profiles` remains the
source of truth for policy availability and model configuration.

The primary action becomes **Stop run** while work is active. A stopped run is
terminal and non-resumable; its persisted timeline and last checkpoint remain
available, but no final score or replay source is created.

## Production build

```powershell
npm.cmd run build
```

The API boundary parses every response from `unknown` into strongly typed view
models. Decimal values serialized as strings are converted to JavaScript
numbers; missing or malformed fields produce an explicit contract error.
