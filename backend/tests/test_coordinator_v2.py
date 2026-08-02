import asyncio

import pytest

from company_bench.agents import BaselineCompanyAgent, CompanyAgent
from company_bench.coordinator import RunCoordinator
from company_bench.dairy_scenario import DAIRY_S9_V3_SCENARIO
from company_bench.models import PolicyKind
from company_bench.policy_factory import AgentBundle, PolicyFactory
from company_bench.repository import MemoryRunRepository
from company_bench.run_models import RunCheckpoint, RunJob, RunStatus
from company_bench.runtime import EpisodeRuntime
from company_bench.runtime_models import AgentTurn, CompanyCommand, SystemStepRecord, TurnRecord
from company_bench.timeline import RunTimelineProjector


async def _wait_for_terminal(
    repository: MemoryRunRepository,
    run_id: str,
) -> RunJob:
    """Wait for one coordinator-owned job to reach a terminal state."""
    async with asyncio.timeout(2):
        while True:
            job = repository.get_job(run_id)
            if job is not None and job.status.terminal:
                return job
            await asyncio.sleep(0)


class _PausingAgent:
    """Delegate a configured number of calls, then remain cancellable."""

    metadata = BaselineCompanyAgent.metadata

    def __init__(self, passthrough_calls: int) -> None:
        self.blocked = asyncio.Event()
        self._delegate = BaselineCompanyAgent()
        self._remaining = passthrough_calls

    async def act(self, turn: AgentTurn) -> CompanyCommand:
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
        super().__init__(DAIRY_S9_V3_SCENARIO.model_copy(update={"days": 1}))
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


class _StopTestPolicyFactory(PolicyFactory):
    """Build deterministic Agents with one cancellable decision."""

    def __init__(
        self,
        repository: MemoryRunRepository,
        blocker: _PausingAgent,
        gateway: _TrackedGateway,
    ) -> None:
        self._blocker = blocker
        self._gateway = gateway
        super().__init__(DAIRY_S9_V3_SCENARIO.model_copy(update={"days": 1}), repository)

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
    MemoryRunRepository,
    RunJob,
    _TrackedGateway,
]:
    """Start a run paused at the requested decision boundary."""
    repository = MemoryRunRepository()
    blocker = _PausingAgent(passthrough_calls)
    active_gateway = gateway or _TrackedGateway()
    factory = _StopTestPolicyFactory(repository, blocker, active_gateway)
    coordinator = RunCoordinator(repository, factory, EpisodeRuntime(factory.scenario))
    submitted = await coordinator.submit(mode=PolicyKind.BASELINE, seed=42)
    async with asyncio.timeout(2):
        await blocker.blocked.wait()
    return coordinator, repository, submitted, active_gateway


def test_coordinator_rejects_a_runtime_for_another_scenario() -> None:
    repository = MemoryRunRepository()
    factory = PolicyFactory(DAIRY_S9_V3_SCENARIO, repository)
    runtime = EpisodeRuntime(DAIRY_S9_V3_SCENARIO.model_copy(update={"days": 1}))

    with pytest.raises(ValueError, match="factory scenarios must match"):
        RunCoordinator(repository, factory, runtime)


@pytest.mark.asyncio
async def test_stop_is_permanent_and_preserves_partial_audit_state() -> None:
    coordinator, repository, submitted, gateway = await _start_blocked_run()
    turns = repository.list_turns(submitted.run_id)
    checkpoint = repository.get_checkpoint(submitted.run_id)
    assert turns
    assert checkpoint is not None

    stopped = await coordinator.stop(submitted.run_id)

    assert stopped.status is RunStatus.STOPPED
    assert stopped.revision > submitted.revision
    assert stopped.finished_at is not None
    assert stopped.error_message is None
    assert repository.get(submitted.run_id) is None
    assert repository.list_turns(submitted.run_id) == turns
    assert repository.get_checkpoint(submitted.run_id) == checkpoint
    assert repository.list_resumable_jobs() == ()
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
    checkpoint = repository.get_checkpoint(submitted.run_id)
    assert checkpoint is not None
    assert repository.list_turns(submitted.run_id) == ()

    stopped = await coordinator.stop(submitted.run_id)
    timeline = RunTimelineProjector(repository).read_day(submitted.run_id, 1)

    assert stopped.status is RunStatus.STOPPED
    assert timeline.context.checkpoint_at == checkpoint.scheduler.now
    assert timeline.moments == ()
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
    assert repository.list_resumable_jobs() == (interrupted,)
    assert repository.get_checkpoint(submitted.run_id) is not None
    assert gateway.close_calls == 1


