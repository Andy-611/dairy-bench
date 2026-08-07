from collections.abc import Callable
from decimal import Decimal

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from company_bench.models import Money, ProductId
from company_bench.precision import ECONOMIC_QUANTUM, EconomicPrecision
from company_bench.runtime_models import (
    Produce,
    QuoteAlert,
    QuoteLevel,
    SetRetailPrice,
    Transform,
    Wait,
)


def test_economic_precision_normalizes_exact_values_and_rejects_excess_places() -> None:
    adapter = TypeAdapter(Money)

    assert adapter.validate_python(Decimal("1.2")) == Decimal("1.2000")
    with pytest.raises(ValidationError, match=r"exact multiple of 0\.0001"):
        adapter.validate_python(Decimal("1.23456"))


def test_derived_values_use_the_two_explicit_rounding_directions() -> None:
    assert Decimal("0.0001") == ECONOMIC_QUANTUM
    assert EconomicPrecision.round(Decimal("1.23445")) == Decimal("1.2344")
    assert EconomicPrecision.round(Decimal("1.23455")) == Decimal("1.2346")
    assert EconomicPrecision.floor_quantity(Decimal("1.23459")) == Decimal("1.2345")


@pytest.mark.parametrize(
    "command_factory",
    (
        lambda: Produce(product=ProductId.RAW_MILK, quantity=Decimal("1.00001")),
        lambda: Transform(
            input_product=ProductId.RAW_MILK,
            output_product=ProductId.BOTTLED_MILK,
            input_quantity=Decimal("1.00001"),
        ),
        lambda: QuoteLevel(
            quantity=Decimal("1.00001"),
            limit_price=Decimal("1"),
        ),
        lambda: QuoteLevel(
            quantity=Decimal("1"),
            limit_price=Decimal("1.00001"),
        ),
        lambda: SetRetailPrice(
            product=ProductId.BOTTLED_MILK,
            unit_price=Decimal("1.00001"),
        ),
        lambda: Wait(
            alerts=(
                QuoteAlert(
                    product=ProductId.RAW_MILK,
                    quote="best_bid",
                    operator="at_least",
                    price=Decimal("1.00001"),
                ),
            )
        ),
    ),
)
def test_every_agent_decimal_input_rejects_excess_precision(
    command_factory: Callable[[], object],
) -> None:
    with pytest.raises(ValidationError, match=r"exact multiple of 0\.0001"):
        command_factory()


@pytest.mark.parametrize(
    ("model", "expected_quantum_count"),
    (
        (Produce, 1),
        (Transform, 1),
        (QuoteLevel, 2),
        (SetRetailPrice, 1),
        (Wait, 1),
    ),
)
def test_agent_command_schemas_publish_the_economic_quantum(
    model: type[BaseModel],
    expected_quantum_count: int,
) -> None:
    schema = model.model_json_schema()

    assert _quantum_count(schema) == expected_quantum_count


def _quantum_count(value: object) -> int:
    if isinstance(value, dict):
        own = int(value.get("multipleOf") == float(ECONOMIC_QUANTUM))
        return own + sum(_quantum_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_quantum_count(item) for item in value)
    return 0
