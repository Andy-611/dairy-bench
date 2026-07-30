# Agent Integration

## V2 agent contract

Dairy Bench runs six companies and gives each company one independent agent.
An agent no longer submits one complete daily plan. It completes a closed cycle
whenever the scheduler wakes it:

```text
Wake
  → AgentTurn(current facts, visible events, private memory)
  → one atomic CompanyCommand
  → EconomyEngine validation and CommandOutcome
  → immutable TurnRecord
```

A day bounds market opening, clearing, consumer sales, expiration, and scoring;
it does not bound provider calls.

The default system schedule is:

| Time | Event |
|---|---|
| 09:00 | Open markets and wake all companies |
| 11:00 | Clear the raw-milk market |
| 16:00 | Clear the bottled-milk market |
| 19:00 | Run consumer sales, expiration, and the end-of-day snapshot |

A normal command occupies the company for 30 virtual minutes. Wakes received
during cooldown are delayed to `available_at` and merged, preserving one action
chain per company. The default daily cap is 20 turns per company.

## Model submission format

The model must select exactly one command authorized for its company role:

- `produce`
- `transform`
- `place_order`
- `cancel_order`
- `set_retail_price`
- `wait`

The runtime—not the model—binds `turn_id`, `company_id`, simulation time, and
`state_version`.

The OpenAI adapter uses Responses API function tools with:

```text
tool_choice = required
parallel_tool_calls = false
max_tool_calls = 1
```

The Codex adapter uses a strict structured-output envelope backed by the same
Pydantic command union. Both provider paths normalize into
`CompanyCommand`. Missing calls, multiple calls, unknown tools, and invalid
arguments become explicit protocol rejections and do not mutate the economy.
A protocol rejection consumes one turn and the normal 30-minute duration, then
schedules `CONTINUE` so the agent can see the error and submit a correction.

## Agent memory

Every company owns an independent `ConversationMemory`. Its request context has
three layers:

1. current cash, inventory, retail price, and standing orders projected from
   `EconomyState`;
2. company-visible events since the previous turn plus the previous outcome;
3. recent complete Turn/Command/Outcome cycles and a deterministic long-term
   summary.

Old cycles compact by estimated token count, never by splitting a command from
its outcome. The default memory budget begins compaction around 12,288 tokens,
while the complete request has an independent estimated ceiling of 16,384. If
current authoritative facts alone exceed the ceiling, the turn fails
explicitly instead of silently removing business facts.

Summaries require no additional model call and are not an economic source of
truth. Complete `TurnRecord` objects remain in the journal permanently;
provider threads are not the benchmark's memory authority.

## Journal, recovery, and replay

The turn journal stores:

- the exact agent observation;
- the runtime-bound command;
- the engine outcome and global `apply_sequence`;
- the observation hash; and
- any explicit protocol error.

After each stable time bucket, new journal records and the replacement
`RunCheckpoint` commit in one transaction. The checkpoint contains the
economic state, scheduler, pending events, company availability, fixed policy
metadata, agent memories, cursors, events, snapshots, and the original episode
start time.

Recovery continues at that atomic boundary. It rejects provider, model, prompt,
or configuration fingerprint drift instead of mixing policies in one run.

Replay verifies each observation hash, reproduces the source command or source
protocol rejection, and compares every outcome. It then verifies that the
source stream is exhausted and final events, snapshots, and score are exactly
equal. Replay creates no provider gateway and makes zero model calls.

`source_run_id` remains in policy metadata and timeline provenance. The
Operations Replay UI may display exported source traces and source token usage,
but labels them as source evidence rather than replay activity.

## Running Codex agents

From the repository root:

```powershell
.\start.cmd
```

The launcher sets `CODEX_HOME=.dairy-bench/codex`, isolated from the user's
personal `%USERPROFILE%\.codex`. First launch may request a separate login.
Backend validation refuses to use the personal Codex home directly.

All six companies receive independent `AsyncCodex` runtimes, and each turn uses
an isolated thread. The runtime is read-only, uses `deny_all` approval, and
disables shell, search, plugins, Codex memory, and multi-agent features.

After a turn completes, Dairy Bench:

1. reads the original session JSONL from `CODEX_HOME/sessions`;
2. exports the public reasoning summary and final structured output; and
3. archives the session only after a successful export.

