"""Lifecycle-independent projection of the latest settled run evaluation."""

from __future__ import annotations

from typing import Protocol

from company_bench.domain.models import EpisodeQuality, EpisodeResult, ProtocolReport
from company_bench.economy.engine import EconomyEngine
from company_bench.economy.scoring import Evaluator
from company_bench.runs.models import (
    AgentUsageSummary,
    PolicyInvocation,
    RunCheckpoint,
    RunEvaluation,
    RunJob,
    RunRecovery,
    RunStatus,
)


class RunEvaluationSource(Protocol):
    """Minimal persistence view needed to project one run evaluation."""

    def get(self, run_id: str) -> EpisodeResult | None: ...

    def get_job(self, run_id: str) -> RunJob | None: ...

    def load_recovery(self, run_id: str) -> RunRecovery | None: ...

    def list_invocations(self, run_id: str) -> tuple[PolicyInvocation, ...]: ...


class RunEvaluationProjector:
    """Score completed episodes or the latest durable settled prefix uniformly."""

    def __init__(
        self,
        source: RunEvaluationSource,
        evaluator: Evaluator,
        engine: EconomyEngine | None = None,
    ) -> None:
        self._source = source
        self._evaluator = evaluator
        self._engine = engine or EconomyEngine()

    def read(self, run_id: str) -> RunEvaluation | None:
        """Return an evaluation once at least one full week has settled."""
        episode = self._source.get(run_id)
        job = self._source.get_job(run_id)
        recovery = self._source.load_recovery(run_id)
        if episode is not None:
            if job is None:
                raise ValueError("completed episode is missing its RunJob")
            if recovery is not None:
                raise ValueError("completed episode cannot retain an active checkpoint")
            self._validate_completed(job, episode)
            return RunEvaluation.from_episode(
                episode,
                AgentUsageSummary.from_invocations(
                    self._source.list_invocations(run_id),
                    episode.scenario.weeks,
                ),
            )

        if recovery is not None and job is None:
            raise ValueError("run checkpoint is missing its RunJob")
        if job is None or recovery is None:
            return None
        checkpoint = recovery.checkpoint
        final_state = checkpoint.economy.base_state
        completed_weeks = final_state.completed_weeks
        if completed_weeks == 0:
            return None
        self._validate_prefix(job, checkpoint, completed_weeks)

        events = tuple(
            record.event
            for record in checkpoint.events
            if record.event.occurred_on.week <= completed_weeks
        )
        turns = tuple(
            record for record in recovery.turns if record.turn.sim_day.week <= completed_weeks
        )
        issues = tuple(
            record.protocol_issue_kind for record in turns if record.protocol_issue_kind is not None
        )
        protocol = ProtocolReport.from_issues(len(turns), issues)
        scenario = final_state.scenario
        return RunEvaluation(
            run_id=run_id,
            scenario=scenario,
            seed=job.seed,
            completed_weeks=completed_weeks,
            provisional=True,
            agent_usage=AgentUsageSummary.from_invocations(
                self._source.list_invocations(run_id),
                completed_weeks,
            ),
            policies=checkpoint.policies,
            snapshots=checkpoint.snapshots,
            score=self._evaluator.evaluate(
                scenario,
                self._engine.initial_state(scenario, job.seed),
                final_state,
                checkpoint.snapshots,
                events,
            ),
            quality=EpisodeQuality(
                benchmark_eligible=protocol.invalid_turn_count == 0,
                protocol=protocol,
            ),
        )

    @staticmethod
    def _validate_completed(job: RunJob, episode: EpisodeResult) -> None:
        """Fail closed when a completed episode and lifecycle identity diverge."""
        if (
            job.run_id != episode.run_id
            or job.status is not RunStatus.COMPLETED
            or job.scenario_id != episode.scenario.scenario_id
            or job.seed != episode.seed
            or job.total_weeks != episode.scenario.weeks
        ):
            raise ValueError("completed episode differs from its RunJob")

    @staticmethod
    def _validate_prefix(
        job: RunJob,
        checkpoint: RunCheckpoint,
        completed_weeks: int,
    ) -> None:
        """Fail closed if lifecycle progress and checkpoint facts diverge."""
        if checkpoint.run_id != job.run_id:
            raise ValueError("evaluation checkpoint belongs to another run")
        if checkpoint.economy.seed != job.seed:
            raise ValueError("evaluation checkpoint seed differs from its run")
        if (
            checkpoint.economy.scenario.scenario_id != job.scenario_id
            or checkpoint.economy.scenario.weeks != job.total_weeks
        ):
            raise ValueError("evaluation checkpoint scenario differs from its run")
        if len(checkpoint.snapshots) != completed_weeks:
            raise ValueError("evaluation checkpoint is not week-settlement consistent")
