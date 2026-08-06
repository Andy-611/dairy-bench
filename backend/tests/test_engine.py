from collections.abc import Callable
from decimal import Decimal

import pytest
from pydantic import ValidationError

from company_bench.dairy_scenario import DAIRY_S9_SCENARIO
from company_bench.economics import OperatingEconomics
from company_bench.engine import EconomyEngine, EconomyState, PendingDelivery
from company_bench.models import (
    MAX_SEED,
    CompanyState,
    ConsumerSaleEvent,
    DayResult,
    DeliveryCompletedEvent,
    FarmOperation,
    InventoryExpiredEvent,
    InventoryLot,
    MilkProcessedEvent,
    MilkProducedEvent,
    ProcessorOperation,
    ProductId,
    TradeExecutedEvent,
)
from company_bench.runtime_models import (
    CommandEnvelope,
    CommandOutcome,
    CompanyCommand,
    DeliveryExpiryBucket,
    MarketSide,
    Produce,
    QuoteLevel,
    QuoteLevelAction,
    SetQuoteLadder,
    SetRetailPrice,
    SimTime,
    SystemEventKind,
    Transform,
)


@pytest.fixture
def engine() -> EconomyEngine:
    """Return one stateless V3 economy engine."""
    return EconomyEngine()


@pytest.fixture
def economy(engine: EconomyEngine) -> EconomyState:
    """Open the canonical first business day."""
    return engine.open_day(engine.initial_state(DAIRY_S9_SCENARIO, seed=7))


def test_initial_decision_projections_are_economically_empty(
    engine: EconomyEngine,
    economy: EconomyState,
) -> None:
    assert engine.marked_surplus(economy, "farm_a") == Decimal("0")
    assert engine.inventory_expiry(economy, "farm_a") == ()


def _at(hour: int, minute: int = 0, *, day: int = 0) -> SimTime:
    """Build a concise simulation timestamp."""
    return SimTime.at(day=day, hour=hour, minute=minute)


def _envelope(
    economy: EconomyState,
    company_id: str,
    command: CompanyCommand,
    at: SimTime,
    sequence: int,
) -> CommandEnvelope:
    """Bind a command to the current shared state version."""
    identity = f"{company_id}.{at.absolute_minute}.{sequence}"
    return CommandEnvelope(
        turn_id=f"turn.{identity}",
        command_id=f"command.{identity}",
        company_id=company_id,
        issued_at=at,
        state_version=economy.state_version,
        command=command,
    )


def _apply(
    engine: EconomyEngine,
    economy: EconomyState,
    company_id: str,
    command: CompanyCommand,
    at: SimTime,
    sequence: int,
) -> tuple[EconomyState, CommandOutcome]:
    """Apply one command and unwrap its sole outcome."""
    updated, outcomes = engine.apply_batch(
        economy,
        (_envelope(economy, company_id, command, at, sequence),),
        first_apply_sequence=sequence,
    )
    return updated, outcomes[0]


def _company(economy: EconomyState, company_id: str) -> CompanyState:
    """Return one company's available economic state."""
    return next(company for company in economy.companies if company.company_id == company_id)


def _quantity(economy: EconomyState, company_id: str, product: ProductId) -> Decimal:
    """Aggregate currently available inventory."""
    return sum(
        (lot.quantity for lot in _company(economy, company_id).inventory if lot.product is product),
        start=Decimal("0"),
    )


def _produce(quantity: Decimal | str) -> Produce:
    """Build the canonical farm command."""
    return Produce(product=ProductId.RAW_MILK, quantity=Decimal(quantity))


def _transform(quantity: Decimal | str) -> Transform:
    """Build the canonical processor command."""
    return Transform(
        input_product=ProductId.RAW_MILK,
        output_product=ProductId.BOTTLED_MILK,
        input_quantity=Decimal(quantity),
    )


def _order(
    side: MarketSide,
    product: ProductId,
    quantity: Decimal | str,
    price: Decimal | str,
) -> SetQuoteLadder:
    """Build one concise limit order."""
    return SetQuoteLadder(
        side=side,
        product=product,
        levels=(
            QuoteLevel(
                quantity=Decimal(quantity),
                limit_price=Decimal(price),
            ),
        ),
    )


def _ladder(
    side: MarketSide,
    product: ProductId,
    levels: tuple[tuple[str, str], ...],
) -> SetQuoteLadder:
    """Build one concise multi-level target ladder as price-quantity pairs."""
    return SetQuoteLadder(
        side=side,
        product=product,
        levels=tuple(
            QuoteLevel(quantity=Decimal(quantity), limit_price=Decimal(price))
            for price, quantity in levels
        ),
    )


def _order_id(outcome: CommandOutcome) -> str:
    """Return the sole target order identity from a one-level ladder result."""
    result = outcome.quote_ladder_result
    assert result is not None and len(result.levels) == 1
    return result.levels[0].order_id


def _open_with_inventory(
    engine: EconomyEngine,
    company_id: str,
    product: ProductId,
    quantity: Decimal,
    *,
    cash: Decimal = Decimal("1000"),
) -> EconomyState:
    """Open day one with one explicit test-only starting lot."""
    return _open_with_lots(
        engine,
        company_id,
        (
            InventoryLot(
                lot_id=f"seed.{company_id}.{product.value}",
                product=product,
                quantity=quantity,
                produced_day=1,
                expires_end_of_day=5,
            ),
        ),
        cash=cash,
    )


