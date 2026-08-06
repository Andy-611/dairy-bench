from decimal import Decimal

import pytest
from pydantic import ValidationError

from company_bench.dairy_scenario import DAIRY_S9_SCENARIO
from company_bench.economics import OperatingEconomics
from company_bench.models import CapacityFunction, CostFunction


def _capacity_function() -> CapacityFunction:
    return CapacityFunction(
        normal_capacity=Decimal("100"),
        persistence=Decimal("0.75"),
        volatility=Decimal("0.10"),
        minimum_factor=Decimal("0.80"),
        maximum_factor=Decimal("1.10"),
    )


def _cost_function() -> CostFunction:
    return CostFunction(
        normal_unit_cost=Decimal("2"),
        daily_volatility=Decimal("0.10"),
        curvature=Decimal("0.50"),
    )


def test_capacity_function_is_persistent_bounded_and_deterministic() -> None:
    function = _capacity_function()

    neutral = function.daily_capacity(Decimal("1"), Decimal("0"))
    persistent = function.daily_capacity(Decimal("1.08"), Decimal("0"))
    upper_bound = function.daily_capacity(Decimal("1.10"), Decimal("1"))
    lower_bound = function.daily_capacity(Decimal("0.80"), Decimal("-1"))

    assert neutral.availability == Decimal("1.0000")
    assert neutral.quantity == Decimal("100.0000")
    assert persistent.availability == Decimal("1.0600")
    assert upper_bound.availability == Decimal("1.1000")
    assert lower_bound.availability == Decimal("0.8000")
    assert lower_bound.quantity == Decimal("80.0000")
    assert function.daily_capacity(Decimal("1.08"), Decimal("0")) == persistent


def test_cost_function_has_private_daily_base_cost_and_convex_batch_cost() -> None:
    function = _cost_function()

    assert function.daily_base_unit_cost(Decimal("-1")) == Decimal("1.8000")
    assert function.daily_base_unit_cost(Decimal("1")) == Decimal("2.2000")

    first = function.incremental_cost(
        daily_capacity=Decimal("100"),
        used_capacity=Decimal("0"),
        quantity=Decimal("20"),
        daily_base_unit_cost=Decimal("2"),
    )
    second = function.incremental_cost(
        daily_capacity=Decimal("100"),
        used_capacity=Decimal("20"),
        quantity=Decimal("20"),
        daily_base_unit_cost=Decimal("2"),
    )
    combined = function.incremental_cost(
        daily_capacity=Decimal("100"),
        used_capacity=Decimal("0"),
        quantity=Decimal("40"),
        daily_base_unit_cost=Decimal("2"),
    )
    minimum_batch = function.incremental_cost(
        daily_capacity=Decimal("100"),
        used_capacity=Decimal("0"),
        quantity=Decimal("0.0001"),
        daily_base_unit_cost=Decimal("1.8"),
    )

    assert first == Decimal("42.0000")
    assert second == Decimal("46.0000")
    assert first + second == combined == Decimal("88.0000")
    assert minimum_batch > 0


def test_cost_function_rejects_capacity_overrun() -> None:
    with pytest.raises(ValueError, match="remaining daily capacity"):
        _cost_function().incremental_cost(
            daily_capacity=Decimal("100"),
            used_capacity=Decimal("90"),
            quantity=Decimal("11"),
            daily_base_unit_cost=Decimal("2"),
        )


def test_economic_functions_reject_configuration_lost_to_precision() -> None:
    with pytest.raises(ValidationError, match="minimum realized capacity"):
        CapacityFunction(
            normal_capacity=Decimal("0.00001"),
            persistence=Decimal("0.75"),
            volatility=Decimal("0.10"),
            minimum_factor=Decimal("0.80"),
            maximum_factor=Decimal("1.10"),
        )
    with pytest.raises(ValidationError, match="minimum daily unit cost"):
        CostFunction(
            normal_unit_cost=Decimal("0.00001"),
            daily_volatility=Decimal("0.10"),
            curvature=Decimal("0.50"),
        )


def test_seeded_daily_economics_are_private_replayable_and_stateful() -> None:
    economics = OperatingEconomics(DAIRY_S9_SCENARIO, seed=7)
    initial = economics.initial_states()
    day_one = economics.open_day(1, initial)
    repeated = economics.open_day(1, initial)
    another_seed = OperatingEconomics(DAIRY_S9_SCENARIO, seed=8)
    other_day_one = another_seed.open_day(1, another_seed.initial_states())

    assert day_one == repeated
    assert day_one != other_day_one
    assert day_one[0] != day_one[1]

    used_day_one = tuple(
        state.consume(Decimal("1")) if state.company_id == "farm_a" else state for state in day_one
    )
    day_two = economics.open_day(2, used_day_one)
    farm_two = next(state for state in day_two if state.company_id == "farm_a")
    reset_from_neutral = economics.open_day(2, initial)
    neutral_farm_two = next(state for state in reset_from_neutral if state.company_id == "farm_a")

    assert farm_two.used_capacity == 0
    assert farm_two.availability != neutral_farm_two.availability
