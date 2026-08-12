"""Asynchronous run lifecycle orchestration."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from company_bench.agents.contracts import (
    PolicyInfrastructureError,
    PolicyQuotaExhaustedError,
)
from company_bench.agents.factory import AgentFactory
from company_bench.diagnostics import bounded_error
from company_bench.domain.models import (
    MAX_SEED,
    PolicyKind,
    PolicyMetadata,
    PolicyProfileId,
)
from company_bench.runs.models import PolicyProfileView, RunJob
from company_bench.runtime.episode import EpisodeRuntime
from company_bench.settings import MAX_PARALLELISM, require_parallelism
from company_bench.storage.store import RunStore


class RunCoordinator:
    """Queue event-driven episodes and persist their terminal outcomes."""

    def __init__(
        self,
        repository: RunStore,
        policy_factory: AgentFactory,
        runtime: EpisodeRuntime,
        *,
        max_concurrent_runs: int = MAX_PARALLELISM,
    ) -> None:
        max_concurrent_runs = require_parallelism(
            max_concurrent_runs,
            "max_concurrent_runs",
        )
        if runtime.scenario != policy_factory.scenario:
            raise ValueError("execution and policy factory scenarios must match")
        self._repository = repository
        self._policy_factory = policy_factory
        self._runtime = runtime
        self._run_slots = asyncio.Semaphore(max_concurrent_runs)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_requests: set[str] = set()

    def profiles(self) -> tuple[PolicyProfileView, ...]:
        """Expose policy availability without secrets."""
        return self._policy_factory.profiles()

    async def start(self) -> None:
        """Restart jobs whose process ended before a terminal state."""
        for job in self._repository.list_auto_resume_jobs():
            self._schedule(job)

    async def submit(
        self,
        *,
        profile_id: PolicyProfileId,
        model: str | None = None,
        seed: int | None = None,
        source_run_id: str | None = None,
    ) -> RunJob:
        """Persist and enqueue one validated run request."""
        kind = profile_id.kind
        if kind is PolicyKind.REPLAY:
            if seed is not None:
                raise ValueError("replay mode inherits the source seed")
            source = self._repository.get(source_run_id) if source_run_id is not None else None
            if source is None:
                raise ValueError("replay source_run_id was not found")
            active_seed = source.seed
        else:
            source = None
            if source_run_id is not None:
                raise ValueError("source_run_id is only valid for replay")
            active_seed = 42 if seed is None else seed
        if not 0 <= active_seed <= MAX_SEED:
            raise ValueError(f"seed must be between 0 and {MAX_SEED}")

        scenario = self._runtime.scenario
        if source is not None and source.scenario != scenario:
            raise ValueError("replay source uses a different scenario")
        if source_run_id is not None and not self._repository.list_turns(source_run_id):
            raise ValueError("replay source has no event-driven turn journal")
        run_id = f"run_{uuid4().hex}"
        await self._policy_factory.ensure_available(profile_id, model)
        metadata = self._policy_factory.profile_metadata(profile_id, model)
        job = RunJob(
            run_id=run_id,
            profile_id=profile_id,
            kind=kind,
            provider=metadata.provider,
            model=model,
            wire_protocol=metadata.wire_protocol,
            adapter_version=metadata.adapter_version,
            prompt_version=metadata.prompt_version,
            config_fingerprint=metadata.config_fingerprint,
            seed=active_seed,
            source_run_id=source_run_id,
            scenario_id=scenario.scenario_id,
            total_weeks=scenario.weeks,
            submitted_at=datetime.now(UTC),
        )
        self._repository.save_job(job)
        self._schedule(job)
        return job

    def get_job(self, run_id: str) -> RunJob | None:
        """Return one persisted lifecycle record."""
        return self._repository.get_job(run_id)

    async def stop(self, run_id: str) -> RunJob:
        """Stop one run until an explicit resume request."""
        job = self._repository.get_job(run_id)
        if job is None:
            raise LookupError(f"Run job '{run_id}' was not found")
        if job.status.terminal:
            return job

        task = self._tasks.get(run_id)
        if task is not None:
            self._stop_requests.add(run_id)
            if task.cancelling() == 0:
                task.cancel()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    raise

        current = self._repository.get_job(run_id) or job
        if current.status.terminal:
            return current
        stopped = current.mark_stopped(datetime.now(UTC))
        self._repository.save_job(stopped)
        return stopped

    async def resume(self, run_id: str) -> RunJob:
        """Continue one recoverable run under its original identity."""
        job = self._repository.get_job(run_id)
        if job is None:
            raise LookupError(f"Run job '{run_id}' was not found")
        if not job.status.resumable:
            raise ValueError(f"a {job.status.value} run cannot be resumed")

        task = self._tasks.get(run_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        current = self._repository.get_job(run_id) or job
        await self._policy_factory.ensure_available(current.profile_id, current.model)
        _require_active_profile(
            current,
            self._policy_factory.profile_metadata(current.profile_id, current.model),
        )
        queued = current.queue_for_resume()
        self._repository.save_job(queued)
        self._schedule(queued)
        return queued

    async def close(self) -> None:
        """Cancel active work and persist an interrupted state."""
        tasks = tuple(self._tasks.values())
        for task in tasks:
            if task.cancelling() == 0:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _schedule(self, job: RunJob) -> None:
        """Start a job once per coordinator process."""
        if job.run_id in self._tasks:
            return
        task = asyncio.create_task(self._execute(job))
        self._tasks[job.run_id] = task
        task.add_done_callback(lambda _: self._forget_task(job.run_id))

    def _forget_task(self, run_id: str) -> None:
        """Release task bookkeeping only after cancellation cleanup is complete."""
        self._tasks.pop(run_id, None)
        self._stop_requests.discard(run_id)

    async def _execute(self, initial_job: RunJob) -> None:
        """Run one isolated episode under the application-level run bound."""
        job = initial_job
        try:
            async with self._run_slots:
                scenario = self._runtime.scenario
                if job.scenario_id != scenario.scenario_id or job.total_weeks != scenario.weeks:
                    raise ValueError("persisted job scenario does not match the active runtime")
                await self._policy_factory.ensure_available(job.profile_id, job.model)
                _require_active_profile(
                    job,
                    self._policy_factory.profile_metadata(job.profile_id, job.model),
                )
                job = job.mark_running(datetime.now(UTC))
                self._repository.save_job(job)
                source = self._repository.get(job.source_run_id) if job.source_run_id else None

                async def report_progress(week: int) -> None:
                    nonlocal job
                    job = job.report_week_completed(week)
                    self._repository.save_job(job)

                recovery = self._repository.load_recovery(job.run_id)
                checkpoint = recovery.checkpoint if recovery is not None else None
                agent_bundle = self._policy_factory.create_agents(
                    run_id=job.run_id,
                    profile_id=job.profile_id,
                    model=job.model,
                    source_turns=(
                        self._repository.list_turns(job.source_run_id)
                        if job.source_run_id is not None
                        else ()
                    ),
                    checkpoints=(checkpoint.agent_states if checkpoint is not None else ()),
                    completed_turns=(recovery.turns if recovery is not None else ()),
                )
                try:
                    execution = await self._runtime.run(
                        agent_bundle.agents,
                        job.seed,
                        run_id=job.run_id,
                        started_at=job.started_at,
                        on_week_completed=report_progress,
                        store=self._repository,
                        recovery=recovery,
                        replay_source=source,
                    )
                    result = execution.episode
                finally:
                    await agent_bundle.close()
                job = job.mark_completed(datetime.now(UTC), result.quality)
                self._repository.complete_job(result, job)
        except asyncio.CancelledError:
            current = self._repository.get_job(job.run_id) or job
            if not current.status.terminal:
                if job.run_id in self._stop_requests:
                    current = current.mark_stopped(datetime.now(UTC))
                else:
                    current = current.mark_interrupted(
                        datetime.now(UTC),
                        "backend stopped before completion",
                    )
                self._repository.save_job(current)
            raise
        except PolicyQuotaExhaustedError as error:
            current = self._repository.get_job(job.run_id) or job
            if not current.status.terminal:
                self._repository.save_job(
                    current.mark_quota_exhausted(
                        datetime.now(UTC),
                        bounded_error(error),
                    )
                )
        except PolicyInfrastructureError as error:
            current = self._repository.get_job(job.run_id) or job
            if not current.status.terminal:
                self._repository.save_job(
                    current.mark_interrupted(
                        datetime.now(UTC),
                        bounded_error(error),
                    )
                )
        except Exception as error:
            current = self._repository.get_job(job.run_id) or job
            if not current.status.terminal:
                self._repository.save_job(
                    current.mark_failed(
                        datetime.now(UTC),
                        bounded_error(error),
                    )
                )


def _require_active_profile(job: RunJob, metadata: PolicyMetadata) -> None:
    """Reject resume when the configured adapter identity has changed."""
    expected = (
        job.profile_id,
        job.kind,
        job.provider,
        job.model,
        job.wire_protocol,
        job.adapter_version,
        job.prompt_version,
        job.config_fingerprint,
    )
    active = (
        metadata.profile_id,
        metadata.kind,
        metadata.provider,
        metadata.model,
        metadata.wire_protocol,
        metadata.adapter_version,
        metadata.prompt_version,
        metadata.config_fingerprint,
    )
    if active != expected:
        raise ValueError("persisted job policy profile differs from the active adapter")
