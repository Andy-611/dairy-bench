import asyncio
from datetime import UTC, datetime

import pytest

from company_bench.agents.company import BaselineCompanyAgent, CompanyAgent
from company_bench.agents.factory import AgentBundle, AgentFactory
from company_bench.domain.models import (
    EpisodeQuality,
    PolicyKind,
    PolicyProfileId,
    ProtocolReport,
)
from company_bench.domain.scenario import DAIRY_S9_SCENARIO
from company_bench.runs.coordinator import RunCoordinator
from company_bench.runs.models import RunCheckpoint, RunJob, RunStatus, RunStopReason
from company_bench.runtime.episode import EpisodeExecution, EpisodeRuntime
from company_bench.runtime.models import AgentTurn, CompanyDecision, SystemStepRecord, TurnRecord
from company_bench.storage.memory import InMemoryRunRepository
from company_bench.timeline.projector import RunTimelineProjector


async def _wait_for_terminal(
    repository: InMemoryRunRepository,
    run_id: str,
) -> RunJob:
    """Wait for one coordinator-owned job to reach a terminal state."""
    async with asyncio.timeout(2):
        while True:
            job = repository.get_job(run_id)
            if job is not None and job.status.terminal:
                return job
            await asyncio.sleep(0)


def _completed_job_for(execution: EpisodeExecution) -> RunJob:
    """Build the lifecycle record paired with one completed baseline episode."""
    result = execution.episode
    return RunJob(
        run_id=result.run_id,
        profile_id=PolicyProfileId.BASELINE,
        kind=PolicyKind.BASELINE,
        status=RunStatus.COMPLETED,
        seed=result.seed,
        scenario_id=result.scenario.scenario_id,
        current_absolute_day=result.scenario.calendar.total_days,
        total_weeks=result.scenario.weeks,
        submitted_at=result.started_at,
        started_at=result.started_at,
        finished_at=result.finished_at,
        quality=result.quality,
    )


class _JobHidingRunRepository(InMemoryRunRepository):
    """Hide one persisted source job to exercise replay fail-closed behavior."""

    hidden_run_id: str | None = None

    def get_job(self, run_id: str) -> RunJob | None:
        return None if run_id == self.hidden_run_id else super().get_job(run_id)


class _PausingAgent:
    """Delegate a configured number of calls, then remain cancellable."""

    metadata = BaselineCompanyAgent.metadata

    def __init__(self, passthrough_calls: int) -> None:
        self.blocked = asyncio.Event()
        self._delegate = BaselineCompanyAgent()
        self._remaining = passthrough_calls

    async def act(self, turn: AgentTurn) -> CompanyDecision:
        """Delegate until the configured pause point."""
        if self._remaining == 0:
            self.blocked.set()
            await asyncio.Future()
        self._remaining -= 1
        return await self._delegate.act(turn)


class _TrackedGateway:
    """Record release of one coordinator-owned provider resource."""

    def __init__(self, close_gate: asyncio.Event | None = None) -> None:
        self.close_cancelled = False
        self.close_calls = 0
        self.close_started = asyncio.Event()
        self._close_gate = close_gate

    async def close(self) -> None:
        """Record one release."""
        self.close_calls += 1
        self.close_started.set()
        if self._close_gate is not None:
            try:
                await self._close_gate.wait()
            except asyncio.CancelledError:
                self.close_cancelled = True
                raise


class _QueueingRuntime(EpisodeRuntime):
    """Hold the active slot so a second run remains queued."""

    def __init__(self) -> None:
        super().__init__(DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1}))
        self.started = asyncio.Event()
        self.started_run_ids: list[str] = []

    async def run(
        self,
        *_: object,
        run_id: str,
        **__: object,
    ) -> None:
        """Record entry, then wait for coordinator cancellation."""
        self.started_run_ids.append(run_id)
        self.started.set()
        await asyncio.Future()


