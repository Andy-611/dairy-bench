# V9 Architecture and Invariants

[English](ARCHITECTURE.md) | [简体中文](ARCHITECTURE.zh-CN.md)

## 1. Simulation boundary

The active scenario, `flow.dairy.base.s9.v9`, contains three farms, three
processors, three retailers, and 52 seven-day trading weeks. The authoritative
decision path is:

```text
Wake -> AgentTurn -> CompanyDecision -> EconomyEngine -> Journal -> next Wake
```

The runtime owns identity, time, state versions, and application order. Only
`EconomyEngine` mutates cash, inventory, orders, jobs, deliveries, prices, or
sales.

## 2. Module ownership

- `domain/models.py`: immutable scenario, observation, event, report, snapshot,
  and score contracts.
- `economy/engine.py`: economic state transitions and weekly settlement.
- `economy/consumer.py`: hidden regimes, finite cohorts, and shared-market
  allocation.
- `economy/ledger.py`: one typed event-ledger projection reused by reports and
  private Agent economics.
- `economy/private_view.py`: company-private cumulative cash flow and executable
  unit economics.
- `economy/reports.py`: authoritative private and public weekly reports.
- `economy/oracle.py`: seed-specific full-information enterprise-surplus
  reference.
- `runtime/episode.py`: scheduling, concurrent inference, deterministic commit,
  recovery, and replay.
- `storage/repository.py`: the narrow atomic persistence interface;
  `storage/memory.py` and `storage/sqlite.py` are its two adapters.
- `agents/providers/newapi/`: the only model transport seam, separated into
  configuration, HTTP transport, wire protocols, and decision gateway.

Dependencies point toward typed domain contracts; web, storage, and UI
projections are never economic authorities.

## 3. Weekly lifecycle

Monday opens the week and realizes each productive company's private capacity
and unit-cost state. Monday-Saturday contain operations, wholesale trading,
one-day jobs, and one-day deliveries. A company receives at most one operating
turn per day and six per week.

Sunday executes in this order:

1. Complete due jobs and deliveries.
2. Close both wholesale books and release unused collateral.
3. Build the post-procurement pricing context for every active retailer.
4. Query all retailers concurrently from the same pre-price state.
5. Commit one price per retailer; reveal none until all queries finish.
6. Settle the finite shared consumer market once and credit revenue.
7. Accrue each active retailer's weekly store cost and pay it from cash.
8. Expire due inventory, then perform the terminal bankruptcy check.
9. Issue weekly reports and commit one snapshot.

The Sunday pricing call has the sole tool `set_retail_price` and does not consume
the six operating turns. A protocol-invalid pricing response retains the prior
price (or `3.5000` in week one), records the protocol violation, and allows the
physical episode to continue as diagnostic-only.

## 4. Shared consumer market and information boundary

The hidden market has three finite willingness-to-pay cohorts. Each week,
consumers buy from the cheapest retailer whose price they accept. Stockouts
spill demand to the next-lowest price. Equal prices use deterministic equal-share
water filling, with only indivisible `0.0001` residual quanta assigned by a
seeded hash.

The market moves through slump, normal, and boom regimes. Every regime lasts
6-10 weeks; normal alternates with an extreme regime, and slump/boom alternate
across extreme periods. A regime changes total population, cohort composition,
and WTP. A hidden run-wide purchasing-power shift also moves all WTP thresholds
by a stable amount for the full episode. Canonical purchasing-power shifts are
`-0.2/-0.1/0/0.1/0.2`; slump/normal/boom add `-0.2/0/+0.2` respectively.

Agents are told:

- there are three cohorts with distinct unknown willingness to pay;
- cheapest-first, stockout-spillover, and equal-price allocation rules;
- the three regime names, 6-10 week duration, and alternation structure;
- regimes may change market size, composition, and WTP, while run-wide
  purchasing power may also move WTP.

Agents are not told cohort quantities, exact willingness-to-pay values,
composition multipliers, exact purchasing-power/regime shifts, the first
extreme, current regime, or transition week.
The complete `ConsumerMarketSpec` remains engine-only; observations contain only
`ConsumerMarketRules`.

## 5. Weekly reports and learning signal

Every observation includes at most the last eight completed reports for that
company and the last eight public retail-market reports.

Private common fields include opening/closing enterprise value, weekly and
cumulative surplus, cash, operating-cost payable, purchases, wholesale sales,
operation cost, expiry, and inventory book positions. Role-specific fields add:

- farm: realized capacity/cost, output, utilization, sold and unsold raw milk;
- processor: raw procurement, processing, yield, utilization, output, and
  bottled sales/inventory;
- retailer: procurement quantity/VWAP, saleable inventory and weighted cost,
  committed price, units sold, revenue, COGS, gross and operating profit, sell-through,
  stockout, market share, expiry, and ending inventory.

