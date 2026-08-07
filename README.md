# Dairy Bench

Dairy Bench is an event-driven multi-agent benchmark for a perishable dairy
supply chain. Three farms, three processors, and three retailers share two spot
markets; each company is controlled by an independent Agent.

The default scenario is `flow.dairy.base.s9.v5`. One episode lasts 30 simulated
days, and every decision follows one typed boundary:

```text
Wake -> AgentTurn -> one CompanyCommand -> EconomyEngine -> Journal -> next Wake
```

Only `EconomyEngine` may mutate cash, inventory, orders, jobs, deliveries, or
trade results. Model text never settles a transaction.

## Policy modes

Dairy Bench exposes exactly three modes:

- **Rule baseline** — deterministic rules; no model call.
- **Model agents via NewAPI** — one isolated Agent and NewAPI HTTP client per
  company. The configured catalog may contain any model family that supports
  function calls through NewAPI's common Chat Completions interface.
- **Exact replay** — reproduces a completed Turn Journal without calling a
  model and rejects observation or outcome drift.

There is no direct model-provider path. All model-backed runs go through the
single NewAPI adapter and validate into the same Pydantic command union.

## V4 behavior

- Continuous, fully collateralized limit-order books for raw and bottled milk.
- Atomic target ladders with up to three price levels and price-time priority.
- FEFO inventory reservation and a shared `0.0001` economic quantum.
- Concurrent same-minute model inference followed by deterministic persisted
  command application order.
- Thirty-minute production, transformation, and delivery events.
- Private per-company memory, immutable journals, atomic checkpoints, recovery,
  and exact replay.

The daily clock is:

| Time | Event |
|---|---|
| 09:00 | Open markets and wake all companies |
| 09:00–18:59 | Accept decisions and continuously match orders |
| 19:00 | Complete due work, close books, then run consumer sales |
| 19:00–19:29 | Process previously committed completions and deliveries |
| 19:30 | Expire inventory and commit the daily snapshot |

See [Architecture and invariants](docs/ARCHITECTURE.md) and
[Agent integration](docs/AGENT_INTEGRATION.md) for the full contracts.

## Install and start

Requirements:

- Python 3.12+
- Node.js 20.19+
- A NewAPI key only for model-backed runs

Install once:

```powershell
cd backend
python -m pip install -e ".[dev]"

cd "..\frontend"
npm.cmd install
cd ..
```

Configure or replace the NewAPI key from the repository root:

```bat
start.cmd --configure-newapi
```

The configuration command validates the key through `/v1/models`, stores a
Windows-user-encrypted credential, and saves the complete non-secret model
catalog under `.dairy-bench/credentials/`. Run it again whenever the key or
catalog changes. If the backend is already open, restart it after configuration.

Start the application:

```bat
start.cmd
```

The launcher opens `http://127.0.0.1:5173`. Use `start.cmd --check` to inspect
local prerequisites and NewAPI configuration without starting services.

The model selector is populated from the NewAPI catalog. Because `/v1/models`
does not prove command-tool compatibility, Dairy Bench still requires exactly
one authorized function call. The adapter prefers `tool_choice="required"` and
omits that hint only when the provider explicitly rejects it, as some thinking
models do. Every request uses a fixed `max_tokens=131072` output budget. It never
falls back to unstructured text or another provider.

The decrypted key exists only in the backend process environment. It never
enters the browser, journal, or SQLite database.

## Runs and data

The selected run and day live in `?run=...&day=...`, so refresh and browser
navigation preserve the view. Exact Replay uses a completed source run and
inherits its seed. Stopping is permanent: existing journals and timeline data
remain readable, but the run receives no final score and cannot become a replay
source.

All mutable state stays under the Git-ignored `.dairy-bench/` directory:

```text
.dairy-bench/
|-- credentials/
`-- data/runs.sqlite3
```

Override the root only when necessary with `DAIRY_BENCH_HOME`.

## Verification

```powershell
cd backend
python -m pytest -q
python -m ruff check .

cd "..\frontend"
npm.cmd run build
```

## Main HTTP interfaces

- `GET /api/policy-profiles`
- `POST /api/runs`
- `GET /api/run-jobs`
- `GET /api/run-jobs/{run_id}`
- `GET /api/runs/{run_id}`
- `GET /api/runs/{run_id}/timeline?day={day}`
- `GET /api/runs/{run_id}/timeline/{entry_id}`
- `GET /api/runs/{run_id}/turns`
- `GET /api/runs/{run_id}/invocations`
