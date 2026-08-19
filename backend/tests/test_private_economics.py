"""Tests for company-private cash-flow and unit-economics projections."""

from decimal import Decimal

from company_bench.domain.calendar import SimDay
from company_bench.domain.models import (
    ConsumerSaleEvent,
    InventoryExpiredEvent,
    MilkProcessedEvent,
    MilkProducedEvent,
    ProductId,
    RetailOperatingCostChargedEvent,
    TradeExecutedEvent,
)
from company_bench.domain.precision import EconomicPrecision
from company_bench.domain.scenario import DAIRY_S9_SCENARIO
from company_bench.economy.engine import EconomyEngine
from company_bench.economy.ledger import CompanyLedger
from company_bench.economy.private_view import PrivateEconomicsProjector
from company_bench.runtime.models import OrderBookView, PriceLevelView


def test_company_ledger_projects_every_owned_economic_flow_once() -> None:
    events = (
        _trade("purchase", ProductId.RAW_MILK, "farm_a", "processor_a", "14"),
        _trade("sale", ProductId.BOTTLED_MILK, "processor_a", "retailer_a", "25"),
        MilkProducedEvent(
            occurred_on=SimDay.at(week=1),
            company_id="processor_a",
            requested_quantity="2",
            actual_quantity="2",
            unit_cost="1.5",
            cash_cost="3",
        ),
        MilkProcessedEvent(
            occurred_on=SimDay.at(week=1),
            company_id="processor_a",
            requested_input="10",
            actual_input="10",
            output_quantity="8",
            cash_cost="4",
        ),
        RetailOperatingCostChargedEvent(
            occurred_on=SimDay.at(week=1),
            company_id="processor_a",
            opening_payable="0",
            cost_accrued="5",
            cash_paid="2",
            closing_payable="3",
        ),
        ConsumerSaleEvent(
            occurred_on=SimDay.at(week=1),
            company_id="processor_a",
            saleable_quantity="5",
            saleable_book_value="10",
            sold_quantity="4",
            retail_price="3",
            revenue="12",
            cost_of_goods_sold="8",
            gross_profit="4",
            sold_out=False,
        ),
        InventoryExpiredEvent(
            occurred_on=SimDay.at(week=1),
            company_id="processor_a",
            lot_id="expired",
            product=ProductId.BOTTLED_MILK,
            quantity="1",
            reference_value_loss="3",
            book_value_loss="2",
        ),
        _trade("rival", ProductId.RAW_MILK, "farm_b", "processor_b", "99"),
    )

    ledger = CompanyLedger.from_events("processor_a", events)

    assert ledger.purchase_spend == Decimal("14")
    assert ledger.purchase(ProductId.RAW_MILK).quantity == Decimal("10")
    assert ledger.purchase(ProductId.RAW_MILK).volume_weighted_unit_price == Decimal("1.4")
    assert ledger.wholesale_revenue == Decimal("25")
    assert ledger.wholesale_sale(ProductId.BOTTLED_MILK).quantity == Decimal("10")
    assert ledger.operation_cost_accrued == Decimal("12")
    assert ledger.operation_cash_paid == Decimal("9")
    assert ledger.consumer_revenue == Decimal("12")
    assert ledger.consumer_sold_quantity == Decimal("4")
    assert ledger.expiry_reference_loss == Decimal("3")
    assert ledger.expiry(ProductId.BOTTLED_MILK).value == Decimal("2")
    assert ledger.produced_quantity == Decimal("2")
    assert ledger.production_cost == Decimal("3")
    assert ledger.processed_input_quantity == Decimal("10")
    assert ledger.processed_output_quantity == Decimal("8")
    assert ledger.processing_cost == Decimal("4")
    assert ledger.require_weekly_consumer_sale() is ledger.consumer_sales[0]


def test_processor_private_economics_combines_ledger_and_executable_prices() -> None:
    engine = EconomyEngine()
    economy = engine.open_week(engine.initial_state(DAIRY_S9_SCENARIO, seed=42))
    observation = engine.observe_active(economy, "processor_a", SimDay.at(week=1))
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
            occurred_on=SimDay.at(week=1),
            company_id="processor_a",
            requested_input="10",
            actual_input="10",
            output_quantity="8",
            cash_cost="4",
        ),
        InventoryExpiredEvent(
            occurred_on=SimDay.at(week=1),
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
    economy = engine.open_week(engine.initial_state(DAIRY_S9_SCENARIO, seed=42))
    projector = PrivateEconomicsProjector()
    farm = projector.project(
        "farm_a",
        engine.observe_active(economy, "farm_a", SimDay.at(week=1)),
        (
            OrderBookView(
                product=ProductId.RAW_MILK,
                bids=(PriceLevelView(unit_price="1.80", quantity="10", order_count=1),),
            ),
        ),
        (),
    ).unit_economics
    retailer_observation = engine.observe_active(
        economy,
        "retailer_a",
        SimDay.at(week=1),
    ).model_copy(update={"retail_price": Decimal("3.00")})
    retailer_view = projector.project(
        "retailer_a",
        retailer_observation,
        (
            OrderBookView(
                product=ProductId.BOTTLED_MILK,
                asks=(PriceLevelView(unit_price="2.20", quantity="10", order_count=1),),
            ),
        ),
        (
            RetailOperatingCostChargedEvent(
                occurred_on=SimDay.at(week=1),
                company_id="retailer_a",
                opening_payable="0",
                cost_accrued="5",
                cash_paid="2",
                closing_payable="3",
            ),
        ),
    )
    retailer = retailer_view.unit_economics

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
    assert retailer_view.cash_flow.operation_cost == Decimal("2")
    assert retailer_view.cash_flow.net_cash_flow == Decimal("-2")


def _trade(
    suffix: str,
    product: ProductId,
    seller_id: str,
    buyer_id: str,
    total_value: str,
) -> TradeExecutedEvent:
    """Build one compact trade fixture."""
    return TradeExecutedEvent(
        occurred_on=SimDay.at(week=1),
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
