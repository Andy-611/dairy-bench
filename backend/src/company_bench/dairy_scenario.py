from decimal import Decimal
from typing import Final, Literal

from company_bench.models import (
    CompanyOperation,
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

__all__ = ("DAIRY_S12_V3_SCENARIO",)

type _CompanySuffix = Literal["a", "b", "c", "d"]
type _CompanyTemplate = tuple[str, CompanyOperation]

_COMPANY_SUFFIXES: Final[tuple[_CompanySuffix, ...]] = ("a", "b", "c", "d")


def _money(value: str) -> Decimal:
    """Create an exact benchmark decimal from a readable literal."""
    return Decimal(value)


def _company(
    suffix: _CompanySuffix,
    template: _CompanyTemplate,
) -> CompanySpec:
    """Compose one immutable company from its operation and roster suffix."""
    name, operation = template
    return CompanySpec(
        company_id=f"{operation.kind}_{suffix}",
        name=f"{name} {suffix.upper()}",
        initial_cash=_money("1000"),
        operation=operation,
    )


def _build_companies() -> tuple[CompanySpec, ...]:
    """Build the canonical four-company roster for every value-chain tier."""
    templates: tuple[_CompanyTemplate, ...] = (
        (
            "牧场",
            FarmOperation(
                daily_capacity=_money("60"),
                unit_cost=_money("1.00"),
            ),
        ),
        (
            "加工厂",
            ProcessorOperation(
                daily_input_capacity=_money("50"),
                yield_rate=_money("0.8"),
                processing_cost_per_input=_money("0.40"),
            ),
        ),
        ("零售商", RetailerOperation()),
    )
    return tuple(
        _company(suffix, template) for template in templates for suffix in _COMPANY_SUFFIXES
    )


def _build_dairy_scenario() -> ScenarioSpec:
    """Build the canonical event-driven Dairy Bench scenario."""
    return ScenarioSpec(
        scenario_id="flow.dairy.base.s12.v3",
        version=3,
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
        companies=_build_companies(),
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


DAIRY_S12_V3_SCENARIO: Final[ScenarioSpec] = _build_dairy_scenario()
