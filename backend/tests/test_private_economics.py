"""Tests for company-private cash-flow and unit-economics projections."""

from decimal import Decimal

from company_bench.domain.models import (
    InventoryExpiredEvent,
    MilkProcessedEvent,
    ProductId,
    TradeExecutedEvent,
)
from company_bench.domain.precision import EconomicPrecision
from company_bench.domain.scenario import DAIRY_S9_SCENARIO
from company_bench.economy.engine import EconomyEngine
from company_bench.runtime.economics import PrivateEconomicsProjector
from company_bench.runtime.models import OrderBookView, PriceLevelView


def test_processor_private_economics_combines_ledger_and_executable_prices() -> None:
    engine = EconomyEngine()
    economy = engine.open_day(engine.initial_state(DAIRY_S9_SCENARIO, seed=42))
    observation = engine.observe_active(economy, "processor_a")
    books = (
        OrderBookView(
            product=ProductId.RAW_MILK,
            asks=(PriceLevelView(unit_price="1.40", quantity="10", order_count=1),),
        ),
        OrderBookView(
            product=ProductId.BOTTLED_MILK,
            bids=(PriceLevelView(unit_price="2.50", quantity="10", order_count=1),),
        ),
    )
    events = (
        _trade("raw", ProductId.RAW_MILK, "farm_a", "processor_a", "14"),
        _trade("bottled", ProductId.BOTTLED_MILK, "processor_a", "retailer_a", "25"),
        MilkProcessedEvent(
            day=1,
            company_id="processor_a",
            requested_input="10",
            actual_input="10",
            output_quantity="8",
            cash_cost="4",
        ),
        InventoryExpiredEvent(
            day=1,
            company_id="processor_a",
            lot_id="expired",
            product=ProductId.BOTTLED_MILK,
            quantity="1",
            reference_value_loss="3",
        ),
        _trade("rival", ProductId.RAW_MILK, "farm_b", "processor_b", "99"),
    )

    view = PrivateEconomicsProjector().project(
        "processor_a",
        observation,
        books,
        events,
    )

    assert view.cash_flow.purchase_spend == Decimal("14")
    assert view.cash_flow.wholesale_revenue == Decimal("25")
    assert view.cash_flow.operation_cost == Decimal("4")
    assert view.cash_flow.expiry_reference_loss == Decimal("3")
    assert view.cash_flow.net_cash_flow == Decimal("7")
    unit = view.unit_economics
    assert unit.best_input_ask == Decimal("1.40")
    assert unit.best_output_bid == Decimal("2.50")
    assert unit.marginal_operation_cost is not None
    assert unit.break_even_output_price == EconomicPrecision.round(
        (Decimal("1.40") + unit.marginal_operation_cost) / Decimal("0.8")
    )
    assert unit.expected_unit_margin == EconomicPrecision.round(
        Decimal("2.50") - unit.break_even_output_price
    )


def test_farm_and_retailer_unit_economics_use_only_executable_prices() -> None:
    engine = EconomyEngine()
    economy = engine.open_day(engine.initial_state(DAIRY_S9_SCENARIO, seed=42))
    projector = PrivateEconomicsProjector()
    farm = projector.project(
        "farm_a",
        engine.observe_active(economy, "farm_a"),
        (
            OrderBookView(
                product=ProductId.RAW_MILK,
                bids=(PriceLevelView(unit_price="1.80", quantity="10", order_count=1),),
            ),
        ),
        (),
    ).unit_economics
    retailer_observation = engine.observe_active(economy, "retailer_a").model_copy(
        update={"retail_price": Decimal("3.00")}
    )
    retailer = projector.project(
        "retailer_a",
        retailer_observation,
        (
            OrderBookView(
                product=ProductId.BOTTLED_MILK,
                asks=(PriceLevelView(unit_price="2.20", quantity="10", order_count=1),),
            ),
        ),
        (),
    ).unit_economics

    assert farm.marginal_operation_cost is not None
    assert farm.break_even_output_price == farm.marginal_operation_cost
    assert farm.expected_unit_margin == EconomicPrecision.round(
        Decimal("1.80") - farm.marginal_operation_cost
    )
    assert retailer_observation.retail_price is not None
    assert retailer.consumer_unit_price == retailer_observation.retail_price
    assert retailer.expected_unit_margin == EconomicPrecision.round(
        retailer_observation.retail_price - Decimal("2.20")
    )


def _trade(
    suffix: str,
    product: ProductId,
    seller_id: str,
    buyer_id: str,
    total_value: str,
) -> TradeExecutedEvent:
    """Build one compact trade fixture."""
    return TradeExecutedEvent(
        day=1,
        trade_id=f"trade_{suffix}",
        maker_order_id=f"maker_{suffix}",
        taker_order_id=f"taker_{suffix}",
        product=product,
        seller_id=seller_id,
        buyer_id=buyer_id,
        quantity="10",
        unit_price=Decimal(total_value) / Decimal("10"),
        total_value=total_value,
        maker_remaining_quantity="0",
        taker_remaining_quantity="0",
    )