def _open_with_lots(
    engine: EconomyEngine,
    company_id: str,
    lots: tuple[InventoryLot, ...],
    *,
    cash: Decimal = Decimal("1000"),
) -> EconomyState:
    """Open day one with explicit test-only inventory lots."""
    world = engine.initial_state(DAIRY_S9_SCENARIO, seed=7)
    companies = tuple(
        company.model_copy(
            update={
                "cash": cash,
                "inventory": lots,
            }
        )
        if company.company_id == company_id
        else company
        for company in world.companies
    )
    return engine.open_day(world.model_copy(update={"companies": companies}))


def _produce_ready(
    engine: EconomyEngine,
    economy: EconomyState,
    quantity: Decimal,
    *,
    start: SimTime | None = None,
    sequence: int = 1,
) -> EconomyState:
    """Produce one farm lot and complete its scheduled job."""
    started_at = start or _at(9)
    started, outcome = _apply(
        engine,
        economy,
        "farm_a",
        _produce(quantity),
        started_at,
        sequence,
    )
    assert outcome.accepted and outcome.job_id is not None
    return engine.complete_operation(
        started,
        outcome.job_id,
        outcome.scheduled_completions[0].at,
    )


def _run_operation(
    engine: EconomyEngine,
    economy: EconomyState,
    company_id: str,
    command: CompanyCommand,
    start: SimTime,
    sequence: int,
) -> tuple[EconomyState, Decimal]:
    """Start and complete one operation, returning its authoritative cash cost."""
    started, outcome = _apply(engine, economy, company_id, command, start, sequence)
    assert outcome.accepted and outcome.job_id is not None
    job = next(item for item in started.jobs if item.job_id == outcome.job_id)
    completed = engine.complete_operation(
        started,
        outcome.job_id,
        outcome.scheduled_completions[0].at,
    )
    return completed, job.cash_cost


def _close_day(engine: EconomyEngine, economy: EconomyState) -> DayResult:
    """Run the fixed 19:00 and 19:30 system transitions."""
    market_close = _at(19, day=economy.day - 1)
    closed = engine.close_markets(economy, market_close)
    settled = engine.settle_consumer_sales(closed, market_close)
    return engine.close_day(settled, _at(19, 30, day=economy.day - 1))


def test_current_scenario_has_nine_typed_companies_and_balanced_nominal_capacity() -> None:
    scenario = DAIRY_S9_SCENARIO
    expected_ids = tuple(
        f"{tier}_{suffix}"
        for tier in ("farm", "processor", "retailer")
        for suffix in ("a", "b", "c")
    )
    operation_kinds = tuple(company.operation.kind for company in scenario.companies)

    processor_output_capacity = sum(
        (
            company.operation.capacity.normal_capacity * company.operation.yield_rate
            for company in scenario.companies
            if isinstance(company.operation, ProcessorOperation)
        ),
        start=Decimal(),
    )
    reference_demand = scenario.demand.base_demand * operation_kinds.count("retailer")

    assert scenario.scenario_id == "flow.dairy.base.s9.v4"
    assert scenario.version == 4
    assert scenario.days == 30
    assert tuple(company.company_id for company in scenario.companies) == expected_ids
    assert tuple(company.initial_cash for company in scenario.companies) == (Decimal("1000"),) * 9
    assert operation_kinds.count("farm") == 3
    assert operation_kinds.count("processor") == 3
    assert operation_kinds.count("retailer") == 3
    assert processor_output_capacity == reference_demand == Decimal("120")
    for operation_type in (FarmOperation, ProcessorOperation):
        operations = tuple(
            company.operation
            for company in scenario.companies
            if isinstance(company.operation, operation_type)
        )
        assert len({(operation.capacity, operation.cost) for operation in operations}) == 1
    assert scenario.runtime.open_minute == 9 * 60
    assert scenario.runtime.close_minute == 19 * 60
    assert scenario.runtime.day_close_minute == 19 * 60 + 30
    assert scenario.runtime.operation_duration_minutes == 30
    assert scenario.runtime.delivery_duration_minutes == 30


@pytest.mark.parametrize("seed", [-1, True, MAX_SEED + 1])
def test_engine_rejects_invalid_seed(seed: int) -> None:
    with pytest.raises(ValueError, match="seed must be"):
        EconomyEngine().initial_state(DAIRY_S9_SCENARIO, seed)


@pytest.mark.parametrize(
    "command_factory",
    [
        lambda: Produce(product=ProductId.RAW_MILK, quantity=Decimal("0.00001")),
        lambda: Transform(
            input_product=ProductId.RAW_MILK,
            output_product=ProductId.BOTTLED_MILK,
            input_quantity=Decimal("0.00001"),
        ),
    ],
)
def test_operation_commands_reject_dust_quantities(
    command_factory: Callable[[], CompanyCommand],
) -> None:
    with pytest.raises(ValidationError, match=r"exact multiple of 0\.0001"):
        command_factory()


