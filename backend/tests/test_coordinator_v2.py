import asyncio

import pytest

from company_bench.agents import BaselineCompanyAgent
from company_bench.coordinator import RunCoordinator
from company_bench.dairy_scenario import DAIRY_S12_V2_SCENARIO
from company_bench.models import PolicyKind
from company_bench.policy_factory import PolicyFactory
from company_bench.repository import MemoryRunRepository
from company_bench.run_models import RunCheckpoint, RunJob, RunStatus
from company_bench.runtime import EpisodeRuntime
from company_bench.runtime_models import SystemStepRecord, TurnRecord


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


def test_coordinator_rejects_a_runtime_for_another_scenario() -> None:
    repository = MemoryRunRepository()
    factory = PolicyFactory(DAIRY_S12_V2_SCENARIO, repository)
    runtime = EpisodeRuntime(DAIRY_S12_V2_SCENARIO.model_copy(update={"days": 1}))

    with pytest.raises(ValueError, match="factory scenarios must match"):
        RunCoordinator(repository, factory, runtime)


@pytest.mark.asyncio
async def test_replay_drift_marks_the_job_failed_without_a_result() -> None:
    scenario = DAIRY_S12_V2_SCENARIO.model_copy(update={"days": 1})
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
    scenario = DAIRY_S12_V2_SCENARIO.model_copy(update={"days": 1})
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