class _StopTestAgentFactory(AgentFactory):
    """Build deterministic Agents with one cancellable decision."""

    def __init__(
        self,
        repository: InMemoryRunRepository,
        blocker: _PausingAgent,
        gateway: _TrackedGateway,
    ) -> None:
        self._blocker = blocker
        self._gateway = gateway
        super().__init__(DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1}), repository)

    def create_agents(self, **_: object) -> AgentBundle:
        """Return fresh baseline actors except for one blocking actor."""
        agents: dict[str, CompanyAgent] = {
            company.company_id: BaselineCompanyAgent() for company in self.scenario.companies
        }
        agents[self.scenario.companies[0].company_id] = self._blocker
        return AgentBundle(agents, (self._gateway,))


async def _start_blocked_run(
    *,
    passthrough_calls: int = 1,
    gateway: _TrackedGateway | None = None,
) -> tuple[
    RunCoordinator,
    InMemoryRunRepository,
    RunJob,
    _TrackedGateway,
]:
    """Start a run paused at the requested decision boundary."""
    repository = InMemoryRunRepository()
    blocker = _PausingAgent(passthrough_calls)
    active_gateway = gateway or _TrackedGateway()
    factory = _StopTestAgentFactory(repository, blocker, active_gateway)
    coordinator = RunCoordinator(repository, factory, EpisodeRuntime(factory.scenario))
    submitted = await coordinator.submit(profile_id=PolicyProfileId.BASELINE, seed=42)
    async with asyncio.timeout(2):
        await blocker.blocked.wait()
    return coordinator, repository, submitted, active_gateway


def test_coordinator_rejects_a_runtime_for_another_scenario() -> None:
    repository = InMemoryRunRepository()
    factory = AgentFactory(DAIRY_S9_SCENARIO, repository)
    runtime = EpisodeRuntime(DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1}))

    with pytest.raises(ValueError, match="factory scenarios must match"):
        RunCoordinator(repository, factory, runtime)


@pytest.mark.asyncio
async def test_stop_preserves_resumable_partial_audit_state() -> None:
    coordinator, repository, submitted, gateway = await _start_blocked_run()
    turns = repository.list_turns(submitted.run_id)
    recovery = repository.load_recovery(submitted.run_id)
    assert turns
    assert recovery is not None

    stopped = await coordinator.stop(submitted.run_id)

    assert stopped.status is RunStatus.STOPPED
    assert stopped.revision > submitted.revision
    assert stopped.finished_at is not None
    assert stopped.error_message is None
    assert stopped.stop_reason is RunStopReason.USER_REQUESTED
    assert repository.get(submitted.run_id) is None
    assert repository.list_turns(submitted.run_id) == turns
    assert repository.load_recovery(submitted.run_id) == recovery
    assert repository.list_auto_resume_jobs() == ()
    assert gateway.close_calls == 1
    assert await coordinator.stop(submitted.run_id) == stopped
    with pytest.raises(LookupError, match="was not found"):
        await coordinator.stop("missing")
    await coordinator.close()


@pytest.mark.asyncio
async def test_stop_before_first_decision_keeps_a_readable_empty_timeline() -> None:
    coordinator, repository, submitted, _ = await _start_blocked_run(
        passthrough_calls=0,
    )
    recovery = repository.load_recovery(submitted.run_id)
    assert recovery is not None
    checkpoint = recovery.checkpoint
    assert repository.list_turns(submitted.run_id) == ()

    stopped = await coordinator.stop(submitted.run_id)
    timeline = RunTimelineProjector(repository).read_week(submitted.run_id, 1)

    assert stopped.status is RunStatus.STOPPED
    assert timeline.context.checkpoint_on == checkpoint.scheduler.today
    assert len(timeline.days) == 7
    assert all(not day.turns and not day.system_steps for day in timeline.days)
    await coordinator.close()


@pytest.mark.asyncio
async def test_concurrent_stop_and_close_do_not_cancel_gateway_cleanup() -> None:
    close_gate = asyncio.Event()
    gateway = _TrackedGateway(close_gate)
    coordinator, repository, submitted, _ = await _start_blocked_run(
        gateway=gateway,
    )

    first_stop = asyncio.create_task(coordinator.stop(submitted.run_id))
    async with asyncio.timeout(2):
        await gateway.close_started.wait()
    second_stop = asyncio.create_task(coordinator.stop(submitted.run_id))
    backend_close = asyncio.create_task(coordinator.close())
    await asyncio.sleep(0)
    close_gate.set()
    first, second, _ = await asyncio.gather(
        first_stop,
        second_stop,
        backend_close,
    )

    assert first == second
    assert first.status is RunStatus.STOPPED
    assert repository.get_job(submitted.run_id) == first
    assert gateway.close_calls == 1
    assert not gateway.close_cancelled