def test_production_is_atomic_busy_capacity_limited_and_completes_in_thirty_minutes(
    engine: EconomyEngine,
    economy: EconomyState,
) -> None:
    original = economy
    capacity = engine.remaining_operation_capacity(economy, "farm_a")
    assert capacity is not None
    economy, oversized = _apply(
        engine,
        economy,
        "farm_a",
        _produce(capacity + Decimal("0.0001")),
        _at(9),
        1,
    )
    assert not oversized.accepted and "capacity" in oversized.reason
    assert economy == original

    economy, started = _apply(
        engine,
        economy,
        "farm_a",
        _produce(capacity),
        _at(9),
        2,
    )
    assert started.accepted and started.job_id is not None
    job = economy.jobs[0]
    expected_surplus = (
        job.quantity * economy.scenario.product(job.product).reference_value - job.cash_cost
    )
    assert started.scheduled_completions[0].kind is SystemEventKind.OPERATION_COMPLETED
    assert started.scheduled_completions[0].at == _at(9, 30)
    assert _company(economy, "farm_a").cash == Decimal("1000") - job.cash_cost
    assert _quantity(economy, "farm_a", ProductId.RAW_MILK) == 0
    assert engine.remaining_operation_capacity(economy, "farm_a") == 0
    assert engine.marked_surplus(economy, "farm_a") == expected_surplus
    assert engine.inventory_expiry(economy, "farm_a") == ()

    busy_state, busy = _apply(
        engine,
        economy,
        "farm_a",
        _produce("1"),
        _at(9, 1),
        3,
    )
    assert not busy.accepted and "busy" in busy.reason
    assert busy_state == economy
    with pytest.raises(ValueError, match="scheduled time"):
        engine.complete_operation(economy, started.job_id, _at(9, 29))

    economy = engine.complete_operation(economy, started.job_id, _at(9, 30))
    event = economy.events[-1]
    assert isinstance(event, MilkProducedEvent)
    assert event.actual_quantity == capacity
    assert _quantity(economy, "farm_a", ProductId.RAW_MILK) == capacity
    assert engine.marked_surplus(economy, "farm_a") == expected_surplus
    assert engine.inventory_expiry(economy, "farm_a")[0].available_quantity == capacity

    unchanged, exhausted = _apply(
        engine,
        economy,
        "farm_a",
        _produce("1"),
        _at(9, 31),
        4,
    )
    assert not exhausted.accepted and "capacity" in exhausted.reason
    assert unchanged == economy


@pytest.mark.parametrize(
    ("inventory", "cash", "requested_quantity", "reason"),
    [
        (Decimal("10"), Decimal("1000"), Decimal("11"), "inventory"),
        (Decimal("10"), Decimal("1"), Decimal("10"), "cash"),
    ],
)
def test_transformation_rejects_an_infeasible_whole_job_without_side_effects(
    engine: EconomyEngine,
    inventory: Decimal,
    cash: Decimal,
    requested_quantity: Decimal,
    reason: str,
) -> None:
    economy = _open_with_inventory(
        engine,
        "processor_a",
        ProductId.RAW_MILK,
        inventory,
        cash=cash,
    )
    original = economy

    economy, outcome = _apply(
        engine,
        economy,
        "processor_a",
        _transform(requested_quantity),
        _at(9),
        1,
    )

    assert not outcome.accepted and reason in outcome.reason
    assert economy == original


def test_transformation_consumes_input_then_completes_with_yield_and_daily_limit(
    engine: EconomyEngine,
) -> None:
    economy = _open_with_inventory(
        engine,
        "processor_a",
        ProductId.RAW_MILK,
        Decimal("100"),
    )
    capacity = engine.remaining_operation_capacity(economy, "processor_a")
    assert capacity is not None
    economy, started = _apply(
        engine,
        economy,
        "processor_a",
        _transform(capacity),
        _at(9),
        1,
    )

    assert started.accepted and started.job_id is not None
    job = economy.jobs[0]
    expected_surplus = (
        (Decimal("100") - capacity)
        * economy.scenario.product(ProductId.RAW_MILK).reference_value
        + job.output_quantity
        * economy.scenario.product(ProductId.BOTTLED_MILK).reference_value
        - job.cash_cost
    )
    assert started.scheduled_completions[0].at == _at(9, 30)
    assert _company(economy, "processor_a").cash == Decimal("1000") - job.cash_cost
    assert _quantity(economy, "processor_a", ProductId.RAW_MILK) == Decimal("100") - capacity
    assert _quantity(economy, "processor_a", ProductId.BOTTLED_MILK) == 0
    assert engine.remaining_operation_capacity(economy, "processor_a") == 0
    assert engine.marked_surplus(economy, "processor_a") == expected_surplus
    expiry = engine.inventory_expiry(economy, "processor_a")
    assert len(expiry) == 1
    assert expiry[0].product is ProductId.RAW_MILK
    assert expiry[0].available_quantity == Decimal("100") - capacity

    unchanged, busy = _apply(
        engine,
        economy,
        "processor_a",
        _transform("1"),
        _at(9, 1),
        2,
    )
    assert not busy.accepted and "busy" in busy.reason
    assert unchanged == economy

    economy = engine.complete_operation(economy, started.job_id, _at(9, 30))
    event = economy.events[-1]
    assert isinstance(event, MilkProcessedEvent)
    assert event.actual_input == capacity
    assert event.output_quantity == capacity * Decimal("0.8")
    assert _quantity(economy, "processor_a", ProductId.BOTTLED_MILK) == (
        capacity * Decimal("0.8")
    )
    assert engine.marked_surplus(economy, "processor_a") == expected_surplus
    expiry_by_product = {
        bucket.product: bucket.available_quantity
        for bucket in engine.inventory_expiry(economy, "processor_a")
    }
    assert expiry_by_product == {
        ProductId.RAW_MILK: Decimal("100") - capacity,
        ProductId.BOTTLED_MILK: capacity * Decimal("0.8"),
    }

    unchanged, exhausted = _apply(
        engine,
        economy,
        "processor_a",
        _transform("1"),
        _at(9, 31),
        3,
    )
    assert not exhausted.accepted and "capacity" in exhausted.reason
    assert unchanged == economy


