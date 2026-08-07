# V4 Architecture and Invariants

## Purpose

V4 models nine independent companies operating a continuous dairy spot
market. It adds credible intraday price discovery and physical lead times while
keeping the benchmark deterministic, auditable, and small enough to reason
about.

The active scenario is `flow.dairy.base.s9.v5`:

- three farms produce raw milk;
- three processors buy raw milk, transform it, and sell bottled milk; and
- three retailers buy bottled milk, set retail prices, and serve consumers.

V2 remains documented as historical behavior. V4 carries no legacy
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
- `ContinuousSpotMarket` owns atomic target-ladder reconciliation, collateral
  reservation, price-time matching, partial fills, and book close for one product.
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

- One `set_quote_ladder` command defines zero to three distinct target prices for
  one product and side. Every level crosses the engine boundary only with a
  positive `OrderQuantity` and `PositiveMoney` price, both exact multiples of
  `0.0001`. Invalid raw commands are rejected before a market session exists;
  they are never rounded.
- A bid removes the four-place, half-even rounded
  `remaining_quantity * limit_price` from available cash and stores it on
  `BuyOrder`.
- An ask removes exact FEFO lots from available inventory and stores them on
  `SellOrder`.
- Production output, inbound deliveries, and lots already held by another ask
  are unavailable.
- All mutable old levels and target levels are staged in one transaction. The
  complete Bid notional or Ask quantity must be backed after reusable old holds
  are released; otherwise the original ladder and every hold remain unchanged.
  There is no per-level partial commit or settlement-time clipping.

The private observation reports available and reserved assets separately. Total
economic ownership is therefore visible without allowing the same asset to back
two commitments.

### Matching

An incoming order repeatedly matches while `best_bid >= best_ask`:

1. better price has priority;
2. equal price uses the persisted `priority_sequence`;
3. the execution price is the resting maker order's limit;
4. execution quantity is the smaller remaining quantity; and
5. any fully backed nonzero remainder stays on the book with its existing
   priority; a rounding-dust or no-longer-fully-backed remainder is withdrawn.

Partial fill describes quantity interaction, not under-collateralization: every
remaining unit is still fully backed. When a bid executes below its limit, the
unused price difference returns to available cash immediately. With one shared
four-place quantum, independently rounded fill and remainder amounts can differ
by one quantum; in that boundary case the fill stands and only the remainder is
released.

The ladder is a target state. Bid targets must arrive from highest to lowest and
Ask targets from lowest to highest; invalid ordering is rejected. Reconciliation
first keeps exact price-and-quantity matches, then replaces remaining same-price
levels, pairs the remaining old and target levels in price-priority order as
replacements, cancels unmatched old levels, and places unmatched targets. Empty
`levels` cancels the complete product-side ladder. Exact matches retain their
order ID, arrival time, and priority; every replacement or placement is an
independent order with a new identity and priority. The market applies new levels from best
to worst price so immediate matching is deterministic.

Because the command supplies no level identity, repricing and a cancel-plus-place
intention are observationally indistinguishable and both lose old priority. The
deterministic pairing defines the auditable result without requiring the Agent
to micromanage order IDs. If any validation, self-cross, or reservation check
fails, the complete staged transaction is discarded. At 19:00, book close
releases all unfilled collateral.

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
not block quote-ladder updates, retail pricing, or wait. Daily used capacity is
tracked independently from the active job, so completing a job does not restore
that day's capacity.

## Decisions and deterministic concurrency

Commands have zero modeled duration except for their physical consequences.
The runtime prevents zero-time loops with a one-decision-per-company-per-minute
throttle and a hard cap of 25 Agent turns per company per day. The current turn
number and limit are part of every `AgentTurn`; reaching the limit produces one
state-neutral, journaled `TURN_LIMIT_REACHED` step. Every later causal Wake is
suppressed without a model call but remains auditable as an
`AGENT_WAKE_SUPPRESSED` step with its typed signals.

Agents woken in the same minute observe the same base `state_version`. Inference
runs concurrently, but commands apply in a full deterministic permutation:

```text
sort_key = SHA256(seed | absolute_minute | company_id)
```

The resulting global `apply_sequence` is journaled as command-processing order.
When that command is applied, each new or replaced ladder level receives its own
persisted order priority sequence in best-to-worst target order; unchanged
levels retain theirs. Real response latency, retries, and provider load are
audited but never feed matching.

Sparse decisions are governed by the in-process `AgentAttention` module. An
accepted `wait` may arm up to three Agent-visible `best_bid`/`best_ask` threshold
alerts, combined with OR semantics, plus an optional absolute fallback. An
explicit fallback must be later on the same business day and at most 120 minutes
away. Omitting it schedules a 120-minute review when that still falls before
market close; otherwise there is no same-day review. Duplicate, hidden, or
already-true alerts reject the entire command without changing economic state.

