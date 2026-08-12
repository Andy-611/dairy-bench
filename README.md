# Dairy Bench

Dairy Bench is an event-driven benchmark for nine independent companies in a
perishable dairy supply chain: three farms, three processors, and three
retailers. The default scenario is `flow.dairy.base.s9.v6` and spans 52 trading
weeks.

Every decision crosses one typed boundary:

```text
Wake -> AgentTurn -> one CompanyDecision -> EconomyEngine -> Journal -> next Wake
```

Only `EconomyEngine` may mutate money, inventory, orders, jobs, deliveries, or
trades. Model text never settles an economic transaction.

## Calendar and settlement

The smallest simulation unit is one day. Each week contains exactly seven
frames in the operations timeline:

| Day | Runtime behavior |
|---|---|
| Monday | Open the week, realize private capacity/cost formulas, wake companies |
| Tuesday-Saturday | Complete due work and deliveries, then run company decisions |
| Sunday | Complete due commitments, close wholesale markets, settle consumer sales, expire inventory, snapshot the week |

There are no Agent calls on Sunday. A company can act at most once per day and
six times per week. Production, transformation, and delivery each take one day.
Raw milk expires after two weekly settlements and bottled milk after four.

Capacity and demand are **not fixed realized quantities**. Values such as farm
normal capacity `60`, processor normal capacity `50`, and retailer base demand
`40` remain inputs to the existing deterministic formulas. Capacity and unit
cost are realized once per company-week; potential demand is realized once per
retailer-week and purchased once on Sunday.

## Policy modes

Dairy Bench exposes exactly three modes:

- **Rule baseline**: deterministic rules and no model call.
- **Model agents via NewAPI**: one isolated Agent per company. Any model family
  in the configured NewAPI catalog may be used if it supports the required
  function-call contract.
- **Completed Run Replay**: replays a completed Turn Journal without model calls
  and rejects observation or outcome drift.

All model traffic uses one NewAPI adapter. There is no direct Codex, OpenAI
Company Agent, Claude, or provider-specific execution path.

## Determinism and concurrency

Independent `RunJob`s execute concurrently. Within one run, Agents woken on the
same day observe the same base state and may infer concurrently. Their decisions
are then applied serially in persisted order:

```text
SHA256(seed | absolute_day | company_id)
```

Provider latency therefore cannot change the economy. By default the backend
allows up to 100 concurrent runs and 100 concurrent NewAPI requests. Lower the
limits when required by the gateway:

```powershell
$env:DAIRY_BENCH_MAX_CONCURRENT_RUNS = "20"
$env:DAIRY_BENCH_MAX_CONCURRENT_NEWAPI_REQUESTS = "50"
```

## Install and start

Requirements: Python 3.12+, Node.js 20.19+, and a NewAPI key only for
model-backed runs.

```powershell
cd backend
python -m pip install -e ".[dev]"

cd "..\frontend"
npm.cmd install
cd ..
```

Configure or replace the NewAPI key:

```bat
start.cmd --configure-newapi
```

The command validates `/v1/models`, stores a Windows-user-encrypted credential,
and refreshes the local model/capability catalogs. Restart a running backend
after changing credentials.

Start the application:

```bat
start.cmd
```

The UI opens at `http://127.0.0.1:5173`. Use `start.cmd --check` for a read-only
prerequisite and credential check.

The adapter requires exactly one authorized function call, disables parallel
tool calls, and gives an invalid completion one structured repair attempt.
Model output limits are calibrated once and cached locally; the confirmed model
limit is sent directly, without staircase growth. The NewAPI key remains only
in the backend process.

## Runs, recovery, and data

The selected view is stored in `?run=...&week=...`. The UI always offers 52 week
buttons; selecting one loads its Monday-Sunday frames. Stopped and interrupted
runs retain their journal and atomic checkpoint and can resume under the same
run ID. Failed runs are terminal. Protocol-invalid runs may retain a diagnostic
score but are excluded from benchmark ranking.

Mutable state is Git-ignored:

```text
.dairy-bench/
|-- credentials/
|   |-- newapi-model-capabilities.json
|   `-- newapi-models.json
`-- data/runs-v6.sqlite3
```

Override the runtime root only when necessary with `DAIRY_BENCH_HOME`.

## Verification

```powershell
cd backend
python -m pytest -q -p no:cacheprovider
python -m ruff check src tests

cd "..\frontend"
npm.cmd run build
```

## HTTP interfaces

- `GET /api/policy-profiles`
- `POST /api/runs`
- `GET /api/run-jobs`
- `GET /api/run-jobs/{run_id}`
- `POST /api/run-jobs/{run_id}/stop`
- `POST /api/run-jobs/{run_id}/resume`
- `GET /api/runs/{run_id}`
- `GET /api/runs/{run_id}/timeline?week={week}`
- `GET /api/runs/{run_id}/timeline/{entry_id}`
- `GET /api/runs/{run_id}/turns`
- `GET /api/runs/{run_id}/invocations`

See [Architecture and invariants](docs/ARCHITECTURE.md) and
[Agent integration](docs/AGENT_INTEGRATION.md) for the complete contracts.