```text
run_artifacts/<run_id>/
├── reasoning/day-001__farm_a__turn-0001.md
└── final_outputs/day-001__farm_a__turn-0001.json
```

The project never reads, copies, or stores `auth.json`. It does not expose
hidden chain-of-thought or decrypt `encrypted_content`.

Archived source sessions are retained for 60 days by default, with at least the
three most recent runs preserved. Cleanup only removes sessions that are inside
the isolated archive, have a strict Dairy Bench title, exceed
`DAIRY_BENCH_CODEX_SESSION_RETENTION_DAYS`, and are outside
`DAIRY_BENCH_CODEX_SESSION_MIN_RUNS`. Exported `run_artifacts` are long-lived
audit records and are not governed by source-session retention.

## Running OpenAI agents

From the repository root:

```powershell
cd backend
$env:OPENAI_API_KEY="your OpenAI API key"

# Optional
$env:DAIRY_BENCH_OPENAI_MODEL="gpt-5.6-terra"
$env:DAIRY_BENCH_OPENAI_REASONING_EFFORT="medium"
$env:DAIRY_BENCH_OPENAI_MAX_OUTPUT_TOKENS="2048"
$env:DAIRY_BENCH_OPENAI_TIMEOUT_SECONDS="60"
$env:DAIRY_BENCH_OPENAI_MAX_ATTEMPTS="3"

python -m uvicorn company_bench.web:create_app --factory --host 127.0.0.1 --port 8000
```

Only the backend reads `OPENAI_API_KEY`; it never enters the browser, journal,
or database. OpenAI-compatible services may be configured with
`DAIRY_BENCH_OPENAI_BASE_URL`.

## Runtime and failure semantics

```text
React
  → FastAPI / RunCoordinator
  → EpisodeRuntime
  → CompanyAgent.act() concurrently
  → EconomyEngine.apply_batch() in deterministic order
  → Turn Journal + Checkpoint
  → Evaluator
```

| Condition | Result |
|---|---|
| Missing or invalid model command | Protocol rejection; economy unchanged; `CONTINUE` after the normal command duration |
| Role, cash, inventory, or state rule fails | Typed engine rejection; run continues |
| Authentication, retry-exhausted network, rate-limit, or provider outage | Entire run fails; no score is emitted |
| Journal failure or runtime invariant violation | Current transaction rolls back and the run fails |

All same-minute agents observe the same base `state_version`. Their requests
may finish in any order, but commands apply in a seeded stable order.

## Audit interfaces

```text
GET /api/runs/{run_id}/timeline?day={day}
GET /api/runs/{run_id}/timeline/{entry_id}
GET /api/runs/{run_id}/turns
GET /api/runs/{run_id}/invocations
GET /api/runs/{run_id}/invocations/{invocation_id}/artifacts
```

`turns` is the authoritative business journal. `invocations` audits provider
calls, tokens, and latency. `timeline` projects causal, human-readable moments.
`artifacts` returns previously exported public Codex files and never calls a
model.

`invocation_id` identifies one real provider call; `domain_turn_id` identifies
one deterministic economic turn. If a process stops after a provider response
but before checkpoint commit, recovery creates a new invocation ID while
retaining the earlier call and its audit data.

## Adding another provider

A V2 adapter implements:

```python
class CommandGateway(Protocol):
    async def generate_command(
        self,
        request: CommandModelRequest,
    ) -> CommandModelResult: ...

    async def close(self) -> None: ...
```

`PolicyFactory` creates a new gateway for each company. The adapter validates
provider output into an authorized `CompanyCommand`, maps content failures to
`ModelOutputError`, and maps network, authentication, and service failures to
`ModelInfrastructureError`. It must not access `EconomyEngine` or another
company's state.

## Verification

Tests do not require live model access:

```powershell
cd backend
python -m pytest --basetemp "..\.tmp\pytest"
python -m ruff check .

cd "..\frontend"
npm.cmd run build
cd ..
```

Coverage includes merged wakes, cooldown, the one-tool protocol, protocol
rejection replay, provider completion-order independence, information
isolation, atomic SQLite writes, exact checkpoint recovery, zero-call replay,
timeline provenance, and typed API parsing.
