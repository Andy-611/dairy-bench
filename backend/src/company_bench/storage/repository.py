"""Persistence interface and shared invariants for benchmark runs."""

from __future__ import annotations

from typing import Protocol

from company_bench.domain.models import EpisodeResult
from company_bench.runs.models import (
    PolicyAuditSink,
    PolicyInvocation,
    ReplaySource,
    RunCheckpoint,
    RunJob,
    RunRecovery,
    RunStatus,
)
from company_bench.runtime.models import SystemStepRecord, TurnRecord

AUTO_RESUME_STATUSES = (
    RunStatus.QUEUED,
    RunStatus.RUNNING,
    RunStatus.INTERRUPTED,
)


class RunRepository(PolicyAuditSink, Protocol):
    """Persist lifecycle, recovery, results, and Agent audit evidence."""

    def get(self, run_id: str) -> EpisodeResult | None:
        """Return an episode, or ``None`` when it does not exist."""
        ...

    def list_replay_sources(self) -> tuple[ReplaySource, ...]:
        """Return every completed run from newest submission to oldest."""
        ...

    def save_job(self, job: RunJob) -> None:
        """Persist or replace a run lifecycle record."""
        ...

    def get_job(self, run_id: str) -> RunJob | None:
        """Return one lifecycle record when present."""
        ...

    def list_jobs(self, limit: int = 50, offset: int = 0) -> tuple[RunJob, ...]:
        """Return the most recently submitted lifecycle records first."""
        ...

    def list_auto_resume_jobs(self) -> tuple[RunJob, ...]:
        """Return jobs that should be scheduled automatically after startup."""
        ...

    def list_invocations(self, run_id: str) -> tuple[PolicyInvocation, ...]:
        """Return one run's Agent invocations in decision order."""
        ...

    def list_turns(self, run_id: str) -> tuple[TurnRecord, ...]:
        """Return one run's complete turn journal in apply order."""
        ...

    def list_system_steps(self, run_id: str) -> tuple[SystemStepRecord, ...]:
        """Return one run's system journal in chronological order."""
        ...

    def save_progress(
        self,
        turns: tuple[TurnRecord, ...],
        system_steps: tuple[SystemStepRecord, ...],
        checkpoint: RunCheckpoint,
    ) -> None:
        """Atomically append journal entries and replace their checkpoint."""
        ...

    def load_recovery(self, run_id: str) -> RunRecovery | None:
        """Load one checkpoint together with its authoritative journals."""
        ...

    def complete_job(
        self,
        result: EpisodeResult,
        completed_job: RunJob,
    ) -> None:
        """Atomically persist a result and its completed lifecycle record."""
        ...


def validate_completion(result: EpisodeResult, completed_job: RunJob) -> None:
    """Reject mismatched or non-completed lifecycle writes."""
    if completed_job.run_id != result.run_id:
        raise ValueError("result and completed job must have the same run_id")
    if completed_job.status != RunStatus.COMPLETED:
        raise ValueError("complete_job requires status=completed")
    if (
        completed_job.scenario_id != result.scenario.scenario_id
        or completed_job.total_weeks != result.scenario.weeks
        or completed_job.seed != result.seed
    ):
        raise ValueError("result and completed job must describe the same episode")
    if completed_job.quality != result.quality:
        raise ValueError("result and completed job must have the same quality")


def turn_order(record: TurnRecord) -> tuple[int, int, str]:
    """Return the stable chronological journal key."""
    return (
        record.turn.sim_day.absolute_day,
        record.outcome.apply_sequence,
        record.turn.turn_id,
    )


def system_step_order(record: SystemStepRecord) -> tuple[int, int, str]:
    """Return the stable chronological system-journal key."""
    return (
        record.occurred_on.absolute_day,
        record.journal_sequence,
        record.entry_id,
    )


def invocation_order(
    invocation: PolicyInvocation,
) -> tuple[int, int, bool, int, str, str]:
    """Order opening calls by company and event-driven calls by application."""
    return (
        invocation.week,
        invocation.absolute_day if invocation.absolute_day is not None else -1,
        invocation.apply_sequence is None,
        invocation.apply_sequence or 0,
        invocation.company_id,
        invocation.invocation_id,
    )


def validate_progress_identity(
    turns: tuple[TurnRecord, ...],
    system_steps: tuple[SystemStepRecord, ...],
    checkpoint: RunCheckpoint,
) -> None:
    """Require every journal delta to belong to the committed run."""
    if any(record.run_id != checkpoint.run_id for record in turns):
        raise ValueError("progress turns must match checkpoint run_id")
    if any(record.run_id != checkpoint.run_id for record in system_steps):
        raise ValueError("progress system steps must match checkpoint run_id")


def require_same_turn(existing: TurnRecord, incoming: TurnRecord) -> None:
    """Reject reuse of one turn identity for different immutable content."""
    if existing != incoming:
        raise ValueError(f"turn identity conflict: {incoming.run_id}/{incoming.turn.turn_id}")


def require_same_system_step(
    existing: SystemStepRecord,
    incoming: SystemStepRecord,
) -> None:
    """Reject reuse of one system identity for different immutable content."""
    if existing != incoming:
        raise ValueError(f"system step identity conflict: {incoming.run_id}/{incoming.entry_id}")
