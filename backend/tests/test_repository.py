import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from company_bench.agents.company import BaselineCompanyAgent
from company_bench.agents.memory import AgentCheckpoint
from company_bench.domain.models import (
    CompanyObservation,
    EpisodeQuality,
    EpisodeResult,
    PolicyDescriptor,
    PolicyKind,
    PolicyProfileId,
    ProtocolIssueKind,
    ProtocolReport,
)
from company_bench.domain.scenario import DAIRY_S9_SCENARIO
from company_bench.economy.engine import EconomyEngine
from company_bench.runs.models import (
    AgentUsageSummary,
    CompanyRuntimeCursor,
    InvocationOutcome,
    PolicyInvocation,
    ReplaySource,
    RunCheckpoint,
    RunJob,
    RunRecovery,
    RunStatus,
    RunStopReason,
)
from company_bench.runtime.episode import EpisodeRuntime
from company_bench.runtime.models import (
    AgentTurn,
    AttentionPlan,
    DecisionEnvelope,
    DecisionOutcome,
    DecisionStatus,
    IdleDecision,
    TurnRecord,
    WakeReason,
)
from company_bench.runtime.scheduler import SchedulerCheckpoint
from company_bench.storage.memory import InMemoryRunRepository
from company_bench.storage.repository import RunRepository
from company_bench.storage.sqlite import SQLiteRunRepository


def run_episode(seed: int = 42) -> EpisodeResult:
    """Create one short real episode through the public runtime interface."""
    scenario = DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1})
    agents = {company.company_id: BaselineCompanyAgent() for company in scenario.companies}
    execution = asyncio.run(
        EpisodeRuntime(scenario).run(
            agents,
            seed,
            run_id=f"repository_{seed}",
        )
    )
    return execution.episode


def test_memory_repository_completed_episode_round_trip() -> None:
    repository = InMemoryRunRepository()
    result = run_episode()

    repository.complete_job(result, _completed_job_for(result))

    assert result.score.efficiency_oracle > 0
    assert repository.get(result.run_id) == result
    assert repository.get("missing") is None


def test_repository_rejects_completion_quality_drift() -> None:
    repository = InMemoryRunRepository()
    result = run_episode()
    invalid_quality = EpisodeQuality(
        benchmark_eligible=False,
        protocol=ProtocolReport.from_issues(
            1,
            (ProtocolIssueKind.MISSING_TOOL_CALL,),
        ),
    )
    completed_job = _completed_job_for(result).model_copy(update={"quality": invalid_quality})

    with pytest.raises(ValueError, match="same quality"):
        repository.complete_job(result, completed_job)


def test_memory_repository_lists_every_job_status_newest_first() -> None:
    repository = InMemoryRunRepository()
    jobs = _history_jobs()

    _assert_job_history(repository, jobs)


def test_memory_repository_lists_completed_replay_sources() -> None:
    repository = InMemoryRunRepository()

    _assert_replay_sources(repository)


def test_sqlite_repository_job_history_survives_reopen(tmp_path: Path) -> None:
    database = tmp_path / "job-history.sqlite3"
    jobs = _history_jobs()

    with SQLiteRunRepository(database) as repository:
        _assert_job_history(repository, jobs)

    with SQLiteRunRepository(database) as reopened:
        assert reopened.list_jobs() == tuple(reversed(jobs))
        assert reopened.list_jobs(2) == tuple(reversed(jobs[-2:]))


def test_sqlite_repository_lists_completed_replay_sources(tmp_path: Path) -> None:
    database = tmp_path / "replay-sources.sqlite3"

    with SQLiteRunRepository(database) as repository:
        expected = _assert_replay_sources(repository)

    with SQLiteRunRepository(database) as reopened:
        assert reopened.list_replay_sources() == expected


def test_memory_repository_turn_journal_and_checkpoint_contract(
    first_observation: CompanyObservation,
) -> None:
    repository = InMemoryRunRepository()
    first = _turn_for("memory_run", first_observation, sequence=1)
    second = _turn_for("memory_run", first_observation, sequence=2)
    checkpoint = _checkpoint_for(first, first_observation)

    _assert_checkpoint_contract(repository, checkpoint, first, second)


