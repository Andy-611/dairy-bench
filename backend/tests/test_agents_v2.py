import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from company_bench.agent_gateway import ScriptedModelGateway
from company_bench.agent_models import CommandSubmission, ModelOutputError
from company_bench.agents import (
    COMMAND_PROMPT_VERSION,
    BaselineCompanyAgent,
    LlmCompanyAgent,
    observation_hash,
)
from company_bench.dairy_scenario import DAIRY_S12_V3_SCENARIO
from company_bench.engine import EconomyEngine
from company_bench.models import (
    CompanyObservation,
    InventoryPosition,
    NoOpDecision,
    PolicyKind,
    PolicyMetadata,
    ProductId,
)
from company_bench.repository import MemoryRunRepository
from company_bench.run_models import InvocationOutcome
from company_bench.runtime import EpisodeRuntime
from company_bench.runtime_models import (
    AgentTurn,
    CommandEnvelope,
    CommandOutcome,
    CommandStatus,
    CompanyCommand,
    DeliveryExpiryBucket,
    IncomingDeliveryView,
    MarketSide,
    MarketView,
    OpenOrderView,
    OperationJobView,
    PlaceOrder,
    Produce,
    ReplaceOrder,
    SetRetailPrice,
    SimTime,
    Transform,
    TurnRecord,
    Wait,
    WakeReason,
)


def _llm_agent(
    run_id: str,
    command: CompanyCommand,
    *,
    company_id: str = "farm_a",
    repository: MemoryRunRepository | None = None,
    memory_token_budget: int = 12_288,
    max_prompt_tokens: int = 16_384,
) -> tuple[LlmCompanyAgent, ScriptedModelGateway, MemoryRunRepository]:
    """Create one isolated scripted V3 Agent and its audit repository."""
    audit_repository = repository if repository is not None else MemoryRunRepository()
    gateway = ScriptedModelGateway(
        lambda _: NoOpDecision(),
        command_factory=lambda _: command,
    )
    agent = LlmCompanyAgent(
        run_id=run_id,
        company_id=company_id,
        gateway=gateway,
        audit_sink=audit_repository,
        metadata=PolicyMetadata(
            name="bounded-agent",
            version="3",
            kind=PolicyKind.OPENAI,
            provider="scripted",
            model="scripted-v3",
            prompt_version=COMMAND_PROMPT_VERSION,
        ),
        memory_token_budget=memory_token_budget,
        max_prompt_tokens=max_prompt_tokens,
    )
    return agent, gateway, audit_repository


def _turn(
    run_id: str,
    observation: CompanyObservation,
    *,
    wake_reason: WakeReason = WakeReason.DAY_OPEN,
    open_orders: tuple[OpenOrderView, ...] = (),
    market_views: tuple[MarketView, ...] = (),
    pending_deliveries: tuple[IncomingDeliveryView, ...] = (),
    active_operation: OperationJobView | None = None,
) -> AgentTurn:
    """Create one runtime-owned V3 company turn."""
    return AgentTurn(
        turn_id=f"{run_id}.{observation.company_id}.t1",
        company_id=observation.company_id,
        sim_time=SimTime(absolute_minute=540),
        state_version=0,
        turn_number_today=1,
        turn_limit_today=observation.runtime.max_turns_per_company_day,
        wake_reasons=(wake_reason,),
        observation=observation,
        available_cash=observation.cash,
        open_orders=open_orders,
        market_views=market_views,
        pending_deliveries=pending_deliveries,
        active_operation=active_operation,
    )