@pytest.mark.parametrize(
    ("company_id", "command", "with_input_inventory"),
    [
        ("farm_a", _produce("10"), False),
        ("processor_a", _transform("10"), True),
    ],
)
def test_later_equal_batches_cost_more_without_split_order_discount(
    engine: EconomyEngine,
    economy: EconomyState,
    company_id: str,
    command: CompanyCommand,
    with_input_inventory: bool,
) -> None:
    if with_input_inventory:
        economy = _open_with_inventory(
            engine,
            company_id,
            ProductId.RAW_MILK,
            Decimal("100"),
        )
    initial_operation = next(
        state for state in economy.operation_states if state.company_id == company_id
    )
    operation = economy.scenario.company(company_id).operation
    assert isinstance(operation, (FarmOperation, ProcessorOperation))

    economy, first_cost = _run_operation(
        engine,
        economy,
        company_id,
        command,
        _at(9),
        1,
    )
    economy, second_cost = _run_operation(
        engine,
        economy,
        company_id,
        command,
        _at(9, 31),
        2,
    )
    combined_cost = operation.cost.incremental_cost(
        daily_capacity=initial_operation.daily_capacity,
        used_capacity=Decimal("0"),
        quantity=Decimal("20"),
        daily_base_unit_cost=initial_operation.daily_base_unit_cost,
    )

    assert second_cost > first_cost
    assert first_cost + second_cost == combined_cost
    assert _company(economy, company_id).cash == Decimal("1000") - combined_cost


def test_bid_collateral_blocks_operation_until_atomic_ladder_cancellation(
    engine: EconomyEngine,
) -> None:
    """Keep quote collateral and private operation capacity in one atomic ledger."""
    quantity = Decimal("10")
    probe = _open_with_inventory(
        engine,
        "processor_a",
        ProductId.RAW_MILK,
        quantity,
    )
    operation_state = next(
        state for state in probe.operation_states if state.company_id == "processor_a"
    )
    operation = probe.scenario.company("processor_a").operation
    assert isinstance(operation, ProcessorOperation)
    operation_cost = operation.cost.incremental_cost(
        daily_capacity=operation_state.daily_capacity,
        used_capacity=operation_state.used_capacity,
        quantity=quantity,
        daily_base_unit_cost=operation_state.daily_base_unit_cost,
    )
    economy = _open_with_inventory(
        engine,
        "processor_a",
        ProductId.RAW_MILK,
        quantity,
        cash=operation_cost,
    )

    economy, quoted = _apply(
        engine,
        economy,
        "processor_a",
        _order(MarketSide.BUY, ProductId.RAW_MILK, "1", "1"),
        _at(9),
        1,
    )
    assert quoted.accepted
    quoted_state = economy

    economy, blocked = _apply(
        engine,
        economy,
        "processor_a",
        _transform(quantity),
        _at(9, 1),
        2,
    )
    assert not blocked.accepted and "cash" in blocked.reason
    assert economy == quoted_state
    assert engine.remaining_operation_capacity(economy, "processor_a") == (
        operation_state.daily_capacity
    )

    economy, cancelled = _apply(
        engine,
        economy,
        "processor_a",
        SetQuoteLadder(
            product=ProductId.RAW_MILK,
            side=MarketSide.BUY,
            levels=(),
        ),
        _at(9, 2),
        3,
    )
    assert cancelled.accepted
    assert engine.reserved_cash(economy, "processor_a") == 0

    economy, started = _apply(
        engine,
        economy,
        "processor_a",
        _transform(quantity),
        _at(9, 3),
        4,
    )
    assert started.accepted
    assert economy.jobs[0].cash_cost == operation_cost
    assert _company(economy, "processor_a").cash == 0
    assert engine.remaining_operation_capacity(economy, "processor_a") == (
        operation_state.daily_capacity - quantity
    )


