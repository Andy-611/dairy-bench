from decimal import Decimal

import pytest

from company_bench.dairy_scenario import DAIRY_S9_V3_SCENARIO
from company_bench.engine import EconomyEngine, EconomyState, PendingDelivery
from company_bench.models import (
    MAX_SEED,
    CompanyState,
    ConsumerSaleEvent,
    DayResult,
    DeliveryCompletedEvent,
    InventoryLot,
    MilkProcessedEvent,
    MilkProducedEvent,
    ProcessorOperation,
    ProductId,
    TradeExecutedEvent,
)
from company_bench.runtime_models import (
    CancelOrder,
    CommandEnvelope,
    CommandOutcome,
    CompanyCommand,
    DeliveryExpiryBucket,
    MarketSide,
    PlaceOrder,
    Produce,
    ReplaceOrder,
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
    return engine.open_day(engine.initial_state(DAIRY_S9_V3_SCENARIO, seed=7))


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
        (
            lot.quantity
            for lot in _company(economy, company_id).inventory
            if lot.product is product
        ),
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
) -> PlaceOrder:
    """Build one concise limit order."""
    return PlaceOrder(
        side=side,
        product=product,
        quantity=Decimal(quantity),
        limit_price=Decimal(price),
    )


def _open_with_inventory(
    engine: EconomyEngine,
    company_id: str,
    product: ProductId,
    quantity: Decimal,
    *,
    cash: Decimal = Decimal("1000"),
) -> EconomyState:
    """Open day one with one explicit test-only starting lot."""
    world = engine.initial_state(DAIRY_S9_V3_SCENARIO, seed=7)
    companies = tuple(
        company.model_copy(
            update={
                "cash": cash,
                "inventory": (
                    InventoryLot(
                        lot_id=f"seed.{company_id}.{product.value}",
                        product=product,
                        quantity=quantity,
                        produced_day=1,
                        expires_end_of_day=5,
                    ),
                ),
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


def _close_day(engine: EconomyEngine, economy: EconomyState) -> DayResult:
    """Run the fixed 19:00 and 19:30 system transitions."""
    market_close = _at(19, day=economy.day - 1)
    closed = engine.close_markets(economy, market_close)
    settled = engine.settle_consumer_sales(closed, market_close)
    return engine.close_day(settled, _at(19, 30, day=economy.day - 1))


def test_v3_scenario_has_nine_typed_companies_and_balanced_capacity() -> None:
    scenario = DAIRY_S9_V3_SCENARIO
    expected_ids = tuple(
        f"{tier}_{suffix}"
        for tier in ("farm", "processor", "retailer")
        for suffix in ("a", "b", "c")
    )
    operation_kinds = tuple(company.operation.kind for company in scenario.companies)

    processor_output_capacity = sum(
        (
            company.operation.daily_input_capacity * company.operation.yield_rate
            for company in scenario.companies
            if isinstance(company.operation, ProcessorOperation)
        ),
        start=Decimal(),
    )
    reference_demand = scenario.demand.base_demand * operation_kinds.count("retailer")

    assert scenario.scenario_id == "flow.dairy.base.s9.v3"
    assert scenario.version == 3
    assert scenario.days == 30
    assert tuple(company.company_id for company in scenario.companies) == expected_ids
    assert tuple(company.initial_cash for company in scenario.companies) == (
        Decimal("1000"),
    ) * 9
    assert operation_kinds.count("farm") == 3
    assert operation_kinds.count("processor") == 3
    assert operation_kinds.count("retailer") == 3
    assert processor_output_capacity == reference_demand == Decimal("120")
    assert scenario.runtime.open_minute == 9 * 60
    assert scenario.runtime.close_minute == 19 * 60
    assert scenario.runtime.day_close_minute == 19 * 60 + 30
    assert scenario.runtime.operation_duration_minutes == 30
    assert scenario.runtime.delivery_duration_minutes == 30


@pytest.mark.parametrize("seed", [-1, True, MAX_SEED + 1])
def test_engine_rejects_invalid_seed(seed: int) -> None:
    with pytest.raises(ValueError, match="seed must be"):
        EconomyEngine().initial_state(DAIRY_S9_V3_SCENARIO, seed)


def test_production_is_atomic_busy_capacity_limited_and_completes_in_thirty_minutes(
    engine: EconomyEngine,
    economy: EconomyState,
) -> None:
    original = economy
    economy, oversized = _apply(
        engine,
        economy,
        "farm_a",
        _produce("61"),
        _at(9),
        1,
    )
    assert not oversized.accepted and "capacity" in oversized.reason
    assert economy == original

    economy, started = _apply(
        engine,
        economy,
        "farm_a",
        _produce("60"),
        _at(9),
        2,
    )
    assert started.accepted and started.job_id is not None
    assert started.scheduled_completions[0].kind is SystemEventKind.OPERATION_COMPLETED
    assert started.scheduled_completions[0].at == _at(9, 30)
    assert _company(economy, "farm_a").cash == Decimal("940")
    assert _quantity(economy, "farm_a", ProductId.RAW_MILK) == 0
    assert engine.remaining_operation_capacity(economy, "farm_a") == 0

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
    assert event.actual_quantity == Decimal("60")
    assert _quantity(economy, "farm_a", ProductId.RAW_MILK) == Decimal("60")

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
        Decimal("50"),
    )
    economy, started = _apply(
        engine,
        economy,
        "processor_a",
        _transform("50"),
        _at(9),
        1,
    )

    assert started.accepted and started.job_id is not None
    assert started.scheduled_completions[0].at == _at(9, 30)
    assert _company(economy, "processor_a").cash == Decimal("980")
    assert _quantity(economy, "processor_a", ProductId.RAW_MILK) == 0
    assert _quantity(economy, "processor_a", ProductId.BOTTLED_MILK) == 0
    assert engine.remaining_operation_capacity(economy, "processor_a") == 0

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
    assert event.actual_input == Decimal("50")
    assert event.output_quantity == Decimal("40.0")
    assert _quantity(economy, "processor_a", ProductId.BOTTLED_MILK) == Decimal("40.0")

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
        _order(MarketSide.BUY, ProductId.RAW_MILK, "1", "1"),
        _at(9, 4),
        5,
    )
    assert not overcommitted.accepted and "cash" in overcommitted.reason
    assert unchanged == economy


@pytest.mark.parametrize("quantity", ("8.9E-91", "59.999999999999999"))
def test_unquantized_place_order_is_rejected_before_assets_change(
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
    assert "exact multiple of 0.0001" in outcome.reason
    assert unchanged == economy


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
def test_unquantized_replace_order_preserves_the_original_commitment(
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
    unchanged, outcome = _apply(
        engine,
        economy,
        "processor_a",
        ReplaceOrder(
            order_id=placed.order_id,
            quantity=Decimal(quantity),
            limit_price=Decimal("1"),
        ),
        _at(9, 1),
        2,
    )

    assert not outcome.accepted
    assert "exact multiple of 0.0001" in outcome.reason
    assert unchanged == economy


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
    assert sum(
        (position.quantity for position in engine.reserved_inventory(economy, "farm_a")),
        start=Decimal("0"),
    ) == order.remaining_quantity
    assert engine.pending_delivery_views(economy, "processor_a")[0].quantity == Decimal(
        "0.0001"
    )
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
    assert _company(economy, "farm_a").cash == Decimal("999.00")
    assert _company(economy, "processor_a").cash == Decimal("991.00")
    assert _quantity(economy, "farm_a", ProductId.RAW_MILK) == 0
    assert engine.reserved_inventory(economy, "farm_a")[0].quantity == 4
    assert _quantity(economy, "processor_a", ProductId.RAW_MILK) == 0
    assert engine.pending_delivery_views(economy, "processor_a")[0].quantity == 6

    completion = outcomes[1].scheduled_completions[0]
    assert completion.kind is SystemEventKind.DELIVERY_COMPLETED
    assert completion.at == _at(10, 1)
    assert completion.reference_id == economy.deliveries[0].delivery_id

    economy = engine.complete_delivery(economy, completion.reference_id, completion.at)
    assert _quantity(economy, "processor_a", ProductId.RAW_MILK) == Decimal("6")
    assert economy.deliveries == ()
    assert isinstance(economy.events[-1], DeliveryCompletedEvent)


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
    assert placed.order_id is not None
    economy, replaced = _apply(
        engine,
        economy,
        "processor_a",
        ReplaceOrder(
            order_id=placed.order_id,
            quantity=Decimal("4"),
            limit_price=Decimal("2"),
        ),
        _at(9, 1),
        2,
    )

    assert replaced.accepted and replaced.order_id != placed.order_id
    assert _company(economy, "processor_a").cash == Decimal("992")
    assert engine.reserved_cash(economy, "processor_a") == Decimal("8")
    order = engine.company_orders(economy, "processor_a")[0]
    assert order.order_id == replaced.order_id
    assert order.priority_sequence == 2

    unchanged, rejected = _apply(
        engine,
        economy,
        "processor_a",
        ReplaceOrder(
            order_id=replaced.order_id,
            quantity=Decimal("1000"),
            limit_price=Decimal("2"),
        ),
        _at(9, 2),
        3,
    )
    assert not rejected.accepted and "cash" in rejected.reason
    assert unchanged == economy

    economy, cancelled = _apply(
        engine,
        economy,
        "processor_a",
        CancelOrder(order_id=replaced.order_id),
        _at(9, 3),
        4,
    )
    assert cancelled.accepted
    assert _company(economy, "processor_a").cash == Decimal("1000")
    assert engine.reserved_cash(economy, "processor_a") == 0
    assert engine.company_orders(economy, "processor_a") == ()


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

    with pytest.raises(ValueError, match="wrong configured minute"):
        engine.close_day(economy, _at(19, 29))
    result = engine.close_day(economy, _at(19, 30))
    assert result.state.day == 1
    assert result.snapshot.consumer_sales == Decimal("10")
    assert result.snapshot.markets == result.state.previous_markets
    assert result.events == economy.events


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
    world = engine.initial_state(DAIRY_S9_V3_SCENARIO, seed=17)
    day_one = _produce_ready(engine, engine.open_day(world), Decimal("1"))
    first_lot = _company(day_one, "farm_a").inventory[0]
    first_sequence = day_one.next_lot_sequence
    result = _close_day(engine, day_one)

    assert first_lot.lot_id.endswith(".l1")
    assert result.state.next_lot_sequence == first_sequence == 2
    day_two = engine.open_day(result.state)
    assert day_two.next_lot_sequence == first_sequence

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
    world = engine.initial_state(DAIRY_S9_V3_SCENARIO, seed=314159)
    observation = engine.observe(world)[0]
    visible = observation.model_dump()

    assert "seed" not in visible
    assert observation.observation_id == "flow.dairy.base.s9.v3|1|farm_a"
    assert observation.products == DAIRY_S9_V3_SCENARIO.products
    assert observation.demand == DAIRY_S9_V3_SCENARIO.demand
    assert observation.scoring == DAIRY_S9_V3_SCENARIO.scoring
    assert observation.runtime == DAIRY_S9_V3_SCENARIO.runtime
    assert tuple(company.company_id for company in observation.public_companies) == tuple(
        company.company_id for company in DAIRY_S9_V3_SCENARIO.companies
    )
    assert all(not hasattr(company, "initial_cash") for company in observation.public_companies)
