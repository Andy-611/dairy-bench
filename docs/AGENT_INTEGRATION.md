# Agent Integration

## V3 agent contract

Dairy Bench runs nine independent company agents: three farms, three
processors, and three retailers. The scheduler wakes an agent for one closed,
auditable decision cycle:

```text
Wake
  -> AgentTurn(authoritative private observation)
  -> exactly one CompanyCommand
  -> EconomyEngine validation and CommandOutcome
  -> immutable TurnRecord
```

An agent does not submit a daily plan and never supplies its own identity,
timestamp, state version, command ID, or turn ID. `EpisodeRuntime` binds those
fields in `CommandEnvelope`.

## Structured command format

The model must choose exactly one role-authorized command:

| Command | Purpose |
|---|---|
| `produce(product, quantity)` | Start one farm production job |
| `transform(input_product, output_product, input_quantity)` | Start one processor conversion job |
| `place_order(side, product, quantity, limit_price)` | Submit a collateralized limit order |
| `replace_order(order_id, quantity, limit_price)` | Atomically replace an owned order and lose its old priority |
| `cancel_order(order_id)` | Cancel an owned resting order and release its remaining hold |
| `set_retail_price(product, unit_price)` | Set a retailer's consumer price |
| `wait(until?)` | Yield until a deadline or another relevant event |

Farms may produce and trade raw milk. Processors may transform and trade raw or
bottled milk. Retailers may trade bottled milk and set its consumer price.

For `place_order` and `replace_order`, `quantity` must be positive and an exact
multiple of `0.0001` (at most four decimal places). The engine rejects the whole
command rather than rounding it; agents should use `wait` instead of submitting
dust quantities.

OpenAI uses Responses API function tools with one required, non-parallel tool
call. Codex uses a strict structured-output envelope. Both validate against the
same discriminated Pydantic `CompanyCommand` union. Missing, multiple, unknown,
unauthorized, or malformed calls become explicit protocol rejections; they do
not mutate the economy.

## What an agent observes

`AgentTurn` contains only runtime-authorized facts for one company:

- simulation time, state version, daily turn number and limit, typed wake reasons,
  and causal references;
- available cash and inventory, plus retail price where applicable;
- cash and FEFO inventory reserved by its own resting orders;
- its own open orders, including remaining quantity and priority sequence;
- anonymous `MarketView` values for relevant products: best bid and ask, top
  three aggregated bid/ask levels, last trade price, and daily volume;
- guaranteed inbound deliveries with product, quantity, exact arrival time, and
  quantity-preserving expiry buckets;
- the active production or transformation job, if any, and remaining daily
  operation capacity;
- company-visible domain events and the previous command outcome; and
- that company's private bounded memory context.

An agent never sees another company's identity in the public order book,
private assets, memory, prompt, or provider trace. The engine, not the prompt,
is the source of truth.

## Market and time semantics

Both product markets use continuous fully collateralized limit books:

1. Order quantity is a positive exact multiple of `0.0001`; invalid precision
   rejects the whole place/replace command without rounding.
2. A bid reserves `quantity * limit_price`; an ask reserves exact FEFO lots.
3. Insufficient available cash or inventory rejects the whole new order.
4. A new order crosses while `best_bid >= best_ask`.
5. Better prices win; equal prices use the persisted arrival priority.
6. The execution price is the resting maker order's price.
7. A fill may consume only part of either order. The remainder keeps its
   priority; an explicit replace receives a new order ID and priority.
8. Buyer price improvement is released immediately. Cancellation or 19:00
   market close releases every unfilled hold.

Every fill pays the seller immediately and creates one automatic delivery to
the buyer 30 virtual minutes later. Until arrival, those lots are visible as
inbound but cannot be transformed, sold, or reserved again. This is deliberately
not a logistics workflow: there is no dispatch command, routing, carrier,
capacity, delay, failure, or escrow state.

`produce` and `transform` also complete after exactly 30 virtual minutes. One
company may have only one active physical operation, but that job does not block
market, wait, or retail-price commands. Starting a job consumes its cash and,
for transformation, input inventory; output becomes available only at completion.

The business window is `[09:00, 19:00)`. Commands have no economic execution
delay. Except for an accepted `wait`, each decision starts a 30-minute Agent
cooldown. The runtime allows at most ten turns per company per day. At 19:00,
due operation and delivery completions run first, then markets close, then
consumer sales run.
Previously committed completions may drain until 19:29; day close occurs at
19:30.

`wait` is an attention plan rather than a polling action. It may declare up to
three anonymous quote conditions over visible `best_bid` or `best_ask` values;
conditions use fixed OR semantics. It may also provide an absolute same-day
fallback no more than 120 minutes away. Without one, the runtime schedules the
same 120-minute fallback when it remains before 19:00. Alerts are one-shot and
are evaluated only after all commands for a minute have committed; a match wakes
the company on the following minute. Duplicate, hidden, or already-true alerts,
and invalid fallback times, reject the whole command without changing the
economy. Own trades, operation completions, and delivery completions also wake
the affected company; a Wake arriving during a non-`wait` cooldown is retained
until the next eligible decision time. Generic book mutations are not broadcast,
and resting orders have no separate review timer. Reaching the daily cap writes an explicit
state-neutral audit step and suppresses further Agent calls for that day. Each
later Wake is still journaled with its typed causal signals.

