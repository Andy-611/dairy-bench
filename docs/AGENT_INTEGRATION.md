# Agent Integration Contract

## 1. Provider boundary

Model-backed companies are exposed only through NewAPI's Chat Completions
transport. Each company owns an isolated `LlmCompanyAgent`, memory, and policy
metadata; runs share only the HTTP connection pool and request semaphore.

The adapter requires exactly one authorized function call. It never accepts a
free-text command, multiple calls, an unknown tool, or a provider-specific
identity/time field. Parallel tool calls are disabled. One invalid completion
may receive one structured repair request; a second failure becomes a protocol
invalid turn.

## 2. Input owned by the runtime

Each call receives `AgentDecisionInput`:

```text
AgentDecisionInput
├── memory
├── turn: AgentTurn
└── decision_constraints
```

`AgentTurn` contains:

- runtime-owned `turn_id`, `company_id`, `sim_day`, and `state_version`;
- `turn_number_this_week` and `turn_limit_this_week`;
- typed wake reasons and causal wake signals;
- the company's role, public scenario rules, products, demand curve, and
  private weekly operation realization;
- available/reserved cash, marked surplus, available inventory and expiry;
- owned open orders and anonymous relevant order books;
- pending deliveries and the active physical operation;
- private cash flow/unit economics, newly visible events, and prior outcome.

The Agent does not receive the seed, other companies' private ledgers, hidden
demand shock, hidden cost/capacity realizations, or future events.

`decision_constraints` explicitly supplies one-day operation/delivery duration,
one-day decision interval, default/max review days, days until Sunday, and used
and remaining productive capacity. These are facts, not suggestions.

## 3. Output contract

The Agent must call exactly one role-authorized decision tool. Every tool returns
one discriminated `CompanyDecision`:

```text
ActionDecision(action, attention)
IdleDecision(attention)
```

Authorized economic actions are:

- farm: `produce`, sell `set_quote_ladder`, `idle`;
- processor: buy/sell `set_quote_ladder`, `transform`, `idle`;
- retailer: buy `set_quote_ladder`, `set_retail_price`, `idle`.

The Runtime wraps the validated decision in a `DecisionEnvelope` with trusted
identity, date, and state version before calling `EconomyEngine`.

## 4. AttentionPlan

Every action and idle decision includes:

```json
{
  "review_after_days": 1,
  "alerts": []
}
```

- `review_after_days` may be `null`, `1`, or `2`.
- `null` uses the one-day bounded fallback when another decision day exists.
- The effective review must remain Monday-Saturday in the current week.
- Up to three OR-combined alerts may watch `best_bid` or `best_ask` with
  `at_least`/`at_most` thresholds.
- An alert must be currently false, observable to that company, and nonduplicate.

Accepted decisions do not create a routine extra wake. Reviews, matching price
alerts, own trades, operation/delivery completions, rejection correction, and
week opening are the valid causes. Same-company wakes on one day coalesce.

## 5. Weekly operating rules visible to the Agent

- Companies may decide only Monday-Saturday.
- One company receives at most one call per day and six calls per week.
- Sunday closes wholesale books, settles consumer purchases once, expires due
  inventory, and contains no Agent call.
- Production, transformation, and delivery take one day and cannot cross into a later week.
- Order books and retail price persist during the week and reset after Sunday.
- Raw/bottled inventory expires after 2/4 weekly settlements.

For productive companies, `weekly_operation` gives realized `K` and base unit
cost `c`. For an additional input quantity `q` after used capacity `u`, the cash
cost remains:

```text
C(x) = c*x + curvature*c*x^2/(2*K)
incremental cost = C(u+q) - C(u)
```

The normal capacity values in the scenario are formula inputs; the Agent must
use the private realized weekly values supplied in its observation.

## 6. Market semantics

- `set_quote_ladder` declares the complete target state for one product/side.
- Zero to three unique levels must be ordered best-to-worst; `[]` clears the
  ladder.
- Unchanged price/quantity levels preserve identity and time priority; changed
  levels are atomically replaced and lose priority.
- Total bids require full cash collateral; total asks require physical FEFO
  inventory.
- Crossing trades immediately at the resting maker price.
- Cash transfers at match time; purchased inventory arrives one day later.
- Every economic price and quantity uses at most four decimal places and every
  productive/quote quantity is at least `0.0001`.

## 7. Deterministic same-day application

All Agents woken on the same day observe one base state and may infer in
parallel. The Runtime validates all outputs and applies them serially by:

```text
SHA256(seed | absolute_day | company_id)
```

The persisted `apply_sequence` is the only economic order. A provider's response
speed cannot improve market priority.

## 8. Memory and audit

After application, the Agent-owned memory receives the complete turn/outcome
cycle. Compaction is deterministic and private to that company. Every physical
provider call records model, provider, usage, latency, attempts, prompt version,
prompt hash, and whether it was applied to a committed turn. Credentials and
raw secrets are never journaled.

Replay Agents consume persisted source turns and make no provider call. They
must match company, simulation day, observation, decision, and recomputed
outcome exactly.

## 9. Minimal adapter interface

An Agent implementation needs one async operation:

```python
class CompanyAgent(Protocol):
    metadata: PolicyMetadata

    async def act(self, turn: AgentTurn) -> CompanyDecision: ...
```

Provider adapters should not duplicate role authorization or economic
validation. They construct the provider request, parse the single tool call,
and return the typed union; Runtime and Engine remain authoritative.
