import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from company_bench.agents import BaselineCompanyAgent
from company_bench.dairy_scenario import DAIRY_S9_SCENARIO
from company_bench.engine import EconomyEngine
from company_bench.memory import AgentCheckpoint
from company_bench.models import (
    CompanyObservation,
    EpisodeResult,
    NoOpDecision,
    PolicyDescriptor,
    PolicyKind,
)
from company_bench.repository import (
    LifecycleRepository,
    MemoryRunRepository,
    SQLiteRunRepository,
)
from company_bench.run_models import (
    CompanyRuntimeCursor,
    InvocationOutcome,
    PolicyInvocation,
    ReplaySource,
    RunCheckpoint,
    RunJob,
    RunStatus,
)
from company_bench.runtime import EpisodeRuntime
from company_bench.runtime_models import (
    AgentTurn,
    CommandEnvelope,
    CommandOutcome,
    CommandStatus,
    SimTime,
    TurnRecord,
    Wait,
    WakeReason,
)
from company_bench.scheduler import SchedulerCheckpoint


def run_episode(seed: int = 42) -> EpisodeResult:
    """Create one real V4 episode through the public runtime interface."""
    agents = {
        company.company_id: BaselineCompanyAgent() for company in DAIRY_S9_SCENARIO.companies
    }
    execution = asyncio.run(
        EpisodeRuntime(DAIRY_S9_SCENARIO).run(
            agents,
            seed,
            run_id=f"repository_{seed}",
        )
    )
    return execution.episode


def test_memory_repository_round_trip_and_summary() -> None:
    repository = MemoryRunRepository()
    result = run_episode()

    repository.save(result)

    assert result.score.efficiency_reference == Decimal("8316.0938")
    assert repository.get(result.run_id) == result
    assert repository.get("missing") is None
    assert repository.list()[0].run_id == result.run_id
    assert repository.list(0) == ()


def test_memory_repository_lists_every_job_status_newest_first() -> None:
    repository = MemoryRunRepository()
    jobs = _history_jobs()

    _assert_job_history(repository, jobs)


def test_memory_repository_lists_completed_replay_sources() -> None:
    repository = MemoryRunRepository()

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
    repository = MemoryRunRepository()
    first = _turn_for("memory_run", first_observation, sequence=1)
    second = _turn_for("memory_run", first_observation, sequence=2)
    checkpoint = _checkpoint_for(first, first_observation)

    _assert_turn_contract(repository, first, second)
    _assert_checkpoint_contract(repository, checkpoint, first, second)


def test_checkpoint_accepts_claude_as_a_memory_owning_policy(
    first_observation: CompanyObservation,
) -> None:
    repository = MemoryRunRepository()
    turn = _turn_for("claude_checkpoint", first_observation, sequence=1)
    checkpoint = _checkpoint_for(
        turn,
        first_observation,
        policy_kind=PolicyKind.CLAUDE,
    )

    repository.save_checkpoint(checkpoint)

    assert repository.get_checkpoint(turn.run_id) == checkpoint
    policy = next(
        candidate
        for candidate in checkpoint.policies
        if candidate.company_id == first_observation.company_id
    )
    assert policy.kind is PolicyKind.CLAUDE
    assert checkpoint.agent_states[0].company_id == first_observation.company_id


def test_memory_repository_progress_is_atomic_and_checkpoint_aligned(
    first_observation: CompanyObservation,
) -> None:
    repository = MemoryRunRepository()

    _assert_progress_contract(repository, first_observation, "memory_progress")


