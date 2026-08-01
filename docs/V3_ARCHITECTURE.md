# V3 Architecture and Invariants

## Purpose

V3 models twelve independent companies operating a continuous dairy spot
market. It adds credible intraday price discovery and physical lead times while
keeping the benchmark deterministic, auditable, and small enough to reason
about.

The active scenario is `flow.dairy.base.s12.v3`:

- four farms produce raw milk;
- four processors buy raw milk, transform it, and sell bottled milk; and
- four retailers buy bottled milk, set retail prices, and serve consumers.

V2 remains documented as historical behavior. V3 carries no legacy
compatibility adapter.

## Module boundaries

```text
CompanyAgent
    | one typed command
    v
EpisodeRuntime ---- Scheduler
    | deterministic batch order
    v
EconomyEngine ---- ContinuousSpotMarket ---- AssetLedger
    | typed state and events
    v
LifecycleRepository ---- Journal + Checkpoint + SQLite
    |
    `---- Evaluator / Timeline / Replay
```

Responsibilities are deliberately narrow:

- `CompanyAgent` chooses one authorized `CompanyCommand`; it cannot mutate state.
- `EpisodeRuntime` owns virtual time, wakes, concurrent inference, seed-derived
  application order, journal sequencing, and checkpoint boundaries.
- `EconomyEngine` owns business authorization, operations, deliveries, consumer
  settlement, inventory expiry, and typed projections.
- `ContinuousSpotMarket` owns reservation, price-time matching, partial fills,
  replacement, cancellation, and book close for one product.
- `AssetLedger` is the transaction-local authority for available cash and FEFO
  inventory. Both product markets share one ledger within an engine transaction.
- `LifecycleRepository` atomically persists journal additions and the replacement
  checkpoint; projections never become a second source of truth.

The design uses immutable Pydantic boundary models. Mutable matching work is
confined to a short transaction-local market session and frozen back into
`EconomyState` before returning.

## Daily event order

The command window is `[09:00, 19:00)`:

| Time | Ordered behavior |
|---|---|
| 09:00 | Open both markets, then wake all companies |
| 09:00-18:59 | Run event-driven decisions and continuous matching |
| 19:00 | Complete due operations and deliveries; close both books; settle consumer sales |
| 19:00-19:29 | Drain only operations and deliveries committed before close |
| 19:30 | Require empty books, jobs, and deliveries; expire lots; write snapshot |

The scheduler orders same-minute system events by explicit priority:

```text
operation/delivery completion -> market close -> consumer sales -> day close
```

Thus a retailer receiving goods exactly at 19:00 may sell them to consumers.
A trade at 18:59 arrives at 19:29 and cannot affect that day's 19:00 consumer
sale, but it is fully delivered before the 19:30 snapshot. No new company
decision is accepted at or after 19:00.

`RuntimeSpec` validates that the day-close boundary is late enough for the
longest operation or delivery started in the last command minute.

## Continuous spot market

Each product has one `MarketState`; both books open and close together. Only
plain DAY limit orders exist.

### Reservation

- Place and replace quantities cross the engine boundary only as a positive
  `OrderQuantity`, an exact multiple of `0.0001`. Invalid raw commands are
  rejected before a market session exists; they are never rounded and cannot
  mutate assets or replace an existing order.
- A bid removes `remaining_quantity * limit_price` from available cash and
  stores it on `BuyOrder`.
- An ask removes exact FEFO lots from available inventory and stores them on
  `SellOrder`.
- Production output, inbound deliveries, and lots already held by another ask
  are unavailable.
- If the entire requested commitment cannot be reserved, the command is rejected
  with no state change. There is no settlement-time clipping.

The private observation reports available and reserved assets separately. Total
economic ownership is therefore visible without allowing the same asset to back
two commitments.

### Matching

An incoming order repeatedly matches while `best_bid >= best_ask`:

1. better price has priority;
2. equal price uses the persisted `priority_sequence`;
3. the execution price is the resting maker order's limit;
4. execution quantity is the smaller remaining quantity; and
5. any remainder stays on the book with its existing priority.

Partial fill describes quantity interaction, not under-collateralization: every
remaining unit is still fully backed. When a bid executes below its limit, the
unused price difference returns to available cash immediately.

`replace_order` atomically releases the old commitment and attempts a new one
with a new identity and priority. If validation or reservation fails, the whole
transaction is discarded and the old order remains. Cancellation and 19:00 book
close release all unfilled collateral. Self-crossing orders are rejected.

### Trade and delivery

At a fill:

```text
buy reserve pays trade value
seller available cash increases immediately
price improvement returns to buyer
seller lots become one PendingDelivery owned by the buyer
30 minutes later those lots enter buyer available inventory
```

`PendingDelivery` is the minimum state needed to enforce lead time and exact
replay. It is not a logistics aggregate: there is no manual dispatch, escrow,
route, carrier, freight price, capacity, delay, loss, or failure transition. A
scheduled completion either occurs exactly once at `arrives_at` or the runtime
invariant fails.

## Physical operations

`ProductionJob` and `TransformationJob` are typed asynchronous commitments.
Starting either job:

- verifies role, daily capacity, idle resource, cash, and inputs;
- consumes cash immediately and transformation input inventory immediately;
- records one completion exactly 30 virtual minutes later; and
- exposes no output until that completion creates a new expiring inventory lot.

Each company has at most one active physical operation. The resource lock does
not block order placement, replacement, cancellation, retail pricing, or wait.
Daily used capacity is tracked independently from the active job, so completing a
job does not restore that day's capacity.

## Decisions and deterministic concurrency

Commands have zero modeled duration except for their physical consequences.
The runtime prevents zero-time loops with a one-decision-per-company-per-minute
throttle and a configurable daily safety cap.

Agents woken in the same minute observe the same base `state_version`. Inference
runs concurrently, but commands apply in a full deterministic permutation:

```text
sort_key = SHA256(seed | absolute_minute | company_id)
```

The resulting global `apply_sequence` is journaled and supplies order arrival
priority. Real response latency, retries, and provider load are audited but never
feed matching. Relevant market participants wake on the following minute after
a book mutation; resting orders also receive a periodic review wake.

## Agent projection and information boundary

The engine builds a private `AgentTurn` rather than exposing `EconomyState`.
Along with available assets and events, it contains:

- `open_orders` owned by the company;
- `market_views` with anonymous best prices, top-three aggregated depth, last
  trade price, and daily volume;
- `reserved_cash` and `reserved_inventory`;
- `pending_deliveries` with exact arrival times and quantity-preserving expiry
  buckets;
- `active_operation`; and
- `remaining_operation_capacity`.

Other companies' identities, holdings, orders, memories, and traces stay hidden.
Natural-language reasoning is non-binding; only the validated structured command
can change the economy.

## Memory, durability, and replay

Every company has its own token-budgeted `ConversationMemory`. Recent complete
Turn/Command/Outcome exchanges remain verbatim; older complete exchanges compact
into a deterministic summary. Memory has no seven-day cutoff and never replaces
current authoritative facts.

The immutable journal records company turns and ordered system steps. A
`RunCheckpoint` captures all state needed to resume exactly, including books and
collateral, jobs, deliveries, scheduler state, per-company memory, sequence
counters, events, and snapshots. Journal additions and checkpoint replacement
commit in one database transaction at a stable virtual-time boundary.

Replay uses the same runtime and engine without model calls. Observation hashes,
commands or protocol rejections, outcomes, `apply_sequence`, system effects,
events, snapshots, and final score must match exactly.

## Core invariants

1. Only `EconomyEngine` changes economic state.
2. One company submits at most one command in a same-minute batch.
3. Every resting order is fully backed by uniquely held cash or inventory.
4. Every live order quantity is positive and aligned to the `0.0001` market
   tick; raw journal commands remain audit evidence, not executable state.
5. Available, reserved, and pending inventory lots have globally unique IDs.
6. One company has at most one active operation; one trade has one pending
   delivery until completion.
7. Product markets share session state and always open or close together.
8. No company command is accepted outside `[09:00, 19:00)`.
9. Books, jobs, and deliveries are empty before the 19:30 day snapshot.
10. Provider completion order never changes economic application order.
11. Journal plus checkpoint, not prompts or UI projections, is the replay
    authority.

## Deliberate non-goals

V3 does not model credit, short selling, forward contracts, bilateral
negotiation, natural-language settlement, shipping choices, transport risk,
consumer agents, or complex exchange order types. These features should be
added only when they create a measurable strategic choice; they must not weaken
the structured settlement and replay invariants above.
