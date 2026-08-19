# Agent Integration Contract

[English](AGENT_INTEGRATION.md) | [简体中文](AGENT_INTEGRATION.zh-CN.md)

## 1. Provider boundary

Model-backed companies use NewAPI Chat Completions, Responses, or Anthropic
Messages adapters behind one `DecisionGateway`. Every company owns an isolated
`LlmCompanyAgent`, memory, and policy metadata. Credential profiles own their
HTTP pools and share the application-level request semaphore.

The adapter accepts exactly one authorized function call. Free text, multiple
calls, unknown tools, and model-supplied identity/time are invalid. One
structured repair is allowed; a second invalid response becomes an audited
protocol error.

## 2. Runtime-owned input

Every call receives a typed `AgentDecisionInput` containing memory, one
`AgentTurn`, and derived decision constraints. The turn contains:

- runtime identity, simulation day, state version, phase, and turn budget;
- typed wake causes and newly visible events;
- company role, products, private weekly operation state, and public consumer
  market rules;
- cash, operating-cost payable, marked surplus, inventory/expiry, orders, books,
  deliveries, and active operation;
- private economics and prior outcome;
- up to eight own weekly reports and eight public retail-market reports;
- on Sunday pricing only, a private post-procurement `RetailPricingContext`.

The pricing context contains current cash, saleable quantity and book value,
weighted unit cost, this week's procurement quantity/spend/VWAP, inventory by
expiry, and previous retail price.

The Agent never receives the seed, competitor private ledgers, competitor
cost/inventory/procurement, exact consumer cohort sizes or WTP,
purchasing-power/regime shifts, regime multipliers, current hidden regime, or
future transitions.

## 3. Decision phases and tools

Monday-Saturday operations authorize:

- farm: `produce`, `set_quote_ladder`, `idle`;
- processor: `transform`, `set_quote_ladder`, `idle`;
- retailer: `set_quote_ladder`, `idle`.

After Sunday market close, each active retailer receives one separate sealed
pricing call. Its only authorized tool is `set_retail_price`; the price applies
to that Sunday's consumer settlement. All retailers infer concurrently from the
same state, so none can react to another current-week price.

Every accepted output is wrapped in a runtime-owned `DecisionEnvelope` before
the engine sees it.

## 4. Public consumer rules

Agents are explicitly told that:

- the shared market has three groups with distinct unknown WTP;
- consumers choose the cheapest acceptable stocked retailer first;
- stockouts spill demand to the next-lowest price;
- equal prices receive deterministic equal-share allocation;
- slump, normal, and boom regimes last 6-10 weeks;
- normal alternates with extreme regimes, which can alter market size, group
  composition, and WTP;
- run-wide purchasing power may also move every group's WTP.

The engine keeps exact group quantities, WTP values, purchasing-power/regime
shifts, multipliers, first extreme, current state, and transition timing hidden.
Sold-out observations are censored:
the Agent learns sales, not unserved latent demand.

## 5. Reports

Private weekly reports expose only the observed company's authoritative
operations, cash/value change, costs, trades, inventory, and expiry. Retailer
reports additionally expose procurement VWAP, saleable book cost, price, sales,
revenue, COGS, gross profit, operating profit, weekly store cost, payable,
sell-through, stockout, and market share.

Public retail reports expose every retailer's posted price, sold quantity,
market share, stockout flag, and status, plus total sales and volume-weighted
average price. Only completed past weeks are visible; both windows are capped at
eight.

## 6. Attention and timing

Operating decisions include an `AttentionPlan`. `review_after_days` is `null`,
`1`, or `2`; the effective review must remain Monday-Saturday in the current
week. Up to three currently-false OR price alerts may watch the best bid/ask.
Same-company wakes on one day coalesce. There is at most one operating call per
day and six per week.

The Sunday price call has no attention scheduling and does not consume the
operating budget. Invalid pricing output retains the prior/default price so the
episode can finish, but marks the result protocol-invalid and diagnostic-only.

## 7. Market and precision semantics

- Quote ladders declare the complete zero-to-three-level target state.
- Unchanged levels retain priority; changed levels are atomically replaced.
- Bids reserve cash and asks reserve FEFO inventory.
- Crosses execute at the resting maker price; delivery takes one day.
- Production/transformation takes one day and cannot cross the week boundary.
- All prices, quantities, costs, and values share exact `0.0001` precision.
- Each active retailer knows its own `weekly_operating_cost`; it is accrued after
  Sunday revenue, and any unpaid balance reduces marked enterprise value.

For productive companies, the private weekly realization supplies `K` and `c`:

```text
C(x) = c*x + curvature*c*x^2/(2*K)
incremental cost = C(u+q) - C(u)
```

## 8. Determinism, memory, and audit

Same-day calls run concurrently and commit by
`SHA256(seed | absolute_day | company_id)`. Provider response speed cannot
improve priority. The Agent's private memory stores complete turn/outcome cycles
and deterministically summarizes older cycles when its token budget is reached;
weekly reports remain authoritative state rather than generated summaries.

Every physical provider call records model/profile/protocol metadata, usage,
latency, attempts, prompt identity, and whether it produced the committed turn.
Secrets are never journaled. Replay makes no provider call and must reproduce
observations, decisions, protocol fallback, and engine outcomes exactly.

## 9. Minimal interface

```python
class CompanyAgent(Protocol):
    metadata: PolicyMetadata

    async def act(self, turn: AgentTurn) -> CompanyDecision: ...
```

Provider adapters parse tool calls only. Role authorization belongs to the
Agent boundary; scheduling belongs to Runtime; economic validation and mutation
belong to `EconomyEngine`.
