import hashlib
from decimal import Decimal

from company_bench.models import (
    ZERO,
    CompanyId,
    DemandSpec,
    Money,
    PositiveMoney,
    Quantity,
    StrictModel,
)

__all__ = ("ConsumerDemandCurve",)


class ConsumerDemandCurve(StrictModel):
    """Own deterministic demand shocks, quantities, and welfare maxima."""

    spec: DemandSpec

    def potential(self, seed: int, day: int, retailer_id: CompanyId) -> Quantity:
        """Return one stable retailer-day demand potential."""
        stream = f"{seed}|consumer_demand|{day}|{retailer_id}".encode()
        value = int.from_bytes(hashlib.sha256(stream).digest()[:8], "big")
        shock = self.spec.shock_min + value % (self.spec.shock_max - self.spec.shock_min + 1)
        return max(ZERO, self.spec.base_demand + Decimal(shock))

    def quantity(self, potential: Quantity, price: Money | None) -> Quantity:
        """Apply the continuous linear price response without integer rounding."""
        if price is None:
            return potential
        adjustment = self.spec.price_sensitivity * (price - self.spec.reference_price)
        return max(ZERO, potential - adjustment)

    def max_net_value(self, potential: Quantity, unit_cost: PositiveMoney) -> Money:
        """Return the continuous maximum consumer inflow net of product cost."""
        demand_at_cost = potential + self.spec.price_sensitivity * (
            self.spec.reference_price - unit_cost
        )
        if demand_at_cost <= ZERO:
            return ZERO
        return demand_at_cost**2 / (Decimal("4") * self.spec.price_sensitivity)