def test_orders_require_authorization_and_full_cash_or_inventory_collateral(
    engine: EconomyEngine,
    economy: EconomyState,
) -> None:
    original = economy
    rejected_commands: tuple[tuple[str, CompanyCommand, str], ...] = (
        (
            "farm_a",
            _order(MarketSide.BUY, ProductId.RAW_MILK, "1", "1"),
            "authorized",
        ),
        (
            "farm_a",
            _order(MarketSide.SELL, ProductId.RAW_MILK, "1", "1"),
            "inventory",
        ),
        (
            "processor_a",
            _order(MarketSide.BUY, ProductId.RAW_MILK, "100", "11"),
            "cash",
        ),
    )
    for sequence, (company_id, command, reason) in enumerate(rejected_commands, start=1):
        economy, outcome = _apply(
            engine,
            economy,
            company_id,
            command,
            _at(9, sequence - 1),
            sequence,
        )
        assert not outcome.accepted and reason in outcome.reason
        assert economy == original

    economy, backed = _apply(
        engine,
        economy,
        "processor_a",
        _order(MarketSide.BUY, ProductId.RAW_MILK, "100", "10"),
        _at(9, 3),
        4,
    )
    assert backed.accepted
    assert _company(economy, "processor_a").cash == 0
    assert engine.reserved_cash(economy, "processor_a") == Decimal("1000")
    assert engine.company_orders(economy, "processor_a")[0].remaining_quantity == 100

    unchanged, overcommitted = _apply(
        engine,
        economy,
        "processor_a",
        _ladder(
            MarketSide.BUY,
            ProductId.RAW_MILK,
            (("10", "100"), ("9", "1")),
        ),
        _at(9, 4),
        5,
    )
    assert not overcommitted.accepted and "cash" in overcommitted.reason
    assert unchanged == economy


@pytest.mark.parametrize("quantity", ("8.9E-91", "59.999999999999999"))
def test_unquantized_quote_level_is_rejected_at_the_schema_boundary(
    economy: EconomyState,
    quantity: str,
) -> None:
    with pytest.raises(ValidationError, match=r"exact multiple of 0\.0001"):
        _order(MarketSide.BUY, ProductId.RAW_MILK, quantity, "1")
    assert economy.state_version == 0


@pytest.mark.parametrize("quantity", ("1E+24", "1E+999999"))
def test_large_tick_aligned_order_is_rejected_without_decimal_failure(
    engine: EconomyEngine,
    economy: EconomyState,
    quantity: str,
) -> None:
    unchanged, outcome = _apply(
        engine,
        economy,
        "processor_a",
        _order(MarketSide.BUY, ProductId.RAW_MILK, quantity, "1"),
        _at(9),
        1,
    )

    assert not outcome.accepted
    assert "cash" in outcome.reason
    assert unchanged == economy


def test_out_of_range_order_quantity_is_rejected_without_decimal_failure(
    engine: EconomyEngine,
    economy: EconomyState,
) -> None:
    unchanged, outcome = _apply(
        engine,
        economy,
        "processor_a",
        _order(MarketSide.BUY, ProductId.RAW_MILK, "1E+1000000", "1"),
        _at(9),
        1,
    )

    assert not outcome.accepted
    assert "supported range" in outcome.reason
    assert unchanged == economy


@pytest.mark.parametrize("quantity", ("8.9E-91", "59.999999999999999"))
def test_invalid_ladder_update_cannot_disturb_the_original_commitment(
    engine: EconomyEngine,
    economy: EconomyState,
    quantity: str,
) -> None:
    economy, placed = _apply(
        engine,
        economy,
        "processor_a",
        _order(MarketSide.BUY, ProductId.RAW_MILK, "1", "1"),
        _at(9),
        1,
    )
    with pytest.raises(ValidationError, match=r"exact multiple of 0\.0001"):
        _order(MarketSide.BUY, ProductId.RAW_MILK, quantity, "1")
    assert engine.company_orders(economy, "processor_a")[0].order_id == _order_id(placed)


def test_minimum_legal_partial_fill_preserves_market_state_invariants(
    engine: EconomyEngine,
    economy: EconomyState,
) -> None:
    economy = _produce_ready(engine, economy, Decimal("60"))
    economy, sell = _apply(
        engine,
        economy,
        "farm_a",
        _order(MarketSide.SELL, ProductId.RAW_MILK, "59.9999", "1.50"),
        _at(9, 31),
        2,
    )
    economy, buy = _apply(
        engine,
        economy,
        "processor_a",
        _order(MarketSide.BUY, ProductId.RAW_MILK, "0.0001", "2.00"),
        _at(9, 32),
        3,
    )

    assert sell.accepted and buy.accepted
    order = engine.company_orders(economy, "farm_a")[0]
    assert order.remaining_quantity == Decimal("59.9998")
    assert (
        sum(
            (bucket.reserved_quantity for bucket in engine.inventory_expiry(economy, "farm_a")),
            start=Decimal("0"),
        )
        == order.remaining_quantity
    )
    assert engine.pending_delivery_views(economy, "processor_a")[0].quantity == Decimal("0.0001")
    assert EconomyState.model_validate_json(economy.model_dump_json()) == economy