@pytest.mark.asyncio
async def test_backend_close_remains_a_resumable_interruption() -> None:
    coordinator, repository, submitted, gateway = await _start_blocked_run()

    await coordinator.close()

    interrupted = repository.get_job(submitted.run_id)
    assert interrupted is not None
    assert interrupted.status is RunStatus.INTERRUPTED
    assert repository.list_auto_resume_jobs() == (interrupted,)
    assert repository.load_recovery(submitted.run_id) is not None
    assert gateway.close_calls == 1


@pytest.mark.asyncio
async def test_stop_newly_queued_run_before_its_task_starts() -> None:
    repository = InMemoryRunRepository()
    runtime = _QueueingRuntime()
    coordinator = RunCoordinator(
        repository,
        AgentFactory(runtime.scenario, repository),
        runtime,
        max_concurrent_runs=1,
    )
    queued = await coordinator.submit(profile_id=PolicyProfileId.BASELINE, seed=2)

    stopped = await coordinator.stop(queued.run_id)

    assert stopped.status is RunStatus.STOPPED
    assert runtime.started_run_ids == []
    assert repository.get(queued.run_id) is None
    await coordinator.close()


@pytest.mark.asyncio
async def test_default_limit_starts_100_runs_and_queues_the_101st() -> None:
    repository = InMemoryRunRepository()
    runtime = _QueueingRuntime()
    coordinator = RunCoordinator(
        repository,
        AgentFactory(runtime.scenario, repository),
        runtime,
    )
    jobs = [
        await coordinator.submit(profile_id=PolicyProfileId.BASELINE, seed=seed)
        for seed in range(101)
    ]

    try:
        async with asyncio.timeout(2):
            while len(runtime.started_run_ids) < 100:
                await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(runtime.started_run_ids) == 100
        assert (repository.get_job(jobs[-1].run_id) or jobs[-1]).status is RunStatus.QUEUED
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_stop_queued_run_never_enters_the_runtime() -> None:
    repository = InMemoryRunRepository()
    runtime = _QueueingRuntime()
    coordinator = RunCoordinator(
        repository,
        AgentFactory(runtime.scenario, repository),
        runtime,
        max_concurrent_runs=1,
    )
    active = await coordinator.submit(profile_id=PolicyProfileId.BASELINE, seed=1)
    async with asyncio.timeout(2):
        await runtime.started.wait()
    queued = await coordinator.submit(profile_id=PolicyProfileId.BASELINE, seed=2)
    await asyncio.sleep(0)
    assert repository.get_job(queued.run_id) == queued

    stopped = await coordinator.stop(queued.run_id)

    assert stopped.status is RunStatus.STOPPED
    assert runtime.started_run_ids == [active.run_id]
    assert repository.get(queued.run_id) is None
    await coordinator.close()


@pytest.mark.asyncio
async def test_replay_drift_marks_the_job_failed_without_a_result() -> None:
    scenario = DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1})
    runtime = EpisodeRuntime(scenario)
    repository = _TamperedJournalRepository()
    source = await runtime.run(
        {company.company_id: BaselineCompanyAgent() for company in scenario.companies},
        8,
        run_id="drift_source",
        store=repository,
    )
    repository.complete_job(
        source.episode,
        _completed_job_for(source),
    )
    coordinator = RunCoordinator(
        repository,
        AgentFactory(scenario, repository),
        runtime=runtime,
    )

    try:
        submitted = await coordinator.submit(
            profile_id=PolicyProfileId.REPLAY,
            source_run_id=source.episode.run_id,
        )
        await _wait_for_terminal(repository, submitted.run_id)
    finally:
        await coordinator.close()

    failed = repository.get_job(submitted.run_id)
    assert failed is not None
    assert failed.status is RunStatus.FAILED
    assert "ReplayDriftError" in (failed.error_message or "")
    assert repository.get(submitted.run_id) is None


