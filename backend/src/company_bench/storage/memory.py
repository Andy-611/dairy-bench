"""In-memory run repository adapter for deterministic tests and local use."""

from __future__ import annotations

from collections import OrderedDict
from threading import RLock

from company_bench.domain.models import EpisodeResult
from company_bench.runs.models import (
    PolicyInvocation,
    ReplaySource,
    RunCheckpoint,
    RunJob,
    RunRecovery,
    RunStatus,
)
from company_bench.runtime.models import SystemStepRecord, TurnRecord
from company_bench.storage.repository import (
    AUTO_RESUME_STATUSES,
    invocation_order,
    require_same_system_step,
    require_same_turn,
    system_step_order,
    turn_order,
    validate_completion,
    validate_progress_identity,
)


class InMemoryRunRepository:
    """Keep benchmark lifecycle data in process memory."""

    def __init__(self) -> None:
        self._results: OrderedDict[str, EpisodeResult] = OrderedDict()
        self._jobs: OrderedDict[str, RunJob] = OrderedDict()
        self._invocations: dict[str, PolicyInvocation] = {}
        self._turns: dict[tuple[str, str], TurnRecord] = {}
        self._system_steps: dict[tuple[str, str], SystemStepRecord] = {}
        self._checkpoints: dict[str, RunCheckpoint] = {}
        self._lock = RLock()

    def get(self, run_id: str) -> EpisodeResult | None:
        """Return an immutable episode when it exists."""
        with self._lock:
            return self._results.get(run_id)

    def save_job(self, job: RunJob) -> None:
        """Persist or replace a run lifecycle record."""
        with self._lock:
            self._save_job(job)

    def get_job(self, run_id: str) -> RunJob | None:
        """Return one lifecycle record when present."""
        with self._lock:
            return self._jobs.get(run_id)

    def list_jobs(self, limit: int = 50, offset: int = 0) -> tuple[RunJob, ...]:
        """Return all lifecycle states from newest submission to oldest."""
        if limit <= 0 or offset < 0:
            return ()
        with self._lock:
            newest_first = reversed(self._jobs.values())
            jobs = sorted(
                newest_first,
                key=lambda job: job.submitted_at,
                reverse=True,
            )
            return tuple(jobs[offset : offset + limit])

    def list_replay_sources(self) -> tuple[ReplaySource, ...]:
        """Return completed runs from newest submission to oldest."""
        with self._lock:
            sources = (
                ReplaySource(
                    run_id=job.run_id,
                    submitted_at=job.submitted_at,
                    benchmark_eligible=job.quality.benchmark_eligible,
                )
                for job in self._jobs.values()
                if job.status is RunStatus.COMPLETED and job.quality is not None
            )
            return tuple(
                sorted(
                    sources,
                    key=lambda source: source.submitted_at,
                    reverse=True,
                )
            )

    def list_auto_resume_jobs(self) -> tuple[RunJob, ...]:
        """Return automatic startup jobs from oldest to newest."""
        with self._lock:
            jobs = (job for job in self._jobs.values() if job.status in AUTO_RESUME_STATUSES)
            return tuple(sorted(jobs, key=lambda job: job.submitted_at))

    def record_invocation(self, invocation: PolicyInvocation) -> None:
        """Upsert one invocation by its deterministic identifier."""
        with self._lock:
            self._invocations[invocation.invocation_id] = invocation

    def list_invocations(self, run_id: str) -> tuple[PolicyInvocation, ...]:
        """Return one run's invocations in deterministic decision order."""
        with self._lock:
            invocations = (
                invocation
                for invocation in self._invocations.values()
                if invocation.run_id == run_id
            )
            return tuple(sorted(invocations, key=invocation_order))

    def list_turns(self, run_id: str) -> tuple[TurnRecord, ...]:
        """Return one run's turns in deterministic apply order."""
        with self._lock:
            records = (
                record
                for (record_run_id, _), record in self._turns.items()
                if record_run_id == run_id
            )
            return tuple(sorted(records, key=turn_order))

    def list_system_steps(self, run_id: str) -> tuple[SystemStepRecord, ...]:
        """Return one run's system steps in chronological journal order."""
        with self._lock:
            records = (
                record
                for (record_run_id, _), record in self._system_steps.items()
                if record_run_id == run_id
            )
            return tuple(sorted(records, key=system_step_order))

    def save_progress(
        self,
        turns: tuple[TurnRecord, ...],
        system_steps: tuple[SystemStepRecord, ...],
        checkpoint: RunCheckpoint,
    ) -> None:
        """Append journal entries and checkpoint under one process lock."""
        validate_progress_identity(turns, system_steps, checkpoint)
        with self._lock:
            staged_turns: dict[tuple[str, str], TurnRecord] = {}
            for record in turns:
                key = (record.run_id, record.turn.turn_id)
                existing = staged_turns.get(key, self._turns.get(key))
                if existing is not None:
                    require_same_turn(existing, record)
                else:
                    staged_turns[key] = record

            staged_steps: dict[tuple[str, str], SystemStepRecord] = {}
            for record in system_steps:
                key = (record.run_id, record.entry_id)
                existing = staged_steps.get(key, self._system_steps.get(key))
                if existing is not None:
                    require_same_system_step(existing, record)
                else:
                    staged_steps[key] = record

            durable_turns = tuple(
                sorted(
                    (
                        record
                        for (run_id, _), record in (self._turns | staged_turns).items()
                        if run_id == checkpoint.run_id
                    ),
                    key=turn_order,
                )
            )
            durable_steps = tuple(
                sorted(
                    (
                        record
                        for (run_id, _), record in (self._system_steps | staged_steps).items()
                        if run_id == checkpoint.run_id
                    ),
                    key=system_step_order,
                )
            )
            RunRecovery(
                checkpoint=checkpoint,
                turns=durable_turns,
                system_steps=durable_steps,
            )
            self._turns.update(staged_turns)
            self._system_steps.update(staged_steps)
            self._checkpoints[checkpoint.run_id] = checkpoint

    def load_recovery(self, run_id: str) -> RunRecovery | None:
        """Load one checkpoint together with its authoritative journals."""
        with self._lock:
            checkpoint = self._checkpoints.get(run_id)
            if checkpoint is None:
                return None
            return RunRecovery(
                checkpoint=checkpoint,
                turns=self.list_turns(run_id),
                system_steps=self.list_system_steps(run_id),
            )

    def complete_job(
        self,
        result: EpisodeResult,
        completed_job: RunJob,
    ) -> None:
        """Persist the result and completed job under one lock."""
        validate_completion(result, completed_job)
        with self._lock:
            self._results.pop(result.run_id, None)
            self._results[result.run_id] = result
            self._save_job(completed_job)
            self._checkpoints.pop(result.run_id, None)

    def _save_job(self, job: RunJob) -> None:
        """Replace one job while the repository lock is held."""
        self._jobs[job.run_id] = job
