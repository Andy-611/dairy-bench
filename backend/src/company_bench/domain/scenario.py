from decimal import Decimal
from typing import Final, Literal

from company_bench.domain.models import (
    CapacityFunction,
    CompanyOperation,
    CompanySpec,
    CostFunction,
    DemandSpec,
    FarmOperation,
    ProcessorOperation,
    ProductId,
    ProductSpec,
    RetailerOperation,
    ScenarioSpec,
    ScoringSpec,
)

__all__ = ("DAIRY_S9_SCENARIO",)

type _CompanySuffix = Literal["a", "b", "c"]
type _CompanyTemplate = tuple[str, CompanyOperation]

_COMPANY_SUFFIXES: Final[tuple[_CompanySuffix, ...]] = ("a", "b", "c")


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
    """Build the canonical three-company roster for every value-chain tier."""
    templates: tuple[_CompanyTemplate, ...] = (
        (
            "牧场",
            FarmOperation(
                capacity=CapacityFunction(
                    normal_capacity=_money("60"),
                    persistence=_money("0.75"),
                    volatility=_money("0.05"),
                    minimum_factor=_money("0.85"),
                    maximum_factor=_money("1.10"),
                ),
                cost=CostFunction(
                    normal_unit_cost=_money("1.00"),
                    weekly_volatility=_money("0.10"),
                    curvature=_money("0.35"),
                ),
            ),
        ),
        (
            "加工厂",
            ProcessorOperation(
                capacity=CapacityFunction(
                    normal_capacity=_money("50"),
                    persistence=_money("0.60"),
                    volatility=_money("0.08"),
                    minimum_factor=_money("0.75"),
                    maximum_factor=_money("1.10"),
                ),
                cost=CostFunction(
                    normal_unit_cost=_money("0.40"),
                    weekly_volatility=_money("0.15"),
                    curvature=_money("0.65"),
                ),
                yield_rate=_money("0.8"),
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
        scenario_id="flow.dairy.base.s9.v7",
        version=7,
        weeks=52,
        products=(
            ProductSpec(
                product=ProductId.RAW_MILK,
                name="原奶",
                shelf_life_weeks=2,
                reference_value=_money("1.00"),
            ),
            ProductSpec(
                product=ProductId.BOTTLED_MILK,
                name="盒装奶",
                shelf_life_weeks=4,
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
        scoring=ScoringSpec(),
    )


DAIRY_S9_SCENARIO: Final[ScenarioSpec] = _build_dairy_scenario()
