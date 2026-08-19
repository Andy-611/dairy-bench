"""SQLite adapter for benchmark runs and Agent audits."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from os import PathLike
from pathlib import Path
from threading import RLock
from typing import Self

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
    validate_completion,
    validate_progress_identity,
)

DATABASE_SCHEMA_VERSION = 16
JOURNAL_PAYLOAD_VERSION = 10


class SQLiteRunRepository:
    """Persist benchmark lifecycle data in a local SQLite database."""

    def __init__(self, database: str | PathLike[str]) -> None:
        database_path = Path(database)
        if str(database) != ":memory:":
            database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(database),
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the owned SQLite connection."""
        with self._lock:
            self._connection.close()

    def get(self, run_id: str) -> EpisodeResult | None:
        """Read the canonical, strongly typed episode representation."""
        with self._lock:
            row = self._connection.execute(
                "SELECT result_json FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return EpisodeResult.model_validate_json(row["result_json"]) if row is not None else None

    def save_job(self, job: RunJob) -> None:
        """Persist or replace a run lifecycle record."""
        with self._lock, self._connection:
            self._upsert_job(job)

    def get_job(self, run_id: str) -> RunJob | None:
        """Return one lifecycle record when present."""
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM run_jobs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return RunJob.model_validate_json(row["payload_json"]) if row is not None else None

    def list_jobs(self, limit: int = 50, offset: int = 0) -> tuple[RunJob, ...]:
        """Return all lifecycle states from newest submission to oldest."""
        if limit <= 0 or offset < 0:
            return ()
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload_json
                FROM run_jobs
                ORDER BY submitted_at DESC, rowid DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return tuple(RunJob.model_validate_json(row["payload_json"]) for row in rows)

    def list_replay_sources(self) -> tuple[ReplaySource, ...]:
        """Return every completed run from newest submission to oldest."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload_json
                FROM run_jobs
                WHERE status = ?
                ORDER BY submitted_at DESC, rowid DESC
                """,
                (RunStatus.COMPLETED.value,),
            ).fetchall()
        jobs = tuple(RunJob.model_validate_json(row["payload_json"]) for row in rows)
        return tuple(
            ReplaySource(
                run_id=job.run_id,
                submitted_at=job.submitted_at,
                benchmark_eligible=job.quality.benchmark_eligible,
            )
            for job in jobs
            if job.quality is not None
        )

    def list_auto_resume_jobs(self) -> tuple[RunJob, ...]:
        """Return automatic startup jobs from oldest to newest."""
        statuses = tuple(status.value for status in AUTO_RESUME_STATUSES)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload_json
                FROM run_jobs
                WHERE status IN (?, ?, ?)
                ORDER BY submitted_at ASC, rowid ASC
                """,
                statuses,
            ).fetchall()
        return tuple(RunJob.model_validate_json(row["payload_json"]) for row in rows)

    def record_invocation(self, invocation: PolicyInvocation) -> None:
        """Upsert one invocation by its deterministic identifier."""
        with self._lock, self._connection:
            self._upsert_invocation(invocation)

    def list_invocations(self, run_id: str) -> tuple[PolicyInvocation, ...]:
        """Return one run's invocations in deterministic decision order."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload_json
                FROM policy_invocations
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchall()
        invocations = (PolicyInvocation.model_validate_json(row["payload_json"]) for row in rows)
        return tuple(sorted(invocations, key=invocation_order))

    def list_turns(self, run_id: str) -> tuple[TurnRecord, ...]:
        """Return one run's turns in deterministic apply order."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT schema_version, payload_json
                FROM run_turns
                WHERE run_id = ?
                ORDER BY absolute_day ASC, apply_sequence ASC, turn_id ASC
                """,
                (run_id,),
            ).fetchall()
        return tuple(TurnRecord.model_validate_json(_current_payload(row)) for row in rows)

    def list_system_steps(self, run_id: str) -> tuple[SystemStepRecord, ...]:
        """Return one run's system steps in chronological journal order."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT schema_version, payload_json
                FROM run_system_steps
                WHERE run_id = ?
                ORDER BY absolute_day ASC, journal_sequence ASC, entry_id ASC
                """,
                (run_id,),
            ).fetchall()
        return tuple(SystemStepRecord.model_validate_json(_current_payload(row)) for row in rows)

    def save_progress(
        self,
        turns: tuple[TurnRecord, ...],
        system_steps: tuple[SystemStepRecord, ...],
        checkpoint: RunCheckpoint,
    ) -> None:
        """Append journal entries and checkpoint in one transaction."""
        validate_progress_identity(turns, system_steps, checkpoint)
        with self._lock, self._connection:
            for record in turns:
                self._insert_turn(record)
            for record in system_steps:
                self._insert_system_step(record)
            self._validate_checkpoint_frontier(checkpoint)
            self._upsert_checkpoint(checkpoint)

    def load_recovery(self, run_id: str) -> RunRecovery | None:
        """Load one checkpoint together with its authoritative journals."""
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM run_checkpoints WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            checkpoint = (
                RunCheckpoint.model_validate_json(row["payload_json"]) if row is not None else None
            )
            if checkpoint is None:
                return None
            return RunRecovery(
                checkpoint=checkpoint,
                turns=self.list_turns(run_id),
                system_steps=self.list_system_steps(run_id),
            )

    def _insert_turn(self, record: TurnRecord) -> None:
        """Insert one immutable journal record inside the active transaction."""
        payload = record.model_dump_json()
        self._connection.execute(
            """
            INSERT INTO run_turns (
                run_id, turn_id, company_id, absolute_day, state_version,
                apply_sequence, schema_version, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, turn_id) DO NOTHING
            """,
            (
                record.run_id,
                record.turn.turn_id,
                record.turn.company_id,
                record.turn.sim_day.absolute_day,
                record.turn.state_version,
                record.outcome.apply_sequence,
                JOURNAL_PAYLOAD_VERSION,
                payload,
            ),
        )
        row = self._connection.execute(
            """
            SELECT schema_version, payload_json
            FROM run_turns
            WHERE run_id = ? AND turn_id = ?
            """,
            (record.run_id, record.turn.turn_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("turn insert did not produce a journal row")
        require_same_turn(
            TurnRecord.model_validate_json(_current_payload(row)),
            record,
        )

    def _validate_checkpoint_frontier(self, checkpoint: RunCheckpoint) -> None:
        """Validate cursor counts without deserializing the full journal."""
        rows = self._connection.execute(
            """
            SELECT company_id, COUNT(*) AS turn_count
            FROM run_turns
            WHERE run_id = ?
            GROUP BY company_id
            """,
            (checkpoint.run_id,),
        ).fetchall()
        counts = {row["company_id"]: row["turn_count"] for row in rows}
        if any(
            cursor.next_turn_sequence != counts.get(cursor.company_id, 0) + 1
            for cursor in checkpoint.cursors
        ):
            raise ValueError("runtime cursor must follow completed company turns")

    def _insert_system_step(self, record: SystemStepRecord) -> None:
        """Insert one immutable system journal row inside a transaction."""
        self._connection.execute(
            """
            INSERT INTO run_system_steps (
                run_id, entry_id, absolute_day, journal_sequence, kind,
                schema_version, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, entry_id) DO NOTHING
            """,
            (
                record.run_id,
                record.entry_id,
                record.occurred_on.absolute_day,
                record.journal_sequence,
                record.kind.value,
                JOURNAL_PAYLOAD_VERSION,
                record.model_dump_json(),
            ),
        )
        row = self._connection.execute(
            """
            SELECT schema_version, payload_json
            FROM run_system_steps
            WHERE run_id = ? AND entry_id = ?
            """,
            (record.run_id, record.entry_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("system step insert did not produce a journal row")
        require_same_system_step(
            SystemStepRecord.model_validate_json(_current_payload(row)),
            record,
        )

    def _upsert_checkpoint(self, checkpoint: RunCheckpoint) -> None:
        """Replace one checkpoint inside the active transaction."""
        self._connection.execute(
            """
            INSERT INTO run_checkpoints (
                run_id, current_week, updated_at, payload_json
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                current_week = excluded.current_week,
                updated_at = excluded.updated_at,
                payload_json = excluded.payload_json
            """,
            (
                checkpoint.run_id,
                checkpoint.economy.week,
                datetime.now(UTC).isoformat(),
                checkpoint.model_dump_json(),
            ),
        )

    def complete_job(
        self,
        result: EpisodeResult,
        completed_job: RunJob,
    ) -> None:
        """Atomically save the complete result and lifecycle record."""
        validate_completion(result, completed_job)
        with self._lock, self._connection:
            self._upsert_run(result)
            self._upsert_job(completed_job)
            self._connection.execute(
                "DELETE FROM run_checkpoints WHERE run_id = ?",
                (result.run_id,),
            )

    def _create_schema(self) -> None:
        """Create the current V9 schema for a fresh benchmark database."""
        current_version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version not in (0, DATABASE_SCHEMA_VERSION):
            raise RuntimeError(f"unsupported database schema version: {current_version}")
        existing_table = self._connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            LIMIT 1
            """
        ).fetchone()
        if current_version == 0 and existing_table is not None:
            raise RuntimeError("unversioned existing databases are unsupported")
        schema = """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            result_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS run_jobs (
            run_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            seed INTEGER NOT NULL,
            source_run_id TEXT,
            scenario_id TEXT NOT NULL,
            current_absolute_day INTEGER NOT NULL,
            total_weeks INTEGER NOT NULL,
            submitted_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            error_message TEXT,
            payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS policy_invocations (
            invocation_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            company_id TEXT NOT NULL,
            week INTEGER NOT NULL,
            outcome TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS run_checkpoints (
            run_id TEXT PRIMARY KEY,
            current_week INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS run_turns (
            run_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            company_id TEXT NOT NULL,
            absolute_day INTEGER NOT NULL,
            state_version INTEGER NOT NULL,
            apply_sequence INTEGER NOT NULL,
            schema_version INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (run_id, turn_id)
        );

        CREATE TABLE IF NOT EXISTS run_system_steps (
            run_id TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            absolute_day INTEGER NOT NULL,
            journal_sequence INTEGER NOT NULL,
            kind TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (run_id, entry_id)
        );

        CREATE INDEX IF NOT EXISTS ix_run_jobs_status_submitted
            ON run_jobs(status, submitted_at);
        CREATE INDEX IF NOT EXISTS ix_invocations_run_week_company
            ON policy_invocations(run_id, week, company_id);
        CREATE INDEX IF NOT EXISTS ix_run_turns_order
            ON run_turns(run_id, absolute_day, apply_sequence, turn_id);
        CREATE INDEX IF NOT EXISTS ix_run_system_steps_order
            ON run_system_steps(run_id, absolute_day, journal_sequence, entry_id);
        """
        with self._lock, self._connection:
            self._connection.executescript(schema)
            self._connection.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")

    def _upsert_job(self, job: RunJob) -> None:
        """Write the canonical typed lifecycle payload and query columns."""
        self._connection.execute(
            """
            INSERT INTO run_jobs (
                run_id, kind, status, seed, source_run_id, scenario_id,
                current_absolute_day, total_weeks, submitted_at, started_at, finished_at,
                error_message, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                kind = excluded.kind,
                status = excluded.status,
                seed = excluded.seed,
                source_run_id = excluded.source_run_id,
                scenario_id = excluded.scenario_id,
                current_absolute_day = excluded.current_absolute_day,
                total_weeks = excluded.total_weeks,
                submitted_at = excluded.submitted_at,
                started_at = excluded.started_at,
                finished_at = excluded.finished_at,
                error_message = excluded.error_message,
                payload_json = excluded.payload_json
            """,
            (
                job.run_id,
                job.kind.value,
                job.status.value,
                job.seed,
                job.source_run_id,
                job.scenario_id,
                job.current_absolute_day,
                job.total_weeks,
                job.submitted_at.isoformat(),
                _isoformat(job.started_at),
                _isoformat(job.finished_at),
                job.error_message,
                job.model_dump_json(),
            ),
        )

    def _upsert_invocation(self, invocation: PolicyInvocation) -> None:
        """Write one idempotent Agent audit record."""
        self._connection.execute(
            """
            INSERT INTO policy_invocations (
                invocation_id, run_id, company_id, week, outcome, provider,
                model, prompt_version, started_at, finished_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(invocation_id) DO UPDATE SET
                run_id = excluded.run_id,
                company_id = excluded.company_id,
                week = excluded.week,
                outcome = excluded.outcome,
                provider = excluded.provider,
                model = excluded.model,
                prompt_version = excluded.prompt_version,
                started_at = excluded.started_at,
                finished_at = excluded.finished_at,
                payload_json = excluded.payload_json
            """,
            (
                invocation.invocation_id,
                invocation.run_id,
                invocation.company_id,
                invocation.week,
                invocation.outcome.value,
                invocation.provider,
                invocation.model,
                invocation.prompt_version,
                invocation.started_at.isoformat(),
                invocation.finished_at.isoformat(),
                invocation.model_dump_json(),
            ),
        )

    def _upsert_run(self, result: EpisodeResult) -> None:
        """Write the canonical completed episode without duplicate projections."""
        self._connection.execute(
            """
            INSERT INTO runs (run_id, result_json)
            VALUES (?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                result_json = excluded.result_json
            """,
            (
                result.run_id,
                result.model_dump_json(),
            ),
        )


def _current_payload(row: sqlite3.Row) -> str:
    """Return a journal payload only when it uses the active contract."""
    version = int(row["schema_version"])
    if version != JOURNAL_PAYLOAD_VERSION:
        raise RuntimeError(f"unsupported journal payload schema version: {version}")
    return str(row["payload_json"])


def _isoformat(value: datetime | None) -> str | None:
    """Serialize an optional datetime accepted by typed lifecycle models."""
    return value.isoformat() if value is not None else None
