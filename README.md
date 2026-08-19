# Dairy Bench

[English](README.md) | [简体中文](README.zh-CN.md)

Dairy Bench is an event-driven benchmark for long-horizon business decisions in
a perishable dairy supply chain. Nine independent companies—three farms, three
processors, and three retailers—must coordinate production, spot trading,
inventory, pricing, and cash without sharing private state.

The current and only supported scenario is `flow.dairy.base.s9.v9`: 52 weekly
settlements, deterministic physical rules, independent company Agents, a shared
consumer market, durable recovery, and auditable enterprise scoring.

## Quick start

Requirements:

- Windows 10/11
- Python 3.12+
- Node.js 20.19+
- a NewAPI key only for model-backed profiles

Install the backend and frontend:

```powershell
cd backend
python -m pip install -e ".[dev]"

cd "..\frontend"
npm.cmd install
cd ..
```

Configure one or more isolated NewAPI profiles:

```bat
start.cmd --configure-newapi model
start.cmd --configure-newapi codex
start.cmd --configure-newapi claude-code
```

Each command validates `/v1/models`, stores the key with Windows user-scoped
encryption, and refreshes that profile's model and capability catalogs. Keys
remain backend-only and are never written to run journals or SQLite.

Start both processes:

```bat
start.cmd
```

The dashboard opens at `http://127.0.0.1:5173`. Run `start.cmd --check` for a
read-only dependency and profile check.

## Policy profiles

| Profile | Controller | Model protocol |
|---|---|---|
| Rule baseline | Deterministic in-process rules | None |
| Model agents via NewAPI | One isolated model Agent per company | Chat Completions |
| Codex via NewAPI | Dairy Bench remains the Agent runtime | Responses |
| Claude Code via NewAPI | Dairy Bench remains the Agent runtime | Anthropic Messages |
| Exact Replay | Replays a completed V9 Turn Journal | None |

All model traffic goes through NewAPI. The protocol-specific profiles are wire
adapters; the benchmark does not launch Codex CLI, Claude Code, or a vendor
Company Agent runtime. Every model completion must contain exactly one
authorized typed function call.

## One trading week

The simulation unit is one day:

| Day | Runtime behavior |
|---|---|
| Monday | Open the week, realize private capacity and cost conditions, wake companies |
| Tuesday–Saturday | Finish due work and deliveries, then process company decisions |
| Sunday | Finish commitments, close spot markets, collect sealed retail prices, settle shared consumer sales, charge store costs, expire inventory, check bankruptcy, issue reports, and snapshot |

Each active company can receive at most one operating call per day and six per
week. Every active retailer receives one additional sealed Sunday pricing call
after all procurement and deliveries. Competing retailers see the same
pre-pricing state and learn the three prices only after all decisions commit.

Production, processing, and delivery each take one simulated day. Raw milk
expires after two weekly settlements; bottled milk expires after four.

## V9 economy and information

The default physical scale is intentionally aligned:

- three farms each have normal weekly capacity `60`;
- three processors each accept normal weekly input `50` and yield `0.8`, for
  aggregate normal bottled output `120`;
- the three normal consumer cohorts also total `120` units;
- each active retailer accrues a fixed weekly store cost of `5.0000`.

Capacity and convex unit cost are realized privately for every productive
company-week. Retail demand is one finite shared market, not three independent
demand curves. Three hidden willingness-to-pay cohorts buy the cheapest
eligible inventory first, spill to the next retailer after a stockout, and split
equal prices deterministically. Persistent slump, normal, and boom regimes alter
market size, cohort composition, and willingness to pay.

Agents know the market rules, regime names, 6–10 week duration, and alternation
structure. They do not receive exact cohort sizes, willingness-to-pay values,
regime multipliers, purchasing-power shifts, current regime, or switch dates.
Each Agent receives its own last eight weekly reports; every Agent also receives
the last eight public retail reports containing prices, sales, market shares,
stockout flags, and company status.

## Determinism, concurrency, and recovery

Independent `RunJob`s execute concurrently. Within a run, Agents woken on the
same day observe the same base state and may infer concurrently. Accepted
decisions apply serially in the persisted order derived from:

```text
SHA256(seed | absolute_day | company_id)
```

