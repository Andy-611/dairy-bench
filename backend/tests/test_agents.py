import json
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from company_bench.agents.company import (
    DECISION_PROMPT_VERSION,
    AgentDecisionConstraints,
    BaselineCompanyAgent,
    LlmCompanyAgent,
    observation_hash,
)
from company_bench.agents.memory import MemoryExchange
from company_bench.domain.models import (
    CompanyObservation,
    InventoryPosition,
    PolicyKind,
    PolicyMetadata,
    ProductId,
)
from company_bench.domain.scenario import DAIRY_S9_SCENARIO
from company_bench.economy.engine import EconomyEngine
from company_bench.runs.models import InvocationOutcome
from company_bench.runtime.episode import EpisodeRuntime
from company_bench.runtime.models import (
    ActionDecision,
    AgentTurn,
    CompanyDecision,
    DecisionEnvelope,
    DecisionOutcome,
    DecisionStatus,
    DeliveryExpiryBucket,
    EconomicCommand,
    IncomingDeliveryView,
    MarketSide,
    OpenOrderView,
    OperationJobView,
    OrderBookView,
    PriceLevelView,
    Produce,
    QuoteLevel,
    SetQuoteLadder,
    SetRetailPrice,
    Transform,
    TurnRecord,
    WakeReason,
)
from company_bench.storage.store import InMemoryRunStore
from tests.support.fakes import ScriptedDecisionGateway, company_decision


def _llm_agent(
    run_id: str,
    decision: CompanyDecision,
    *,
    company_id: str = "farm_a",
    repository: InMemoryRunStore | None = None,
    memory_token_budget: int = 12_288,
) -> tuple[LlmCompanyAgent, ScriptedDecisionGateway, InMemoryRunStore]:
    """Create one isolated scripted V6 Agent and its audit repository."""
    audit_repository = repository if repository is not None else InMemoryRunStore()
    gateway = ScriptedDecisionGateway(lambda _: decision)
    agent = LlmCompanyAgent(
        run_id=run_id,
        company_id=company_id,
        gateway=gateway,
        audit_sink=audit_repository,
        metadata=PolicyMetadata(
            name="bounded-agent",
            kind=PolicyKind.MODEL,
            provider="scripted",
            model="scripted-v6",
            prompt_version=DECISION_PROMPT_VERSION,
        ),
        memory_token_budget=memory_token_budget,
    )
    return agent, gateway, audit_repository


def _turn(
    run_id: str,
    observation: CompanyObservation,
    *,
    wake_reason: WakeReason = WakeReason.WEEK_OPEN,
    open_orders: tuple[OpenOrderView, ...] = (),
    order_books: tuple[OrderBookView, ...] = (),
    pending_deliveries: tuple[IncomingDeliveryView, ...] = (),
    active_operation: OperationJobView | None = None,
) -> AgentTurn:
    """Create one runtime-owned V6 company turn."""
    return AgentTurn(
        turn_id=f"{run_id}.{observation.company_id}.t1",
        company_id=observation.company_id,
        sim_day=observation.sim_day,
        state_version=0,
        turn_number_this_week=1,
        turn_limit_this_week=observation.runtime.max_turns_per_company_week,
        wake_reasons=(wake_reason,),
        observation=observation,
        available_cash=observation.cash,
        marked_surplus=Decimal(),
        open_orders=open_orders,
        order_books=order_books,
        pending_deliveries=pending_deliveries,
        active_operation=active_operation,
    )


def _observation(company_id: str) -> CompanyObservation:
    """Read one company's initial V6 observation."""
    engine = EconomyEngine()
    world = engine.initial_state(DAIRY_S9_SCENARIO, seed=42)
    return next(
        observation for observation in engine.observe(world) if observation.company_id == company_id
    )


def _with_inventory(
    observation: CompanyObservation,
    *,
    raw_milk: Decimal = Decimal("0"),
    bottled_milk: Decimal = Decimal("0"),
) -> CompanyObservation:
    """Replace an observation's aggregate inventory for a focused Agent test."""
    return observation.model_copy(
        update={
            "inventory": (
                InventoryPosition(product=ProductId.RAW_MILK, quantity=raw_milk),
                InventoryPosition(product=ProductId.BOTTLED_MILK, quantity=bottled_milk),
            )
        }
    )


