"""Typed lifecycle and audit records for asynchronous benchmark runs."""

from __future__ import annotations

from collections.abc import Hashable, Iterable
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import Field, model_validator

from company_bench.engine import EconomyState
from company_bench.memory import AgentCheckpoint
from company_bench.models import (
    CompanyDecision,
    CompanyId,
    CompanyObservation,
    DaySnapshot,
    EventRecord,
    Identifier,
    PolicyDescriptor,
    PolicyKind,
    StrictModel,
)
from company_bench.runtime_models import (
    CommandOutcome,
    CompanyCommand,
    SimTime,
    SystemStepRecord,
    TurnRecord,
)
from company_bench.scheduler import SchedulerCheckpoint


class RunStatus(StrEnum):
    """Persistent lifecycle state of one requested episode."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        """Return whether no more work is scheduled for this job."""
        return self in {self.COMPLETED, self.FAILED}


class RunJob(StrictModel):
    """Progress record returned immediately by the run API."""

    run_id: Identifier
    mode: PolicyKind
    status: RunStatus = RunStatus.QUEUED
    seed: int
    source_run_id: Identifier | None = None
    scenario_id: Identifier
    current_day: int = Field(default=0, ge=0)
    total_days: int = Field(ge=1)
    submitted_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_progress(self) -> RunJob:
        """Keep reported progress inside the scenario duration."""
        if self.current_day > self.total_days:
            raise ValueError("current_day must not exceed total_days")
        return self


class CompanyRuntimeCursor(StrictModel):
    """Per-company counters required for deterministic turn restoration."""

    company_id: CompanyId
    next_turn_sequence: int = Field(ge=1)
    last_visible_event_sequence: int = Field(default=0, ge=0)
    available_at: SimTime | None = None


class RunCheckpoint(StrictModel):
    """Complete atomic state needed to resume one V2 episode."""

    schema_version: Literal[1] = 1
    run_id: Identifier
    episode_started_at: datetime
    economy: EconomyState
    scheduler: SchedulerCheckpoint
    policies: tuple[PolicyDescriptor, ...]
    agent_states: tuple[AgentCheckpoint, ...]
    turns: tuple[TurnRecord, ...] = ()
    system_steps: tuple[SystemStepRecord, ...] = ()
    events: tuple[EventRecord, ...] = ()
    snapshots: tuple[DaySnapshot, ...] = ()
    cursors: tuple[CompanyRuntimeCursor, ...] = ()

    @model_validator(mode="after")
    def validate_identity(self) -> RunCheckpoint:
        """Keep every nested recovery value inside the same run."""
        expected_company_ids = tuple(
            company.company_id for company in self.economy.scenario.companies
        )
        if tuple(policy.company_id for policy in self.policies) != expected_company_ids:
            raise ValueError("checkpoint policies must match scenario company order")

        company_ids = [checkpoint.company_id for checkpoint in self.agent_states]
        _require_unique(company_ids, "agent checkpoint company_ids")
        expected_memory_ids = [
            policy.company_id
            for policy in self.policies
            if policy.kind in {PolicyKind.CODEX, PolicyKind.OPENAI}
        ]
        if company_ids != expected_memory_ids:
            raise ValueError(
                "Agent checkpoints must match memory-owning policies in scenario order"
            )
        if any(checkpoint.run_id != self.run_id for checkpoint in self.agent_states):
            raise ValueError("agent checkpoints must belong to the checkpoint run")

        turn_ids = [record.turn.turn_id for record in self.turns]
        _require_unique(turn_ids, "checkpoint turn_ids")
        _require_unique(
            (record.outcome.apply_sequence for record in self.turns),
            "checkpoint apply sequences",
        )
        if any(record.run_id != self.run_id for record in self.turns):
            raise ValueError("turn records must belong to the checkpoint run")
        if self.turns != tuple(sorted(self.turns, key=_checkpoint_turn_order)):
            raise ValueError("checkpoint turns must be in deterministic apply order")

        step_ids = [record.entry_id for record in self.system_steps]
        _require_unique(step_ids, "checkpoint system step ids")
        if any(record.run_id != self.run_id for record in self.system_steps):
            raise ValueError("system steps must belong to the checkpoint run")
        if self.system_steps != tuple(
            sorted(
                self.system_steps,
                key=lambda record: (
                    record.occurred_at.absolute_minute,
                    record.journal_sequence,
                    record.entry_id,
                ),
            )
        ):
            raise ValueError("checkpoint system steps must be chronological")
        journal_sequences = [
            sequence
            for sequence in (
                *(record.journal_sequence for record in self.turns),
                *(record.journal_sequence for record in self.system_steps),
            )
            if sequence is not None
        ]
        _require_unique(journal_sequences, "checkpoint journal sequences")
        if journal_sequences and sorted(journal_sequences) != list(
            range(1, max(journal_sequences) + 1)
        ):
            raise ValueError("checkpoint journal sequences must be contiguous")

        _require_unique(
            (record.sequence for record in self.events),
            "checkpoint event sequences",
        )
        if tuple(record.sequence for record in self.events) != tuple(
            range(1, len(self.events) + 1)
        ):
            raise ValueError("checkpoint event sequences must be contiguous")
        _require_unique(
            (snapshot.day for snapshot in self.snapshots),
            "checkpoint snapshot days",
        )
        if tuple(snapshot.day for snapshot in self.snapshots) != tuple(
            range(1, len(self.snapshots) + 1)
        ):
            raise ValueError("checkpoint snapshot days must be contiguous")

        cursor_ids = [cursor.company_id for cursor in self.cursors]
        _require_unique(cursor_ids, "runtime cursor company_ids")
        economy_company_ids = {company.company_id for company in self.economy.companies}
        if set(cursor_ids) != economy_company_ids:
            raise ValueError("runtime cursors must match the active economy")
        turn_counts = {company_id: 0 for company_id in economy_company_ids}
        for record in self.turns:
            if record.turn.company_id not in turn_counts:
                raise ValueError("checkpoint turn company must belong to the active economy")
            turn_counts[record.turn.company_id] += 1
        for cursor in self.cursors:
            if cursor.next_turn_sequence != turn_counts[cursor.company_id] + 1:
                raise ValueError("runtime cursor must follow completed company turns")
            if cursor.last_visible_event_sequence > len(self.events):
                raise ValueError("runtime cursor cannot exceed checkpoint events")
        return self


class PolicyProfileView(StrictModel):
    """Safe server-side policy configuration exposed to the browser."""

    mode: PolicyKind
    label: str
    available: bool
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    description: str
    unavailable_reason: str | None = None


class InvocationOutcome(StrEnum):
    """Result category of one company Agent call."""

    SUCCESS = "success"
    AGENT_ERROR = "agent_error"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


class TokenUsage(StrictModel):
    """Provider token counters retained without estimating money."""

    input_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class PolicyInvocation(StrictModel):
    """Auditable provider call for a V1 day or V2 atomic turn."""

    invocation_id: Identifier
    run_id: Identifier
    company_id: CompanyId
    day: int = Field(ge=1)
    observation: CompanyObservation
    provider: str
    model: str
    prompt_version: str
    prompt_hash: str
    started_at: datetime
    finished_at: datetime
    outcome: InvocationOutcome
    decision: CompanyDecision | None = None
    domain_turn_id: Identifier | None = None
    sim_minute: int | None = Field(default=None, ge=0)
    state_version: int | None = Field(default=None, ge=0)
    apply_sequence: int | None = Field(default=None, ge=1)
    command: CompanyCommand | None = None
    command_outcome: CommandOutcome | None = None
    error_kind: str | None = None
    error_message: str | None = Field(default=None, max_length=500)
    response_id: str | None = None
    provider_turn_id: str | None = None
    request_id: str | None = None
    usage: TokenUsage = TokenUsage()
    attempts: int = Field(default=1, ge=1)
    latency_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_v2_turn(self) -> PolicyInvocation:
        """Keep optional V2 command audit fields complete and consistent."""
        if self.domain_turn_id is not None and (
            self.sim_minute is None
            or self.state_version is None
            or (self.outcome is InvocationOutcome.SUCCESS and self.command is None)
        ):
            raise ValueError("V2 invocation requires time, state, and successful command")
        if self.command_outcome is not None:
            if self.domain_turn_id != self.command_outcome.turn_id:
                raise ValueError("command outcome must match domain_turn_id")
            if self.command is None:
                raise ValueError("command outcome requires a command")
            if self.apply_sequence != self.command_outcome.apply_sequence:
                raise ValueError("command outcome must match apply_sequence")
        return self


class PolicyAuditSink(Protocol):
    """Persistence port used by Agent policies without repository coupling."""

    def record_invocation(self, invocation: PolicyInvocation) -> None:
        """Persist or replace one deterministic invocation record."""
        ...


def _require_unique(values: Iterable[Hashable], label: str) -> None:
    """Reject duplicate checkpoint identities with one shared rule."""
    items = tuple(values)
    if len(items) != len(set(items)):
        raise ValueError(f"{label} must be unique")


def _checkpoint_turn_order(record: TurnRecord) -> tuple[int, int, str]:
    """Return the canonical order stored by a recovery checkpoint."""
    return (
        record.turn.sim_time.absolute_minute,
        record.outcome.apply_sequence,
        record.turn.turn_id,
    )