def test_memory_repository_orders_v2_invocations_by_turn_application(
    first_observation: CompanyObservation,
) -> None:
    repository = MemoryRunRepository()

    _assert_invocation_order(repository, first_observation, "memory_invocations")


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
        assert repository.list_resumable_jobs() == (queued_job,)
        assert repository.list_invocations(result.run_id) == (invocation,)
        assert repository.get_checkpoint(result.run_id) is not None

        completed_job = queued_job.model_copy(
            update={
                "status": RunStatus.COMPLETED,
                "current_day": 30,
                "started_at": result.started_at,
                "finished_at": result.finished_at,
            }
        )
        repository.complete_job(result, completed_job)
        assert repository.get(result.run_id) == result
        assert repository.get_job(result.run_id) == completed_job
        assert repository.list_resumable_jobs() == ()
        assert repository.get_checkpoint(result.run_id) is None
        assert repository.list_turns(result.run_id) == (completion_turn,)
        assert repository.get("missing") is None
        summaries = repository.list()
        assert len(summaries) == 1
        assert summaries[0].run_id == result.run_id
        assert summaries[0].final_score == result.score.final_score

    expected_rows = {
        "runs": 1,
        "run_company_results": len(result.score.companies),
        "run_daily_snapshots": sum(len(snapshot.companies) for snapshot in result.snapshots),
        "run_decisions": len(result.decisions),
        "run_events": len(result.events),
        "run_jobs": 1,
        "policy_invocations": 1,
        "run_checkpoints": 0,
        "run_turns": 1,
        "run_system_steps": 0,
    }
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert connection.execute(
            "SELECT schema_version FROM run_turns"
        ).fetchone()[0] == 5
        assert tuple(row[1] for row in connection.execute("PRAGMA table_info(runs)")) == (
            "run_id",
            "scenario_id",
            "scenario_version",
            "scenario_json",
            "seed",
            "started_at",
            "finished_at",
            "score_version",
            "final_score",
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


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5, 6, 7, 8, 10])
def test_sqlite_repository_rejects_non_v9_databases(
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
    database = tmp_path / "v2-state.sqlite3"
    first = _turn_for("sqlite_run", first_observation, sequence=1)
    second = _turn_for("sqlite_run", first_observation, sequence=2)
    checkpoint = _checkpoint_for(first, first_observation)
    assert checkpoint.schema_version == 7

    with SQLiteRunRepository(database) as repository:
        _assert_turn_contract(repository, first, second)
        repository.save_checkpoint(checkpoint)
        assert repository.get_checkpoint(first.run_id) == checkpoint

    with SQLiteRunRepository(database) as reopened:
        assert reopened.list_turns(first.run_id) == (first, second)
        assert reopened.get_checkpoint(first.run_id) == checkpoint
        reopened.clear_checkpoint(first.run_id)
        reopened.clear_checkpoint(first.run_id)
        assert reopened.get_checkpoint(first.run_id) is None


def test_sqlite_repository_rejects_stale_journal_payload_schema(
    tmp_path: Path,
    first_observation: CompanyObservation,
) -> None:
    database = tmp_path / "stale-journal.sqlite3"
    record = _turn_for("stale_journal", first_observation)
    with SQLiteRunRepository(database) as repository:
        repository.record_turn(record)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE run_turns SET schema_version = 4")

    with (
        SQLiteRunRepository(database) as repository,
        pytest.raises(RuntimeError, match="journal payload schema version: 4"),
    ):
        repository.list_turns(record.run_id)


def test_sqlite_repository_progress_is_atomic_and_checkpoint_aligned(
    tmp_path: Path,
    first_observation: CompanyObservation,
) -> None:
    with SQLiteRunRepository(tmp_path / "progress.sqlite3") as repository:
        _assert_progress_contract(repository, first_observation, "sqlite_progress")


def test_sqlite_repository_orders_v2_invocations_by_turn_application(
    tmp_path: Path,
    first_observation: CompanyObservation,
) -> None:
    with SQLiteRunRepository(tmp_path / "invocations.sqlite3") as repository:
        _assert_invocation_order(repository, first_observation, "sqlite_invocations")


def _assert_turn_contract(
    repository: LifecycleRepository,
    first: TurnRecord,
    second: TurnRecord,
) -> None:
    """Exercise ordered append, exact retry, and identity conflict semantics."""
    repository.record_turn(second)
    repository.record_turn(first)
    repository.record_turn(first)

    assert repository.list_turns(first.run_id) == (first, second)
    conflicting = first.model_copy(update={"observation_hash": "different"})
    with pytest.raises(ValueError, match="turn identity conflict"):
        repository.record_turn(conflicting)
    assert repository.list_turns("missing") == ()


def _assert_job_history(
    repository: LifecycleRepository,
    jobs: tuple[RunJob, ...],
) -> None:
    """Exercise all-state discovery, ordering, and limit semantics."""
    for job in reversed(jobs):
        repository.save_job(job)

    expected = tuple(reversed(jobs))
    assert repository.list_jobs() == expected
    assert repository.list_jobs(2) == expected[:2]
    assert repository.list_jobs(0) == ()
    assert {job.status for job in repository.list_jobs()} == set(RunStatus)
    assert repository.list_resumable_jobs() == tuple(job for job in jobs if not job.status.terminal)


def _history_jobs() -> tuple[RunJob, ...]:
    """Build one lifecycle record for every status in submission order."""
    submitted_at = datetime(2026, 1, 1, tzinfo=UTC)
    return tuple(
        RunJob(
            run_id=f"history_{status.value}",
            mode=PolicyKind.BASELINE,
            status=status,
            seed=sequence,
            scenario_id=DAIRY_S9_SCENARIO.scenario_id,
            total_days=DAIRY_S9_SCENARIO.days,
            submitted_at=submitted_at + timedelta(minutes=sequence),
        )
        for sequence, status in enumerate(RunStatus)
    )


def _assert_replay_sources(repository: LifecycleRepository) -> tuple[ReplaySource, ...]:
    """Exercise completed-only discovery and submission ordering."""
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    older = RunJob(
        run_id="replay_older",
        mode=PolicyKind.BASELINE,
        status=RunStatus.COMPLETED,
        seed=1,
        scenario_id=DAIRY_S9_SCENARIO.scenario_id,
        total_days=DAIRY_S9_SCENARIO.days,
        submitted_at=base_time,
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
        ReplaySource(run_id=job.run_id, submitted_at=job.submitted_at) for job in (newer, older)
    )
    assert repository.list_replay_sources() == expected
    return expected


def _assert_checkpoint_contract(
    repository: LifecycleRepository,
    checkpoint: RunCheckpoint,
    first: TurnRecord,
    second: TurnRecord,
) -> None:
    """Exercise atomic replacement and idempotent checkpoint removal."""
    repository.save_checkpoint(checkpoint)
    assert repository.get_checkpoint(checkpoint.run_id) == checkpoint

    cursor = checkpoint.cursors[0].model_copy(update={"next_turn_sequence": 3})
    replacement = checkpoint.model_copy(
        update={
            "turns": (first, second),
            "cursors": (cursor, *checkpoint.cursors[1:]),
        }
    )
    repository.save_checkpoint(replacement)
    assert repository.get_checkpoint(checkpoint.run_id) == replacement

    repository.clear_checkpoint(checkpoint.run_id)
    repository.clear_checkpoint(checkpoint.run_id)
    assert repository.get_checkpoint(checkpoint.run_id) is None

    invalid_sequence = checkpoint.model_dump()
    invalid_sequence["cursors"][0]["next_turn_sequence"] = 99
    with pytest.raises(ValueError, match="follow completed company turns"):
        RunCheckpoint.model_validate(invalid_sequence)

    invalid_visibility = checkpoint.model_dump()
    invalid_visibility["cursors"][0]["last_visible_event_sequence"] = 1
    with pytest.raises(ValueError, match="cannot exceed checkpoint events"):
        RunCheckpoint.model_validate(invalid_visibility)


def _assert_progress_contract(
    repository: LifecycleRepository,
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
            "turns": (first, second),
            "cursors": (cursor, *first_checkpoint.cursors[1:]),
        }
    )
    repository.save_progress((second,), (), second_checkpoint)
    repository.save_progress((second,), (), second_checkpoint)
    assert repository.list_turns(run_id) == (first, second)
    assert repository.get_checkpoint(run_id) == second_checkpoint

    with pytest.raises(ValueError, match="progress turns must match checkpoint turns"):
        repository.save_progress((third,), (), second_checkpoint)
    with pytest.raises(ValueError, match="checkpoint turns must match the durable journal"):
        repository.save_progress((), (), first_checkpoint)

    assert repository.list_turns(run_id) == (first, second)
    assert repository.get_checkpoint(run_id) == second_checkpoint