def test_match_pays_seller_now_and_delivers_inventory_thirty_minutes_later(
    engine: EconomyEngine,
    economy: EconomyState,
) -> None:
    economy = _produce_ready(engine, economy, Decimal("10"))
    at = _at(9, 31)
    sell = _envelope(
        economy,
        "farm_a",
        _order(MarketSide.SELL, ProductId.RAW_MILK, "10", "1.50"),
        at,
        2,
    )
    buy = _envelope(
        economy,
        "processor_a",
        _order(MarketSide.BUY, ProductId.RAW_MILK, "6", "2.00"),
        at,
        3,
    )

    economy, outcomes = engine.apply_batch(
        economy,
        (sell, buy),
        first_apply_sequence=2,
        apply_sequences=(2, 3),
    )

    assert all(outcome.accepted for outcome in outcomes)
    assert outcomes[0].events == ()
    assert len(outcomes[1].events) == 1
    trade = outcomes[1].events[0]
    assert isinstance(trade, TradeExecutedEvent)
    assert trade.quantity == Decimal("6")
    assert trade.unit_price == Decimal("1.50")
    assert trade.total_value == Decimal("9.00")
    production = next(event for event in economy.events if isinstance(event, MilkProducedEvent))
    assert _company(economy, "farm_a").cash == (
        Decimal("1000") - production.cash_cost + trade.total_value
    )
    assert _company(economy, "processor_a").cash == Decimal("991.00")
    assert _quantity(economy, "farm_a", ProductId.RAW_MILK) == 0
    farm_expiry = engine.inventory_expiry(economy, "farm_a")
    assert len(farm_expiry) == 1
    assert farm_expiry[0].available_quantity == 0
    assert farm_expiry[0].reserved_quantity == 4
    assert _quantity(economy, "processor_a", ProductId.RAW_MILK) == 0
    assert engine.pending_delivery_views(economy, "processor_a")[0].quantity == 6
    assert engine.inventory_expiry(economy, "processor_a") == ()
    raw_reference = economy.scenario.product(ProductId.RAW_MILK).reference_value
    expected_farm_surplus = (
        production.actual_quantity * raw_reference
        - production.cash_cost
        + trade.total_value
        - trade.quantity * raw_reference
    )
    assert engine.marked_surplus(economy, "farm_a") == expected_farm_surplus
    assert engine.marked_surplus(economy, "processor_a") == Decimal("-3")

    completion = outcomes[1].scheduled_completions[0]
    assert completion.kind is SystemEventKind.DELIVERY_COMPLETED
    assert completion.at == _at(10, 1)
    assert completion.reference_id == economy.deliveries[0].delivery_id

    economy = engine.complete_delivery(economy, completion.reference_id, completion.at)
    assert _quantity(economy, "processor_a", ProductId.RAW_MILK) == Decimal("6")
    assert economy.deliveries == ()
    assert isinstance(economy.events[-1], DeliveryCompletedEvent)
    assert engine.marked_surplus(economy, "processor_a") == Decimal("-3")
    assert engine.inventory_expiry(economy, "processor_a")[0].available_quantity == 6


def test_pending_delivery_view_preserves_each_expiry_bucket() -> None:
    delivery = PendingDelivery(
        delivery_id="delivery_1",
        trade_id="trade_1",
        buyer_id="processor_a",
        arrives_at=_at(10),
        lots=(
            InventoryLot(
                lot_id="delivery_lot_1",
                product=ProductId.RAW_MILK,
                quantity=Decimal("2"),
                produced_day=1,
                expires_end_of_day=1,
            ),
            InventoryLot(
                lot_id="delivery_lot_2",
                product=ProductId.RAW_MILK,
                quantity=Decimal("3"),
                produced_day=1,
                expires_end_of_day=2,
            ),
        ),
    )

    assert delivery.view().expiry_buckets == (
        DeliveryExpiryBucket(quantity=Decimal("2"), expires_end_of_day=1),
        DeliveryExpiryBucket(quantity=Decimal("3"), expires_end_of_day=2),
    )


def test_inventory_expiry_splits_available_and_fefo_reserved_quantities(
    engine: EconomyEngine,
) -> None:
    economy = _open_with_lots(
        engine,
        "farm_a",
        (
            InventoryLot(
                lot_id="seed.farm_a.raw.early",
                product=ProductId.RAW_MILK,
                quantity=Decimal("4"),
                produced_day=1,
                expires_end_of_day=2,
            ),
            InventoryLot(
                lot_id="seed.farm_a.raw.late",
                product=ProductId.RAW_MILK,
                quantity=Decimal("6"),
                produced_day=1,
                expires_end_of_day=3,
            ),
        ),
    )
    economy, placed = _apply(
        engine,
        economy,
        "farm_a",
        _order(MarketSide.SELL, ProductId.RAW_MILK, "5", "2"),
        _at(9),
        1,
    )

    assert placed.accepted and _order_id(placed)
    buckets = engine.inventory_expiry(economy, "farm_a")
    assert tuple(
        (
            bucket.expires_end_of_day,
            bucket.available_quantity,
            bucket.reserved_quantity,
        )
        for bucket in buckets
    ) == (
        (2, Decimal("0"), Decimal("4")),
        (3, Decimal("5"), Decimal("1")),
    )
    assert engine.marked_surplus(economy, "farm_a") == Decimal("10")


