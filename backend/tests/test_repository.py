import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from company_bench.application import RunService
from company_bench.models import (
    CompanyObservation,
    EpisodeResult,
    NoOpDecision,
    PolicyKind,
)
from company_bench.repository import MemoryRunRepository, SQLiteRunRepository
from company_bench.run_models import (
    InvocationOutcome,
    PolicyInvocation,
    RunJob,
    RunStatus,
)


def run_episode(seed: int = 42) -> EpisodeResult:
    """Create one real episode through the public application service."""
    return asyncio.run(RunService(MemoryRunRepository()).run(seed))


def test_memory_repository_round_trip_and_summary() -> None:
    repository = MemoryRunRepository()
    result = run_episode()

    repository.save(result)

    assert repository.get(result.run_id) == result
    assert repository.get("missing") is None
    assert repository.list()[0].run_id == result.run_id
    assert repository.list(0) == ()


def test_sqlite_repository_persists_complete_episode_and_projections(
    tmp_path: Path,
    first_observation: CompanyObservation,
) -> None:
    database = tmp_path / "runs.sqlite3"
    result = run_episode()
    queued_job = _job_for(result)
    invocation = _invocation_for(result.run_id, first_observation)

    with SQLiteRunRepository(database) as repository:
        repository.save_job(queued_job)
        repository.record_invocation(invocation)
        repository.record_invocation(invocation)
        assert repository.get_job(result.run_id) == queued_job
        assert repository.list_resumable_jobs() == (queued_job,)
        assert repository.list_invocations(result.run_id) == (invocation,)

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
        assert repository.get("missing") is None
        summaries = repository.list()
        assert len(summaries) == 1
        assert summaries[0].run_id == result.run_id

    expected_rows = {
        "runs": 1,
        "run_company_results": len(result.score.companies),
        "run_daily_snapshots": sum(
            len(snapshot.companies) for snapshot in result.snapshots
        ),
        "run_decisions": len(result.decisions),
        "run_events": len(result.events),
        "run_jobs": 1,
        "policy_invocations": 1,
        "run_checkpoints": 0,
    }
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        for table, expected in expected_rows.items():
            actual = connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            assert actual == expected

    with SQLiteRunRepository(database) as reopened:
        assert reopened.get(result.run_id) == result
        assert reopened.get_job(result.run_id) == completed_job
        assert reopened.list_invocations(result.run_id) == (invocation,)


def test_sqlite_v1_database_upgrades_without_losing_completed_run(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.sqlite3"
    result = run_episode(seed=7)
    _create_v1_database(database, result)

    with SQLiteRunRepository(database) as repository:
        assert repository.get(result.run_id) == result

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "runs",
            "run_jobs",
            "policy_invocations",
            "run_checkpoints",
        }.issubset(tables)
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


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


def _create_v1_database(
    database: Path,
    result: EpisodeResult,
) -> None:
    """Create a minimal real v1 canonical table containing legacy JSON."""
    legacy_payload = result.model_dump(mode="json")
    for policy in legacy_payload["policies"]:
        for field in (
            "kind",
            "provider",
            "model",
            "prompt_version",
            "config_fingerprint",
            "source_run_id",
        ):
            policy.pop(field)

    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL,
                scenario_version INTEGER NOT NULL,
                scenario_json TEXT NOT NULL,
                seed INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                efficiency TEXT NOT NULL,
                fairness TEXT NOT NULL,
                gini TEXT NOT NULL,
                eligible INTEGER NOT NULL CHECK (eligible IN (0, 1)),
                consumer_fill_rate TEXT NOT NULL,
                expired_quantity TEXT NOT NULL,
                result_json TEXT NOT NULL
            );
            PRAGMA user_version = 1;
            """
        )
        connection.execute(
            """
            INSERT INTO runs (
                run_id, scenario_id, scenario_version, scenario_json, seed,
                started_at, finished_at, efficiency, fairness, gini, eligible,
                consumer_fill_rate, expired_quantity, result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.run_id,
                result.scenario.scenario_id,
                result.scenario.version,
                result.scenario.model_dump_json(),
                result.seed,
                result.started_at.isoformat(),
                result.finished_at.isoformat(),
                str(result.score.efficiency),
                str(result.score.fairness),
                str(result.score.gini),
                int(result.score.eligible),
                str(result.score.consumer_fill_rate),
                str(result.score.expired_quantity),
                json.dumps(legacy_payload),
            ),
        )
