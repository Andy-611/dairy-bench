from decimal import Decimal

from company_bench.models import (
    CompanySpec,
    DemandSpec,
    FarmOperation,
    ProcessorOperation,
    ProductId,
    ProductSpec,
    RetailerOperation,
    ScenarioSpec,
    ScoringSpec,
)


def _money(value: str) -> Decimal:
    """Create an exact benchmark decimal from a readable literal."""
    return Decimal(value)


def build_dairy_scenario() -> ScenarioSpec:
    """Build the fixed six-company Dairy Bench V1 scenario."""
    starting_cash = _money("1000")
    companies = (
        CompanySpec(
            company_id="farm_a",
            name="牧场 A",
            initial_cash=starting_cash,
            operation=FarmOperation(
                daily_capacity=_money("60"),
                unit_cost=_money("1.00"),
            ),
        ),
        CompanySpec(
            company_id="farm_b",
            name="牧场 B",
            initial_cash=starting_cash,
            operation=FarmOperation(
                daily_capacity=_money("60"),
                unit_cost=_money("1.00"),
            ),
        ),
        CompanySpec(
            company_id="processor_a",
            name="加工厂 A",
            initial_cash=starting_cash,
            operation=ProcessorOperation(
                daily_input_capacity=_money("50"),
                yield_rate=_money("0.8"),
                processing_cost_per_input=_money("0.40"),
            ),
        ),
        CompanySpec(
            company_id="processor_b",
            name="加工厂 B",
            initial_cash=starting_cash,
            operation=ProcessorOperation(
                daily_input_capacity=_money("50"),
                yield_rate=_money("0.8"),
                processing_cost_per_input=_money("0.40"),
            ),
        ),
        CompanySpec(
            company_id="retailer_a",
            name="零售商 A",
            initial_cash=starting_cash,
            operation=RetailerOperation(),
        ),
        CompanySpec(
            company_id="retailer_b",
            name="零售商 B",
            initial_cash=starting_cash,
            operation=RetailerOperation(),
        ),
    )
    return ScenarioSpec(
        scenario_id="flow.dairy.base.s6.v1",
        version=1,
        days=30,
        products=(
            ProductSpec(
                product=ProductId.RAW_MILK,
                name="原奶",
                shelf_life_days=2,
                reference_value=_money("1.00"),
            ),
            ProductSpec(
                product=ProductId.BOTTLED_MILK,
                name="盒装奶",
                shelf_life_days=4,
                reference_value=_money("1.75"),
            ),
        ),
        companies=companies,
        demand=DemandSpec(
            base_demand=_money("40"),
            reference_price=_money("3.50"),
            price_sensitivity=_money("8"),
            shock_min=-5,
            shock_max=5,
        ),
        scoring=ScoringSpec(
            max_gini=_money("0.20"),
            max_within_tier_growth_gap=_money("0.20"),
        ),
    )


DAIRY_V1_SCENARIO = build_dairy_scenario()


def build_dairy_v2_scenario() -> ScenarioSpec:
    """Build V2 with the same economy and an event-driven runtime."""
    return DAIRY_V1_SCENARIO.model_copy(
        update={
            "scenario_id": "flow.dairy.base.s6.v2",
            "version": 2,
        }
    )


DAIRY_V2_SCENARIO = build_dairy_v2_scenario()
