"""Shared finite-consumer market with hidden deterministic business regimes."""

from __future__ import annotations

import hashlib
from decimal import Decimal

from pydantic import Field, model_validator

from company_bench.domain.models import (
    ZERO,
    CompanyId,
    ConsumerGroup,
    ConsumerMarketSpec,
    MarketRegime,
    PositiveMoney,
    Quantity,
    StrictModel,
)
from company_bench.domain.precision import ECONOMIC_QUANTUM, EconomicPrecision

__all__ = (
    "ConsumerPopulation",
    "RetailOffer",
    "RetailSaleAllocation",
    "SharedConsumerMarket",
    "SharedRetailSettlement",
)


class ConsumerPopulationCohort(StrictModel):
    """One realized hidden cohort for a simulation week."""

    group: ConsumerGroup
    quantity: Quantity
    maximum_willingness_to_pay: PositiveMoney


class ConsumerPopulation(StrictModel):
    """Complete hidden consumer population for one week."""

    week: int = Field(ge=1)
    regime: MarketRegime
    cohorts: tuple[ConsumerPopulationCohort, ...] = Field(min_length=3, max_length=3)

    @property
    def quantity(self) -> Quantity:
        """Return total realized consumer quantity."""
        return sum((cohort.quantity for cohort in self.cohorts), start=ZERO)


class RetailOffer(StrictModel):
    """One retailer's committed price and saleable inventory."""

    company_id: CompanyId
    unit_price: PositiveMoney | None = None
    available_quantity: Quantity


class RetailSaleAllocation(StrictModel):
    """Shared-market units allocated to one retailer."""

    company_id: CompanyId
    sold_quantity: Quantity


class SharedRetailSettlement(StrictModel):
    """Deterministic allocation across all retailers and cohorts."""

    population: ConsumerPopulation
    sales: tuple[RetailSaleAllocation, ...]

    @model_validator(mode="after")
    def validate_allocation(self) -> SharedRetailSettlement:
        """Reject duplicate retailers and sales beyond the realized population."""
        company_ids = tuple(sale.company_id for sale in self.sales)
        if len(company_ids) != len(set(company_ids)):
            raise ValueError("retail settlement companies must be unique")
        if sum((sale.sold_quantity for sale in self.sales), start=ZERO) > (
            self.population.quantity
        ):
            raise ValueError("retail settlement cannot exceed consumer population")
        return self


