from decimal import Decimal
from typing import Final, Literal

from company_bench.domain.models import (
    CapacityFunction,
    CompanyOperation,
    CompanySpec,
    ConsumerCohortSpec,
    ConsumerGroup,
    ConsumerMarketSpec,
    CostFunction,
    FarmOperation,
    MarketRegime,
    ProcessorOperation,
    ProductId,
    ProductSpec,
    RegimeComposition,
    RegimeWillingnessShift,
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
        ("零售商", RetailerOperation(weekly_operating_cost=_money("5.00"))),
    )
    return tuple(
        _company(suffix, template) for template in templates for suffix in _COMPANY_SUFFIXES
    )


def _build_dairy_scenario() -> ScenarioSpec:
    """Build the canonical event-driven Dairy Bench scenario."""
    return ScenarioSpec(
        scenario_id="flow.dairy.base.s9.v9",
        version=9,
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
        consumer_market=ConsumerMarketSpec(
            cohorts=(
                ConsumerCohortSpec(
                    group=ConsumerGroup.LOW,
                    base_quantity=_money("40"),
                    maximum_willingness_to_pay=_money("3.00"),
                ),
                ConsumerCohortSpec(
                    group=ConsumerGroup.MEDIUM,
                    base_quantity=_money("50"),
                    maximum_willingness_to_pay=_money("3.80"),
                ),
                ConsumerCohortSpec(
                    group=ConsumerGroup.HIGH,
                    base_quantity=_money("30"),
                    maximum_willingness_to_pay=_money("5.00"),
                ),
            ),
            compositions=(
                RegimeComposition(
                    regime=MarketRegime.SLUMP,
                    low=_money("0.8"),
                    medium=_money("0.5"),
                    high=_money("0.3"),
                ),
                RegimeComposition(
                    regime=MarketRegime.NORMAL,
                    low=_money("1.0"),
                    medium=_money("1.0"),
                    high=_money("1.0"),
                ),
                RegimeComposition(
                    regime=MarketRegime.BOOM,
                    low=_money("1.1"),
                    medium=_money("1.5"),
                    high=_money("2.0"),
                ),
            ),
            purchasing_power_shifts=tuple(
                _money(value) for value in ("-0.20", "-0.10", "0", "0.10", "0.20")
            ),
            regime_willingness_shifts=(
                RegimeWillingnessShift(
                    regime=MarketRegime.SLUMP,
                    shift=_money("-0.20"),
                ),
                RegimeWillingnessShift(
                    regime=MarketRegime.NORMAL,
                    shift=_money("0"),
                ),
                RegimeWillingnessShift(
                    regime=MarketRegime.BOOM,
                    shift=_money("0.20"),
                ),
            ),
            minimum_regime_weeks=6,
            maximum_regime_weeks=10,
            initial_retail_price=_money("3.50"),
        ),
        scoring=ScoringSpec(),
    )


DAIRY_S9_SCENARIO: Final[ScenarioSpec] = _build_dairy_scenario()
