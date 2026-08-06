# Dairy Bench

Dairy Bench is an event-driven multi-agent benchmark for a perishable dairy
supply chain. Three farms, three processors, and three retailers share two spot
markets; each of the nine companies is controlled by an independent agent.

The default scenario is `flow.dairy.base.s9.v3`. An episode lasts 30 simulated
days, and every decision is one strongly typed atomic command:

```text
Wake -> AgentTurn -> one CompanyCommand -> EconomyEngine -> Journal -> next Wake
```

Only the deterministic economy engine may mutate cash, inventory, orders, jobs,
deliveries, or trade results. Natural-language text never settles a transaction.

## V3 at a glance

- Continuous fully collateralized limit-order books for raw and bottled milk.
  Crossing orders trade immediately with price-time priority, the maker's price,
  and partial fills. In one model call, an agent may set a target ladder of up
  to three independent price levels for one product and side.
- Bids reserve their full limit-price cash commitment; asks reserve exact FEFO
  inventory lots. A ladder update either reconciles every level atomically or
  leaves the original orders and collateral unchanged.
- Order quantities use a fixed `0.0001` market tick. Non-positive, dust, or
  over-precision quote levels are rejected without rounding.
- Same-minute agent calls run concurrently, then commands commit in a persisted
  `SHA256(seed | minute | company)` order. Provider response latency is audited
  but cannot change the economic result.
- `produce` and `transform` occupy the company's physical resource for 30 virtual
  minutes. Market and retail-price commands remain instantaneous, subject to the
  one-decision-per-virtual-minute throttle.
- A trade pays the seller immediately and schedules automatic buyer delivery 30
  minutes later. There is no manual dispatch, route, carrier, or escrow workflow.
- Each agent sees every anonymous aggregated price level, its own queue-aware
  orders, expiry-aware available and reserved assets, inbound deliveries, its
  active operation, and remaining daily operation capacity.
- Every company has private token-budgeted memory. Immutable journals, atomic
  checkpoints, crash recovery, and exact replay remain the durable authority.

The daily clock is:

| Time | Event |
|---|---|
| 09:00 | Open both markets and wake all companies |
| 09:00-18:59 | Accept company decisions and continuously match orders |
| 19:00 | Complete due jobs and deliveries, close books, release DAY-order holds, then run consumer sales |
| 19:00-19:29 | Process only previously committed completions and deliveries |
| 19:30 | Expire inventory and commit the end-of-day snapshot |

See [V3 architecture and invariants](docs/V3_ARCHITECTURE.md) and
[Agent integration](docs/AGENT_INTEGRATION.md) for the full contract.

## System outline

```text
React -> FastAPI -> RunCoordinator -> EpisodeRuntime -> Scheduler + EconomyEngine
                         |                 `-> Evaluator
                         |-> PolicyFactory -> CompanyAgent x 9
                         `-> LifecycleRepository -> Journal + Checkpoint + SQLite
```

Agents may use the rule baseline, Codex, OpenAI, or exact replay. OpenAI uses
native function tools; Codex uses an equivalent strict structured-output
adapter. Every provider path validates into the same Pydantic command union.

## Requirements and startup

- Python 3.12+
- Node.js 20.19+
- Codex login or an OpenAI API key only for the corresponding agent mode

Install once from the repository root:

```powershell
cd backend
python -m pip install -e ".[dev]"

cd "..\frontend"
npm.cmd install
cd ..
```

Start FastAPI and Vite:

```powershell
.\start.cmd
```

The launcher opens `http://127.0.0.1:5173`. The rule baseline needs no model
credentials.

The page automatically opens a newly submitted or active run. The current run
and day live in `?run=...&day=...`, so refresh and browser navigation preserve
the view. Selecting `Exact Replay` replaces the seed with a source dropdown that
lists every completed run from newest to oldest. Choosing a source opens its
persisted results and timeline; starting the replay makes no model calls and
verifies deterministic equality.

While a run is active, the same primary action becomes `Stop run`.
Stopping is permanent: committed journals, checkpoints, and timeline evidence
remain readable, but the run receives no final score, cannot resume, and is not
an Exact Replay source.

## Model-backed agents

`start.cmd` uses the repository-owned `.dairy-bench/codex` directory instead
of `%USERPROFILE%\.codex`. Each company receives an independent runtime and
private memory; every Codex turn uses an isolated thread. Public reasoning
summaries and final structured outputs are exported under:

```text
run_artifacts/<run_id>/
|-- reasoning/
`-- final_outputs/
```

For OpenAI agents, set the key in the shell that starts the backend:

```powershell
$env:OPENAI_API_KEY="your-key"
$env:DAIRY_BENCH_OPENAI_MODEL="gpt-5.6-terra"  # optional

python -m uvicorn company_bench.web:create_app --factory --host 127.0.0.1 --port 8000
```

Keys never enter the browser, journal, or benchmark database. Provider
infrastructure failure fails the run rather than fabricating an economic action.
Invalid structured output becomes an explicit protocol rejection with no
economic mutation.

## Data and verification

The default score-v7 database is `backend/data/dairy_bench_v7.sqlite3`. Override it with
`DAIRY_BENCH_DB`; override artifacts with `DAIRY_BENCH_ARTIFACTS_DIR`.

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
- `GET /api/runs/{run_id}/invocations/{invocation_id}/artifacts`
- `GET /api/runs`

## Design documentation

- [V3 architecture and invariants](docs/V3_ARCHITECTURE.md)
- [V3 Agent integration](docs/AGENT_INTEGRATION.md)
- [Historical V2 architecture](docs/V2_ARCHITECTURE.md)
- [Historical V1 MVP framework](docs/MVP_FRAMEWORK.md)
- [Historical V1 scenario catalog](docs/SCENARIO_CATALOG_V1.md)