@pytest.mark.asyncio
async def test_stop_newly_queued_run_before_its_task_starts() -> None:
    repository = MemoryRunRepository()
    runtime = _QueueingRuntime()
    coordinator = RunCoordinator(
        repository,
        PolicyFactory(runtime.scenario, repository),
        runtime,
    )
    queued = await coordinator.submit(mode=PolicyKind.BASELINE, seed=2)

    stopped = await coordinator.stop(queued.run_id)

    assert stopped.status is RunStatus.STOPPED
    assert runtime.started_run_ids == []
    assert repository.get(queued.run_id) is None
    await coordinator.close()


@pytest.mark.asyncio
async def test_stop_queued_run_never_enters_the_runtime() -> None:
    repository = MemoryRunRepository()
    runtime = _QueueingRuntime()
    coordinator = RunCoordinator(
        repository,
        PolicyFactory(runtime.scenario, repository),
        runtime,
    )
    active = await coordinator.submit(mode=PolicyKind.BASELINE, seed=1)
    async with asyncio.timeout(2):
        await runtime.started.wait()
    queued = await coordinator.submit(mode=PolicyKind.BASELINE, seed=2)
    await asyncio.sleep(0)
    assert repository.get_job(queued.run_id) == queued

    stopped = await coordinator.stop(queued.run_id)

    assert stopped.status is RunStatus.STOPPED
    assert runtime.started_run_ids == [active.run_id]
    assert repository.get(queued.run_id) is None
    await coordinator.close()


@pytest.mark.asyncio
async def test_replay_drift_marks_the_job_failed_without_a_result() -> None:
    scenario = DAIRY_S9_V3_SCENARIO.model_copy(update={"days": 1})
    runtime = EpisodeRuntime(scenario)
    source = await runtime.run(
        {company.company_id: BaselineCompanyAgent() for company in scenario.companies},
        8,
        run_id="drift_source",
    )
    repository = MemoryRunRepository()
    repository.save(source.episode)
    for index, record in enumerate(source.turns):
        repository.record_turn(
            record.model_copy(update={"observation_hash": "tampered"}) if index == 0 else record
        )
    coordinator = RunCoordinator(
        repository,
        PolicyFactory(scenario, repository),
        runtime=runtime,
    )

    try:
        submitted = await coordinator.submit(
            mode=PolicyKind.REPLAY,
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


class _InterruptAfterCheckpoint:
    def __init__(self, repository: MemoryRunRepository) -> None:
        self._repository = repository

    def save_progress(
        self,
        records: tuple[TurnRecord, ...],
        system_steps: tuple[SystemStepRecord, ...],
        checkpoint: RunCheckpoint,
    ) -> None:
        self._repository.save_progress(records, system_steps, checkpoint)
        raise RuntimeError("simulated process stop")


@pytest.mark.asyncio
async def test_start_resumes_a_v2_checkpoint_to_completion() -> None:
    scenario = DAIRY_S9_V3_SCENARIO.model_copy(update={"days": 1})
    runtime = EpisodeRuntime(scenario)
    seed = 18
    run_id = "restart_resume"
    agents = {company.company_id: BaselineCompanyAgent() for company in scenario.companies}
    expected = await runtime.run(agents, seed, run_id=run_id)
    repository = MemoryRunRepository()

    with pytest.raises(RuntimeError, match="simulated process stop"):
        await runtime.run(
            {company.company_id: BaselineCompanyAgent() for company in scenario.companies},
            seed,
            run_id=run_id,
            store=_InterruptAfterCheckpoint(repository),
        )
    checkpoint = repository.get_checkpoint(run_id)
    assert checkpoint is not None
    repository.save_job(
        RunJob(
            run_id=run_id,
            mode=PolicyKind.BASELINE,
            status=RunStatus.INTERRUPTED,
            seed=seed,
            scenario_id=scenario.scenario_id,
            current_day=len(checkpoint.snapshots),
            total_days=scenario.days,
            submitted_at=checkpoint.episode_started_at,
            started_at=checkpoint.episode_started_at,
            error_message="simulated process stop",
        )
    )
    coordinator = RunCoordinator(
        repository,
        PolicyFactory(scenario, repository),
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
    assert repository.get_checkpoint(run_id) is None