def test_checkpoint_accepts_model_as_a_memory_owning_policy(
    first_observation: CompanyObservation,
) -> None:
    repository = InMemoryRunRepository()
    turn = _turn_for("model_checkpoint", first_observation, sequence=1)
    checkpoint = _checkpoint_for(
        turn,
        first_observation,
        policy_kind=PolicyKind.MODEL,
    )

    repository.save_progress((turn,), (), checkpoint)

    assert repository.load_recovery(turn.run_id) == RunRecovery(
        checkpoint=checkpoint,
        turns=(turn,),
    )
    policy = next(
        candidate
        for candidate in checkpoint.policies
        if candidate.company_id == first_observation.company_id
    )
    assert policy.kind is PolicyKind.MODEL
    assert checkpoint.agent_states[0].company_id == first_observation.company_id


def test_memory_repository_progress_is_atomic_and_checkpoint_aligned(
    first_observation: CompanyObservation,
) -> None:
    repository = InMemoryRunRepository()

    _assert_progress_contract(repository, first_observation, "memory_progress")


def test_memory_repository_orders_invocations_by_turn_application(
    first_observation: CompanyObservation,
) -> None:
    repository = InMemoryRunRepository()

    _assert_invocation_order(repository, first_observation, "memory_invocations")


def test_agent_usage_summary_excludes_unsettled_weeks(
    first_observation: CompanyObservation,
) -> None:
    settled = _invocation_for("usage_summary", first_observation)
    future = settled.model_copy(update={"invocation_id": "usage_summary.future", "week": 2})

    summary = AgentUsageSummary.from_invocations((settled, future), through_week=1)

    assert summary is not None
    assert summary.invocation_count == 1
    assert summary.successful_invocations == 1
    assert summary.providers == (settled.provider,)
    assert summary.models == (settled.model,)
    assert summary.usage == settled.usage


def test_sqlite_repository_persists_complete_episode_and_projections(
    tmp_path: Path,
    first_observation: CompanyObservation,
) -> None:
    database = tmp_path / "runs.sqlite3"
    result = run_episode()
    queued_job = _job_for(result)
    invocation = _invocation_for(result.run_id, first_observation)
    completion_turn = _turn_for(result.run_id, first_observation)
    completion_checkpoint = _checkpoint_for(completion_turn, first_observation)

    with SQLiteRunRepository(database) as repository:
        repository.save_job(queued_job)
        repository.record_invocation(invocation)
        repository.record_invocation(invocation)
        repository.save_progress((completion_turn,), (), completion_checkpoint)
        assert repository.get_job(result.run_id) == queued_job
        assert repository.list_auto_resume_jobs() == (queued_job,)
        assert repository.list_invocations(result.run_id) == (invocation,)
        assert repository.load_recovery(result.run_id) is not None

        completed_job = queued_job.model_copy(
            update={
                "status": RunStatus.COMPLETED,
                "current_absolute_day": result.scenario.calendar.total_days,
                "started_at": result.started_at,
                "finished_at": result.finished_at,
                "quality": result.quality,
            }
        )
        repository.complete_job(result, completed_job)
        assert repository.get(result.run_id) == result
        assert repository.get_job(result.run_id) == completed_job
        assert repository.list_auto_resume_jobs() == ()
        assert repository.load_recovery(result.run_id) is None
        assert repository.list_turns(result.run_id) == (completion_turn,)
        assert repository.get("missing") is None
    expected_rows = {
        "runs": 1,
        "run_jobs": 1,
        "policy_invocations": 1,
        "run_checkpoints": 0,
        "run_turns": 1,
        "run_system_steps": 0,
    }
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 16
        assert connection.execute("SELECT schema_version FROM run_turns").fetchone()[0] == 10
        assert tuple(row[1] for row in connection.execute("PRAGMA table_info(runs)")) == (
            "run_id",
            "result_json",
        )
        for table, expected in expected_rows.items():
            actual = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert actual == expected

    with SQLiteRunRepository(database) as reopened:
        assert reopened.get(result.run_id) == result
        assert reopened.get_job(result.run_id) == completed_job
        assert reopened.list_invocations(result.run_id) == (invocation,)
        assert reopened.list_turns(result.run_id) == (completion_turn,)