def _assert_invocation_order(
    repository: LifecycleRepository,
    observation: CompanyObservation,
    run_id: str,
) -> None:
    """Require provider completion order not to affect the audit projection."""
    first = _v2_invocation_for(_turn_for(run_id, observation, sequence=1), observation)
    second = _v2_invocation_for(_turn_for(run_id, observation, sequence=2), observation)

    repository.record_invocation(second)
    repository.record_invocation(first)

    assert repository.list_invocations(run_id) == (first, second)


def _turn_for(
    run_id: str,
    observation: CompanyObservation,
    *,
    sequence: int = 1,
) -> TurnRecord:
    """Build one complete wait turn with deterministic runtime identities."""
    at = SimTime.at(day=0, hour=9, minute=(sequence - 1) * 10)
    turn_id = f"{run_id}.turn.{sequence}"
    command_id = f"{run_id}.command.{sequence}"
    turn = AgentTurn(
        turn_id=turn_id,
        company_id=observation.company_id,
        sim_time=at,
        state_version=sequence - 1,
        turn_number_today=sequence,
        turn_limit_today=observation.runtime.max_turns_per_company_day,
        wake_reasons=(WakeReason.DAY_OPEN if sequence == 1 else WakeReason.CONTINUE,),
        observation=observation,
        available_cash=observation.cash,
        marked_surplus=Decimal(),
    )
    envelope = CommandEnvelope(
        turn_id=turn_id,
        command_id=command_id,
        company_id=observation.company_id,
        issued_at=at,
        state_version=sequence - 1,
        command=Wait(until=at.plus(10)),
    )
    outcome = CommandOutcome(
        turn_id=turn_id,
        command_id=command_id,
        company_id=observation.company_id,
        occurred_at=at,
        status=CommandStatus.ACCEPTED,
        accepted=True,
        resulting_state_version=sequence,
        apply_sequence=sequence,
        next_available_at=at.plus(10),
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
    policy_kind: PolicyKind = PolicyKind.OPENAI,
) -> RunCheckpoint:
    """Build a complete, versioned recovery payload around one turn."""
    engine = EconomyEngine()
    world = engine.initial_state(DAIRY_S9_SCENARIO, seed=42)
    return RunCheckpoint(
        run_id=record.run_id,
        episode_started_at=datetime(2026, 1, 1, tzinfo=UTC),
        economy=engine.open_day(world),
        scheduler=SchedulerCheckpoint(
            now=record.turn.sim_time,
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
        turns=(record,),
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
        mode=PolicyKind.BASELINE,
        seed=result.seed,
        scenario_id=result.scenario.scenario_id,
        total_days=result.scenario.days,
        submitted_at=result.started_at,
    )


def _invocation_for(
    run_id: str,
    observation: CompanyObservation,
) -> PolicyInvocation:
    """Build one typed Agent audit row for adapter round-trip tests."""
    now = datetime.now(UTC)
    return PolicyInvocation(
        invocation_id=f"{run_id}.1.{observation.company_id}",
        run_id=run_id,
        company_id=observation.company_id,
        day=1,
        observation=observation,
        provider="scripted",
        model="scripted-v1",
        prompt_version="test-v1",
        prompt_hash="test-hash",
        started_at=now,
        finished_at=now,
        outcome=InvocationOutcome.SUCCESS,
        decision=NoOpDecision(reason="test"),
    )


def _v2_invocation_for(
    record: TurnRecord,
    observation: CompanyObservation,
) -> PolicyInvocation:
    """Build one completed V2 command audit tied to its applied turn."""
    now = datetime.now(UTC)
    return PolicyInvocation(
        invocation_id=f"{record.turn.turn_id}.invocation",
        run_id=record.run_id,
        company_id=record.turn.company_id,
        day=observation.day,
        observation=observation,
        provider="scripted",
        model="scripted-v2",
        prompt_version="test-v2",
        prompt_hash=f"hash-{record.turn.turn_id}",
        started_at=now,
        finished_at=now,
        outcome=InvocationOutcome.SUCCESS,
        domain_turn_id=record.turn.turn_id,
        sim_minute=record.turn.sim_time.absolute_minute,
        state_version=record.turn.state_version,
        apply_sequence=record.outcome.apply_sequence,
        command=record.envelope.command,
        command_outcome=record.outcome,
    )