def _three_level_quantities(quantity: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """Split one exact total into the baseline's 40/40/20 quote shape."""
    first = quantity * Decimal("0.4")
    second = quantity * Decimal("0.4")
    return first, second, quantity - first - second


def test_quote_ladder_uses_the_strict_v6_action_schema() -> None:
    adapter = TypeAdapter(EconomicCommand)
    command = adapter.validate_json(
        '{"kind":"set_quote_ladder","product":"raw_milk",'
        '"side":"sell","levels":[{"quantity":"10","limit_price":"1.20"},'
        '{"quantity":"5","limit_price":"1.40"}]}'
    )

    assert command == SetQuoteLadder(
        product=ProductId.RAW_MILK,
        side=MarketSide.SELL,
        levels=(
            QuoteLevel(quantity=Decimal("10"), limit_price=Decimal("1.20")),
            QuoteLevel(quantity=Decimal("5"), limit_price=Decimal("1.40")),
        ),
    )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        adapter.validate_python(
            {
                "kind": "set_quote_ladder",
                "product": "raw_milk",
                "side": "sell",
                "levels": [],
                "order_id": "order_7",
            }
        )


def test_quote_ladder_enforces_depth_order_and_exact_quantities() -> None:
    assert (
        SetQuoteLadder(
            product=ProductId.RAW_MILK,
            side=MarketSide.BUY,
            levels=(),
        ).levels
        == ()
    )

    with pytest.raises(ValidationError, match="at most 3 items"):
        SetQuoteLadder(
            product=ProductId.RAW_MILK,
            side=MarketSide.BUY,
            levels=tuple(
                QuoteLevel(quantity=Decimal("1"), limit_price=Decimal(price))
                for price in ("1.40", "1.30", "1.20", "1.10")
            ),
        )
    with pytest.raises(ValidationError, match="descending"):
        SetQuoteLadder(
            product=ProductId.RAW_MILK,
            side=MarketSide.BUY,
            levels=(
                QuoteLevel(quantity=Decimal("1"), limit_price=Decimal("1.40")),
                QuoteLevel(quantity=Decimal("1"), limit_price=Decimal("1.50")),
            ),
        )
    with pytest.raises(ValidationError, match=r"multiple of 0\.0001"):
        QuoteLevel(quantity=Decimal("0.00009"), limit_price=Decimal("1.40"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("company_id", "allowed_tools"),
    (
        (
            "farm_a",
            ("produce", "set_quote_ladder", "idle"),
        ),
        (
            "processor_a",
            ("transform", "set_quote_ladder", "idle"),
        ),
        (
            "retailer_a",
            (
                "set_quote_ladder",
                "set_retail_price",
                "idle",
            ),
        ),
    ),
)
async def test_llm_agent_exposes_v6_commands_and_weekly_market_facts(
    company_id: str,
    allowed_tools: tuple[str, ...],
) -> None:
    observation = _observation(company_id)
    agent, gateway, _ = _llm_agent(
        "v3_prompt",
        company_decision(),
        company_id=company_id,
    )
    market = OrderBookView(
        product=ProductId.RAW_MILK,
        bids=(
            PriceLevelView(
                unit_price=Decimal("1.50"),
                quantity=Decimal("30"),
                order_count=2,
            ),
        ),
        asks=(
            PriceLevelView(
                unit_price=Decimal("1.60"),
                quantity=Decimal("10"),
                order_count=1,
            ),
        ),
    )

    await agent.act(_turn("v3_prompt", observation, order_books=(market,)))

    request = gateway.decision_requests[0]
    prompt_input = json.loads(request.input_text)
    assert request.allowed_tools == allowed_tools
    assert "continuous spot market" in request.instructions
    assert "Your sole objective is to maximize your own company's profit." in request.instructions
    assert "resting price" in request.instructions
    assert "C(u+q)-C(u)" in request.instructions
    assert "decision_constraints.delivery_duration_days" in request.instructions
    assert "decision_constraints.operation_duration_days" in request.instructions
    assert "set_quote_ladder" in request.instructions
    assert "zero to three unique" in request.instructions
    assert "use [] to cancel" in request.instructions
    assert "place_order" not in request.instructions
    assert "replace_order" not in request.instructions
    assert "cancel_order" not in request.instructions
    assert "at least 0.0001" in request.instructions
    assert "at most four decimal places" in request.instructions
    assert "queue_ahead_quantity is the same-price quantity ahead" in request.instructions
    assert "marked_surplus is guaranteed marked asset value" in request.instructions
    level = prompt_input["turn"]["order_books"][0]["bids"][0]
    assert level == {"unit_price": "1.5000", "quantity": "30.0000", "order_count": 2}
    assert prompt_input["turn"]["marked_surplus"] == "0.0000"
    assert "market_views" not in prompt_input["turn"]
    assert "remaining_operation_capacity" not in prompt_input["turn"]
    constraints = prompt_input["decision_constraints"]
    assert constraints["operation_duration_days"] == (
        observation.runtime.operation_duration_days
    )
    assert constraints["delivery_duration_days"] == (
        observation.runtime.delivery_duration_days
    )
    assert constraints["decision_interval_days"] == (
        observation.runtime.decision_interval_days
    )
    assert constraints["default_review_days"] == observation.runtime.default_review_days
    assert constraints["max_review_days"] == observation.runtime.max_review_days
    assert constraints["days_until_settlement"] == observation.sim_day.days_until_settlement
    observed_payload = prompt_input["turn"]["observation"]
    if observation.weekly_operation is None:
        assert "weekly_operation" not in observed_payload
        assert "used_operation_capacity" not in constraints
        assert "remaining_operation_capacity" not in constraints
    else:
        weekly_operation = observed_payload["weekly_operation"]
        assert weekly_operation["company_id"] == observation.company_id
        assert "weekly_base_unit_cost" in weekly_operation
        assert constraints["used_operation_capacity"] == str(
            observation.weekly_operation.used_capacity
        )
        assert constraints["remaining_operation_capacity"] == str(
            observation.weekly_operation.remaining_capacity
        )
    assert agent.metadata.prompt_version == DECISION_PROMPT_VERSION


def test_decision_constraints_derive_current_remaining_capacity() -> None:
    observation = _observation("farm_a")
    operation = observation.weekly_operation
    assert operation is not None
    used_capacity = Decimal("10.1234")
    observation = observation.model_copy(
        update={"weekly_operation": operation.model_copy(update={"used_capacity": used_capacity})}
    )
    turn = _turn("capacity_projection", observation)

    constraints = AgentDecisionConstraints.from_turn(turn)

    assert constraints.used_operation_capacity == used_capacity
    assert constraints.remaining_operation_capacity == operation.weekly_capacity - used_capacity
    assert "remaining_operation_capacity" not in turn.model_dump()


@pytest.mark.asyncio
async def test_baseline_farm_produces_then_offers_completed_inventory() -> None:
    agent = BaselineCompanyAgent()
    observation = _observation("farm_a")

    assert await agent.act(_turn("farm_open", observation)) == company_decision(
        Produce(product=ProductId.RAW_MILK, quantity=Decimal("50")),
    )

    stocked = _with_inventory(observation, raw_milk=Decimal("50"))
    assert await agent.act(
        _turn(
            "farm_stocked",
            stocked,
            wake_reason=WakeReason.OPERATION_COMPLETED,
        )
    ) == company_decision(
        SetQuoteLadder(
            product=ProductId.RAW_MILK,
            side=MarketSide.SELL,
            levels=(
                QuoteLevel(quantity=Decimal("20"), limit_price=Decimal("1.20")),
                QuoteLevel(quantity=Decimal("20"), limit_price=Decimal("1.40")),
                QuoteLevel(quantity=Decimal("10"), limit_price=Decimal("1.60")),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_baseline_floors_orders_without_overcommitting_inventory() -> None:
    agent = BaselineCompanyAgent()
    observation = _observation("farm_a")
    stocked = _with_inventory(
        observation,
        raw_milk=Decimal("49.9999"),
    )

    decision = await agent.act(
        _turn(
            "farm_precision",
            stocked,
            wake_reason=WakeReason.OPERATION_COMPLETED,
        )
    )

    assert isinstance(decision, ActionDecision)
    assert isinstance(decision.action, SetQuoteLadder)
    assert decision.action.levels == (
        QuoteLevel(quantity=Decimal("19.9999"), limit_price=Decimal("1.20")),
        QuoteLevel(quantity=Decimal("19.9999"), limit_price=Decimal("1.40")),
        QuoteLevel(quantity=Decimal("10.0001"), limit_price=Decimal("1.60")),
    )
    assert sum((level.quantity for level in decision.action.levels), Decimal()) == Decimal(
        "49.9999"
    )


@pytest.mark.asyncio
async def test_llm_agent_accepts_a_schema_valid_quote_ladder_for_engine_audit() -> None:
    command = SetQuoteLadder(
        product=ProductId.RAW_MILK,
        side=MarketSide.SELL,
        levels=(QuoteLevel(quantity=Decimal("0.0001"), limit_price=Decimal("1.40")),),
    )
    expected = company_decision(command)
    agent, _, repository = _llm_agent("dust_output", expected)

    submitted = await agent.act(_turn("dust_output", _observation("farm_a")))

    assert submitted == expected
    assert repository.list_invocations("dust_output")[0].outcome is InvocationOutcome.SUCCESS


@pytest.mark.asyncio
async def test_baseline_processor_procures_transforms_and_trades_while_busy() -> None:
    agent = BaselineCompanyAgent()
    observation = _observation("processor_a")
    assert observation.weekly_operation is not None
    capacity = min(observation.weekly_operation.weekly_capacity, Decimal("50"))
    first, second, third = _three_level_quantities(capacity)

    assert await agent.act(_turn("processor_procure", observation)) == company_decision(
        SetQuoteLadder(
            product=ProductId.RAW_MILK,
            side=MarketSide.BUY,
            levels=(
                QuoteLevel(quantity=first, limit_price=Decimal("1.60")),
                QuoteLevel(quantity=second, limit_price=Decimal("1.50")),
                QuoteLevel(quantity=third, limit_price=Decimal("1.40")),
            ),
        ),
    )

    raw_stock = _with_inventory(observation, raw_milk=Decimal("50"))
    assert await agent.act(
        _turn(
            "processor_transform",
            raw_stock,
            wake_reason=WakeReason.DELIVERY_COMPLETED,
        )
    ) == company_decision(
        Transform(
            input_product=ProductId.RAW_MILK,
            output_product=ProductId.BOTTLED_MILK,
            input_quantity=capacity,
        ),
    )

    bottled_stock = _with_inventory(observation, bottled_milk=Decimal("20"))
    active_operation = OperationJobView(
        job_id="job_1",
        kind="transformation",
        completes_on=observation.sim_day.plus_days(1),
        output_product=ProductId.BOTTLED_MILK,
        output_quantity=Decimal("40"),
    )
    assert await agent.act(
        _turn(
            "processor_sell",
            bottled_stock,
            wake_reason=WakeReason.PRICE_ALERT,
            active_operation=active_operation,
        )
    ) == company_decision(
        SetQuoteLadder(
            product=ProductId.BOTTLED_MILK,
            side=MarketSide.SELL,
            levels=(
                QuoteLevel(quantity=Decimal("8"), limit_price=Decimal("2.50")),
                QuoteLevel(quantity=Decimal("8"), limit_price=Decimal("2.65")),
                QuoteLevel(quantity=Decimal("4"), limit_price=Decimal("2.80")),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_baseline_retailer_prices_then_buys_only_uncovered_demand() -> None:
    agent = BaselineCompanyAgent()
    observation = _observation("retailer_a")

    assert await agent.act(_turn("retailer_price", observation)) == company_decision(
        SetRetailPrice(
            product=ProductId.BOTTLED_MILK,
            unit_price=Decimal("3.50"),
        ),
    )

    priced = observation.model_copy(update={"retail_price": Decimal("3.50")})
    delivery = IncomingDeliveryView(
        trade_id="trade_1",
        product=ProductId.BOTTLED_MILK,
        quantity=Decimal("15"),
        arrives_on=observation.sim_day.plus_days(1),
        expiry_buckets=(
            DeliveryExpiryBucket(
                quantity=Decimal("15"),
                expires_end_of_week=4,
            ),
        ),
    )
    assert await agent.act(
        _turn(
            "retailer_buy",
            priced,
            wake_reason=WakeReason.PRICE_ALERT,
            pending_deliveries=(delivery,),
        )
    ) == company_decision(
        SetQuoteLadder(
            product=ProductId.BOTTLED_MILK,
            side=MarketSide.BUY,
            levels=(
                QuoteLevel(quantity=Decimal("10"), limit_price=Decimal("2.80")),
                QuoteLevel(quantity=Decimal("10"), limit_price=Decimal("2.65")),
                QuoteLevel(quantity=Decimal("5"), limit_price=Decimal("2.50")),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_baseline_does_not_duplicate_a_resting_order() -> None:
    agent = BaselineCompanyAgent()
    observation = _observation("processor_a")
    assert observation.weekly_operation is not None
    capacity = min(observation.weekly_operation.remaining_capacity, Decimal("50"))
    quantities = _three_level_quantities(capacity)
    orders = tuple(
        OpenOrderView(
            order_id=f"order_{index}",
            owner_id="processor_a",
            side=MarketSide.BUY,
            product=ProductId.RAW_MILK,
            remaining_quantity=quantity,
            limit_price=price,
            placed_on=observation.sim_day,
            priority_sequence=index,
            queue_ahead_quantity=Decimal(),
        )
        for index, (quantity, price) in enumerate(
            zip(quantities, map(Decimal, ("1.60", "1.50", "1.40")), strict=True),
            start=1,
        )
    )

    assert (
        await agent.act(
            _turn(
                "processor_idle",
                observation,
                wake_reason=WakeReason.PRICE_ALERT,
                open_orders=orders,
            )
        )
        == company_decision()
    )


@pytest.mark.asyncio
async def test_llm_agent_delegates_complete_request_budget_to_provider_adapter(
    first_observation: CompanyObservation,
) -> None:
    agent, gateway, repository = _llm_agent(
        "prompt_limit",
        company_decision(),
        memory_token_budget=100,
    )
    turn = _turn("prompt_limit", first_observation)

    assert await agent.act(turn) == company_decision()

    assert len(gateway.decision_requests) == 1
    invocation = repository.list_invocations("prompt_limit")[0]
    assert invocation.outcome is InvocationOutcome.SUCCESS
    assert invocation.domain_turn_id == turn.turn_id


@pytest.mark.asyncio
async def test_llm_agent_audits_and_remembers_one_complete_turn(
    first_observation: CompanyObservation,
) -> None:
    run_id = "complete_cycle"
    decision = company_decision(Produce(product="raw_milk", quantity="12"))
    agent, gateway, repository = _llm_agent(run_id, decision)
    turn = _turn(run_id, first_observation)

    assert await agent.act(turn) == decision

    envelope = DecisionEnvelope(
        turn_id=turn.turn_id,
        decision_id=f"{turn.turn_id}.decision",
        company_id=turn.company_id,
        issued_on=turn.sim_day,
        state_version=turn.state_version,
        decision=decision,
    )
    outcome = DecisionOutcome(
        turn_id=turn.turn_id,
        decision_id=envelope.decision_id,
        company_id=turn.company_id,
        occurred_on=turn.sim_day,
        status=DecisionStatus.ACCEPTED,
        accepted=True,
        resulting_state_version=1,
        apply_sequence=1,
        next_available_on=turn.sim_day.plus_days(1),
    )
    record = TurnRecord(
        run_id=run_id,
        turn=turn,
        envelope=envelope,
        outcome=outcome,
        observation_hash=observation_hash(turn),
    )
    agent.remember(record)

    assert len(gateway.decision_requests) == 1
    checkpoint = agent.checkpoint()
    assert checkpoint.revision == 1
    assert len(checkpoint.exchanges) == 1
    assert checkpoint.exchanges[0] == MemoryExchange.from_record(record)
    invocation = repository.list_invocations(run_id)[0]
    assert invocation.outcome is InvocationOutcome.SUCCESS
    assert invocation.decision == decision
    assert invocation.decision_outcome == outcome
    assert invocation.apply_sequence == 1


@pytest.mark.asyncio
async def test_retried_domain_turn_preserves_each_physical_provider_call(
    first_observation: CompanyObservation,
) -> None:
    run_id = "physical_retry"
    repository = InMemoryRunStore()
    first, _, _ = _llm_agent(
        run_id,
        company_decision(),
        repository=repository,
    )
    second, _, _ = _llm_agent(
        run_id,
        company_decision(),
        repository=repository,
    )
    turn = _turn(run_id, first_observation)

    await first.act(turn)
    await second.act(turn)

    invocations = repository.list_invocations(run_id)
    assert len(invocations) == 2
    assert len({invocation.invocation_id for invocation in invocations}) == 2
    assert {invocation.domain_turn_id for invocation in invocations} == {turn.turn_id}


@pytest.mark.asyncio
async def test_llm_agents_complete_a_runtime_day_with_audited_memory() -> None:
    scenario = DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1})
    run_id = "llm_runtime_cycle"
    repository = InMemoryRunStore()
    agents: dict[str, LlmCompanyAgent] = {}
    for company in scenario.companies:
        agent, _, _ = _llm_agent(
            run_id,
            company_decision(),
            company_id=company.company_id,
            repository=repository,
        )
        agents[company.company_id] = agent

    execution = await EpisodeRuntime(scenario).run(
        agents,
        42,
        run_id=run_id,
        store=repository,
    )

    invocations = repository.list_invocations(run_id)
    checkpoint = repository.get_checkpoint(run_id)
    assert checkpoint is not None
    assert tuple(state.company_id for state in checkpoint.agent_states) == tuple(
        company.company_id for company in scenario.companies
    )
    assert len(invocations) == len(execution.turns)
    assert all(invocation.outcome is InvocationOutcome.SUCCESS for invocation in invocations)
    assert all(invocation.decision_outcome is not None for invocation in invocations)
    assert len({invocation.prompt_hash for invocation in invocations}) == len(invocations)
    assert {company_id: agent.checkpoint().revision for company_id, agent in agents.items()} == {
        company.company_id: sum(
            record.turn.company_id == company.company_id for record in execution.turns
        )
        for company in scenario.companies
    }