@pytest.mark.parametrize("version", range(1, 16))
def test_sqlite_repository_rejects_non_v16_databases(
    tmp_path: Path,
    version: int,
) -> None:
    database = tmp_path / f"schema-v{version}.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA user_version = {version}")

    with pytest.raises(RuntimeError, match=f"unsupported database schema version: {version}"):
        SQLiteRunRepository(database)


def test_sqlite_repository_rejects_unversioned_existing_schema(tmp_path: Path) -> None:
    database = tmp_path / "unversioned.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="unversioned existing databases are unsupported"):
        SQLiteRunRepository(database)


def test_sqlite_repository_turn_journal_and_checkpoint_survive_reopen(
    tmp_path: Path,
    first_observation: CompanyObservation,
) -> None:
    database = tmp_path / "recovery-state.sqlite3"
    first = _turn_for("sqlite_run", first_observation, sequence=1)
    second = _turn_for("sqlite_run", first_observation, sequence=2)
    checkpoint = _checkpoint_for(first, first_observation)
    assert checkpoint.schema_version == 12

    with SQLiteRunRepository(database) as repository:
        repository.save_progress((first,), (), checkpoint)
        cursor = checkpoint.cursors[0].model_copy(update={"next_turn_sequence": 3})
        final_checkpoint = checkpoint.model_copy(
            update={"cursors": (cursor, *checkpoint.cursors[1:])}
        )
        repository.save_progress((second,), (), final_checkpoint)

    with SQLiteRunRepository(database) as reopened:
        assert reopened.list_turns(first.run_id) == (first, second)
        assert reopened.load_recovery(first.run_id) == RunRecovery(
            checkpoint=final_checkpoint,
            turns=(first, second),
        )


def test_sqlite_repository_rejects_stale_journal_payload_schema(
    tmp_path: Path,
    first_observation: CompanyObservation,
) -> None:
    database = tmp_path / "stale-journal.sqlite3"
    record = _turn_for("stale_journal", first_observation)
    checkpoint = _checkpoint_for(record, first_observation)
    with SQLiteRunRepository(database) as repository:
        repository.save_progress((record,), (), checkpoint)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE run_turns SET schema_version = 5")

    with (
        SQLiteRunRepository(database) as repository,
        pytest.raises(RuntimeError, match="journal payload schema version: 5"),
    ):
        repository.list_turns(record.run_id)


def test_sqlite_repository_progress_is_atomic_and_checkpoint_aligned(
    tmp_path: Path,
    first_observation: CompanyObservation,
) -> None:
    with SQLiteRunRepository(tmp_path / "progress.sqlite3") as repository:
        _assert_progress_contract(repository, first_observation, "sqlite_progress")


def test_sqlite_repository_orders_invocations_by_turn_application(
    tmp_path: Path,
    first_observation: CompanyObservation,
) -> None:
    with SQLiteRunRepository(tmp_path / "invocations.sqlite3") as repository:
        _assert_invocation_order(repository, first_observation, "sqlite_invocations")


def _assert_job_history(
    repository: RunRepository,
    jobs: tuple[RunJob, ...],
) -> None:
    """Exercise all-state discovery, ordering, and limit semantics."""
    for job in reversed(jobs):
        repository.save_job(job)

    expected = tuple(reversed(jobs))
    assert repository.list_jobs() == expected
    assert repository.list_jobs(2) == expected[:2]
    assert repository.list_jobs(2, 2) == expected[2:4]
    assert repository.list_jobs(0) == ()
    assert repository.list_jobs(2, -1) == ()
    assert {job.status for job in repository.list_jobs()} == set(RunStatus)
    auto_resume_statuses = {
        RunStatus.INTERRUPTED,
        RunStatus.QUEUED,
        RunStatus.RUNNING,
    }
    assert repository.list_auto_resume_jobs() == tuple(
        job for job in jobs if job.status in auto_resume_statuses
    )


