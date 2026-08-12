from decimal import Decimal

import pytest
from pydantic import ValidationError

from company_bench.domain.calendar import SimDay, Weekday
from company_bench.domain.models import (
    MAX_SEED,
    CompanyBankruptEvent,
    CompanyState,
    CompanyStatus,
    ConsumerSaleEvent,
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
from company_bench.domain.scenario import DAIRY_S9_SCENARIO
from company_bench.economy.engine import EconomyEngine, EconomyState
from company_bench.runtime.models import (
    ActionDecision,
    AttentionPlan,
    DecisionEnvelope,
    DecisionOutcome,
    EconomicCommand,
    MarketSide,
    Produce,
    QuoteLevel,
    QuoteLevelAction,
    SetQuoteLadder,
    SetRetailPrice,
    Transform,
)

ZERO = Decimal()


@pytest.fixture
def engine() -> EconomyEngine:
    """Return one stateless economy engine."""
    return EconomyEngine()


@pytest.fixture
def economy(engine: EconomyEngine) -> EconomyState:
    """Open the canonical first trading week."""
    return engine.open_week(engine.initial_state(DAIRY_S9_SCENARIO, seed=7))


def _day(weekday: Weekday, *, week: int = 1) -> SimDay:
    """Build one named day in a trading week."""
    return SimDay.at(week=week, weekday=weekday)


def _envelope(
    economy: EconomyState,
    company_id: str,
    command: EconomicCommand,
    on: SimDay,
    sequence: int,
) -> DecisionEnvelope:
    """Bind one command to authoritative runtime identity and state."""
    identity = f"{company_id}.d{on.absolute_day}.{sequence}"
    return DecisionEnvelope(
        turn_id=f"turn.{identity}",
        decision_id=f"decision.{identity}",
        company_id=company_id,
        issued_on=on,
        state_version=economy.state_version,
        decision=ActionDecision(action=command, attention=AttentionPlan()),
    )


def _apply(
    engine: EconomyEngine,
    economy: EconomyState,
    company_id: str,
    command: EconomicCommand,
    on: SimDay,
    sequence: int,
) -> tuple[EconomyState, DecisionOutcome]:
    """Apply one command and return its sole outcome."""
    updated, outcomes = engine.apply_batch(
        economy,
        (_envelope(economy, company_id, command, on, sequence),),
        first_apply_sequence=sequence,
    )
    return updated, outcomes[0]


def _company(economy: EconomyState, company_id: str) -> CompanyState:
    """Return one company's available state."""
    return next(company for company in economy.companies if company.company_id == company_id)


def _quantity(economy: EconomyState, company_id: str, product: ProductId) -> Decimal:
    """Aggregate one company's available product inventory."""
    return sum(
        (
            lot.quantity
            for lot in _company(economy, company_id).inventory
            if lot.product is product
        ),
        start=ZERO,
    )


def _produce(quantity: Decimal | str) -> Produce:
    """Build a raw-milk production command."""
    return Produce(product=ProductId.RAW_MILK, quantity=Decimal(quantity))


def _transform(quantity: Decimal | str) -> Transform:
    """Build a raw-to-bottled transformation command."""
    return Transform(
        input_product=ProductId.RAW_MILK,
        output_product=ProductId.BOTTLED_MILK,
        input_quantity=Decimal(quantity),
    )


def _quote(
    side: MarketSide,
    product: ProductId,
    quantity: Decimal | str,
    price: Decimal | str,
) -> SetQuoteLadder:
    """Build one single-level target quote ladder."""
    return SetQuoteLadder(
        side=side,
        product=product,
        levels=(
            QuoteLevel(quantity=Decimal(quantity), limit_price=Decimal(price)),
        ),
    )


def _cancel(side: MarketSide, product: ProductId) -> SetQuoteLadder:
    """Build an empty target ladder that cancels one side."""
    return SetQuoteLadder(side=side, product=product, levels=())


def _open_with_lots(
    engine: EconomyEngine,
    company_id: str,
    lots: tuple[InventoryLot, ...],
    *,
    cash: Decimal = Decimal("1000"),
) -> EconomyState:
    """Open week one with explicit test inventory for one company."""
    world = engine.initial_state(DAIRY_S9_SCENARIO, seed=7)
    companies = tuple(
        company.model_copy(update={"cash": cash, "inventory": lots})
        if company.company_id == company_id
        else company
        for company in world.companies
    )
    return engine.open_week(world.model_copy(update={"companies": companies}))


def _open_with_inventory(
    engine: EconomyEngine,
    company_id: str,
    product: ProductId,
    quantity: Decimal | str,
    *,
    expires_end_of_week: int = 4,
    cash: Decimal = Decimal("1000"),
) -> EconomyState:
    """Open week one with one traceable lot."""
    return _open_with_lots(
        engine,
        company_id,
        (
            InventoryLot(
                lot_id=f"seed.{company_id}.{product.value}",
                product=product,
                quantity=Decimal(quantity),
                produced_week=1,
                expires_end_of_week=expires_end_of_week,
            ),
        ),
        cash=cash,
    )


def _complete_job(
    engine: EconomyEngine,
    economy: EconomyState,
    outcome: DecisionOutcome,
) -> EconomyState:
    """Complete the operation scheduled by one accepted decision."""
    assert outcome.accepted and outcome.job_id is not None
    completion = outcome.scheduled_completions[0]
    return engine.complete_operation(economy, outcome.job_id, completion.scheduled_for)


def _settle_week(engine: EconomyEngine, economy: EconomyState):
    """Run the fixed Sunday close, consumer settlement, and week close."""
    sunday = _day(Weekday.SUNDAY, week=economy.week)
    closed = engine.close_markets(economy, sunday)
    sold = engine.settle_consumer_sales(closed, sunday)
    return engine.close_week(sold, sunday)


def test_scenario_uses_52_weeks_and_formula_driven_weekly_quantities(
    economy: EconomyState,
) -> None:
    scenario = economy.scenario

    assert scenario.weeks == 52
    assert scenario.runtime.operation_duration_days == 1
    assert scenario.runtime.delivery_duration_days == 1
    assert scenario.runtime.max_turns_per_company_week == 6
    assert tuple(product.shelf_life_weeks for product in scenario.products) == (2, 4)
    assert scenario.demand.base_demand == Decimal("40.0000")
    assert tuple(
        company.operation.capacity.normal_capacity
        for company in scenario.companies
        if isinstance(company.operation, (FarmOperation, ProcessorOperation))
    ) == (
        Decimal("60.0000"),
        Decimal("60.0000"),
        Decimal("60.0000"),
        Decimal("50.0000"),
        Decimal("50.0000"),
        Decimal("50.0000"),
    )
    assert any(
        state.weekly_capacity
        != scenario.company(state.company_id).operation.capacity.normal_capacity
        for state in economy.operation_states
    )


@pytest.mark.parametrize("seed", [-1, True, MAX_SEED + 1])
def test_engine_rejects_invalid_seed(engine: EconomyEngine, seed: int) -> None:
    with pytest.raises(ValueError, match="seed must be an integer"):
        engine.initial_state(DAIRY_S9_SCENARIO, seed)


def test_bankruptcy_uses_a_strict_sub_one_asset_threshold(
    engine: EconomyEngine,
) -> None:
    world = engine.initial_state(DAIRY_S9_SCENARIO, seed=7)
    companies = tuple(
        company.model_copy(
            update={
                "cash": (
                    Decimal("1.0000")
                    if company.company_id == "farm_a"
                    else Decimal("0.9999")
                    if company.company_id == "farm_b"
                    else company.cash
                )
            }
        )
        for company in world.companies
    )

    economy = engine.open_week(world.model_copy(update={"companies": companies}))

    assert _company(economy, "farm_a").status is CompanyStatus.ACTIVE
    assert _company(economy, "farm_b").status is CompanyStatus.BANKRUPT
    assert tuple(
        event.company_id
        for event in economy.events
        if isinstance(event, CompanyBankruptEvent)
    ) == ("farm_b",)
    with pytest.raises(ValueError, match="bankrupt companies"):
        engine.observe_active(economy, "farm_b", _day(Weekday.MONDAY))


def test_bankruptcy_delists_company_without_cancelling_funded_delivery(
    engine: EconomyEngine,
) -> None:
    world = engine.initial_state(DAIRY_S9_SCENARIO, seed=7)
    raw_lot = InventoryLot(
        lot_id="seed.farm_a.raw",
        product=ProductId.RAW_MILK,
        quantity=Decimal("0.5"),
        produced_week=1,
        expires_end_of_week=2,
    )
    bottled_lot = InventoryLot(
        lot_id="seed.processor_a.bottled",
        product=ProductId.BOTTLED_MILK,
        quantity=Decimal("0.2"),
        produced_week=1,
        expires_end_of_week=4,
    )
    companies = tuple(
        company.model_copy(update={"inventory": (raw_lot,)})
        if company.company_id == "farm_a"
        else company.model_copy(
            update={"cash": Decimal("1.6"), "inventory": (bottled_lot,)}
        )
        if company.company_id == "processor_a"
        else company
        for company in world.companies
    )
    economy = engine.open_week(world.model_copy(update={"companies": companies}))
    monday = _day(Weekday.MONDAY)
    economy, bottled_ask = _apply(
        engine,
        economy,
        "processor_a",
        _quote(MarketSide.SELL, ProductId.BOTTLED_MILK, "0.2", "2"),
        monday,
        1,
    )
    economy, _ = _apply(
        engine,
        economy,
        "farm_a",
        _quote(MarketSide.SELL, ProductId.RAW_MILK, "0.5", "3.2"),
        monday,
        2,
    )
    economy, purchase = _apply(
        engine,
        economy,
        "processor_a",
        _quote(MarketSide.BUY, ProductId.RAW_MILK, "0.5", "3.2"),
        monday,
        3,
    )

    bankruptcy = next(
        event
        for event in purchase.events
        if isinstance(event, CompanyBankruptEvent)
    )
    assert purchase.accepted
    assert _company(economy, "processor_a").status is CompanyStatus.BANKRUPT
    assert bankruptcy.total_assets == Decimal("0.8500")
    assert bankruptcy.cancelled_order_ids == (
        bottled_ask.quote_ladder_result.levels[0].order_id,
    )
    assert economy.deliveries
    assert all(
        order.owner_id != "processor_a"
        for market in economy.markets
        for order in market.orders
    )

    delivery = economy.deliveries[0]
    delivered = engine.complete_delivery(economy, delivery.delivery_id, delivery.arrives_on)

    assert _company(delivered, "processor_a").status is CompanyStatus.BANKRUPT
    assert _quantity(delivered, "processor_a", ProductId.RAW_MILK) == Decimal("0.5")
    rejected, outcome = _apply(
        engine,
        delivered,
        "processor_a",
        SetRetailPrice(product=ProductId.BOTTLED_MILK, unit_price=Decimal("3")),
        delivery.arrives_on,
        4,
    )
    assert rejected == delivered
    assert not outcome.accepted
    assert outcome.reason == "company is bankrupt and delisted"


def test_observations_expose_monday_and_private_weekly_economics(
    engine: EconomyEngine,
) -> None:
    world = engine.initial_state(DAIRY_S9_SCENARIO, seed=7)
    observations = engine.observe(world)

    assert len(observations) == 9
    assert {observation.sim_day for observation in observations} == {_day(Weekday.MONDAY)}
    assert all(observation.scenario_weeks == 52 for observation in observations)
    assert all("seed" not in observation.model_dump() for observation in observations)
    assert all(
        (observation.weekly_operation is not None)
        == isinstance(observation.operation, (FarmOperation, ProcessorOperation))
        for observation in observations
    )


def test_active_observations_require_the_current_decision_week(
    engine: EconomyEngine,
    economy: EconomyState,
) -> None:
    with pytest.raises(ValueError, match="current active week"):
        engine.observe_active(economy, "farm_a", _day(Weekday.MONDAY, week=2))
    with pytest.raises(ValueError, match="Monday-Saturday"):
        engine.observe_active(economy, "farm_a", _day(Weekday.SUNDAY))


def test_production_costs_cash_and_completes_one_day_later(
    engine: EconomyEngine,
    economy: EconomyState,
) -> None:
    monday = _day(Weekday.MONDAY)
    starting_cash = _company(economy, "farm_a").cash
    started, outcome = _apply(engine, economy, "farm_a", _produce("10"), monday, 1)

    assert outcome.accepted
    assert outcome.next_available_on == _day(Weekday.TUESDAY)
    assert outcome.scheduled_completions[0].scheduled_for == _day(Weekday.TUESDAY)
    assert _company(started, "farm_a").cash < starting_cash
    assert _quantity(started, "farm_a", ProductId.RAW_MILK) == ZERO

    completed = _complete_job(engine, started, outcome)
    lot = _company(completed, "farm_a").inventory[0]

    assert lot.quantity == Decimal("10.0000")
    assert lot.produced_week == 1
    assert lot.expires_end_of_week == 2
    assert isinstance(completed.events[-1], MilkProducedEvent)
    assert completed.events[-1].occurred_on == _day(Weekday.TUESDAY)


def test_operation_is_busy_and_weekly_capacity_is_atomic(
    engine: EconomyEngine,
    economy: EconomyState,
) -> None:
    monday = _day(Weekday.MONDAY)
    started, first = _apply(engine, economy, "farm_a", _produce("1"), monday, 1)
    busy, second = _apply(engine, started, "farm_a", _produce("1"), monday, 2)

    assert first.accepted
    assert not second.accepted
    assert "busy" in (second.reason or "")
    assert busy == started

    capacity = economy.operation_states[0].weekly_capacity
    rejected, overrun = _apply(
        engine,
        economy,
        "farm_a",
        _produce(capacity + Decimal("0.0001")),
        monday,
        3,
    )
    assert not overrun.accepted
    assert "production capacity" in (overrun.reason or "")
    assert rejected == economy


def test_equal_later_batches_have_higher_convex_cost(
    engine: EconomyEngine,
    economy: EconomyState,
) -> None:
    first_started, first = _apply(
        engine,
        economy,
        "farm_a",
        _produce("10"),
        _day(Weekday.MONDAY),
        1,
    )
    first_job = first_started.jobs[0]
    after_first = _complete_job(engine, first_started, first)
    second_started, second = _apply(
        engine,
        after_first,
        "farm_a",
        _produce("10"),
        _day(Weekday.WEDNESDAY),
        2,
    )
    second_job = second_started.jobs[0]

    assert second.accepted
    assert second_job.cash_cost > first_job.cash_cost


def test_transformation_consumes_input_and_produces_bottled_milk_next_day(
    engine: EconomyEngine,
) -> None:
    economy = _open_with_inventory(
        engine,
        "processor_a",
        ProductId.RAW_MILK,
        "20",
        expires_end_of_week=2,
    )
    started, outcome = _apply(
        engine,
        economy,
        "processor_a",
        _transform("10"),
        _day(Weekday.MONDAY),
        1,
    )

    assert outcome.accepted
    assert _quantity(started, "processor_a", ProductId.RAW_MILK) == Decimal("10.0000")
    completed = _complete_job(engine, started, outcome)
    bottled = next(
        lot
        for lot in _company(completed, "processor_a").inventory
        if lot.product is ProductId.BOTTLED_MILK
    )

    assert bottled.quantity == Decimal("8.0000")
    assert bottled.expires_end_of_week == 4
    assert isinstance(completed.events[-1], MilkProcessedEvent)


def test_quote_authorization_and_collateral_fail_without_side_effects(
    engine: EconomyEngine,
    economy: EconomyState,
) -> None:
    monday = _day(Weekday.MONDAY)
    unauthorized, wrong_side = _apply(
        engine,
        economy,
        "farm_a",
        _quote(MarketSide.BUY, ProductId.RAW_MILK, "1", "1"),
        monday,
        1,
    )
    unstocked, no_inventory = _apply(
        engine,
        economy,
        "farm_a",
        _quote(MarketSide.SELL, ProductId.RAW_MILK, "1", "1"),
        monday,
        2,
    )
    overfunded, no_cash = _apply(
        engine,
        economy,
        "processor_a",
        _quote(MarketSide.BUY, ProductId.RAW_MILK, "1000", "2"),
        monday,
        3,
    )

    assert not wrong_side.accepted and "not authorized" in (wrong_side.reason or "")
    assert not no_inventory.accepted and "inventory" in (no_inventory.reason or "")
    assert not no_cash.accepted and "cash" in (no_cash.reason or "")
    assert unauthorized == unstocked == overfunded == economy


def test_quote_ladder_keeps_replaces_and_cancels_identity_atomically(
    engine: EconomyEngine,
) -> None:
    economy = _open_with_inventory(engine, "farm_a", ProductId.RAW_MILK, "20")
    monday = _day(Weekday.MONDAY)
    quoted, placed = _apply(
        engine,
        economy,
        "farm_a",
        _quote(MarketSide.SELL, ProductId.RAW_MILK, "10", "1.2"),
        monday,
        1,
    )
    original = placed.quote_ladder_result
    assert original is not None
    original_id = original.levels[0].order_id

    kept_state, kept = _apply(
        engine,
        quoted,
        "farm_a",
        _quote(MarketSide.SELL, ProductId.RAW_MILK, "10", "1.2"),
        _day(Weekday.TUESDAY),
        2,
    )
    assert kept.quote_ladder_result is not None
    assert kept.quote_ladder_result.levels[0].action is QuoteLevelAction.KEEP
    assert kept.quote_ladder_result.levels[0].order_id == original_id
    assert kept_state == quoted

    replaced_state, replaced = _apply(
        engine,
        quoted,
        "farm_a",
        _quote(MarketSide.SELL, ProductId.RAW_MILK, "8", "1.3"),
        _day(Weekday.WEDNESDAY),
        3,
    )
    assert replaced.quote_ladder_result is not None
    assert replaced.quote_ladder_result.levels[0].action is QuoteLevelAction.REPLACE
    assert replaced.quote_ladder_result.levels[0].replaced_order_id == original_id

    cancelled_state, cancelled = _apply(
        engine,
        replaced_state,
        "farm_a",
        _cancel(MarketSide.SELL, ProductId.RAW_MILK),
        _day(Weekday.THURSDAY),
        4,
    )
    assert cancelled.quote_ladder_result is not None
    assert cancelled.quote_ladder_result.cancelled_order_ids
    assert engine.company_orders(cancelled_state, "farm_a") == ()
    assert _quantity(cancelled_state, "farm_a", ProductId.RAW_MILK) == Decimal("20.0000")


def test_trade_pays_immediately_and_delivers_one_day_later(
    engine: EconomyEngine,
) -> None:
    economy = _open_with_inventory(
        engine,
        "farm_a",
        ProductId.RAW_MILK,
        "10",
        expires_end_of_week=2,
    )
    seller_cash = _company(economy, "farm_a").cash
    buyer_cash = _company(economy, "processor_a").cash
    quoted, _ = _apply(
        engine,
        economy,
        "farm_a",
        _quote(MarketSide.SELL, ProductId.RAW_MILK, "10", "1.5"),
        _day(Weekday.MONDAY),
        1,
    )
    traded, outcome = _apply(
        engine,
        quoted,
        "processor_a",
        _quote(MarketSide.BUY, ProductId.RAW_MILK, "4", "1.5"),
        _day(Weekday.MONDAY),
        2,
    )

    assert outcome.accepted
    assert isinstance(outcome.events[0], TradeExecutedEvent)
    assert _company(traded, "farm_a").cash == seller_cash + Decimal("6.0000")
    assert _company(traded, "processor_a").cash == buyer_cash - Decimal("6.0000")
    delivery = traded.deliveries[0]
    assert delivery.arrives_on == _day(Weekday.TUESDAY)
    assert _quantity(traded, "processor_a", ProductId.RAW_MILK) == ZERO
    pending = engine.pending_delivery_views(traded, "processor_a")[0]
    assert pending.expiry_buckets[0].expires_end_of_week == 2

    delivered = engine.complete_delivery(traded, delivery.delivery_id, delivery.arrives_on)
    assert _quantity(delivered, "processor_a", ProductId.RAW_MILK) == Decimal("4.0000")
    assert isinstance(delivered.events[-1], DeliveryCompletedEvent)


def test_inventory_expiry_distinguishes_available_and_reserved_fefo_lots(
    engine: EconomyEngine,
) -> None:
    economy = _open_with_lots(
        engine,
        "farm_a",
        (
            InventoryLot(
                lot_id="early",
                product=ProductId.RAW_MILK,
                quantity=Decimal("3"),
                produced_week=1,
                expires_end_of_week=1,
            ),
            InventoryLot(
                lot_id="later",
                product=ProductId.RAW_MILK,
                quantity=Decimal("4"),
                produced_week=1,
                expires_end_of_week=2,
            ),
        ),
    )
    quoted, _ = _apply(
        engine,
        economy,
        "farm_a",
        _quote(MarketSide.SELL, ProductId.RAW_MILK, "5", "1.5"),
        _day(Weekday.MONDAY),
        1,
    )

    buckets = engine.inventory_expiry(quoted, "farm_a")
    assert (buckets[0].available_quantity, buckets[0].reserved_quantity) == (
        ZERO,
        Decimal("3.0000"),
    )
    assert (buckets[1].available_quantity, buckets[1].reserved_quantity) == (
        Decimal("2.0000"),
        Decimal("2.0000"),
    )


def test_sunday_enforces_close_sale_and_expiry_order(engine: EconomyEngine) -> None:
    economy = _open_with_inventory(
        engine,
        "retailer_a",
        ProductId.BOTTLED_MILK,
        "50",
        expires_end_of_week=1,
    )
    priced, outcome = _apply(
        engine,
        economy,
        "retailer_a",
        SetRetailPrice(product=ProductId.BOTTLED_MILK, unit_price=Decimal("3.5")),
        _day(Weekday.MONDAY),
        1,
    )
    assert outcome.accepted

    with pytest.raises(ValueError, match="Sunday"):
        engine.close_markets(priced, _day(Weekday.SATURDAY))
    with pytest.raises(ValueError, match="markets must close"):
        engine.settle_consumer_sales(priced, _day(Weekday.SUNDAY))

    sunday = _day(Weekday.SUNDAY)
    closed = engine.close_markets(priced, sunday)
    with pytest.raises(ValueError, match="consumer sales must settle"):
        engine.close_week(closed, sunday)
    settled = engine.settle_consumer_sales(closed, sunday)
    result = engine.close_week(settled, sunday)

    sale = next(
        event
        for event in result.events
        if isinstance(event, ConsumerSaleEvent) and event.company_id == "retailer_a"
    )
    assert sale.occurred_on == sunday
    assert sale.sold_quantity > ZERO
    assert result.snapshot.week == 1
    assert result.snapshot.consumer_sales == sale.sold_quantity
    assert any(isinstance(event, InventoryExpiredEvent) for event in result.events)
    assert result.state.completed_weeks == 1


def test_raw_milk_produced_in_week_one_expires_at_end_of_week_two(
    engine: EconomyEngine,
    economy: EconomyState,
) -> None:
    started, outcome = _apply(
        engine,
        economy,
        "farm_a",
        _produce("5"),
        _day(Weekday.MONDAY),
        1,
    )
    completed = _complete_job(engine, started, outcome)
    week_one = _settle_week(engine, completed)
    assert _quantity(engine.open_week(week_one.state), "farm_a", ProductId.RAW_MILK) == Decimal(
        "5.0000"
    )

    week_two_economy = engine.open_week(week_one.state)
    week_two = _settle_week(engine, week_two_economy)
    expired = [event for event in week_two.events if isinstance(event, InventoryExpiredEvent)]

    assert len(expired) == 1
    assert expired[0].occurred_on == _day(Weekday.SUNDAY, week=2)
    assert _quantity(engine.open_week(week_two.state), "farm_a", ProductId.RAW_MILK) == ZERO


def test_retail_prices_and_weekly_books_reset_only_after_sunday(
    engine: EconomyEngine,
) -> None:
    economy = _open_with_inventory(engine, "farm_a", ProductId.RAW_MILK, "5")
    quoted, _ = _apply(
        engine,
        economy,
        "farm_a",
        _quote(MarketSide.SELL, ProductId.RAW_MILK, "5", "1.5"),
        _day(Weekday.MONDAY),
        1,
    )
    priced, _ = _apply(
        engine,
        quoted,
        "retailer_a",
        SetRetailPrice(product=ProductId.BOTTLED_MILK, unit_price=Decimal("3.5")),
        _day(Weekday.SATURDAY),
        2,
    )

    assert engine.company_orders(priced, "farm_a")
    observation = engine.observe_active(priced, "retailer_a", _day(Weekday.SATURDAY))
    assert observation.retail_price == Decimal("3.5000")

    next_week = engine.open_week(_settle_week(engine, priced).state)
    assert engine.company_orders(next_week, "farm_a") == ()
    assert engine.observe_active(
        next_week,
        "retailer_a",
        _day(Weekday.MONDAY, week=2),
    ).retail_price is None


def test_schema_rejects_sub_quantum_operation_and_quote_values() -> None:
    with pytest.raises(ValidationError, match=r"multiple of 0\.0001"):
        _produce("0.00001")
    with pytest.raises(ValidationError, match=r"multiple of 0\.0001"):
        _quote(MarketSide.BUY, ProductId.RAW_MILK, "1.00001", "1")