## Deterministic concurrency

All agents woken in the same virtual minute observe the same base state version
and their provider calls run concurrently. Completion speed does not establish
economic priority. Before applying commands, the runtime sorts companies by:

```text
SHA256(seed | absolute_minute | company_id)
```

It then commits commands serially and persists the global `apply_sequence`.
Provider latency, retries, and token use remain invocation-audit metrics only.
This makes a run replayable and comparable without pretending network latency is
a business decision.

## Private memory

Each company owns one `ConversationMemory`, model client, and gateway lifecycle.
The provider request combines:

1. current authoritative `AgentTurn` facts;
2. recent complete Turn/Command/Outcome exchanges; and
3. a deterministic long-horizon summary of older complete exchanges.

Compaction is token-budget driven, not a fixed seven-day window. It never splits
a command from its outcome and requires no extra model call. The default
compaction trigger is 12,288 estimated tokens; the full prompt has an independent
16,384-token ceiling. If authoritative current facts cannot fit, the turn fails
explicitly rather than hiding business state.

Memory helps the agent reason but is not the economic authority. Complete
`TurnRecord` objects stay in the immutable journal even after prompt compaction.

## Journal, checkpoint, and replay

The turn journal records the exact observation, runtime-bound command, outcome,
`apply_sequence`, observation hash, causal references, and any protocol error.
System steps record job completion, delivery, market close, consumer sales, and
day close alongside their economic effects.

After each stable virtual-time bucket, new journal entries and the replacement
`RunCheckpoint` commit atomically. The checkpoint contains the typed economic
state, open books and collateral, pending jobs and deliveries, scheduler,
company availability, events, snapshots, memories, cursors, and fixed policy
fingerprints.

Recovery resumes only from that boundary and rejects provider, model, prompt,
scenario, or configuration drift. Exact replay creates no provider gateway: it
verifies every observation hash, reproduces each recorded command or protocol
rejection, compares every outcome and system step, and finally requires equal
events, snapshots, and score.

## Running Codex agents

From the repository root:

```powershell
.\start.cmd
```

The launcher sets `CODEX_HOME=.dairy-bench/codex`, isolated from the user's
personal `%USERPROFILE%\.codex`. All nine companies receive independent
`AsyncCodex` runtimes and each turn uses an isolated thread. The benchmark
runtime is read-only with shell, search, plugins, Codex memory, and multi-agent
features disabled.

After a turn, Dairy Bench exports the public reasoning summary and final
structured output, then archives the source session only after export succeeds:

```text
run_artifacts/<run_id>/
|-- reasoning/day-001__farm_a__turn-0001.md
`-- final_outputs/day-001__farm_a__turn-0001.json
```

The project never reads or stores `auth.json`, hidden chain-of-thought, or
encrypted reasoning content.

## Running OpenAI agents

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
or database. `DAIRY_BENCH_OPENAI_BASE_URL` may target a compatible service.

## Failure semantics

| Condition | Result |
|---|---|
| Missing or invalid model command | Protocol rejection; economy unchanged; correction may be attempted after the 30-minute decision interval |
| Role, collateral, ownership, capacity, or time rule fails | Typed engine rejection; run continues |
| Authentication or retry-exhausted provider failure | Entire run fails; no misleading score is emitted |
| Journal failure or runtime invariant violation | Current transaction rolls back and the run fails |

## Audit interfaces

```text
GET /api/run-jobs
GET /api/run-jobs/{run_id}
GET /api/runs/{run_id}/timeline?day={day}
GET /api/runs/{run_id}/timeline/{entry_id}
GET /api/runs/{run_id}/turns
GET /api/runs/{run_id}/invocations
GET /api/runs/{run_id}/invocations/{invocation_id}/artifacts
```

`turns` is the authoritative business journal. `invocations` audits provider
calls, latency, and tokens. `timeline` is a causal human-readable projection.
Exported `artifacts` are source evidence and never trigger another model call.
Run-history projection is available for every lifecycle status; a missing final
episode or score never hides already committed journal and checkpoint evidence.
Exact replay remains a separate completed-run verification operation.

## Adding another provider

A V3 adapter implements:

```python
class CommandGateway(Protocol):
    async def generate_command(
        self,
        request: CommandModelRequest,
    ) -> CommandModelResult: ...

    async def close(self) -> None: ...
```

`PolicyFactory` creates one gateway per company. The adapter validates output
into an authorized `CompanyCommand`, maps content failures to
`ModelOutputError`, and maps infrastructure failures to
`ModelInfrastructureError`. It must not access `EconomyEngine` or another
company's state.