@pytest.mark.asyncio
async def test_replay_rejects_a_source_result_without_run_job() -> None:
    scenario = DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1})
    runtime = EpisodeRuntime(scenario)
    repository = _JobHidingRunRepository()
    source = await runtime.run(
        {company.company_id: BaselineCompanyAgent() for company in scenario.companies},
        9,
        run_id="orphan_replay_source",
        store=repository,
    )
    repository.complete_job(source.episode, _completed_job_for(source))
    repository.hidden_run_id = source.episode.run_id
    coordinator = RunCoordinator(
        repository,
        AgentFactory(scenario, repository),
        runtime=runtime,
    )

    try:
        with pytest.raises(ValueError, match="replay source is missing its RunJob"):
            await coordinator.submit(
                profile_id=PolicyProfileId.REPLAY,
                source_run_id=source.episode.run_id,
            )
    finally:
        await coordinator.close()


class _InterruptAfterCheckpoint:
    def __init__(self, repository: InMemoryRunRepository) -> None:
        self._repository = repository

    def save_progress(
        self,
        records: tuple[TurnRecord, ...],
        system_steps: tuple[SystemStepRecord, ...],
        checkpoint: RunCheckpoint,
    ) -> None:
        self._repository.save_progress(records, system_steps, checkpoint)
        raise RuntimeError("simulated process stop")


class _TamperedJournalRepository(InMemoryRunRepository):
    """Corrupt one durable source Turn without exposing non-atomic writes."""

    def __init__(self) -> None:
        super().__init__()
        self._tampered = False

    def save_progress(
        self,
        records: tuple[TurnRecord, ...],
        system_steps: tuple[SystemStepRecord, ...],
        checkpoint: RunCheckpoint,
    ) -> None:
        if records and not self._tampered:
            records = (
                records[0].model_copy(update={"observation_hash": "tampered"}),
                *records[1:],
            )
            self._tampered = True
        super().save_progress(records, system_steps, checkpoint)


async def _save_checkpointed_job(
    repository: InMemoryRunRepository,
    runtime: EpisodeRuntime,
    *,
    run_id: str,
    seed: int,
    status: RunStatus,
) -> tuple[EpisodeExecution, RunJob]:
    """Persist one partial run alongside its deterministic completed reference."""
    scenario = runtime.scenario
    agents = {company.company_id: BaselineCompanyAgent() for company in scenario.companies}
    expected = await runtime.run(agents, seed, run_id=run_id)
    with pytest.raises(RuntimeError, match="simulated process stop"):
        await runtime.run(
            {company.company_id: BaselineCompanyAgent() for company in scenario.companies},
            seed,
            run_id=run_id,
            store=_InterruptAfterCheckpoint(repository),
        )
    recovery = repository.load_recovery(run_id)
    assert recovery is not None
    checkpoint = recovery.checkpoint
    job = RunJob(
        run_id=run_id,
        profile_id=PolicyProfileId.BASELINE,
        kind=PolicyKind.BASELINE,
        status=status,
        seed=seed,
        scenario_id=scenario.scenario_id,
        current_absolute_day=len(checkpoint.snapshots),
        total_weeks=scenario.weeks,
        submitted_at=checkpoint.episode_started_at,
        started_at=checkpoint.episode_started_at,
        finished_at=checkpoint.episode_started_at,
        error_message=None if status is RunStatus.STOPPED else "simulated interruption",
    )
    repository.save_job(job)
    return expected, job


