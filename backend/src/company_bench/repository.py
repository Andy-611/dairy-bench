"""Persistence ports and adapters for benchmark runs and Agent audits."""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from datetime import datetime
from os import PathLike
from pathlib import Path
from threading import RLock
from typing import Protocol, Self

from company_bench.models import EpisodeResult, EventRecord, RunSummary, TradeExecutedEvent
from company_bench.run_models import (
    PolicyAuditSink,
    PolicyInvocation,
    RunJob,
    RunStatus,
)

_DATABASE_SCHEMA_VERSION = 2
_PAYLOAD_SCHEMA_VERSION = 1
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


class LifecycleRepository(RunRepository, PolicyAuditSink, Protocol):
    """Persist complete episodes, asynchronous jobs, and Agent audits."""

    def save_job(self, job: RunJob) -> None:
        """Persist or replace a run lifecycle record."""

    def get_job(self, run_id: str) -> RunJob | None:
        """Return one lifecycle record when present."""

    def list_resumable_jobs(self) -> tuple[RunJob, ...]:
        """Return jobs that may safely be scheduled after startup."""

    def list_invocations(self, run_id: str) -> tuple[PolicyInvocation, ...]:
        """Return one run's Agent invocations in decision order."""

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

    def list_resumable_jobs(self) -> tuple[RunJob, ...]:
        """Return resumable jobs from oldest to newest."""
        with self._lock:
            return tuple(job for job in self._jobs.values() if job.status in _RESUMABLE_STATUSES)

    def record_invocation(self, invocation: PolicyInvocation) -> None:
        """Upsert one invocation by its deterministic identifier."""
        with self._lock:
            self._invocations[invocation.invocation_id] = invocation

    def list_invocations(self, run_id: str) -> tuple[PolicyInvocation, ...]:
        """Return one run's invocations in day and company order."""
        with self._lock:
            invocations = (
                invocation
                for invocation in self._invocations.values()
                if invocation.run_id == run_id
            )
            return tuple(
                sorted(
                    invocations,
                    key=lambda invocation: (
                        invocation.day,
                        invocation.company_id,
                    ),
                )
            )

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

    def _save_result(self, result: EpisodeResult) -> None:
        """Replace one result while retaining completion order."""
        self._results.pop(result.run_id, None)
        self._results[result.run_id] = result

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
                       efficiency, fairness, eligible
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
        """Return one run's invocations in day and company order."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload_json
                FROM policy_invocations
                WHERE run_id = ?
                ORDER BY day ASC, company_id ASC
                """,
                (run_id,),
            ).fetchall()
        return tuple(PolicyInvocation.model_validate_json(row["payload_json"]) for row in rows)

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

    def _create_schema(self) -> None:
        """Create schema v2, upgrading v1 databases without data loss."""
        current_version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version not in (0, 1, _DATABASE_SCHEMA_VERSION):
            raise RuntimeError(f"unsupported database schema version: {current_version}")
        schema = """
        CREATE TABLE IF NOT EXISTS runs (
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

        CREATE INDEX IF NOT EXISTS ix_run_jobs_status_submitted
            ON run_jobs(status, submitted_at);
        CREATE INDEX IF NOT EXISTS ix_invocations_run_day_company
            ON policy_invocations(run_id, day, company_id);
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
                started_at, finished_at, efficiency, fairness, gini, eligible,
                consumer_fill_rate, expired_quantity, result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                scenario_id = excluded.scenario_id,
                scenario_version = excluded.scenario_version,
                scenario_json = excluded.scenario_json,
                seed = excluded.seed,
                started_at = excluded.started_at,
                finished_at = excluded.finished_at,
                efficiency = excluded.efficiency,
                fairness = excluded.fairness,
                gini = excluded.gini,
                eligible = excluded.eligible,
                consumer_fill_rate = excluded.consumer_fill_rate,
                expired_quantity = excluded.expired_quantity,
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
                str(summary.efficiency),
                str(summary.fairness),
                str(result.score.gini),
                int(summary.eligible),
                str(result.score.consumer_fill_rate),
                str(result.score.expired_quantity),
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
        efficiency=result.score.efficiency,
        fairness=result.score.fairness,
        eligible=result.score.eligible,
    )


def _validate_completion(
    result: EpisodeResult,
    completed_job: RunJob,
) -> None:
    """Reject mismatched or non-completed lifecycle writes."""
    if completed_job.run_id != result.run_id:
        raise ValueError("result and completed job must have the same run_id")
    if completed_job.status != RunStatus.COMPLETED:
        raise ValueError("complete_job requires status=completed")


def _isoformat(value: datetime | None) -> str | None:
    """Serialize an optional datetime accepted by typed lifecycle models."""
    return value.isoformat() if value is not None else None