def test_replace_is_atomic_and_cancel_releases_remaining_collateral(
    engine: EconomyEngine,
    economy: EconomyState,
) -> None:
    economy, placed = _apply(
        engine,
        economy,
        "processor_a",
        _order(MarketSide.BUY, ProductId.RAW_MILK, "10", "1"),
        _at(9),
        1,
    )
    placed_id = _order_id(placed)
    economy, replaced = _apply(
        engine,
        economy,
        "processor_a",
        _order(MarketSide.BUY, ProductId.RAW_MILK, "4", "2"),
        _at(9, 1),
        2,
    )

    replaced_id = _order_id(replaced)
    assert replaced.accepted and replaced_id != placed_id
    assert replaced.quote_ladder_result is not None
    assert replaced.quote_ladder_result.levels[0].action is QuoteLevelAction.REPLACE
    assert _company(economy, "processor_a").cash == Decimal("992")
    assert engine.reserved_cash(economy, "processor_a") == Decimal("8")
    assert engine.marked_surplus(economy, "processor_a") == 0
    order = engine.company_orders(economy, "processor_a")[0]
    assert order.order_id == replaced_id
    assert order.priority_sequence == 2

    unchanged, rejected = _apply(
        engine,
        economy,
        "processor_a",
        _order(MarketSide.BUY, ProductId.RAW_MILK, "1000", "2"),
        _at(9, 2),
        3,
    )
    assert not rejected.accepted and "cash" in rejected.reason
    assert unchanged == economy
    assert engine.marked_surplus(unchanged, "processor_a") == 0

    economy, cancelled = _apply(
        engine,
        economy,
        "processor_a",
        SetQuoteLadder(
            product=ProductId.RAW_MILK,
            side=MarketSide.BUY,
            levels=(),
        ),
        _at(9, 3),
        4,
    )
    assert cancelled.accepted
    assert _company(economy, "processor_a").cash == Decimal("1000")
    assert engine.reserved_cash(economy, "processor_a") == 0
    assert engine.company_orders(economy, "processor_a") == ()
    assert engine.marked_surplus(economy, "processor_a") == 0


def test_active_state_rejects_order_identity_reused_across_product_books(
    engine: EconomyEngine,
    economy: EconomyState,
) -> None:
    economy, placed = _apply(
        engine,
        economy,
        "processor_a",
        _order(MarketSide.BUY, ProductId.RAW_MILK, "1", "1"),
        _at(9),
        1,
    )
    assert placed.accepted
    raw_order = economy.markets[0].orders[0]
    duplicate = raw_order.model_copy(update={"product": ProductId.BOTTLED_MILK})
    invalid = economy.model_copy(
        update={
            "markets": (
                economy.markets[0],
                economy.markets[1].model_copy(update={"orders": (duplicate,)}),
            )
        }
    )

    with pytest.raises(ValueError, match="global market order ids"):
        EconomyState.model_validate(invalid.model_dump())


def test_consumer_sales_run_after_market_close_and_day_closes_at_nineteen_thirty(
    engine: EconomyEngine,
) -> None:
    economy = _open_with_inventory(
        engine,
        "retailer_a",
        ProductId.BOTTLED_MILK,
        Decimal("10"),
    )
    economy, outcome = _apply(
        engine,
        economy,
        "retailer_a",
        SetRetailPrice(
            product=ProductId.BOTTLED_MILK,
            unit_price=Decimal("3.50"),
        ),
        _at(9),
        1,
    )
    assert outcome.accepted
    with pytest.raises(ValueError, match="markets must close"):
        engine.settle_consumer_sales(economy, _at(19))

    economy = engine.close_markets(economy, _at(19))
    assert all(not market.is_open and not market.orders for market in economy.markets)
    economy = engine.settle_consumer_sales(economy, _at(19))

    sales = [event for event in economy.events if isinstance(event, ConsumerSaleEvent)]
    retailer_sale = next(event for event in sales if event.company_id == "retailer_a")
    assert len(sales) == 3
    assert retailer_sale.sold_quantity == Decimal("10")
    assert retailer_sale.revenue == Decimal("35.00")
    assert _company(economy, "retailer_a").cash == Decimal("1035.00")
    assert _quantity(economy, "retailer_a", ProductId.BOTTLED_MILK) == 0
    assert engine.marked_surplus(economy, "retailer_a") == Decimal("35.00")

    with pytest.raises(ValueError, match="wrong configured minute"):
        engine.close_day(economy, _at(19, 29))
    result = engine.close_day(economy, _at(19, 30))
    assert result.state.day == 1
    assert result.snapshot.consumer_sales == Decimal("10")
    assert result.snapshot.markets == result.state.previous_markets
    assert result.events == economy.events
    retailer_snapshot = next(
        company for company in result.snapshot.companies if company.company_id == "retailer_a"
    )
    assert retailer_snapshot.surplus == Decimal("35.00")


def test_marked_surplus_recognizes_expiry_only_at_day_close(
    engine: EconomyEngine,
) -> None:
    economy = _open_with_lots(
        engine,
        "farm_a",
        (
            InventoryLot(
                lot_id="seed.farm_a.expiring",
                product=ProductId.RAW_MILK,
                quantity=Decimal("3"),
                produced_day=1,
                expires_end_of_day=1,
            ),
        ),
    )

    assert engine.marked_surplus(economy, "farm_a") == Decimal("3")
    assert engine.inventory_expiry(economy, "farm_a")[0].expires_end_of_day == 1

    result = _close_day(engine, economy)
    farm_snapshot = next(
        company for company in result.snapshot.companies if company.company_id == "farm_a"
    )
    expiry = next(event for event in result.events if isinstance(event, InventoryExpiredEvent))
    assert expiry.reference_value_loss == Decimal("3")
    assert farm_snapshot.surplus == Decimal("0")

    day_two = engine.open_day(result.state)
    assert engine.marked_surplus(day_two, "farm_a") == Decimal("0")
    assert engine.inventory_expiry(day_two, "farm_a") == ()


