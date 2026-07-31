# Dairy Bench

Dairy Bench is an event-driven multi-agent benchmark in which four farms, four
processors, and four retailers operate a shared perishable dairy supply chain.
Each company is controlled by one independent agent.

An episode lasts 30 simulated days. The decision unit is not a daily plan; it
is an atomic company turn:

```text
Wake → AgentTurn → one CompanyCommand → Engine Outcome → Journal → next Wake
```

Only the deterministic economy engine may change cash, inventory, orders, or
trade results. Agents can only submit strongly typed business commands.

## What is implemented

- Twelve heterogeneous companies, raw and bottled milk, FEFO inventory, and two
  spot markets.
- Independent rule, Codex, OpenAI, or exact-replay agents for every company.
- A virtual-minute clock, fixed market-clearing times, event-driven wakes,
  concurrent same-time inference, and deterministic serial command commits.
- Exactly one strongly typed atomic command per turn. OpenAI uses native
  function tools; Codex uses an equivalent strict structured adapter.
- Independent token-budget memory, model gateway, and model client lifecycles
  for all twelve agents.
- An immutable turn journal, atomic checkpoints, crash recovery, and replay
  without model calls.
- Background execution, 30-day progress polling, explicit failures, and full
  provider-call auditing.
- Efficiency, fairness, fulfillment, and waste metrics over a deterministic
  economy.
- SQLite persistence, a FastAPI backend, and an English React dashboard.
- A turn-first Operations Replay that joins wakes, observations, commands,
  outcomes, economic effects, system steps, and source-run traces.

```text
React → FastAPI → RunCoordinator → EpisodeRuntime → Scheduler + EconomyEngine
                         │                 └─ Evaluator
                         ├─ PolicyFactory
                         │   ├─ BaselineCompanyAgent
                         │   ├─ LlmCompanyAgent × 12 → Gateway × 12
                         │   └─ ReplayCompanyAgent × 12
                         └─ LifecycleRepository → Journal + Checkpoint + SQLite
```

## Requirements

- Python 3.12+
- Node.js 20.19+
- Codex login or an OpenAI API key only when using the corresponding agent

From the repository root, install dependencies once:

```powershell
cd backend
python -m pip install -e ".[dev]"

cd "..\frontend"
npm.cmd install
cd ..
```

Start both applications from the repository root:

```powershell
.\start.cmd
```

The launcher starts FastAPI and Vite, then opens
`http://127.0.0.1:5173`. The rule baseline works without model credentials.

## Codex agents

`start.cmd` uses the repository-owned `.dairy-bench/codex` directory and does
not write benchmark sessions into `%USERPROFILE%\.codex`. The first launch may
ask you to complete `codex login` for this isolated Codex home.

Each company receives an independent Codex runtime and each company turn uses
an isolated thread. Context consists of a deterministic summary plus recent
complete Turn/Command/Outcome cycles under a fixed token budget.

Every readable public reasoning summary and final structured output is exported
to:

```text
run_artifacts/<run_id>/
├── reasoning/
└── final_outputs/
```

Source traces remain auditable during replay. They are labelled as source-run
usage and are never counted as model calls made by the replay itself.

## OpenAI agents

Set the API key in the same PowerShell session that starts the backend:

```powershell
$env:OPENAI_API_KEY="your-key"
$env:DAIRY_BENCH_OPENAI_MODEL="gpt-5.6-terra"  # optional

python -m uvicorn company_bench.web:create_app --factory --host 127.0.0.1 --port 8000
```

The key is read only by the backend. It is never sent to the browser or stored
in the benchmark database.

Model-call volume depends on wakes and each agent's `wait` decisions, subject
to the configured per-company daily turn limit. Invalid model output becomes
an explicit protocol rejection without changing the economy, then receives a
`CONTINUE` wake after the normal command duration so it can be corrected.
Exhausted authentication, network, or provider failures fail the run instead
of producing a misleading score.

## Data and verification

The default database is:

```text
backend/data/dairy_bench.sqlite3
```

Override it with `DAIRY_BENCH_DB`. Override the artifact directory with
`DAIRY_BENCH_ARTIFACTS_DIR`.

From the repository root, run verification:

```powershell
cd backend
python -m pytest -q
python -m ruff check .

cd "..\frontend"
npm.cmd run build
cd ..
```

## Main HTTP interfaces

- `GET /api/policy-profiles` — list available policy modes and model profiles.
- `POST /api/runs` — create a background run and return a `202 RunJob`.
- `GET /api/run-jobs/{run_id}` — read lifecycle status and day progress.
- `GET /api/runs/{run_id}` — read a completed episode.
- `GET /api/runs/{run_id}/timeline?day={day}` — read one Operations Replay day.
- `GET /api/runs/{run_id}/timeline/{entry_id}` — read one detailed turn or
  system-step entry.
- `GET /api/runs/{run_id}/turns` — read the immutable raw turn journal.
- `GET /api/runs/{run_id}/invocations` — read provider-call audits.
- `GET /api/runs/{run_id}/invocations/{invocation_id}/artifacts` — read an
  exported Codex public reasoning summary and final output.
- `GET /api/runs` — list completed runs.

## Design documentation

- [Agent integration](docs/AGENT_INTEGRATION.md)
- [V2 architecture and invariants](docs/V2_ARCHITECTURE.md)
- [Historical V1 MVP framework](docs/MVP_FRAMEWORK.md)
- [Historical V1 scenario catalog](docs/SCENARIO_CATALOG_V1.md)
