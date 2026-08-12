# V6 Architecture and Invariants

## 1. Simulation boundary

The active scenario, `flow.dairy.base.s9.v6`, contains nine independent
companies and 52 trading weeks. The authoritative clock is `SimDay`; an episode
contains 364 days and no minute-level economic time.

```text
TradingCalendar
└── Week 1..52
    ├── Monday..Saturday: decision days
    └── Sunday: settlement day
```

The core boundary remains:

```text
AgentTurn -> CompanyDecision -> DecisionEnvelope -> EconomyEngine -> DecisionOutcome
```

Identity, simulation day, state version, and application sequence are assigned
by the runtime. The Agent never supplies them.

## 2. Layer ownership

- `domain/calendar.py` owns `SimDay`, weekdays, and episode bounds.
- `domain/models.py` owns immutable scenario, observation, event, snapshot, and
  score contracts.
- `runtime/models.py` owns Agent decisions, wakes, scheduler events, and journal
  records.
- `economy/engine.py` is the only authority that mutates economic state.
- `runtime/episode.py` schedules days, invokes policies, and commits results.
- `runs/` owns independent job lifecycle and concurrency.
- `storage/store.py` owns atomic journal/checkpoint persistence.
- `timeline/` projects persisted state into 52 weekly views of seven days each.
- `agents/providers/` is the sole model transport boundary through NewAPI.

The dependency direction points toward typed domain contracts. Storage, web,
and UI projections never become economic authorities.

## 3. Weekly lifecycle

### Monday

1. Open the trading week.
2. Realize private productive capacity and base unit cost from the existing
   seeded formulas.
3. Reset weekly order books, retail prices, used capacity, and turn budgets.
4. Wake all nine companies.

### Tuesday-Saturday

For each day:

1. Start the day.
2. Complete production/transformation jobs due that day.
3. Complete deliveries due that day.
4. Coalesce all same-company wakes into one Agent turn.
5. Infer same-day decisions concurrently.
6. Apply those decisions serially in deterministic order.

Orders and retail prices persist through Saturday. Production, transformation,
and delivery each take exactly one day and must complete within the same week.

### Sunday

Sunday has no Agent calls. Its fixed economic order is:

1. Complete due jobs and deliveries.
2. Close both wholesale order books and release unused collateral.
3. Settle each retailer's consumer demand once.
4. Expire inventory whose `expires_end_of_week` is due.
5. Commit one immutable `WeekSnapshot`.
6. Advance to the next week.

This order allows Saturday commitments to arrive before Sunday sales while
preventing post-settlement decisions.

## 4. Formula frequency

The migration changes time frequency, not the economic formula inputs.

- `normal_capacity=60` for farms and `normal_capacity=50` for processors are
  parameters of `CapacityFunction`, not realized fixed capacities.
- Weekly capacity retains seeded persistence, volatility, and clipping.
- `base_demand=40` is a parameter of `DemandSpec`, not a fixed sale quantity.
- Potential demand retains the seeded shock and continuous price-demand curve.
- Productive cost retains the convex cumulative cost function.

Each capacity/cost realization occurs once per company-week. Each potential
demand realization occurs once per retailer-week and is settled on Sunday.
Initial cash, reference values, and formula parameters are not multiplied by
seven.

## 5. Inventory, markets, and physical commitments

- Raw milk shelf life: 2 weekly settlements.
- Bottled milk shelf life: 4 weekly settlements.
- FEFO reservation and delivery preserve original lot expiry.
- Orders are fully collateralized with cash or physical inventory.
- A target quote ladder atomically keeps, places, replaces, or cancels up to
  three levels.
- Matching remains price-time priority at the resting maker price.
- Cash transfers at trade time; inventory becomes usable after the one-day
  delivery.
- A company owns at most one active production resource.
- All economic quantities use the `0.0001` quantum.

## 6. Attention and bounded turns

Every decision includes an `AttentionPlan`:

- `review_after_days`: optional positive delay, maximum 2;
- default review: 1 day when another Monday-Saturday remains;
- up to three quote alerts;
- no review may cross into Sunday or a later week.

Event wakes, alerts, and reviews for the same company/day coalesce. One company
can receive at most one Agent call per day and six calls per week. Reaching the
limit writes an explicit audit step and suppresses later wakes. Sunday never
consumes a turn.

## 7. Deterministic concurrency

All companies woken on one day observe the same pre-apply state. Provider calls
may run concurrently, but economic application is serial:

```text
sort_key = SHA256(seed | absolute_day | company_id)
```

Each committed turn persists `apply_sequence`. Consequently network latency,
completion order, and parallel run load cannot change the simulated economy.

Different `RunJob`s own separate runtimes, schedulers, economy states,
checkpoints, journals, and policy instances. The application shares only the
NewAPI HTTP transport and its bounded request semaphore.

## 8. Persistence and replay

One atomic progress transaction stores:

- new immutable `TurnRecord`s;
- new immutable `SystemStepRecord`s;
- the complete current checkpoint;
- run progress through the completed week.

A completion transaction stores the final `EpisodeResult` and removes the
checkpoint. Interrupted/stopped runs resume from the same run ID. Completed
replay uses the source observations and decisions but recomputes every engine
outcome; any journal, event, snapshot, score, or quality drift fails replay.

V6 intentionally uses a fresh `runs-v6.sqlite3` schema and does not read older
runtime payloads.

## 9. Scoring

One valid episode produces one score after all 52 weekly snapshots exist:

```text
E_raw = Σ_i [V_i(T) - V_i(0)]
E_ref = seeded maximum net value across 52 retailer-week markets
E = clip(E_raw / E_ref, 0, 1)
F = 1 - mean(tier Gini) / (2/3)
B = bankrupt companies / 9
Score = 100 × E × sqrt(F × (1 - B))
```

`V_i` is cash plus reference-valued inventory. Under `s9-enterprise-v4`, a
company is counted once as bankrupt when its net worth is at or below `1.0000`
in any weekly snapshot. Protocol validity is independent: any invalid Agent
turn makes the result diagnostic-only.

## 10. Required invariants

1. Exactly 52 weekly settlements and 52 snapshots complete an episode.
2. Every projected week contains exactly seven ordered `TimelineDayFrame`s.
3. Company decisions exist only Monday-Saturday and at most once per company/day.
4. Sunday order is completions, market close, consumer sales, expiry, snapshot.
5. Weekly capacity and demand are formula realizations, never hard-coded output.
6. No job, delivery, review, or open order crosses a week boundary.
7. Same-day concurrent inference always has deterministic serial application.
8. Only the engine mutates economic state.
9. Journal/checkpoint progress is atomic and replay drift is fatal.