def _history_jobs() -> tuple[RunJob, ...]:
    """Build one lifecycle record for every status in submission order."""
    submitted_at = datetime(2026, 1, 1, tzinfo=UTC)
    return tuple(
        RunJob(
            run_id=f"history_{status.value}",
            profile_id=PolicyProfileId.BASELINE,
            kind=PolicyKind.BASELINE,
            status=status,
            seed=sequence,
            scenario_id=DAIRY_S9_SCENARIO.scenario_id,
            total_weeks=DAIRY_S9_SCENARIO.weeks,
            submitted_at=submitted_at + timedelta(minutes=sequence),
            stop_reason=(RunStopReason.USER_REQUESTED if status is RunStatus.STOPPED else None),
            quality=_clean_quality() if status is RunStatus.COMPLETED else None,
        )
        for sequence, status in enumerate(RunStatus)
    )


def _assert_replay_sources(repository: RunRepository) -> tuple[ReplaySource, ...]:
    """Exercise completed-only discovery and submission ordering."""
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    older = RunJob(
        run_id="replay_older",
        profile_id=PolicyProfileId.BASELINE,
        kind=PolicyKind.BASELINE,
        status=RunStatus.COMPLETED,
        seed=1,
        scenario_id=DAIRY_S9_SCENARIO.scenario_id,
        total_weeks=DAIRY_S9_SCENARIO.weeks,
        submitted_at=base_time,
        quality=_clean_quality(),
    )
    newer = older.model_copy(
        update={
            "run_id": "replay_newer",
            "seed": 2,
            "submitted_at": base_time + timedelta(minutes=1),
        }
    )
    failed = newer.model_copy(
        update={
            "run_id": "replay_failed",
            "status": RunStatus.FAILED,
            "quality": None,
            "submitted_at": base_time + timedelta(minutes=2),
        }
    )
    stopped = failed.model_copy(
        update={
            "run_id": "replay_stopped",
            "status": RunStatus.STOPPED,
            "submitted_at": base_time + timedelta(minutes=3),
        }
    )
    for job in (newer, failed, stopped, older):
        repository.save_job(job)

    expected = tuple(
        ReplaySource(
            run_id=job.run_id,
            submitted_at=job.submitted_at,
            benchmark_eligible=True,
        )
        for job in (newer, older)
    )
    assert repository.list_replay_sources() == expected
    return expected


def _assert_checkpoint_contract(
    repository: RunRepository,
    checkpoint: RunCheckpoint,
    first: TurnRecord,
    second: TurnRecord,
) -> None:
    """Exercise ordered append, exact retry, conflict, and checkpoint replacement."""
    repository.save_progress((first,), (), checkpoint)
    assert repository.load_recovery(checkpoint.run_id) == RunRecovery(
        checkpoint=checkpoint,
        turns=(first,),
    )

    cursor = checkpoint.cursors[0].model_copy(update={"next_turn_sequence": 3})
    replacement = checkpoint.model_copy(
        update={
            "cursors": (cursor, *checkpoint.cursors[1:]),
        }
    )
    repository.save_progress((second,), (), replacement)
    repository.save_progress((second,), (), replacement)
    assert repository.load_recovery(checkpoint.run_id) == RunRecovery(
        checkpoint=replacement,
        turns=(first, second),
    )
    conflicting = first.model_copy(update={"observation_hash": "different"})
    with pytest.raises(ValueError, match="turn identity conflict"):
        repository.save_progress((conflicting,), (), replacement)
    assert repository.list_turns("missing") == ()

    invalid_sequence = checkpoint.model_copy(
        update={
            "cursors": (
                checkpoint.cursors[0].model_copy(update={"next_turn_sequence": 99}),
                *checkpoint.cursors[1:],
            )
        }
    )
    with pytest.raises(ValueError, match="follow completed company turns"):
        RunRecovery(checkpoint=invalid_sequence, turns=(first,))

    invalid_visibility = checkpoint.model_dump()
    invalid_visibility["cursors"][0]["last_visible_event_sequence"] = 1
    with pytest.raises(ValueError, match="cannot exceed checkpoint events"):
        RunCheckpoint.model_validate(invalid_visibility)


