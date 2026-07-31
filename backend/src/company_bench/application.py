"""Episode orchestration and application use cases."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import TypeAdapter

from company_bench.agent_models import PolicyInfrastructureError
from company_bench.engine import EconomyEngine
from company_bench.models import (
    CompanyDecision,
    CompanyObservation,
    EpisodeResult,
    EventRecord,
    NoOpDecision,
    PolicyDescriptor,
    PolicyFailedEvent,
    RecordedDecision,
    RunSummary,
    ScenarioSpec,
)
from company_bench.policies import BaselinePolicy, CompanyPolicy
from company_bench.repository import RunRepository
from company_bench.scoring import Evaluator

_DECISION_ADAPTER = TypeAdapter(CompanyDecision)


class DairyBenchmark:
    """Run one explicit pre-V2 daily-decision scenario."""

    def __init__(
        self,
        scenario: ScenarioSpec,
        *,
        engine: EconomyEngine | None = None,
        evaluator: Evaluator | None = None,
        policy_timeout_seconds: float = 5.0,
    ) -> None:
        if scenario.version >= 2:
            raise ValueError("DairyBenchmark does not implement event-driven V2 scenarios")
        if policy_timeout_seconds <= 0:
            raise ValueError("policy_timeout_seconds must be positive")
        self.scenario = scenario
        self._engine = engine or EconomyEngine()
        self._evaluator = evaluator or Evaluator()
        self._policy_timeout_seconds = policy_timeout_seconds

    async def run(
        self,
        policies: Mapping[str, CompanyPolicy],
        seed: int,
        *,
        run_id: str | None = None,
        on_day_completed: Callable[[int], Awaitable[None]] | None = None,
    ) -> EpisodeResult:
        """Run all companies for the configured number of days."""
        self._validate_policy_set(policies)
        started_at = datetime.now(UTC)
        initial_state = self._engine.initial_state(self.scenario, seed)
        state = initial_state
        decisions: list[RecordedDecision] = []
        event_records: list[EventRecord] = []
        snapshots = []

        while state.day < self.scenario.days:
            observations = self._engine.observe(state)
            daily_decisions, policy_events = await self._collect_decisions(
                observations,
                policies,
            )
            day_result = self._engine.step(state, daily_decisions)
            decisions.extend(daily_decisions)
            for event in (*policy_events, *day_result.events):
                event_records.append(
                    EventRecord(
                        sequence=len(event_records) + 1,
                        event=event,
                    )
                )
            snapshots.append(day_result.snapshot)
            state = day_result.state
            if on_day_completed is not None:
                await on_day_completed(state.day)

        score = self._evaluator.evaluate(
            self.scenario,
            initial_state,
            state,
            tuple(snapshots),
            tuple(record.event for record in event_records),
        )
        finished_at = datetime.now(UTC)
        return EpisodeResult(
            run_id=run_id or f"run_{uuid4().hex}",
            scenario=self.scenario,
            seed=seed,
            started_at=started_at,
            finished_at=finished_at,
            policies=tuple(
                PolicyDescriptor(
                    company_id=company.company_id,
                    **policies[company.company_id].metadata.model_dump(),
                )
                for company in self.scenario.companies
            ),
            decisions=tuple(decisions),
            events=tuple(event_records),
            snapshots=tuple(snapshots),
            score=score,
        )

    def _validate_policy_set(
        self,
        policies: Mapping[str, CompanyPolicy],
    ) -> None:
        expected = {company.company_id for company in self.scenario.companies}
        actual = set(policies)
        if actual != expected:
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            raise ValueError(
                f"policies must match scenario companies; "
                f"missing={missing}, unexpected={unexpected}"
            )

    async def _collect_decisions(
        self,
        observations: tuple[CompanyObservation, ...],
        policies: Mapping[str, CompanyPolicy],
    ) -> tuple[tuple[RecordedDecision, ...], tuple[PolicyFailedEvent, ...]]:
        results = await asyncio.gather(
            *(
                self._ask_policy(observation, policies[observation.company_id])
                for observation in observations
            )
        )
        return (
            tuple(result[0] for result in results),
            tuple(result[1] for result in results if result[1] is not None),
        )

    async def _ask_policy(
        self,
        observation: CompanyObservation,
        policy: CompanyPolicy,
    ) -> tuple[RecordedDecision, PolicyFailedEvent | None]:
        failure: PolicyFailedEvent | None = None
        try:
            decision = await asyncio.wait_for(
                policy.decide(observation),
                timeout=self._policy_timeout_seconds,
            )
            decision = _DECISION_ADAPTER.validate_python(decision)
        except PolicyInfrastructureError:
            raise
        except Exception as error:  # Policies are untrusted adapters.
            reason = self._safe_failure_reason(error)
            decision = NoOpDecision(reason="policy_failed")
            failure = PolicyFailedEvent(
                day=observation.day,
                company_id=observation.company_id,
                reason=reason,
            )
        return (
            RecordedDecision(
                observation_id=observation.observation_id,
                day=observation.day,
                company_id=observation.company_id,
                decision=decision,
            ),
            failure,
        )

    @staticmethod
    def _safe_failure_reason(error: Exception) -> str:
        """Return bounded, stable diagnostic text for an adapter failure."""
        detail = str(error).strip()
        message = f"{type(error).__name__}: {detail[:240]}" if detail else type(error).__name__
        return message[:300]


class RunService:
    """Persist episodes produced by an explicit pre-V2 benchmark."""

    def __init__(
        self,
        repository: RunRepository,
        benchmark: DairyBenchmark,
    ) -> None:
        self._repository = repository
        self._benchmark = benchmark

    async def run(
        self,
        seed: int,
        policies: Mapping[str, CompanyPolicy] | None = None,
    ) -> EpisodeResult:
        """Run and atomically persist one completed episode."""
        active_policies = (
            {company.company_id: BaselinePolicy() for company in self._benchmark.scenario.companies}
            if policies is None
            else policies
        )
        result = await self._benchmark.run(active_policies, seed)
        self._repository.save(result)
        return result

    def list_runs(self, limit: int = 50) -> tuple[RunSummary, ...]:
        """List the latest completed episodes."""
        return self._repository.list(limit)

    def get_run(self, run_id: str) -> EpisodeResult | None:
        """Get one completed episode by identity."""
        return self._repository.get(run_id)
