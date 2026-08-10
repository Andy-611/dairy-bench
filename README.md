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
- **Model agents via NewAPI** — one isolated Agent and gateway per company,
  backed by one application-wide NewAPI HTTP transport. The configured catalog
  may contain any model family that supports function calls through NewAPI's
  common Chat Completions interface.
- **Completed Run Replay** — reproduces a completed Turn Journal without calling a
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
  and deterministic completed-run replay.

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
catalog changes; doing so invalidates capabilities verified through the previous
route. If the backend is already open, restart it after configuration.

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
models do. It always disables parallel tool calls and gives an invalid completion
one structured repair attempt. Before the first run of a model is queued, a short
calibration request confirms its documented maximum output limit or extracts the
exact limit from an explicit NewAPI rejection. The confirmed value is cached in a
versioned local capability catalog and sent directly as `max_tokens`; there is no
staircase growth. Complete serialized input is checked before transport. The
adapter never falls back to unstructured text or another provider.

The decrypted key exists only in the backend process environment. It never
enters the browser, journal, or SQLite database.

## Runs and data

The selected run and day live in `?run=...&day=...`, so refresh and browser
navigation preserve the view. **All Runs** browses every persisted lifecycle
state and its committed timeline. **Completed Run Replay** uses only a completed
source run and inherits its seed. Stopped and interrupted runs can resume under
the same run ID; only interrupted work auto-resumes after a backend restart.
Failed runs are terminal. A protocol-invalid episode still completes with a
diagnostic score and replay source, but is explicitly excluded from benchmark
comparison.

Run submissions are independent: the backend executes up to 100 `RunJob`s at
once and queues later jobs. All model runs share one HTTP connection pool and a
100-request NewAPI semaphore; retry backoff happens after releasing its permit.
The UI keeps every active job refreshed, so another model can be submitted while
earlier runs continue. **All Runs** selects, stops, or resumes one job at a time.

Both concurrency limits default to, and cannot exceed, 100. They can be lowered
for a constrained gateway with `DAIRY_BENCH_MAX_CONCURRENT_RUNS` and
`DAIRY_BENCH_MAX_CONCURRENT_NEWAPI_REQUESTS`.

All mutable state stays under the Git-ignored `.dairy-bench/` directory:

```text
.dairy-bench/
|-- credentials/
|   |-- newapi-model-capabilities.json
|   `-- newapi-models.json
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
