"""Asynchronous run lifecycle orchestration."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from company_bench.application import DairyBenchmark
from company_bench.diagnostics import bounded_error
from company_bench.models import MAX_SEED, PolicyKind, ScenarioSpec
from company_bench.policy_factory import PolicyFactory
from company_bench.repository import LifecycleRepository
from company_bench.run_models import PolicyProfileView, RunJob, RunStatus
from company_bench.runtime import EpisodeRuntime


class RunCoordinator:
    """Queue episodes, report progress, and persist terminal outcomes."""

    def __init__(
        self,
        repository: LifecycleRepository,
        policy_factory: PolicyFactory,
        benchmark: DairyBenchmark | None = None,
        *,
        runtime: EpisodeRuntime | None = None,
        max_concurrent_runs: int = 1,
    ) -> None:
        if max_concurrent_runs <= 0:
            raise ValueError("max_concurrent_runs must be positive")
        self._repository = repository
        self._policy_factory = policy_factory
        self._benchmark = benchmark or DairyBenchmark(
            policy_timeout_seconds=policy_factory.policy_timeout_seconds
        )
        self._runtime = runtime
        self._semaphore = asyncio.Semaphore(max_concurrent_runs)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def profiles(self) -> tuple[PolicyProfileView, ...]:
        """Expose policy availability without secrets."""
        return self._policy_factory.profiles()

    async def start(self) -> None:
        """Restart jobs whose process ended before a terminal state."""
        for job in self._repository.list_resumable_jobs():
            self._schedule(job)

    async def submit(
        self,
        *,
        mode: PolicyKind,
        seed: int | None = None,
        source_run_id: str | None = None,
    ) -> RunJob:
        """Persist and enqueue one validated run request."""
        if mode is PolicyKind.REPLAY:
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

        scenario = self._scenario
        if source is not None and source.scenario != scenario:
            raise ValueError("replay source uses a different scenario")
        if (
            source_run_id is not None
            and self._runtime is not None
            and not self._repository.list_turns(source_run_id)
        ):
            raise ValueError("replay source has no V2 turn journal")
        run_id = f"run_{uuid4().hex}"
        self._policy_factory.ensure_available(mode)
        job = RunJob(
            run_id=run_id,
            mode=mode,
            seed=active_seed,
            source_run_id=source_run_id,
            scenario_id=scenario.scenario_id,
            total_days=scenario.days,
            submitted_at=datetime.now(UTC),
        )
        self._repository.save_job(job)
        self._schedule(job)
        return job

    def get_job(self, run_id: str) -> RunJob | None:
        """Return one persisted lifecycle record."""
        return self._repository.get_job(run_id)

    async def close(self) -> None:
        """Cancel active work and persist an interrupted state."""
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _schedule(self, job: RunJob) -> None:
        """Start a job once per coordinator process."""
        if job.run_id in self._tasks:
            return
        task = asyncio.create_task(self._execute(job))
        self._tasks[job.run_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job.run_id, None))

    async def _execute(self, initial_job: RunJob) -> None:
        """Run one episode under the global model-call concurrency bound."""
        job = initial_job
        try:
            async with self._semaphore:
                scenario = self._scenario
                if job.scenario_id != scenario.scenario_id or job.total_days != scenario.days:
                    raise ValueError("persisted job scenario does not match the active runtime")
                job = job.model_copy(
                    update={
                        "status": RunStatus.RUNNING,
                        "started_at": job.started_at or datetime.now(UTC),
                        "finished_at": None,
                        "error_message": None,
                    }
                )
                self._repository.save_job(job)
                source = self._repository.get(job.source_run_id) if job.source_run_id else None

                async def report_progress(day: int) -> None:
                    nonlocal job
                    job = job.model_copy(update={"current_day": day})
                    self._repository.save_job(job)

                if self._runtime is None:
                    bundle = self._policy_factory.create(
                        run_id=job.run_id,
                        mode=job.mode,
                        source=source,
                    )
                    try:
                        result = await self._benchmark.run(
                            bundle.policies,
                            job.seed,
                            run_id=job.run_id,
                            on_day_completed=report_progress,
                        )
                    finally:
                        await bundle.close()
                else:
                    checkpoint = self._repository.get_checkpoint(job.run_id)
                    agent_bundle = self._policy_factory.create_agents(
                        run_id=job.run_id,
                        mode=job.mode,
                        source_turns=(
                            self._repository.list_turns(job.source_run_id)
                            if job.source_run_id is not None
                            else ()
                        ),
                        checkpoints=(checkpoint.agent_states if checkpoint is not None else ()),
                        completed_turns=(checkpoint.turns if checkpoint is not None else ()),
                    )
                    try:
                        execution = await self._runtime.run(
                            agent_bundle.agents,
                            job.seed,
                            run_id=job.run_id,
                            started_at=job.started_at,
                            on_day_completed=report_progress,
                            store=self._repository,
                            checkpoint=checkpoint,
                            replay_source=source,
                        )
                        result = execution.episode
                    finally:
                        await agent_bundle.close()
                job = job.model_copy(
                    update={
                        "status": RunStatus.COMPLETED,
                        "current_day": job.total_days,
                        "finished_at": datetime.now(UTC),
                    }
                )
                self._repository.complete_job(result, job)
        except asyncio.CancelledError:
            self._repository.save_job(
                job.model_copy(
                    update={
                        "status": RunStatus.INTERRUPTED,
                        "finished_at": datetime.now(UTC),
                        "error_message": "backend stopped before completion",
                    }
                )
            )
            raise
        except Exception as error:
            self._repository.save_job(
                job.model_copy(
                    update={
                        "status": RunStatus.FAILED,
                        "finished_at": datetime.now(UTC),
                        "error_message": bounded_error(error),
                    }
                )
            )

    @property
    def _scenario(self) -> ScenarioSpec:
        """Return the single scenario served by this coordinator."""
        return self._runtime.scenario if self._runtime is not None else self._benchmark.scenario