Provider latency therefore cannot change economic ordering. The default ceiling
is 100 concurrent runs and 100 concurrent NewAPI requests. Gateways with lower
limits can override both values:

```powershell
$env:DAIRY_BENCH_MAX_CONCURRENT_RUNS = "20"
$env:DAIRY_BENCH_MAX_CONCURRENT_NEWAPI_REQUESTS = "50"
```

Journal entries and checkpoints commit atomically. A stopped or interrupted run
can resume under the same run ID; failed runs are terminal. Exact Replay remains
model-free and rejects observation, decision, outcome, or lineage drift.

## Scoring

After every completed week, running, stopped, interrupted, and failed runs expose
the same provisional score, company table, usage summary, and trend panels as a
completed run. Only fully settled weeks are included.

For nine companies, the official enterprise score is:

```text
Score = 100 × E × sqrt(F × P)

E = clamp(total realized enterprise surplus / feasible Oracle surplus, 0, 1)
F = 1 - global Gini(final enterprise values) / (8/9)
P = 1 - L/9
```

`L` is the number of companies with strictly negative final surplus. A company
with zero surplus is non-loss-making. `D` is reported separately as the number
of bankrupt companies; bankruptcy occurs when total assets are strictly below
`1.0000` at Sunday settlement.

The deterministic Oracle uses the realized capacities, convex costs, processing
yield, shelf lives, shared consumer market, and mandatory store costs. It
maximizes total **enterprise** surplus and deliberately excludes consumer
surplus.

## Local data

Mutable state is Git-ignored:

```text
.dairy-bench/
|-- credentials/
|   |-- newapi-model/{token.clixml,models.json,model-capabilities.json}
|   |-- newapi-codex/{token.clixml,models.json,model-capabilities.json}
|   `-- newapi-claude-code/{token.clixml,models.json,model-capabilities.json}
`-- data/
    |-- oracle-v2/
    `-- runs-v9.sqlite3
```

Set `DAIRY_BENCH_HOME` only when a different runtime root is required. This
release intentionally has no migration or reader for databases from earlier
scenarios or payload contracts; start with an empty `data` directory.

## Project map

```text
backend/src/company_bench/
|-- agents/       policy adapters, memory, and NewAPI protocols
|-- domain/       typed scenario, company, event, and precision contracts
|-- economy/      engine, markets, ledger, reports, valuation, scoring, Oracle
|-- runs/         run lifecycle and prefix evaluation
|-- runtime/      deterministic scheduler and episode orchestration
|-- storage/      repository seam plus in-memory and SQLite adapters
|-- timeline/     journal-derived read models and market reconstruction
`-- web/          FastAPI adapter

frontend/src/
|-- app/          composition and run workspace state
|-- features/     runs, market, timeline, and evaluation views
`-- shared/       typed API client, formatting, labels, and reusable UI
```

The core write path is deliberately singular:

```text
Wake → AgentTurn → one CompanyDecision → EconomyEngine → Journal → next Wake
```

Only `EconomyEngine` mutates economic state. Model text and UI projections never
settle transactions.

## Verification

```powershell
cd backend
python -m pytest -q -p no:cacheprovider
python -m ruff check src tests

cd "..\frontend"
npm.cmd run build

cd ..
start.cmd --check
```

## HTTP interfaces

- `GET /api/health` — API, scenario, score, database, and Journal contract identity
- `GET /api/policy-profiles`
- `POST /api/runs`
- `GET /api/run-jobs`
- `GET /api/run-jobs/{run_id}`
- `POST /api/run-jobs/{run_id}/stop`
- `POST /api/run-jobs/{run_id}/resume`
- `GET /api/replay-sources`
- `GET /api/runs/{run_id}` — completed episodes only
- `GET /api/runs/{run_id}/evaluation`
- `GET /api/runs/{run_id}/timeline?week={week}`
- `GET /api/runs/{run_id}/timeline/{entry_id}`
- `GET /api/runs/{run_id}/turns`
- `GET /api/runs/{run_id}/invocations`

For complete contracts, see:

- [Architecture and invariants](docs/ARCHITECTURE.md)
- [Agent integration](docs/AGENT_INTEGRATION.md)
- [Frontend guide](frontend/README.md)