def _assert_progress_contract(
    repository: RunRepository,
    observation: CompanyObservation,
    run_id: str,
) -> None:
    """Require journal and checkpoint updates to advance as one unit."""
    first = _turn_for(run_id, observation, sequence=1)
    second = _turn_for(run_id, observation, sequence=2)
    third = _turn_for(run_id, observation, sequence=3)
    first_checkpoint = _checkpoint_for(first, observation)
    repository.save_progress((first,), (), first_checkpoint)

    cursor = first_checkpoint.cursors[0].model_copy(update={"next_turn_sequence": 3})
    second_checkpoint = first_checkpoint.model_copy(
        update={
            "cursors": (cursor, *first_checkpoint.cursors[1:]),
        }
    )
    repository.save_progress((second,), (), second_checkpoint)
    repository.save_progress((second,), (), second_checkpoint)
    assert repository.list_turns(run_id) == (first, second)
    assert repository.load_recovery(run_id) == RunRecovery(
        checkpoint=second_checkpoint,
        turns=(first, second),
    )

    with pytest.raises(ValueError, match="follow completed company turns"):
        repository.save_progress((third,), (), second_checkpoint)
    with pytest.raises(ValueError, match="follow completed company turns"):
        repository.save_progress((), (), first_checkpoint)

    assert repository.list_turns(run_id) == (first, second)
    recovery = repository.load_recovery(run_id)
    assert recovery is not None
    assert recovery.checkpoint == second_checkpoint


def _assert_invocation_order(
    repository: RunRepository,
    observation: CompanyObservation,
    run_id: str,
) -> None:
    """Require provider completion order not to affect the audit projection."""
    first = _invocation_for_turn(_turn_for(run_id, observation, sequence=1), observation)
    second = _invocation_for_turn(_turn_for(run_id, observation, sequence=2), observation)

    repository.record_invocation(second)
    repository.record_invocation(first)

    assert repository.list_invocations(run_id) == (first, second)


def _turn_for(
    run_id: str,
    observation: CompanyObservation,
    *,
    sequence: int = 1,
) -> TurnRecord:
    """Build one complete idle decision with deterministic runtime identities."""
    at = observation.sim_day.plus_days(sequence - 1)
    observed_today = observation.model_copy(update={"sim_day": at})
    turn_id = f"{run_id}.turn.{sequence}"
    decision_id = f"{run_id}.decision.{sequence}"
    turn = AgentTurn(
        turn_id=turn_id,
        company_id=observation.company_id,
        sim_day=at,
        state_version=sequence - 1,
        turn_number_this_week=sequence,
        turn_limit_this_week=observation.runtime.max_turns_per_company_week,
        wake_reasons=(WakeReason.WEEK_OPEN if sequence == 1 else WakeReason.REVIEW_DUE,),
        observation=observed_today,
        available_cash=observation.cash,
        marked_surplus=Decimal(),
    )
    envelope = DecisionEnvelope(
        turn_id=turn_id,
        decision_id=decision_id,
        company_id=observation.company_id,
        issued_on=at,
        state_version=sequence - 1,
        decision=IdleDecision(attention=AttentionPlan()),
    )
    outcome = DecisionOutcome(
        turn_id=turn_id,
        decision_id=decision_id,
        company_id=observation.company_id,
        occurred_on=at,
        status=DecisionStatus.ACCEPTED,
        accepted=True,
        resulting_state_version=sequence,
        apply_sequence=sequence,
    )
    return TurnRecord(
        run_id=run_id,
        turn=turn,
        envelope=envelope,
        outcome=outcome,
        observation_hash=f"observation-{sequence}",
    )


