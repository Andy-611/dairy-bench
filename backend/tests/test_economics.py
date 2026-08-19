from decimal import Decimal

import pytest
from pydantic import ValidationError

from company_bench.domain.models import CapacityFunction, CostFunction
from company_bench.domain.scenario import DAIRY_S9_SCENARIO
from company_bench.economy.operations import OperatingEconomics


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
        weekly_volatility=Decimal("0.10"),
        curvature=Decimal("0.50"),
    )


def test_capacity_function_is_persistent_bounded_and_deterministic() -> None:
    function = _capacity_function()

    neutral = function.weekly_capacity(Decimal("1"), Decimal("0"))
    persistent = function.weekly_capacity(Decimal("1.08"), Decimal("0"))
    upper_bound = function.weekly_capacity(Decimal("1.10"), Decimal("1"))
    lower_bound = function.weekly_capacity(Decimal("0.80"), Decimal("-1"))

    assert neutral.availability == Decimal("1.0000")
    assert neutral.quantity == Decimal("100.0000")
    assert persistent.availability == Decimal("1.0600")
    assert upper_bound.availability == Decimal("1.1000")
    assert lower_bound.availability == Decimal("0.8000")
    assert lower_bound.quantity == Decimal("80.0000")
    assert function.weekly_capacity(Decimal("1.08"), Decimal("0")) == persistent


def test_cost_function_has_private_weekly_base_cost_and_convex_batch_cost() -> None:
    function = _cost_function()

    assert function.weekly_base_unit_cost(Decimal("-1")) == Decimal("1.8000")
    assert function.weekly_base_unit_cost(Decimal("1")) == Decimal("2.2000")

    first = function.incremental_cost(
        weekly_capacity=Decimal("100"),
        used_capacity=Decimal("0"),
        quantity=Decimal("20"),
        weekly_base_unit_cost=Decimal("2"),
    )
    second = function.incremental_cost(
        weekly_capacity=Decimal("100"),
        used_capacity=Decimal("20"),
        quantity=Decimal("20"),
        weekly_base_unit_cost=Decimal("2"),
    )
    combined = function.incremental_cost(
        weekly_capacity=Decimal("100"),
        used_capacity=Decimal("0"),
        quantity=Decimal("40"),
        weekly_base_unit_cost=Decimal("2"),
    )
    minimum_batch = function.incremental_cost(
        weekly_capacity=Decimal("100"),
        used_capacity=Decimal("0"),
        quantity=Decimal("0.0001"),
        weekly_base_unit_cost=Decimal("1.8"),
    )

    assert first == Decimal("42.0000")
    assert second == Decimal("46.0000")
    assert first + second == combined == Decimal("88.0000")
    assert minimum_batch > 0


def test_cost_function_rejects_capacity_overrun() -> None:
    with pytest.raises(ValueError, match="remaining weekly capacity"):
        _cost_function().incremental_cost(
            weekly_capacity=Decimal("100"),
            used_capacity=Decimal("90"),
            quantity=Decimal("11"),
            weekly_base_unit_cost=Decimal("2"),
        )


def test_direct_capacity_consumption_normalizes_or_rejects_precision() -> None:
    state = OperatingEconomics(DAIRY_S9_SCENARIO, seed=7).initial_states()[0]

    consumed = state.consume(Decimal("1.00000"))

    assert str(consumed.used_capacity) == "1.0000"
    with pytest.raises(ValueError, match=r"exact multiple of 0\.0001"):
        state.consume(Decimal("1.00001"))


def test_economic_functions_reject_configuration_lost_to_precision() -> None:
    with pytest.raises(ValidationError, match=r"exact multiple of 0\.0001"):
        CapacityFunction(
            normal_capacity=Decimal("0.00001"),
            persistence=Decimal("0.75"),
            volatility=Decimal("0.10"),
            minimum_factor=Decimal("0.80"),
            maximum_factor=Decimal("1.10"),
        )
    with pytest.raises(ValidationError, match=r"exact multiple of 0\.0001"):
        CostFunction(
            normal_unit_cost=Decimal("0.00001"),
            weekly_volatility=Decimal("0.10"),
            curvature=Decimal("0.50"),
        )


def test_seeded_weekly_economics_are_private_replayable_and_stateful() -> None:
    economics = OperatingEconomics(DAIRY_S9_SCENARIO, seed=7)
    initial = economics.initial_states()
    week_one = economics.open_week(1, initial)
    repeated = economics.open_week(1, initial)
    another_seed = OperatingEconomics(DAIRY_S9_SCENARIO, seed=8)
    other_week_one = another_seed.open_week(1, another_seed.initial_states())

    assert week_one == repeated
    assert week_one != other_week_one
    assert week_one[0] != week_one[1]

    used_week_one = tuple(
        state.consume(Decimal("1")) if state.company_id == "farm_a" else state for state in week_one
    )
    week_two = economics.open_week(2, used_week_one)
    farm_two = next(state for state in week_two if state.company_id == "farm_a")
    reset_from_neutral = economics.open_week(2, initial)
    neutral_farm_two = next(state for state in reset_from_neutral if state.company_id == "farm_a")

    assert farm_two.used_capacity == 0
    assert farm_two.availability != neutral_farm_two.availability
