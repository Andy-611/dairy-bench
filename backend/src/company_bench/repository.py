"""Persistence ports and adapters for benchmark runs and Agent audits."""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from collections.abc import Iterable
from datetime import UTC, datetime
from os import PathLike
from pathlib import Path
from threading import RLock
from typing import Protocol, Self

from company_bench.models import EpisodeResult, EventRecord, RunSummary, TradeExecutedEvent
from company_bench.run_models import (
    PolicyAuditSink,
    PolicyInvocation,
    ReplaySource,
    RunCheckpoint,
    RunJob,
    RunStatus,
)
from company_bench.runtime_models import SystemStepRecord, TurnRecord

_DATABASE_SCHEMA_VERSION = 9
_PAYLOAD_SCHEMA_VERSION = 5
_RESUMABLE_STATUSES = (
    RunStatus.QUEUED,
    RunStatus.RUNNING,
    RunStatus.INTERRUPTED,
)


class RunRepository(Protocol):
    """Store and retrieve complete benchmark episodes."""

    def save(self, result: EpisodeResult) -> None:
        """Persist one complete episode."""

    def get(self, run_id: str) -> EpisodeResult | None:
        """Return an episode, or ``None`` when it does not exist."""

    def list(self, limit: int = 50) -> tuple[RunSummary, ...]:
        """Return the most recently completed episodes first."""

    def list_replay_sources(self) -> tuple[ReplaySource, ...]:
        """Return every completed run from newest submission to oldest."""


class LifecycleRepository(RunRepository, PolicyAuditSink, Protocol):
    """Persist complete episodes, asynchronous jobs, and Agent audits."""

    def save_job(self, job: RunJob) -> None:
        """Persist or replace a run lifecycle record."""

    def get_job(self, run_id: str) -> RunJob | None:
        """Return one lifecycle record when present."""

    def list_jobs(self, limit: int = 50) -> tuple[RunJob, ...]:
        """Return the most recently submitted lifecycle records first."""

    def list_resumable_jobs(self) -> tuple[RunJob, ...]:
        """Return jobs that may safely be scheduled after startup."""

    def list_invocations(self, run_id: str) -> tuple[PolicyInvocation, ...]:
        """Return one run's Agent invocations in decision order."""

    def record_turn(self, record: TurnRecord) -> None:
        """Append one immutable, idempotent turn journal entry."""

    def list_turns(self, run_id: str) -> tuple[TurnRecord, ...]:
        """Return one run's complete turn journal in apply order."""

    def record_system_step(self, record: SystemStepRecord) -> None:
        """Append one immutable, idempotent system journal entry."""

    def list_system_steps(self, run_id: str) -> tuple[SystemStepRecord, ...]:
        """Return one run's system journal in chronological order."""

    def save_checkpoint(self, checkpoint: RunCheckpoint) -> None:
        """Atomically replace one run's complete recovery state."""

    def save_progress(
        self,
        turns: tuple[TurnRecord, ...],
        system_steps: tuple[SystemStepRecord, ...],
        checkpoint: RunCheckpoint,
    ) -> None:
        """Atomically append journal entries and replace their checkpoint."""

    def get_checkpoint(self, run_id: str) -> RunCheckpoint | None:
        """Return one run's latest complete recovery state."""

    def clear_checkpoint(self, run_id: str) -> None:
        """Remove one run's recovery state while retaining its journal."""

    def complete_job(
        self,
        result: EpisodeResult,
        completed_job: RunJob,
    ) -> None:
        """Atomically persist a result and its completed lifecycle record."""


