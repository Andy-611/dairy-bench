from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal, DecimalException
from typing import Annotated

from pydantic import AfterValidator, Field
from pydantic.fields import FieldInfo

ECONOMIC_QUANTUM = Decimal("0.0001")


class EconomicPrecision:
    """Central precision contract for every economic Decimal."""

    @staticmethod
    def require_exact(value: Decimal) -> Decimal:
        """Reject values that are not exact multiples of the economic quantum."""
        if not value.is_finite():
            raise ValueError("economic value must be finite")
        decimal_tuple = value.as_tuple()
        excess_places = ECONOMIC_QUANTUM.as_tuple().exponent - decimal_tuple.exponent
        digits = decimal_tuple.digits
        if excess_places > 0 and (excess_places > len(digits) or any(digits[-excess_places:])):
            raise ValueError(f"economic value must be an exact multiple of {ECONOMIC_QUANTUM}")
        return value

    @staticmethod
    def round(value: Decimal) -> Decimal:
        """Round a finite economic result with unbiased half-even rounding."""
        return EconomicPrecision._quantize(value, ROUND_HALF_EVEN)

    @staticmethod
    def normalize_exact(value: Decimal) -> Decimal:
        """Normalize one already-exact value to the canonical four-place form."""
        EconomicPrecision.require_exact(value)
        return EconomicPrecision.round(value)

    @staticmethod
    def floor_quantity(value: Decimal) -> Decimal:
        """Round a nonnegative physical output down without creating product."""
        if not value.is_finite() or value < 0:
            raise ValueError("physical quantity must be nonnegative and finite")
        return EconomicPrecision._quantize(value, ROUND_DOWN)

    @staticmethod
    def _quantize(value: Decimal, rounding: str) -> Decimal:
        if not value.is_finite():
            raise ValueError("economic value must be finite")
        try:
            return value.quantize(ECONOMIC_QUANTUM, rounding=rounding)
        except DecimalException as error:
            raise ValueError("economic value cannot be represented at fixed precision") from error


def economic_field(
    *,
    ge: Decimal | None = None,
    gt: Decimal | None = None,
    le: Decimal | None = None,
    lt: Decimal | None = None,
) -> FieldInfo:
    """Build one schema-visible field governed by the shared quantum."""
    return Field(
        allow_inf_nan=False,
        ge=ge,
        gt=gt,
        json_schema_extra={"multipleOf": float(ECONOMIC_QUANTUM)},
        le=le,
        lt=lt,
    )


ECONOMIC_NORMALIZER = AfterValidator(EconomicPrecision.normalize_exact)

type EconomicDecimal = Annotated[Decimal, economic_field(), ECONOMIC_NORMALIZER]