@pytest.mark.asyncio
async def test_start_resumes_a_checkpoint_to_completion() -> None:
    scenario = DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1})
    runtime = EpisodeRuntime(scenario)
    seed = 18
    run_id = "restart_resume"
    repository = InMemoryRunRepository()
    expected, _ = await _save_checkpointed_job(
        repository,
        runtime,
        run_id=run_id,
        seed=seed,
        status=RunStatus.INTERRUPTED,
    )
    coordinator = RunCoordinator(
        repository,
        AgentFactory(scenario, repository),
        runtime=runtime,
    )

    await coordinator.start()
    try:
        completed = await _wait_for_terminal(repository, run_id)
        unchanged = await coordinator.stop(run_id)
        assert unchanged == completed
        assert unchanged.revision == completed.revision
    finally:
        await coordinator.close()

    result = repository.get(run_id)
    assert completed.status is RunStatus.COMPLETED
    assert result is not None
    assert repository.list_turns(run_id) == expected.turns
    assert result.events == expected.episode.events
    assert result.snapshots == expected.episode.snapshots
    assert result.score == expected.episode.score
    assert repository.load_recovery(run_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    (RunStatus.INTERRUPTED, RunStatus.STOPPED),
)
async def test_explicit_resume_continues_the_same_checkpointed_run(
    status: RunStatus,
) -> None:
    scenario = DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1})
    runtime = EpisodeRuntime(scenario)
    repository = InMemoryRunRepository()
    run_id = f"explicit_resume_{status.value}"
    expected, suspended = await _save_checkpointed_job(
        repository,
        runtime,
        run_id=run_id,
        seed=19,
        status=status,
    )
    coordinator = RunCoordinator(
        repository,
        AgentFactory(scenario, repository),
        runtime=runtime,
    )

    if status is not RunStatus.INTERRUPTED:
        await coordinator.start()
        await asyncio.sleep(0)
        assert repository.get_job(run_id) == suspended

    try:
        queued = await coordinator.resume(run_id)
        completed = await _wait_for_terminal(repository, run_id)
    finally:
        await coordinator.close()

    result = repository.get(run_id)
    assert queued.run_id == suspended.run_id
    assert queued.status is RunStatus.QUEUED
    assert queued.current_absolute_day == suspended.current_absolute_day
    assert queued.finished_at is None
    assert queued.error_message is None
    assert queued.stop_reason is None
    assert completed.status is RunStatus.COMPLETED
    assert result is not None
    assert repository.list_turns(run_id) == expected.turns
    assert result.score == expected.episode.score
    assert repository.load_recovery(run_id) is None


@pytest.mark.asyncio
async def test_stopped_run_without_checkpoint_resumes_from_the_beginning() -> None:
    scenario = DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1})
    repository = InMemoryRunRepository()
    job = RunJob(
        run_id="resume_from_start",
        profile_id=PolicyProfileId.BASELINE,
        kind=PolicyKind.BASELINE,
        status=RunStatus.STOPPED,
        seed=20,
        scenario_id=scenario.scenario_id,
        total_weeks=scenario.weeks,
        submitted_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    repository.save_job(job)
    coordinator = RunCoordinator(
        repository,
        AgentFactory(scenario, repository),
        runtime=EpisodeRuntime(scenario),
    )

    try:
        queued = await coordinator.resume(job.run_id)
        completed = await _wait_for_terminal(repository, job.run_id)
    finally:
        await coordinator.close()

    assert queued.run_id == job.run_id
    assert completed.status is RunStatus.COMPLETED
    assert repository.get(job.run_id) is not None


@pytest.mark.asyncio
async def test_resume_rejects_irrecoverable_or_completed_runs() -> None:
    scenario = DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1})
    repository = InMemoryRunRepository()
    base = RunJob(
        run_id="resume_rejected",
        profile_id=PolicyProfileId.BASELINE,
        kind=PolicyKind.BASELINE,
        status=RunStatus.FAILED,
        seed=21,
        scenario_id=scenario.scenario_id,
        total_weeks=scenario.weeks,
        submitted_at=datetime.now(UTC),
    )
    repository.save_job(base)
    coordinator = RunCoordinator(
        repository,
        AgentFactory(scenario, repository),
        runtime=EpisodeRuntime(scenario),
    )

    with pytest.raises(ValueError, match="failed run cannot be resumed"):
        await coordinator.resume(base.run_id)
    repository.save_job(
        base.model_copy(
            update={
                "status": RunStatus.COMPLETED,
                "quality": EpisodeQuality(
                    benchmark_eligible=True,
                    protocol=ProtocolReport(total_turn_count=0, invalid_turn_count=0),
                ),
            }
        )
    )
    with pytest.raises(ValueError, match="completed run cannot be resumed"):
        await coordinator.resume(base.run_id)
    with pytest.raises(LookupError, match="was not found"):
        await coordinator.resume("missing")
    await coordinator.close()