The public retail report contains each retailer's status, posted price, sold
quantity, market share, and stockout flag, plus market total sales and
volume-weighted average price. Competitor cash, costs, purchases, and exact
inventory remain private. Unmet latent demand is never disclosed.

## 6. Physical and accounting rules

- Farm normal capacity is `60`; processor normal input capacity is `50`.
  Across three firms this intentionally creates a `180` raw-supply buffer for
  `150` processor input. With `0.8` yield, normal bottled capacity is exactly
  `120`, matching normal three-cohort demand. Seeded persistence, volatility,
  and bounds produce private weekly values.
- Productive cost is convex:
  `C(x)=c*x+curvature*c*x^2/(2*K)`.
- Processor yield is `0.8` bottled units per raw input unit.
- Raw and bottled milk last 2 and 4 weekly settlements respectively.
- FEFO reservations and delivery preserve lot origin, expiry, and book cost.
- Bids and asks are fully collateralized; matches use price-time priority and
  the resting maker price.
- Every economic price, quantity, value, and cost uses the shared `0.0001`
  precision. Retail prices have no separate rounding rule.
- Each active retailer accrues `5.0000` after Sunday consumer revenue. Available
  cash pays the balance; any unpaid amount remains a liability and reduces value.

## 7. Determinism, concurrency, and recovery

Same-day Agents observe one pre-apply state and may infer concurrently. Their
decisions commit serially by:

```text
SHA256(seed | absolute_day | company_id)
```

Sunday prices also use one shared observation state; allocation occurs only
after every price is committed. Provider latency cannot alter the economy.

Independent `RunJob`s own separate runtime, scheduler, economy, policies,
Journal, and Checkpoint. Atomic progress transactions make interrupted runs
resumable. Replay re-executes source decisions and fails on observation,
protocol-fallback, outcome, event, snapshot, score, or quality drift.

The launcher reuses an existing backend only when `/api/health` matches the API,
scenario, score, database, and Journal payload contracts. A stale checkout on
port 8000 is treated as an explicit conflict.

V9 intentionally uses `runs-v9.sqlite3` with database schema 16 and payload
schema 10. The repository accepts only the current contracts; it contains no
migration or reader for earlier runtime databases.

## 8. Bankruptcy

After authoritative economic transitions, guaranteed value includes cash,
reference-valued owned/reserved inventory, in-transit purchases, and funded
operation output, less operating-cost payables. A company is bankrupt only when
this value is strictly below
`1.0000`; equality remains active. Bankruptcy is irreversible, delists orders
and retail price, cancels future wakes, and emits one event.

## 9. Scoring

For a settled horizon `T` from week 1 through week 52:

```text
E_raw    = sum_i [V_i(T) - V_i(0)]
E_oracle = seed-specific full-information enterprise-surplus reference
E        = clip(E_raw / E_oracle, 0, 1)
G_all    = Gini(V_1(T), ..., V_9(T))
F_all    = clip(1 - G_all / (8/9), 0, 1)
L        = count_i[V_i(T) - V_i(0) < 0]
P        = 1 - L/9
Score    = 100 * E * sqrt(F_all * P)
```

`V_i(T)` is cash plus reference-valued terminal inventory less operating-cost
payables. The Oracle uses the
realized capacity/cost paths, convex costs, processor yield, perishability,
realized hidden WTP, shared cohorts, retailer operating costs, and terminal
reference values. Its objective contains
enterprise surplus only; consumer surplus is excluded. A small upward numerical
margin prevents solver tolerance from understating the reference.

`D` is the bankruptcy count. `L` is the strictly loss-making company count;
zero surplus is non-loss and therefore included in `P`. Counts and the loss rate
are diagnostics, while `D` is not a rate. Any protocol-invalid turn makes the
score diagnostic-only.

`RunEvaluationProjector` reads either the immutable completed episode or the
latest durable checkpoint behind one interface. A running, stopped,
interrupted, or failed run is scored only after at least one full week has
settled. Current partial-week events and Agent turns are excluded, and the
Oracle is solved for the same `T`-week horizon. Prefix scores are marked
provisional and never enter formal rankings; the immutable week-52 episode
remains the official result and Exact Replay source.

## 10. Required invariants

1. Exactly 52 settlements and snapshots complete the canonical episode.
2. Operating turns occur Monday-Saturday; Sunday adds only sealed retailer pricing.
3. Every retailer receives at most one price decision before shared settlement.
4. Hidden demand parameters never enter an Agent observation or prompt payload.
5. Reports contain only completed past weeks and at most eight rolling weeks.
6. Shared sales are input-order independent and never exceed inventory or population.
7. No job, delivery, review, or wholesale order crosses a weekly boundary.
8. Only the engine mutates economic state; Journal and Checkpoint commits are atomic.
9. Same-state inference plus deterministic commit makes provider timing irrelevant.
10. Net value below, but not equal to, `1.0000` triggers irreversible bankruptcy.