def test_consumer_demand_remains_continuous_after_price_adjustment(
    engine: EconomyEngine,
) -> None:
    economy = _open_with_inventory(
        engine,
        "retailer_a",
        ProductId.BOTTLED_MILK,
        Decimal("100"),
    )
    economy, outcome = _apply(
        engine,
        economy,
        "retailer_a",
        SetRetailPrice(
            product=ProductId.BOTTLED_MILK,
            unit_price=Decimal("3.55"),
        ),
        _at(9),
        1,
    )
    assert outcome.accepted

    economy = engine.close_markets(economy, _at(19))
    economy = engine.settle_consumer_sales(economy, _at(19))
    sale = next(
        event
        for event in economy.events
        if isinstance(event, ConsumerSaleEvent) and event.company_id == "retailer_a"
    )

    assert sale.demand_quantity == sale.potential_demand_quantity - Decimal("0.40")
    assert sale.demand_quantity != sale.demand_quantity.to_integral_value()


def test_operation_started_at_eighteen_fifty_nine_finishes_before_day_close(
    engine: EconomyEngine,
    economy: EconomyState,
) -> None:
    economy, started = _apply(
        engine,
        economy,
        "farm_a",
        _produce("1"),
        _at(18, 59),
        1,
    )
    assert started.accepted and started.job_id is not None
    assert started.scheduled_completions[0].at == _at(19, 29)

    economy = engine.close_markets(economy, _at(19))
    economy = engine.settle_consumer_sales(economy, _at(19))
    economy = engine.complete_operation(economy, started.job_id, _at(19, 29))
    result = engine.close_day(economy, _at(19, 30))

    farm = next(company for company in result.state.companies if company.company_id == "farm_a")
    assert sum((lot.quantity for lot in farm.inventory), start=Decimal("0")) == 1
    assert isinstance(result.events[-1], MilkProducedEvent)


def test_lot_sequence_is_persisted_across_business_days(engine: EconomyEngine) -> None:
    world = engine.initial_state(DAIRY_S9_SCENARIO, seed=17)
    day_one = _produce_ready(engine, engine.open_day(world), Decimal("1"))
    day_one_operation = next(
        state for state in day_one.operation_states if state.company_id == "farm_a"
    )
    first_lot = _company(day_one, "farm_a").inventory[0]
    first_sequence = day_one.next_lot_sequence
    result = _close_day(engine, day_one)

    assert first_lot.lot_id.endswith(".l1")
    assert result.state.next_lot_sequence == first_sequence == 2
    day_two = engine.open_day(result.state)
    day_two_operation = next(
        state for state in day_two.operation_states if state.company_id == "farm_a"
    )
    economics = OperatingEconomics(DAIRY_S9_SCENARIO, seed=17)
    reset_day_two = economics.open_day(2, economics.initial_states())
    reset_farm = next(state for state in reset_day_two if state.company_id == "farm_a")

    assert day_two.next_lot_sequence == first_sequence
    assert day_one_operation.used_capacity == Decimal("1")
    assert day_two_operation.used_capacity == 0
    assert day_two_operation.availability != reset_farm.availability

    day_two = _produce_ready(
        engine,
        day_two,
        Decimal("1"),
        start=_at(9, day=1),
        sequence=2,
    )
    lot_ids = {lot.lot_id for lot in _company(day_two, "farm_a").inventory}
    assert any(lot_id.endswith(".l2") for lot_id in lot_ids)
    assert day_two.next_lot_sequence == 3


def test_observation_hides_seed_and_exposes_public_rules(engine: EconomyEngine) -> None:
    world = engine.initial_state(DAIRY_S9_SCENARIO, seed=314159)
    observation = engine.observe(world)[0]
    active_observation = engine.observe_active(engine.open_day(world), "farm_a")
    visible = observation.model_dump()

    assert "seed" not in visible
    assert observation.observation_id == "flow.dairy.base.s9.v4|1|farm_a"
    assert observation.products == DAIRY_S9_SCENARIO.products
    assert observation.demand == DAIRY_S9_SCENARIO.demand
    assert observation.scoring == DAIRY_S9_SCENARIO.scoring
    assert observation.runtime == DAIRY_S9_SCENARIO.runtime
    assert observation.daily_operation == active_observation.daily_operation
    assert observation.daily_operation is not None
    assert tuple(company.company_id for company in observation.public_companies) == tuple(
        company.company_id for company in DAIRY_S9_SCENARIO.companies
    )
    assert all(not hasattr(company, "initial_cash") for company in observation.public_companies)
    assert all(not hasattr(company, "daily_operation") for company in observation.public_companies)
    retailer = next(item for item in engine.observe(world) if item.company_id == "retailer_a")
    assert retailer.daily_operation is None