class MemoryRunRepository:
    """Keep benchmark lifecycle data in process memory."""

    def __init__(self) -> None:
        self._results: OrderedDict[str, EpisodeResult] = OrderedDict()
        self._jobs: OrderedDict[str, RunJob] = OrderedDict()
        self._invocations: dict[str, PolicyInvocation] = {}
        self._turns: dict[tuple[str, str], TurnRecord] = {}
        self._system_steps: dict[tuple[str, str], SystemStepRecord] = {}
        self._checkpoints: dict[str, RunCheckpoint] = {}
        self._lock = RLock()

    def save(self, result: EpisodeResult) -> None:
        """Persist one complete episode."""
        with self._lock:
            self._save_result(result)

    def get(self, run_id: str) -> EpisodeResult | None:
        """Return an independent copy of an episode."""
        with self._lock:
            return self._results.get(run_id)

    def list(self, limit: int = 50) -> tuple[RunSummary, ...]:
        """Return the most recently saved episodes first."""
        if limit <= 0:
            return ()
        with self._lock:
            results = tuple(reversed(self._results.values()))[:limit]
            return tuple(_to_summary(result) for result in results)

    def save_job(self, job: RunJob) -> None:
        """Persist or replace a run lifecycle record."""
        with self._lock:
            self._save_job(job)

    def get_job(self, run_id: str) -> RunJob | None:
        """Return one lifecycle record when present."""
        with self._lock:
            return self._jobs.get(run_id)

    def list_jobs(self, limit: int = 50) -> tuple[RunJob, ...]:
        """Return all lifecycle states from newest submission to oldest."""
        if limit <= 0:
            return ()
        with self._lock:
            newest_first = reversed(self._jobs.values())
            jobs = sorted(
                newest_first,
                key=lambda job: job.submitted_at,
                reverse=True,
            )
            return tuple(jobs[:limit])

    def list_replay_sources(self) -> tuple[ReplaySource, ...]:
        """Return every completed run from newest submission to oldest."""
        with self._lock:
            sources = (
                ReplaySource(run_id=job.run_id, submitted_at=job.submitted_at)
                for job in self._jobs.values()
                if job.status == RunStatus.COMPLETED
            )
            return tuple(
                sorted(
                    sources,
                    key=lambda source: source.submitted_at,
                    reverse=True,
                )
            )

    def list_resumable_jobs(self) -> tuple[RunJob, ...]:
        """Return resumable jobs from oldest to newest."""
        with self._lock:
            jobs = (job for job in self._jobs.values() if job.status in _RESUMABLE_STATUSES)
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
            return tuple(sorted(invocations, key=_invocation_order))

    def record_turn(self, record: TurnRecord) -> None:
        """Append one immutable turn, accepting exact retries only."""
        with self._lock:
            self._record_turn(record)

    def list_turns(self, run_id: str) -> tuple[TurnRecord, ...]:
        """Return one run's turns in deterministic apply order."""
        with self._lock:
            records = (
                record
                for (record_run_id, _), record in self._turns.items()
                if record_run_id == run_id
            )
            return tuple(sorted(records, key=_turn_order))

    def record_system_step(self, record: SystemStepRecord) -> None:
        """Append one immutable system step, accepting exact retries only."""
        with self._lock:
            self._record_system_step(record)

    def list_system_steps(self, run_id: str) -> tuple[SystemStepRecord, ...]:
        """Return one run's system steps in chronological journal order."""
        with self._lock:
            records = (
                record
                for (record_run_id, _), record in self._system_steps.items()
                if record_run_id == run_id
            )
            return tuple(sorted(records, key=_system_step_order))

    def save_checkpoint(self, checkpoint: RunCheckpoint) -> None:
        """Atomically replace one run's complete recovery state."""
        with self._lock:
            self._checkpoints[checkpoint.run_id] = checkpoint

    def save_progress(
        self,
        turns: tuple[TurnRecord, ...],
        system_steps: tuple[SystemStepRecord, ...],
        checkpoint: RunCheckpoint,
    ) -> None:
        """Append journal entries and checkpoint under one process lock."""
        _validate_progress_identity(turns, system_steps, checkpoint)
        with self._lock:
            staged_turns: dict[tuple[str, str], TurnRecord] = {}
            for record in turns:
                key = (record.run_id, record.turn.turn_id)
                existing = staged_turns.get(key, self._turns.get(key))
                if existing is not None:
                    _require_same_turn(existing, record)
                else:
                    staged_turns[key] = record
            staged_steps: dict[tuple[str, str], SystemStepRecord] = {}
            for record in system_steps:
                key = (record.run_id, record.entry_id)
                existing = staged_steps.get(key, self._system_steps.get(key))
                if existing is not None:
                    _require_same_system_step(existing, record)
                else:
                    staged_steps[key] = record
            durable_turns = (
                record
                for (run_id, _), record in (self._turns | staged_turns).items()
                if run_id == checkpoint.run_id
            )
            durable_steps = (
                record
                for (run_id, _), record in (self._system_steps | staged_steps).items()
                if run_id == checkpoint.run_id
            )
            _require_matching_checkpoint(durable_turns, durable_steps, checkpoint)
            self._turns.update(staged_turns)
            self._system_steps.update(staged_steps)
            self._checkpoints[checkpoint.run_id] = checkpoint

    def get_checkpoint(self, run_id: str) -> RunCheckpoint | None:
        """Return one run's latest recovery state."""
        with self._lock:
            return self._checkpoints.get(run_id)

    def clear_checkpoint(self, run_id: str) -> None:
        """Remove recovery state while retaining the immutable journal."""
        with self._lock:
            self._checkpoints.pop(run_id, None)

    def complete_job(
        self,
        result: EpisodeResult,
        completed_job: RunJob,
    ) -> None:
        """Persist the result and completed job under one lock."""
        _validate_completion(result, completed_job)
        with self._lock:
            self._save_result(result)
            self._save_job(completed_job)
            self._checkpoints.pop(result.run_id, None)

    def _save_result(self, result: EpisodeResult) -> None:
        """Replace one result while retaining completion order."""
        self._results.pop(result.run_id, None)
        self._results[result.run_id] = result

    def _record_turn(self, record: TurnRecord) -> None:
        """Append one turn while the repository lock is held."""
        key = (record.run_id, record.turn.turn_id)
        existing = self._turns.get(key)
        if existing is not None:
            _require_same_turn(existing, record)
            return
        self._turns[key] = record

    def _record_system_step(self, record: SystemStepRecord) -> None:
        """Append one system step while the repository lock is held."""
        key = (record.run_id, record.entry_id)
        existing = self._system_steps.get(key)
        if existing is not None:
            _require_same_system_step(existing, record)
            return
        self._system_steps[key] = record

    def _save_job(self, job: RunJob) -> None:
        """Replace one job while retaining submission order."""
        self._jobs[job.run_id] = job


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

    def save(self, result: EpisodeResult) -> None:
        """Save the canonical result and all audit projections atomically."""
        summary = _to_summary(result)
        with self._lock, self._connection:
            self._upsert_run(result, summary)
            self._replace_projections(result)

    def get(self, run_id: str) -> EpisodeResult | None:
        """Read the canonical, strongly typed episode representation."""
        with self._lock:
            row = self._connection.execute(
                "SELECT result_json FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return EpisodeResult.model_validate_json(row["result_json"]) if row is not None else None

    def list(self, limit: int = 50) -> tuple[RunSummary, ...]:
        """Read compact run summaries without deserializing full episodes."""
        if limit <= 0:
            return ()
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT run_id, scenario_id, seed, started_at, finished_at,
                       score_version, final_score
                FROM runs
                ORDER BY finished_at DESC, rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(RunSummary.model_validate(dict(row)) for row in rows)

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

    def list_jobs(self, limit: int = 50) -> tuple[RunJob, ...]:
        """Return all lifecycle states from newest submission to oldest."""
        if limit <= 0:
            return ()
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload_json
                FROM run_jobs
                ORDER BY submitted_at DESC, rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(RunJob.model_validate_json(row["payload_json"]) for row in rows)

    def list_replay_sources(self) -> tuple[ReplaySource, ...]:
        """Return every completed run from newest submission to oldest."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT run_id, submitted_at
                FROM run_jobs
                WHERE status = ?
                ORDER BY submitted_at DESC, rowid DESC
                """,
                (RunStatus.COMPLETED.value,),
            ).fetchall()
        return tuple(ReplaySource.model_validate(dict(row)) for row in rows)

    def list_resumable_jobs(self) -> tuple[RunJob, ...]:
        """Return resumable jobs from oldest to newest."""
        statuses = tuple(status.value for status in _RESUMABLE_STATUSES)
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
        return tuple(sorted(invocations, key=_invocation_order))

    def record_turn(self, record: TurnRecord) -> None:
        """Append one immutable turn, accepting exact retries only."""
        with self._lock, self._connection:
            self._insert_turn(record)

    def list_turns(self, run_id: str) -> tuple[TurnRecord, ...]:
        """Return one run's turns in deterministic apply order."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT schema_version, payload_json
                FROM run_turns
                WHERE run_id = ?
                ORDER BY sim_minute ASC, apply_sequence ASC, turn_id ASC
                """,
                (run_id,),
            ).fetchall()
        return tuple(
            TurnRecord.model_validate_json(_current_payload(row)) for row in rows
        )

    def record_system_step(self, record: SystemStepRecord) -> None:
        """Append one immutable system step, accepting exact retries only."""
        with self._lock, self._connection:
            self._insert_system_step(record)

    def list_system_steps(self, run_id: str) -> tuple[SystemStepRecord, ...]:
        """Return one run's system steps in chronological journal order."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT schema_version, payload_json
                FROM run_system_steps
                WHERE run_id = ?
                ORDER BY sim_minute ASC, journal_sequence ASC, entry_id ASC
                """,
                (run_id,),
            ).fetchall()
        return tuple(
            SystemStepRecord.model_validate_json(_current_payload(row)) for row in rows
        )

    def save_checkpoint(self, checkpoint: RunCheckpoint) -> None:
        """Atomically replace the canonical versioned checkpoint payload."""
        with self._lock, self._connection:
            self._upsert_checkpoint(checkpoint)

    def save_progress(
        self,
        turns: tuple[TurnRecord, ...],
        system_steps: tuple[SystemStepRecord, ...],
        checkpoint: RunCheckpoint,
    ) -> None:
        """Append journal entries and checkpoint in one transaction."""
        _validate_progress_identity(turns, system_steps, checkpoint)
        with self._lock, self._connection:
            for record in turns:
                self._insert_turn(record)
            for record in system_steps:
                self._insert_system_step(record)
            turn_rows = self._connection.execute(
                """
                SELECT schema_version, payload_json
                FROM run_turns
                WHERE run_id = ?
                """,
                (checkpoint.run_id,),
            ).fetchall()
            step_rows = self._connection.execute(
                """
                SELECT schema_version, payload_json
                FROM run_system_steps
                WHERE run_id = ?
                """,
                (checkpoint.run_id,),
            ).fetchall()
            durable_turns = (
                TurnRecord.model_validate_json(_current_payload(row)) for row in turn_rows
            )
            durable_steps = (
                SystemStepRecord.model_validate_json(_current_payload(row)) for row in step_rows
            )
            _require_matching_checkpoint(durable_turns, durable_steps, checkpoint)
            self._upsert_checkpoint(checkpoint)

    def get_checkpoint(self, run_id: str) -> RunCheckpoint | None:
        """Return one run's latest complete recovery state."""
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM run_checkpoints WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return RunCheckpoint.model_validate_json(row["payload_json"]) if row is not None else None

    def clear_checkpoint(self, run_id: str) -> None:
        """Remove recovery state while retaining the immutable journal."""
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM run_checkpoints WHERE run_id = ?",
                (run_id,),
            )

    def _insert_turn(self, record: TurnRecord) -> None:
        """Insert one immutable journal record inside the active transaction."""
        payload = record.model_dump_json()
        self._connection.execute(
            """
            INSERT INTO run_turns (
                run_id, turn_id, company_id, sim_minute, state_version,
                apply_sequence, schema_version, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, turn_id) DO NOTHING
            """,
            (
                record.run_id,
                record.turn.turn_id,
                record.turn.company_id,
                record.turn.sim_time.absolute_minute,
                record.turn.state_version,
                record.outcome.apply_sequence,
                _PAYLOAD_SCHEMA_VERSION,
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
        _require_same_turn(
            TurnRecord.model_validate_json(_current_payload(row)),
            record,
        )

    def _insert_system_step(self, record: SystemStepRecord) -> None:
        """Insert one immutable system journal row inside a transaction."""
        self._connection.execute(
            """
            INSERT INTO run_system_steps (
                run_id, entry_id, sim_minute, journal_sequence, kind,
                schema_version, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, entry_id) DO NOTHING
            """,
            (
                record.run_id,
                record.entry_id,
                record.occurred_at.absolute_minute,
                record.journal_sequence,
                record.kind.value,
                _PAYLOAD_SCHEMA_VERSION,
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
        _require_same_system_step(
            SystemStepRecord.model_validate_json(_current_payload(row)),
            record,
        )

    def _upsert_checkpoint(self, checkpoint: RunCheckpoint) -> None:
        """Replace one checkpoint inside the active transaction."""
        self._connection.execute(
            """
            INSERT INTO run_checkpoints (
                run_id, current_day, updated_at, payload_json
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                current_day = excluded.current_day,
                updated_at = excluded.updated_at,
                payload_json = excluded.payload_json
            """,
            (
                checkpoint.run_id,
                checkpoint.economy.day,
                datetime.now(UTC).isoformat(),
                checkpoint.model_dump_json(),
            ),
        )

    def complete_job(
        self,
        result: EpisodeResult,
        completed_job: RunJob,
    ) -> None:
        """Atomically save the complete result, projections, and job."""
        _validate_completion(result, completed_job)
        summary = _to_summary(result)
        with self._lock, self._connection:
            self._upsert_run(result, summary)
            self._replace_projections(result)
            self._upsert_job(completed_job)
            self._connection.execute(
                "DELETE FROM run_checkpoints WHERE run_id = ?",
                (result.run_id,),
            )

    def _create_schema(self) -> None:
        """Create the score-v9 schema for new benchmark databases."""
        current_version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version not in (0, _DATABASE_SCHEMA_VERSION):
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
            scenario_id TEXT NOT NULL,
            scenario_version INTEGER NOT NULL,
            scenario_json TEXT NOT NULL,
            seed INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            score_version TEXT NOT NULL,
            final_score TEXT NOT NULL,
            result_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS run_company_results (
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            company_id TEXT NOT NULL,
            company_name TEXT NOT NULL,
            tier TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            initial_value TEXT NOT NULL,
            final_cash TEXT NOT NULL,
            final_inventory_value TEXT NOT NULL,
            final_value TEXT NOT NULL,
            surplus TEXT NOT NULL,
            growth TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (run_id, company_id)
        );

        CREATE TABLE IF NOT EXISTS run_daily_snapshots (
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            day INTEGER NOT NULL,
            company_id TEXT NOT NULL,
            tier TEXT NOT NULL,
            cash TEXT NOT NULL,
            raw_milk_quantity TEXT NOT NULL,
            bottled_milk_quantity TEXT NOT NULL,
            inventory_value TEXT NOT NULL,
            net_worth TEXT NOT NULL,
            surplus TEXT NOT NULL,
            daily_consumer_sales TEXT NOT NULL,
            daily_expired_quantity TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (run_id, day, company_id)
        );

        CREATE TABLE IF NOT EXISTS run_decisions (
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            day INTEGER NOT NULL,
            company_id TEXT NOT NULL,
            observation_id TEXT NOT NULL,
            decision_type TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (run_id, day, company_id)
        );

        CREATE TABLE IF NOT EXISTS run_events (
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL,
            day INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            actor_company_id TEXT,
            counterparty_company_id TEXT,
            schema_version INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (run_id, sequence)
        );

        CREATE INDEX IF NOT EXISTS ix_snapshots_run_day
            ON run_daily_snapshots(run_id, day);
        CREATE INDEX IF NOT EXISTS ix_events_run_day
            ON run_events(run_id, day);

        CREATE TABLE IF NOT EXISTS run_jobs (
            run_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            seed INTEGER NOT NULL,
            source_run_id TEXT,
            scenario_id TEXT NOT NULL,
            current_day INTEGER NOT NULL,
            total_days INTEGER NOT NULL,
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
            day INTEGER NOT NULL,
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
            current_day INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS run_turns (
            run_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            company_id TEXT NOT NULL,
            sim_minute INTEGER NOT NULL,
            state_version INTEGER NOT NULL,
            apply_sequence INTEGER NOT NULL,
            schema_version INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (run_id, turn_id)
        );

        CREATE TABLE IF NOT EXISTS run_system_steps (
            run_id TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            sim_minute INTEGER NOT NULL,
            journal_sequence INTEGER NOT NULL,
            kind TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (run_id, entry_id)
        );

        CREATE INDEX IF NOT EXISTS ix_run_jobs_status_submitted
            ON run_jobs(status, submitted_at);
        CREATE INDEX IF NOT EXISTS ix_invocations_run_day_company
            ON policy_invocations(run_id, day, company_id);
        CREATE INDEX IF NOT EXISTS ix_run_turns_order
            ON run_turns(run_id, sim_minute, apply_sequence, turn_id);
        CREATE INDEX IF NOT EXISTS ix_run_system_steps_order
            ON run_system_steps(run_id, sim_minute, journal_sequence, entry_id);
        """
        with self._lock, self._connection:
            self._connection.executescript(schema)
            self._connection.execute(f"PRAGMA user_version = {_DATABASE_SCHEMA_VERSION}")

    def _upsert_job(self, job: RunJob) -> None:
        """Write the canonical typed lifecycle payload and query columns."""
        self._connection.execute(
            """
            INSERT INTO run_jobs (
                run_id, mode, status, seed, source_run_id, scenario_id,
                current_day, total_days, submitted_at, started_at, finished_at,
                error_message, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                mode = excluded.mode,
                status = excluded.status,
                seed = excluded.seed,
                source_run_id = excluded.source_run_id,
                scenario_id = excluded.scenario_id,
                current_day = excluded.current_day,
                total_days = excluded.total_days,
                submitted_at = excluded.submitted_at,
                started_at = excluded.started_at,
                finished_at = excluded.finished_at,
                error_message = excluded.error_message,
                payload_json = excluded.payload_json
            """,
            (
                job.run_id,
                job.mode.value,
                job.status.value,
                job.seed,
                job.source_run_id,
                job.scenario_id,
                job.current_day,
                job.total_days,
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
                invocation_id, run_id, company_id, day, outcome, provider,
                model, prompt_version, started_at, finished_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(invocation_id) DO UPDATE SET
                run_id = excluded.run_id,
                company_id = excluded.company_id,
                day = excluded.day,
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
                invocation.day,
                invocation.outcome.value,
                invocation.provider,
                invocation.model,
                invocation.prompt_version,
                invocation.started_at.isoformat(),
                invocation.finished_at.isoformat(),
                invocation.model_dump_json(),
            ),
        )

    def _upsert_run(
        self,
        result: EpisodeResult,
        summary: RunSummary,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO runs (
                run_id, scenario_id, scenario_version, scenario_json, seed,
                started_at, finished_at, score_version, final_score, result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                scenario_id = excluded.scenario_id,
                scenario_version = excluded.scenario_version,
                scenario_json = excluded.scenario_json,
                seed = excluded.seed,
                started_at = excluded.started_at,
                finished_at = excluded.finished_at,
                score_version = excluded.score_version,
                final_score = excluded.final_score,
                result_json = excluded.result_json
            """,
            (
                summary.run_id,
                summary.scenario_id,
                result.scenario.version,
                result.scenario.model_dump_json(),
                summary.seed,
                summary.started_at.isoformat(),
                summary.finished_at.isoformat(),
                summary.score_version,
                str(summary.final_score),
                result.model_dump_json(),
            ),
        )

    def _replace_projections(self, result: EpisodeResult) -> None:
        for statement in (
            "DELETE FROM run_company_results WHERE run_id = ?",
            "DELETE FROM run_daily_snapshots WHERE run_id = ?",
            "DELETE FROM run_decisions WHERE run_id = ?",
            "DELETE FROM run_events WHERE run_id = ?",
        ):
            self._connection.execute(statement, (result.run_id,))

        self._insert_company_results(result)
        self._insert_snapshots(result)
        self._insert_decisions(result)
        self._insert_events(result)

    def _insert_company_results(self, result: EpisodeResult) -> None:
        policies = {policy.company_id: policy for policy in result.policies}
        rows = (
            (
                result.run_id,
                company.company_id,
                result.scenario.company(company.company_id).name,
                company.tier.value,
                policies[company.company_id].name,
                policies[company.company_id].version,
                str(company.initial_value),
                str(company.final_cash),
                str(company.final_inventory_value),
                str(company.final_value),
                str(company.surplus),
                str(company.growth),
                company.model_dump_json(),
            )
            for company in result.score.companies
        )
        self._connection.executemany(
            """
            INSERT INTO run_company_results (
                run_id, company_id, company_name, tier, policy_name,
                policy_version, initial_value, final_cash,
                final_inventory_value, final_value, surplus, growth,
                payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    def _insert_snapshots(self, result: EpisodeResult) -> None:
        rows: list[tuple[str | int, ...]] = []
        for snapshot in result.snapshots:
            rows.extend(
                (
                    result.run_id,
                    snapshot.day,
                    company.company_id,
                    company.tier.value,
                    str(company.cash),
                    str(company.raw_milk_quantity),
                    str(company.bottled_milk_quantity),
                    str(company.inventory_value),
                    str(company.net_worth),
                    str(company.surplus),
                    str(company.daily_consumer_sales),
                    str(company.daily_expired_quantity),
                    company.model_dump_json(),
                )
                for company in snapshot.companies
            )
        self._connection.executemany(
            """
            INSERT INTO run_daily_snapshots (
                run_id, day, company_id, tier, cash, raw_milk_quantity,
                bottled_milk_quantity, inventory_value, net_worth, surplus,
                daily_consumer_sales, daily_expired_quantity, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    def _insert_decisions(self, result: EpisodeResult) -> None:
        rows = (
            (
                result.run_id,
                recorded.day,
                recorded.company_id,
                recorded.observation_id,
                recorded.decision.kind,
                _PAYLOAD_SCHEMA_VERSION,
                recorded.model_dump_json(),
            )
            for recorded in result.decisions
        )
        self._connection.executemany(
            """
            INSERT INTO run_decisions (
                run_id, day, company_id, observation_id, decision_type,
                schema_version, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    def _insert_events(self, result: EpisodeResult) -> None:
        def event_row(
            record: EventRecord,
        ) -> tuple[str, int, int, str, str, str | None, int, str]:
            event = record.event
            actor_id, counterparty_id = (
                (event.seller_id, event.buyer_id)
                if isinstance(event, TradeExecutedEvent)
                else (event.company_id, None)
            )
            return (
                result.run_id,
                record.sequence,
                event.day,
                event.event_type,
                actor_id,
                counterparty_id,
                _PAYLOAD_SCHEMA_VERSION,
                record.model_dump_json(),
            )

        self._connection.executemany(
            """
            INSERT INTO run_events (
                run_id, sequence, day, event_type, actor_company_id,
                counterparty_company_id, schema_version, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            map(event_row, result.events),
        )


def _to_summary(result: EpisodeResult) -> RunSummary:
    """Create the compact representation returned by list endpoints."""
    return RunSummary(
        run_id=result.run_id,
        scenario_id=result.scenario.scenario_id,
        seed=result.seed,
        started_at=result.started_at,
        finished_at=result.finished_at,
        score_version=result.score.score_version,
        final_score=result.score.final_score,
    )


def _current_payload(row: sqlite3.Row) -> str:
    """Return a journal payload only when it uses the active contract."""
    version = int(row["schema_version"])
    if version != _PAYLOAD_SCHEMA_VERSION:
        raise RuntimeError(f"unsupported journal payload schema version: {version}")
    return str(row["payload_json"])


def _validate_completion(
    result: EpisodeResult,
    completed_job: RunJob,
) -> None:
    """Reject mismatched or non-completed lifecycle writes."""
    if completed_job.run_id != result.run_id:
        raise ValueError("result and completed job must have the same run_id")
    if completed_job.status != RunStatus.COMPLETED:
        raise ValueError("complete_job requires status=completed")
    if (
        completed_job.scenario_id != result.scenario.scenario_id
        or completed_job.total_days != result.scenario.days
        or completed_job.seed != result.seed
    ):
        raise ValueError("result and completed job must describe the same episode")


def _turn_order(record: TurnRecord) -> tuple[int, int, str]:
    """Return the stable chronological journal key."""
    return (
        record.turn.sim_time.absolute_minute,
        record.outcome.apply_sequence,
        record.turn.turn_id,
    )


def _system_step_order(record: SystemStepRecord) -> tuple[int, int, str]:
    """Return the stable chronological system-journal key."""
    return (
        record.occurred_at.absolute_minute,
        record.journal_sequence,
        record.entry_id,
    )


def _invocation_order(
    invocation: PolicyInvocation,
) -> tuple[int, int, bool, int, str, str]:
    """Order daily calls by company and event-driven calls by time and application."""
    return (
        invocation.day,
        invocation.sim_minute if invocation.sim_minute is not None else -1,
        invocation.apply_sequence is None,
        invocation.apply_sequence or 0,
        invocation.company_id,
        invocation.invocation_id,
    )


def _validate_progress_identity(
    turns: tuple[TurnRecord, ...],
    system_steps: tuple[SystemStepRecord, ...],
    checkpoint: RunCheckpoint,
) -> None:
    """Require each journal delta to be carried exactly by the checkpoint."""
    if any(record.run_id != checkpoint.run_id for record in turns):
        raise ValueError("progress turns must match checkpoint run_id")
    if any(record.run_id != checkpoint.run_id for record in system_steps):
        raise ValueError("progress system steps must match checkpoint run_id")
    checkpoint_turns = {record.turn.turn_id: record for record in checkpoint.turns}
    if any(checkpoint_turns.get(record.turn.turn_id) != record for record in turns):
        raise ValueError("progress turns must match checkpoint turns")
    checkpoint_steps = {record.entry_id: record for record in checkpoint.system_steps}
    if any(checkpoint_steps.get(record.entry_id) != record for record in system_steps):
        raise ValueError("progress system steps must match checkpoint system steps")


def _require_matching_checkpoint(
    durable_turns: Iterable[TurnRecord],
    durable_system_steps: Iterable[SystemStepRecord],
    checkpoint: RunCheckpoint,
) -> None:
    """Require the recovery payload to describe the exact durable journal."""
    durable_turns_by_id = {record.turn.turn_id: record for record in durable_turns}
    checkpoint_turns = {record.turn.turn_id: record for record in checkpoint.turns}
    if durable_turns_by_id != checkpoint_turns:
        raise ValueError("checkpoint turns must match the durable journal")
    durable_steps_by_id = {record.entry_id: record for record in durable_system_steps}
    checkpoint_steps = {record.entry_id: record for record in checkpoint.system_steps}
    if durable_steps_by_id != checkpoint_steps:
        raise ValueError("checkpoint system steps must match the durable journal")


def _require_same_turn(existing: TurnRecord, incoming: TurnRecord) -> None:
    """Reject reuse of one turn identity for different immutable content."""
    if existing != incoming:
        raise ValueError(f"turn identity conflict: {incoming.run_id}/{incoming.turn.turn_id}")


def _require_same_system_step(
    existing: SystemStepRecord,
    incoming: SystemStepRecord,
) -> None:
    """Reject reuse of one system identity for different immutable content."""
    if existing != incoming:
        raise ValueError(f"system step identity conflict: {incoming.run_id}/{incoming.entry_id}")


def _isoformat(value: datetime | None) -> str | None:
    """Serialize an optional datetime accepted by typed lifecycle models."""
    return value.isoformat() if value is not None else None
