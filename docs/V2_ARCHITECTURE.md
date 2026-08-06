# Dairy Bench V2 Architecture

V2 replaces the daily company plan with a closed, event-driven company turn:

```text
Wake → AgentTurn → one CompanyCommand → CommandOutcome → TurnRecord
```

A day still defines market opening, two clearing sessions, consumer sales,
expiration, and scoring. It no longer defines how often an agent may think.

## Deep modules

### `EpisodeRuntime`

The runtime exposes one main operation, `run()`, while owning the complete
simulation protocol:

- advance one monotonic virtual-minute clock;
- execute system events before company turns at the same minute;
- track an independent `available_at` for every company;
- delay and merge wakes that arrive while a company is busy;
- build all same-minute observations from one base `state_version`;
- query company agents concurrently;
- commit commands in a seeded, deterministic order;
- append the journal and replace the checkpoint atomically; and
- evaluate the completed episode.

### `EconomyEngine`

The engine is the only writer of cash, inventory, orders, production, trades,
and retail outcomes. Agents and model providers may propose commands; they
cannot call state mutation methods directly.

`EconomyState` is a serializable intraday state containing company accounts,
standing orders, used capacity, retail prices, market status, and current-day
events. The V1 `step()` path remains available only for reading and validating
legacy scenarios.

### `ConversationMemory`

Every company owns an independent memory instance:

- current operating facts are projected fresh from `EconomyState`;
- recent history keeps complete Turn/Command/Outcome cycles;
- capacity is governed by a token budget rather than a fixed number of days;
- old cycles become deterministic summaries carrying a `source_hash`;
- the complete provider request has a separate estimated prompt-token limit;
- summaries help the agent reason but never become authoritative economic
  state.

### `RunTimelineProjector`

The projector is the read-side module behind the Operations Timeline. It joins the
authoritative turn journal, system steps, provider audits, replay lineage, and
exported public artifacts into one typed timeline. The browser does not
reconstruct causality or infer model calls from timestamps.

The projector also keeps source and replay accounting separate: a replay makes
zero provider calls, while source-run traces and token usage remain available
as provenance.

## Time protocol

The default `flow.dairy.base.s12.v2` scenario schedules:

| Time | System step |
|---|---|
| 09:00 | Open markets and wake every company |
| 11:00 | Clear the raw-milk market |
| 16:00 | Clear the bottled-milk market |
| 19:00 | Execute consumer sales, expiration, and the end-of-day snapshot |

A normal command occupies a company for 30 virtual minutes. `wait` is not
polled. A company wakes only at its explicit deadline, the next market opening,
or another material system event. Each company has a daily turn cap of 20.

Each company has one valid future action chain. An external wake received
during cooldown moves to `available_at` and merges with any existing wake.
When another event wakes the company earlier, its prior wait or continuation
timer is cancelled.

Within one virtual-minute bucket:

1. system steps run first;
2. multiple wake reasons for one company are merged;
3. every agent observes the same base state version;
4. model queries run concurrently; and
5. commands commit in stable replayable order.

Provider response speed therefore cannot change the economic result.

## Command protocol

Each turn permits exactly one role-authorized atomic command:

- `Produce`
- `Transform`
- `SetQuoteLadder`
- `SetRetailPrice`
- `Wait`

`SetQuoteLadder` uses one command to set zero to three independent target
price-quantity levels for one product and side. The same deterministic
Keep/Replace/Cancel/Place reconciliation and all-or-nothing collateral rule
described by the current market contract apply; an empty ladder withdraws all
quotes on that product side.

The runtime binds `turn_id`, `company_id`, `sim_time`, and `state_version`.
Models cannot supply or forge those fields.

The OpenAI adapter uses provider-native function tools with:

```text
tool_choice = required
parallel_tool_calls = false
max_tool_calls = 1
```

The Codex adapter uses the same Pydantic command union in a strict structured
envelope. Both paths yield exactly one `CompanyCommand`. No tool call, multiple
calls, an unknown tool, or invalid arguments become an explicit protocol
rejection; they never silently become a successful `wait`. A protocol
rejection leaves the economy unchanged, consumes the normal turn duration, and
schedules `CONTINUE` so the agent can correct its output, subject to the daily
turn cap and business-hours boundary.

## Journal, checkpoints, and replay

The V2 turn journal records:

- the exact `AgentTurn` observation;
- the runtime-bound `CommandEnvelope`;
- the engine `CommandOutcome`;
- the observation hash;
- base and resulting state versions;
- the global apply sequence; and
- the original protocol error, when no valid command was produced.

The versioned `RunCheckpoint` contains:

- `EconomyState`;
- `SchedulerCheckpoint`;
- every company's `PolicyDescriptor` and `AgentCheckpoint`;
- committed turns, events, and snapshots;
- company turn/event cursors and `available_at` values; and
- the episode's original `started_at`.

New journal records and the replacement checkpoint commit in one repository
transaction. Recovery therefore restores the world, clock, and agent memories
to the same atomic boundary. Before resuming, the runtime compares provider,
model, prompt version, and configuration fingerprint to prevent one episode
from silently mixing policies.

A replay agent consumes the source turn journal in order. It verifies the
current observation hash, returns the source command or source protocol
rejection, and compares every resulting outcome. Before scoring, it verifies
that the source stream is exhausted and that final events, snapshots, and score
are identical. It creates no model gateway and makes no provider calls. Any
economic, visibility, or scheduling drift fails fast.

## Operations Timeline

The Operations Timeline is turn-first rather than event-first. It groups one day into
virtual-minute moments and presents:

- system steps at that minute;
- merged wake reasons;
- same-base-version concurrent observations;
- deterministic command apply order;
- typed acceptance or rejection outcomes;
- direct economic effects and the next scheduled wake; and
- provider trace or source-run replay provenance.

Direct command effects are nested under their turn. Market clearing, consumer
sales, expiration, and end-of-day state changes are shown as system steps. This
avoids displaying the same economic action twice.

No-effect waits are displayed by default and remain fully auditable. Company,
status, and command filters still apply.
Entry details are fetched lazily so a 30-day run does not require every trace
payload up front.

## Information boundary

A company observation contains only:

- its own cash, aggregated inventory, and retail price;
- its own standing orders;
- public scenario rules and the previous day's market summary;
- events visible to that company since its previous turn; and
- its previous outcome and private memory.

Private events return only to the affected company. Trades are visible only to
the buyer and seller. Another company's cash, inventory, commands, provider
transcript, and memory never enter the current agent's input.

## Required invariants

- Simulation time is monotonic; cooldown cannot schedule an event in the past.
- A company has at most one outstanding turn at a given time.
- A turn applies at most one command.
- Every command in one time bucket shares the same base `state_version`.
- Only `EconomyEngine` mutates economic state.
- The same seed and command stream produce identical events, snapshots, and
  scores.
- Replay makes zero provider calls.
- Checkpoint recovery matches uninterrupted execution.
- Memory compaction never splits a command/outcome cycle.
- A company cannot observe another company's private facts.
- Timeline projections never call a model or mutate the simulation.

Scheduler, runtime, memory, repository, projector, and end-to-end tests enforce
these boundaries.