Alerts are one-shot. They observe only the final committed order books after all
seed-ordered commands for a minute have applied, then wake the company on the
following minute. Own trades, operation completion, and delivery completion are
important wakes; generic order-book mutations are not broadcast and resting
orders do not receive an independent polling timer.

## Agent projection and information boundary

The engine builds a private `AgentTurn` rather than exposing `EconomyState`.
Along with available assets and events, it contains:

- `open_orders` owned by the company, each with current same-price FIFO quantity
  ahead;
- `order_books` with every anonymous aggregated price level, its quantity and
  order count, plus last trade price and daily volume;
- `reserved_cash`, `marked_surplus`, and `inventory_expiry`, whose buckets split
  available from ask-reserved spot inventory;
- `pending_deliveries` with exact arrival times and quantity-preserving expiry
  buckets;
- `active_operation` and the authoritative daily operation state; and
- the current daily turn number and hard limit.

The provider adapter adds one typed `decision_constraints` projection with
explicit runtime limits and derived used/remaining operation capacity. It is
rebuilt from `AgentTurn` for every request and never owns economic state.

`marked_surplus` values available cash, bid-reserved cash, available and
ask-reserved inventory, pending deliveries, and guaranteed active-operation
output at immutable product reference values, then subtracts the company's
episode initial cash. Moving the same asset between available, reserved,
in-transit, and completed states is therefore value-neutral. `inventory_expiry`
covers spot inventory only; pending deliveries retain their own expiry buckets,
and work in process enters an expiry bucket only when completed.

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
collateral, jobs, deliveries, scheduler state, armed attention plans,
per-company memory, sequence counters, events, and snapshots. Each armed plan is
validated against its source Wait turn and exact fallback wake. Journal additions
and checkpoint replacement commit in one database transaction at a stable
virtual-time boundary.

Replay uses the same runtime and engine without model calls. Observation hashes,
commands or protocol rejections, outcomes, `apply_sequence`, system effects,
events, snapshots, and final score must match exactly.

## Episode evaluation

Each valid episode receives one seed-specific score. For each of the 30 days and
three retailers, the evaluator derives potential demand `A` from the episode
seed and uses the continuous net-value ceiling `(A + 14)^2 / 32`. Their sum is
`E_ref`; it is never selected from participating models. Aggregate company value
growth is `E_raw`, and `E = clip(E_raw / E_ref, 0, 1)`.

The evaluator computes a raw growth Gini for each three-company tier. With a
finite-sample maximum of `2/3`, normalized fairness is
`F = 1 - (G_farm + G_processor + G_retailer) / 2`. A company is bankrupt when
its day-end cash plus reference-valued inventory has reached zero on any day;
with `D` bankrupt companies, `B = D / 9`. The final score is:

```text
Score = 100 * E * sqrt(F * (1 - B))
```

Under `s9-enterprise-v2`, these formulas retain full intermediate precision;
each published `CompanyScore` and `ScoreCard` Decimal is normalized to four
places only at its public boundary.

Economic outcomes do not create eligibility gates. An incomplete,
protocol-invalid, technically failed, or replay-divergent episode produces no
score.

## Core invariants

1. Only `EconomyEngine` changes economic state.
2. One company submits at most one command in a same-minute batch.
3. Every resting order is fully backed by uniquely held cash or inventory.
4. One company has at most three distinct active price levels per product and
   side; one ladder command commits all of its changes or none of them.
5. Every live order quantity is positive and aligned to the `0.0001` market
   tick; raw journal commands remain audit evidence, not executable state.
6. Every active order has an independent persisted priority sequence; one
   command's `apply_sequence` is not reused as three order priorities.
7. Available, reserved, and pending inventory lots have globally unique IDs.
8. One company has at most one active operation; one trade has one pending
   delivery until completion.
9. Product markets share session state and always open or close together.
10. No company command is accepted outside `[09:00, 19:00)`.
11. Books, jobs, and deliveries are empty before the 19:30 day snapshot.
12. Provider completion order never changes economic application order.
13. Journal plus checkpoint, not prompts or UI projections, is the replay
    authority.
14. Attention reads only the anonymous Agent market projection; observer UI
    order-book data can never wake an Agent or change economic state.
15. Every economic Decimal crossing a domain boundary is a canonical multiple
    of `0.0001`. External over-precision is rejected; derived cash uses
    half-even rounding, derived physical quantity rounds down, and zero-value
    settlements are atomically rejected.

## Deliberate non-goals

V4 does not model credit, short selling, forward contracts, bilateral
negotiation, natural-language settlement, shipping choices, transport risk,
consumer agents, or complex exchange order types. These features should be
added only when they create a measurable strategic choice; they must not weaken
the structured settlement and replay invariants above.