def _checkpoint_for(
    record: TurnRecord,
    observation: CompanyObservation,
    *,
    policy_kind: PolicyKind = PolicyKind.MODEL,
) -> RunCheckpoint:
    """Build a complete, versioned recovery payload around one turn."""
    engine = EconomyEngine()
    world = engine.initial_state(DAIRY_S9_SCENARIO, seed=42)
    return RunCheckpoint(
        run_id=record.run_id,
        episode_started_at=datetime(2026, 1, 1, tzinfo=UTC),
        economy=engine.open_week(world),
        scheduler=SchedulerCheckpoint(
            today=record.turn.sim_day,
            next_sequence=1,
        ),
        policies=tuple(
            PolicyDescriptor(
                company_id=company.company_id,
                name="checkpoint-test",
                kind=(
                    policy_kind
                    if company.company_id == observation.company_id
                    else PolicyKind.BASELINE
                ),
                profile_id=(
                    PolicyProfileId.NEWAPI_MODEL
                    if company.company_id == observation.company_id
                    and policy_kind is PolicyKind.MODEL
                    else PolicyProfileId.BASELINE
                ),
                **(
                    {
                        "provider": "scripted",
                        "model": "scripted-current",
                        "wire_protocol": "scripted-tools",
                        "adapter_version": "scripted-v1",
                        "prompt_version": "test-current",
                        "config_fingerprint": "scripted-config",
                    }
                    if company.company_id == observation.company_id
                    and policy_kind is PolicyKind.MODEL
                    else {}
                ),
            )
            for company in DAIRY_S9_SCENARIO.companies
        ),
        agent_states=(
            AgentCheckpoint(
                run_id=record.run_id,
                company_id=observation.company_id,
                revision=0,
                max_tokens=1_024,
                chars_per_token=4,
            ),
        ),
        cursors=tuple(
            CompanyRuntimeCursor(
                company_id=company.company_id,
                next_turn_sequence=(2 if company.company_id == observation.company_id else 1),
            )
            for company in DAIRY_S9_SCENARIO.companies
        ),
    )


def _job_for(result: EpisodeResult) -> RunJob:
    """Build a queued lifecycle record matching an episode identity."""
    return RunJob(
        run_id=result.run_id,
        profile_id=PolicyProfileId.BASELINE,
        kind=PolicyKind.BASELINE,
        seed=result.seed,
        scenario_id=result.scenario.scenario_id,
        total_weeks=result.scenario.weeks,
        submitted_at=result.started_at,
    )


def _completed_job_for(result: EpisodeResult) -> RunJob:
    """Build a completed lifecycle record matching an episode."""
    return _job_for(result).model_copy(
        update={
            "status": RunStatus.COMPLETED,
            "current_absolute_day": result.scenario.calendar.total_days,
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "quality": result.quality,
        }
    )


def _clean_quality() -> EpisodeQuality:
    """Build protocol-clean quality metadata for lifecycle-only tests."""
    return EpisodeQuality(
        benchmark_eligible=True,
        protocol=ProtocolReport(total_turn_count=0, invalid_turn_count=0),
    )


def _invocation_for(
    run_id: str,
    observation: CompanyObservation,
) -> PolicyInvocation:
    """Build one typed Agent audit row for adapter round-trip tests."""
    return _invocation_for_turn(_turn_for(run_id, observation), observation)


def _invocation_for_turn(
    record: TurnRecord,
    observation: CompanyObservation,
) -> PolicyInvocation:
    """Build one completed decision audit tied to its applied turn."""
    now = datetime.now(UTC)
    return PolicyInvocation(
        invocation_id=f"{record.turn.turn_id}.invocation",
        run_id=record.run_id,
        company_id=record.turn.company_id,
        week=record.turn.sim_day.week,
        observation=observation,
        profile_id=PolicyProfileId.NEWAPI_MODEL,
        provider="scripted",
        model="scripted-current",
        wire_protocol="scripted-tools",
        adapter_version="scripted-v1",
        config_fingerprint="scripted-config",
        prompt_version="test-current",
        prompt_hash=f"hash-{record.turn.turn_id}",
        started_at=now,
        finished_at=now,
        outcome=InvocationOutcome.SUCCESS,
        domain_turn_id=record.turn.turn_id,
        absolute_day=record.turn.sim_day.absolute_day,
        state_version=record.turn.state_version,
        apply_sequence=record.outcome.apply_sequence,
        decision=record.envelope.decision,
        decision_outcome=record.outcome,
    )