class SharedConsumerMarket:
    """Own hidden regimes, cohort realization, and order-independent allocation."""

    def __init__(self, spec: ConsumerMarketSpec) -> None:
        self._spec = spec

    def population(self, seed: int, week: int) -> ConsumerPopulation:
        """Realize one deterministic hidden weekly population."""
        regime = self.regime(seed, week)
        composition = self._spec.composition(regime)
        willingness_shift = self.purchasing_power_shift(seed) + self._spec.regime_willingness_shift(
            regime
        )
        return ConsumerPopulation(
            week=week,
            regime=regime,
            cohorts=tuple(
                ConsumerPopulationCohort(
                    group=cohort.group,
                    quantity=EconomicPrecision.floor_quantity(
                        cohort.base_quantity * composition.multiplier(cohort.group)
                    ),
                    maximum_willingness_to_pay=EconomicPrecision.round(
                        cohort.maximum_willingness_to_pay + willingness_shift
                    ),
                )
                for cohort in self._spec.cohorts
            ),
        )

    def purchasing_power_shift(self, seed: int) -> Decimal:
        """Return the hidden run-wide purchasing-power shift."""
        shifts = self._spec.purchasing_power_shifts
        return shifts[self._stable_int(seed, "purchasing_power") % len(shifts)]

    def regime(self, seed: int, week: int) -> MarketRegime:
        """Resolve a week from the hidden alternating regime schedule."""
        if week < 1:
            raise ValueError("consumer market week must be positive")
        first_extreme = (
            MarketRegime.SLUMP
            if self._stable_int(seed, "first_extreme") % 2 == 0
            else MarketRegime.BOOM
        )
        covered = 0
        regime_index = 0
        while covered < week:
            duration = self._regime_duration(seed, regime_index)
            if week <= covered + duration:
                if regime_index % 2 == 0:
                    return MarketRegime.NORMAL
                extreme_index = regime_index // 2
                if extreme_index % 2 == 0:
                    return first_extreme
                return (
                    MarketRegime.BOOM if first_extreme is MarketRegime.SLUMP else MarketRegime.SLUMP
                )
            covered += duration
            regime_index += 1
        raise RuntimeError("consumer regime schedule did not cover the requested week")

    def settle(
        self,
        seed: int,
        week: int,
        offers: tuple[RetailOffer, ...],
    ) -> SharedRetailSettlement:
        """Allocate finite cohort demand by price, stockout spillover, and fair ties."""
        company_ids = tuple(offer.company_id for offer in offers)
        if len(company_ids) != len(set(company_ids)):
            raise ValueError("retail offers must be unique by company")
        population = self.population(seed, week)
        remaining_supply = {offer.company_id: offer.available_quantity for offer in offers}
        sales = {offer.company_id: ZERO for offer in offers}
        prices = {offer.company_id: offer.unit_price for offer in offers}

        for cohort in population.cohorts:
            remaining_demand = cohort.quantity
            price_levels = sorted(
                {
                    price
                    for company_id, price in prices.items()
                    if price is not None
                    and price <= cohort.maximum_willingness_to_pay
                    and remaining_supply[company_id] > ZERO
                }
            )
            for price in price_levels:
                if remaining_demand <= ZERO:
                    break
                tied = tuple(
                    company_id
                    for company_id, company_price in prices.items()
                    if company_price == price and remaining_supply[company_id] > ZERO
                )
                target = min(
                    remaining_demand,
                    sum((remaining_supply[company_id] for company_id in tied), start=ZERO),
                )
                allocation = self._equal_allocation(
                    seed,
                    week,
                    cohort.group,
                    price,
                    target,
                    {company_id: remaining_supply[company_id] for company_id in tied},
                )
                allocated = sum(allocation.values(), start=ZERO)
                for company_id, quantity in allocation.items():
                    sales[company_id] += quantity
                    remaining_supply[company_id] -= quantity
                remaining_demand -= allocated

        return SharedRetailSettlement(
            population=population,
            sales=tuple(
                RetailSaleAllocation(company_id=company_id, sold_quantity=sales[company_id])
                for company_id in sorted(sales)
            ),
        )

    def _regime_duration(self, seed: int, regime_index: int) -> int:
        width = self._spec.maximum_regime_weeks - self._spec.minimum_regime_weeks + 1
        return self._spec.minimum_regime_weeks + (
            self._stable_int(seed, "duration", regime_index) % width
        )

    @classmethod
    def _equal_allocation(
        cls,
        seed: int,
        week: int,
        group: ConsumerGroup,
        price: Decimal,
        quantity: Decimal,
        capacities: dict[CompanyId, Decimal],
    ) -> dict[CompanyId, Decimal]:
        """Water-fill tied retailers and assign only final quanta by stable hash."""
        allocations = {company_id: ZERO for company_id in capacities}
        active = set(capacities)
        remaining = quantity
        while active and remaining >= ECONOMIC_QUANTUM:
            share = EconomicPrecision.floor_quantity(remaining / Decimal(len(active)))
            if share <= ZERO:
                break
            constrained = tuple(
                company_id
                for company_id in active
                if capacities[company_id] - allocations[company_id] <= share
            )
            if constrained:
                for company_id in constrained:
                    amount = capacities[company_id] - allocations[company_id]
                    allocations[company_id] += amount
                    remaining -= amount
                    active.remove(company_id)
                continue
            for company_id in active:
                allocations[company_id] += share
                remaining -= share
            break

        ranked = sorted(
            active,
            key=lambda company_id: (
                cls._stable_int(seed, week, group.value, price, company_id),
                company_id,
            ),
        )
        while remaining >= ECONOMIC_QUANTUM:
            recipient = next(
                (
                    company_id
                    for company_id in ranked
                    if capacities[company_id] - allocations[company_id] >= ECONOMIC_QUANTUM
                ),
                None,
            )
            if recipient is None:
                break
            allocations[recipient] += ECONOMIC_QUANTUM
            remaining -= ECONOMIC_QUANTUM
            ranked = (*ranked[1:], ranked[0]) if len(ranked) > 1 else ranked
        return allocations

    @staticmethod
    def _stable_int(*parts: object) -> int:
        payload = "|".join(str(part) for part in parts).encode()
        return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