def _observation(company_id: str) -> CompanyObservation:
    """Read one company's initial V3 observation."""
    engine = EconomyEngine()
    world = engine.initial_state(DAIRY_S12_V3_SCENARIO, seed=42)
    return next(
        observation
        for observation in engine.observe(world)
        if observation.company_id == company_id
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


def test_replace_order_uses_the_strict_v3_command_schema() -> None:
    submission = CommandSubmission.model_validate_json(
        '{"command":{"kind":"replace_order","order_id":"order_7",'
        '"quantity":"12.5","limit_price":"1.55"}}'
    )

    assert submission.command == ReplaceOrder(
        order_id="order_7",
        quantity=Decimal("12.5"),
        limit_price=Decimal("1.55"),
    )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CommandSubmission.model_validate(
            {
                "command": {
                    "kind": "replace_order",
                    "order_id": "order_7",
                    "quantity": "12.5",
                    "limit_price": "1.55",
                    "side": "buy",
                }
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("company_id", "allowed_commands"),
    (
        (
            "farm_a",
            ("produce", "place_order", "replace_order", "cancel_order", "wait"),
        ),
        (
            "processor_a",
            ("transform", "place_order", "replace_order", "cancel_order", "wait"),
        ),
        (
            "retailer_a",
            (
                "place_order",
                "replace_order",
                "cancel_order",
                "set_retail_price",
                "wait",
            ),
        ),
    ),
)
async def test_llm_agent_exposes_v3_commands_and_continuous_market_facts(
    company_id: str,
    allowed_commands: tuple[str, ...],
) -> None:
    observation = _observation(company_id)
    agent, gateway, _ = _llm_agent(
        "v3_prompt",
        Wait(),
        company_id=company_id,
    )
    market = MarketView(
        product=ProductId.RAW_MILK,
        best_bid=Decimal("1.50"),
        best_ask=Decimal("1.60"),
    )

    await agent.act(_turn("v3_prompt", observation, market_views=(market,)))

    request = gateway.command_requests[0]
    prompt_input = json.loads(request.input_text)
    assert request.allowed_commands == allowed_commands
    assert "continuous spot market" in request.instructions
    assert "resting price" in request.instructions
    assert "after 30 virtual minutes" in request.instructions
    assert "replace_order" in request.instructions
    assert "at least 0.0001" in request.instructions
    assert "at most four decimal places" in request.instructions
    assert prompt_input["turn"]["market_views"][0]["best_bid"] == "1.50"
    assert agent.metadata.version == "3"
    assert agent.metadata.prompt_version == COMMAND_PROMPT_VERSION


@pytest.mark.asyncio
async def test_baseline_farm_produces_then_offers_completed_inventory() -> None:
    agent = BaselineCompanyAgent()
    observation = _observation("farm_a")

    assert await agent.act(_turn("farm_open", observation)) == Produce(
        product=ProductId.RAW_MILK,
        quantity=Decimal("50"),
    )

    stocked = _with_inventory(observation, raw_milk=Decimal("50"))
    assert await agent.act(
        _turn(
            "farm_stocked",
            stocked,
            wake_reason=WakeReason.OPERATION_COMPLETED,
        )
    ) == PlaceOrder(
        side=MarketSide.SELL,
        product=ProductId.RAW_MILK,
        quantity=Decimal("50"),
        limit_price=Decimal("1.40"),
    )


@pytest.mark.asyncio
async def test_baseline_floors_orders_without_overcommitting_inventory() -> None:
    agent = BaselineCompanyAgent()
    observation = _observation("farm_a")
    stocked = _with_inventory(
        observation,
        raw_milk=Decimal("49.999999999999999"),
    )

    command = await agent.act(
        _turn(
            "farm_precision",
            stocked,
            wake_reason=WakeReason.OPERATION_COMPLETED,
        )
    )

    assert isinstance(command, PlaceOrder)
    assert command.quantity == Decimal("49.9999")
    assert command.quantity <= stocked.quantity(ProductId.RAW_MILK)

    dust = _with_inventory(observation, raw_milk=Decimal("0.00009"))
    assert await agent.act(
        _turn(
            "farm_dust",
            dust,
            wake_reason=WakeReason.OPERATION_COMPLETED,
        )
    ) == Wait()


@pytest.mark.asyncio
async def test_llm_agent_preserves_a_dust_order_for_engine_audit() -> None:
    command = PlaceOrder(
        side=MarketSide.SELL,
        product=ProductId.RAW_MILK,
        quantity=Decimal("8.9E-91"),
        limit_price=Decimal("1.40"),
    )
    agent, _, repository = _llm_agent("dust_output", command)

    submitted = await agent.act(_turn("dust_output", _observation("farm_a")))

    assert submitted == command
    assert repository.list_invocations("dust_output")[0].outcome is InvocationOutcome.SUCCESS

@pytest.mark.asyncio
async def test_baseline_processor_procures_transforms_and_trades_while_busy() -> None:
    agent = BaselineCompanyAgent()
    observation = _observation("processor_a")

    assert await agent.act(_turn("processor_procure", observation)) == PlaceOrder(
        side=MarketSide.BUY,
        product=ProductId.RAW_MILK,
        quantity=Decimal("50"),
        limit_price=Decimal("1.60"),
    )

    raw_stock = _with_inventory(observation, raw_milk=Decimal("50"))
    assert await agent.act(
        _turn(
            "processor_transform",
            raw_stock,
            wake_reason=WakeReason.DELIVERY_COMPLETED,
        )
    ) == Transform(
        input_product=ProductId.RAW_MILK,
        output_product=ProductId.BOTTLED_MILK,
        input_quantity=Decimal("50"),
    )

    bottled_stock = _with_inventory(observation, bottled_milk=Decimal("20"))
    active_operation = OperationJobView(
        job_id="job_1",
        kind="transformation",
        completes_at=SimTime(absolute_minute=570),
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
    ) == PlaceOrder(
        side=MarketSide.SELL,
        product=ProductId.BOTTLED_MILK,
        quantity=Decimal("20"),
        limit_price=Decimal("2.50"),
    )


@pytest.mark.asyncio
async def test_baseline_retailer_prices_then_buys_only_uncovered_demand() -> None:
    agent = BaselineCompanyAgent()
    observation = _observation("retailer_a")

    assert await agent.act(_turn("retailer_price", observation)) == SetRetailPrice(
        product=ProductId.BOTTLED_MILK,
        unit_price=Decimal("3.50"),
    )

    priced = observation.model_copy(update={"retail_price": Decimal("3.50")})
    delivery = IncomingDeliveryView(
        trade_id="trade_1",
        product=ProductId.BOTTLED_MILK,
        quantity=Decimal("15"),
        arrives_at=SimTime(absolute_minute=570),
        expiry_buckets=(
            DeliveryExpiryBucket(
                quantity=Decimal("15"),
                expires_end_of_day=4,
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
    ) == PlaceOrder(
        side=MarketSide.BUY,
        product=ProductId.BOTTLED_MILK,
        quantity=Decimal("25"),
        limit_price=Decimal("2.80"),
    )


@pytest.mark.asyncio
async def test_baseline_does_not_duplicate_a_resting_order() -> None:
    agent = BaselineCompanyAgent()
    observation = _observation("processor_a")
    order = OpenOrderView(
        order_id="order_1",
        owner_id="processor_a",
        side=MarketSide.BUY,
        product=ProductId.RAW_MILK,
        remaining_quantity=Decimal("20"),
        limit_price=Decimal("1.60"),
        placed_at=SimTime(absolute_minute=540),
        priority_sequence=1,
    )

    assert await agent.act(
        _turn(
            "processor_wait",
            observation,
            wake_reason=WakeReason.PRICE_ALERT,
            open_orders=(order,),
        )
    ) == Wait()


@pytest.mark.asyncio
async def test_llm_agent_rejects_an_oversized_prompt_before_calling_provider(
    first_observation: CompanyObservation,
) -> None:
    agent, gateway, repository = _llm_agent(
        "prompt_limit",
        Wait(),
        memory_token_budget=100,
        max_prompt_tokens=200,
    )
    turn = _turn("prompt_limit", first_observation)

    with pytest.raises(ModelOutputError, match="prompt-token limit"):
        await agent.act(turn)

    assert gateway.command_requests == []
    invocation = repository.list_invocations("prompt_limit")[0]
    assert invocation.outcome is InvocationOutcome.AGENT_ERROR
    assert invocation.domain_turn_id == turn.turn_id


@pytest.mark.asyncio
async def test_llm_agent_audits_and_remembers_one_complete_turn(
    first_observation: CompanyObservation,
) -> None:
    run_id = "complete_cycle"
    command = Produce(product="raw_milk", quantity="12")
    agent, gateway, repository = _llm_agent(run_id, command)
    turn = _turn(run_id, first_observation)

    assert await agent.act(turn) == command

    envelope = CommandEnvelope(
        turn_id=turn.turn_id,
        command_id=f"{turn.turn_id}.command",
        company_id=turn.company_id,
        issued_at=turn.sim_time,
        state_version=turn.state_version,
        command=command,
    )
    outcome = CommandOutcome(
        turn_id=turn.turn_id,
        command_id=envelope.command_id,
        company_id=turn.company_id,
        occurred_at=turn.sim_time,
        status=CommandStatus.ACCEPTED,
        accepted=True,
        resulting_state_version=1,
        apply_sequence=1,
        next_available_at=turn.sim_time.plus(1),
    )
    record = TurnRecord(
        run_id=run_id,
        turn=turn,
        envelope=envelope,
        outcome=outcome,
        observation_hash=observation_hash(turn),
    )
    agent.remember(record)

    assert len(gateway.command_requests) == 1
    checkpoint = agent.checkpoint()
    assert checkpoint.revision == 1
    assert len(checkpoint.exchanges) == 1
    assert checkpoint.exchanges[0].model_dump() == record.model_dump()
    invocation = repository.list_invocations(run_id)[0]
    assert invocation.outcome is InvocationOutcome.SUCCESS
    assert invocation.command == command
    assert invocation.command_outcome == outcome
    assert invocation.apply_sequence == 1


@pytest.mark.asyncio
async def test_retried_domain_turn_preserves_each_physical_provider_call(
    first_observation: CompanyObservation,
) -> None:
    run_id = "physical_retry"
    repository = MemoryRunRepository()
    first, _, _ = _llm_agent(
        run_id,
        Wait(),
        repository=repository,
    )
    second, _, _ = _llm_agent(
        run_id,
        Wait(),
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
    scenario = DAIRY_S12_V3_SCENARIO.model_copy(update={"days": 1})
    run_id = "llm_runtime_cycle"
    repository = MemoryRunRepository()
    agents: dict[str, LlmCompanyAgent] = {}
    for company in scenario.companies:
        agent, _, _ = _llm_agent(
            run_id,
            Wait(),
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
    assert all(invocation.command_outcome is not None for invocation in invocations)
    assert len({invocation.prompt_hash for invocation in invocations}) == len(invocations)
    assert {company_id: agent.checkpoint().revision for company_id, agent in agents.items()} == {
        company.company_id: sum(
            record.turn.company_id == company.company_id for record in execution.turns
        )
        for company in scenario.companies
    }
