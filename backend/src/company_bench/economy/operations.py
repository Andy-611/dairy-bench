"""Deterministic private operating conditions for productive companies."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal

from company_bench.domain.models import (
    ONE,
    CompanyId,
    ProductiveOperation,
    ScenarioSpec,
    WeeklyOperationState,
)

_INNOVATION_SCALE = 10_000
_INNOVATION_VALUES = 2 * _INNOVATION_SCALE + 1


@dataclass(frozen=True, slots=True)
class OperatingEconomics:
    """Own initial and weekly realization of private operating economics."""

    scenario: ScenarioSpec
    seed: int

    def initial_states(self) -> tuple[WeeklyOperationState, ...]:
        """Return neutral pre-episode states in canonical company order."""
        return tuple(
            WeeklyOperationState(
                company_id=company_id,
                availability=ONE,
                weekly_capacity=operation.capacity.normal_capacity,
                weekly_base_unit_cost=operation.cost.normal_unit_cost,
            )
            for company_id, operation in self._operations()
        )

    def open_week(
        self,
        week: int,
        previous_states: tuple[WeeklyOperationState, ...],
    ) -> tuple[WeeklyOperationState, ...]:
        """Advance private conditions and reset every weekly capacity counter."""
        if not 1 <= week <= self.scenario.weeks:
            raise ValueError("operation week must belong to the scenario")
        previous = {state.company_id: state for state in previous_states}
        operations = self._operations()
        expected = tuple(company_id for company_id, _ in operations)
        if tuple(previous) != expected:
            raise ValueError("previous operation states must follow scenario order")

        states: list[WeeklyOperationState] = []
        for company_id, operation in operations:
            prior = previous[company_id]
            capacity = operation.capacity.weekly_capacity(
                prior.availability,
                self._innovation(week, company_id, "capacity"),
            )
            states.append(
                WeeklyOperationState(
                    company_id=company_id,
                    availability=capacity.availability,
                    weekly_capacity=capacity.quantity,
                    weekly_base_unit_cost=operation.cost.weekly_base_unit_cost(
                        self._innovation(week, company_id, "cost")
                    ),
                )
            )
        return tuple(states)

    def _operations(self) -> tuple[tuple[CompanyId, ProductiveOperation], ...]:
        """Return productive operations in canonical scenario order."""
        return tuple(
            (company.company_id, company.operation)
            for company in self.scenario.productive_companies
        )

    def _innovation(self, week: int, company_id: CompanyId, domain: str) -> Decimal:
        """Map one named SHA-256 stream to an exact value in [-1, 1]."""
        stream = f"{self.seed}|operation_{domain}|{week}|{company_id}".encode()
        value = int.from_bytes(hashlib.sha256(stream).digest()[:8], "big")
        centered = value % _INNOVATION_VALUES - _INNOVATION_SCALE
        return Decimal(centered) / Decimal(_INNOVATION_SCALE)
