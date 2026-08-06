from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from company_bench.models import (
    CompanyDecision,
    CompanyObservation,
    FarmDecision,
    FarmOperation,
    PolicyKind,
    PolicyMetadata,
    ProcessorDecision,
    ProcessorOperation,
    RetailerDecision,
    RetailerOperation,
)


class CompanyPolicy(Protocol):
    """Decision interface implemented by every company controller."""

    name: str
    version: str
    metadata: PolicyMetadata

    async def decide(
        self,
        observation: CompanyObservation,
    ) -> CompanyDecision:
        """Return one company's decision for the observed day."""
        ...


@dataclass(frozen=True, slots=True)
class FixedPolicy:
    """Return the same validated decision, primarily for exact tests."""

    decision: CompanyDecision
    name: str = "fixed"
    version: str = "1"

    @property
    def metadata(self) -> PolicyMetadata:
        """Describe this deterministic controller."""
        return PolicyMetadata(
            name=self.name,
            version=self.version,
            kind=PolicyKind.BASELINE,
        )

    async def decide(
        self,
        observation: CompanyObservation,
    ) -> CompanyDecision:
        """Return the configured decision without mutating it."""
        return self.decision


@dataclass(frozen=True, slots=True)
class BaselinePolicy:
    """Simple transparent policy that keeps the value chain moving."""

    name: str = "baseline"
    version: str = "1"

    @property
    def metadata(self) -> PolicyMetadata:
        """Describe this transparent rule controller."""
        return PolicyMetadata(
            name=self.name,
            version=self.version,
            kind=PolicyKind.BASELINE,
        )

    async def decide(
        self,
        observation: CompanyObservation,
    ) -> CompanyDecision:
        """Choose the documented V1 action for the company's operation."""
        operation = observation.operation
        if isinstance(operation, FarmOperation):
            quantity = min(_daily_capacity(observation), Decimal("50"))
            return FarmDecision(
                produce_quantity=quantity,
                raw_offer_quantity=quantity,
                minimum_raw_price=Decimal("1.40"),
            )
        if isinstance(operation, ProcessorOperation):
            input_quantity = min(
                _daily_capacity(observation),
                Decimal("50"),
            )
            return ProcessorDecision(
                raw_bid_quantity=input_quantity,
                maximum_raw_price=Decimal("1.60"),
                process_quantity=input_quantity,
                bottled_offer_quantity=min(
                    input_quantity * operation.yield_rate,
                    Decimal("40"),
                ),
                minimum_bottled_price=Decimal("2.50"),
            )
        if isinstance(operation, RetailerOperation):
            return RetailerDecision(
                bottled_bid_quantity=Decimal("40"),
                maximum_bottled_price=Decimal("2.80"),
                retail_price=Decimal("3.50"),
            )
        raise TypeError(f"unsupported operation: {type(operation).__name__}")


def _daily_capacity(observation: CompanyObservation) -> Decimal:
    """Return the productive company's private realized capacity."""
    if observation.daily_operation is None:
        raise ValueError("productive policy requires daily operating economics")
    return observation.daily_operation.daily_capacity
