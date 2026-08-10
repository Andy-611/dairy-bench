"""Typed lifecycle and audit records for asynchronous benchmark runs."""

from __future__ import annotations

from collections.abc import Hashable, Iterable
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from company_bench.agents.memory import AgentCheckpoint
from company_bench.domain.models import (
    CompanyId,
    CompanyObservation,
    DaySnapshot,
    EpisodeQuality,
    EventRecord,
    Identifier,
    PolicyDescriptor,
    PolicyKind,
    StrictModel,
)
from company_bench.economy.engine import EconomyState
from company_bench.runtime.attention import AgentAttention, ArmedWait, AttentionRejected
from company_bench.runtime.models import (
    CommandOutcome,
    CompanyCommand,
    JournalEntryKind,
    SimTime,
    SystemEventKind,
    SystemStepRecord,
    TurnRecord,
    Wait,
    WakeReason,
)
from company_bench.runtime.scheduler import SchedulerCheckpoint


class RunStatus(StrEnum):
    """Persistent lifecycle state of one requested episode."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    STOPPED = "stopped"

    @property
    def terminal(self) -> bool:
        """Return whether no more work is scheduled for this job."""
        return self in {self.COMPLETED, self.FAILED, self.STOPPED}

    @property
    def resumable(self) -> bool:
        """Return whether the same run may be explicitly continued."""
        return self in {self.INTERRUPTED, self.STOPPED}


class RunJob(StrictModel):
    """Progress record returned immediately by the run API."""

    run_id: Identifier
    mode: PolicyKind
    model: str | None = Field(default=None, min_length=1, max_length=256)
    status: RunStatus = RunStatus.QUEUED
    revision: int = Field(default=0, ge=0)
    seed: int
    source_run_id: Identifier | None = None
    scenario_id: Identifier
    current_day: int = Field(default=0, ge=0)
    total_days: int = Field(ge=1)
    submitted_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = Field(default=None, max_length=500)
    quality: EpisodeQuality | None = None

    @model_validator(mode="after")
    def validate_progress(self) -> RunJob:
        """Keep reported progress inside the scenario duration."""
        if self.current_day > self.total_days:
            raise ValueError("current_day must not exceed total_days")
        if self.mode is PolicyKind.MODEL and self.model is None:
            raise ValueError("Model Agent jobs require a model")
        if self.mode is not PolicyKind.MODEL and self.model is not None:
            raise ValueError("model is only valid for Model Agent jobs")
        if (self.status is RunStatus.COMPLETED) != (self.quality is not None):
            raise ValueError("only completed runs require quality metadata")
        return self

    def mark_running(self, started_at: datetime) -> Self:
        """Start or resume the job and advance its persistent revision."""
        return self._advance(
            status=RunStatus.RUNNING,
            started_at=self.started_at or started_at,
            finished_at=None,
            error_message=None,
        )

    def report_progress(self, day: int) -> Self:
        """Record one newly completed simulation day."""
        return self._advance(current_day=day)

    def mark_completed(self, finished_at: datetime, quality: EpisodeQuality) -> Self:
        """Finish the full horizon without an error."""
        return self._advance(
            status=RunStatus.COMPLETED,
            current_day=self.total_days,
            finished_at=finished_at,
            error_message=None,
            quality=quality,
        )

    def mark_interrupted(self, finished_at: datetime, reason: str) -> Self:
        """Persist a resumable interruption."""
        return self._advance(
            status=RunStatus.INTERRUPTED,
            finished_at=finished_at,
            error_message=reason,
        )

    def mark_failed(self, finished_at: datetime, reason: str) -> Self:
        """Persist a terminal failure without inventing a score."""
        return self._advance(
            status=RunStatus.FAILED,
            finished_at=finished_at,
            error_message=reason,
        )

    def mark_stopped(self, finished_at: datetime) -> Self:
        """Pause the job by explicit user request without inventing a result."""
        return self._advance(
            status=RunStatus.STOPPED,
            finished_at=finished_at,
            error_message=None,
        )

    def queue_for_resume(self) -> Self:
        """Queue an explicitly resumable run while preserving its identity and progress."""
        if not self.status.resumable:
            raise ValueError(f"a {self.status.value} run cannot be resumed")
        return self._advance(
            status=RunStatus.QUEUED,
            finished_at=None,
            error_message=None,
        )

    def _advance(self, **changes: object) -> Self:
        """Apply one validated lifecycle transition."""
        candidate = self.model_copy(
            update={**changes, "revision": self.revision + 1},
        )
        return type(self).model_validate_json(candidate.model_dump_json())


class ReplaySource(StrictModel):
    """Completed run identity available for deterministic replay."""

    run_id: Identifier
    submitted_at: datetime
    benchmark_eligible: bool


class CompanyRuntimeCursor(StrictModel):
    """Per-company counters required for deterministic turn restoration."""

    company_id: CompanyId
    next_turn_sequence: int = Field(ge=1)
    last_visible_event_sequence: int = Field(default=0, ge=0)
    available_at: SimTime | None = None
    active_wait: ArmedWait | None = None


class RunCheckpoint(StrictModel):
    """Current mutable state needed to resume one V4 episode."""

    schema_version: Literal[8] = 8
    run_id: Identifier
    episode_started_at: datetime
    economy: EconomyState
    scheduler: SchedulerCheckpoint
    policies: tuple[PolicyDescriptor, ...]
    agent_states: tuple[AgentCheckpoint, ...]
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
            if policy.kind is PolicyKind.MODEL
        ]
        if company_ids != expected_memory_ids:
            raise ValueError(
                "Agent checkpoints must match memory-owning policies in scenario order"
            )
        if any(checkpoint.run_id != self.run_id for checkpoint in self.agent_states):
            raise ValueError("agent checkpoints must belong to the checkpoint run")

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
        for cursor in self.cursors:
            if cursor.last_visible_event_sequence > len(self.events):
                raise ValueError("runtime cursor cannot exceed checkpoint events")
        _validate_runtime_commitments(self)
        return self


class RunRecovery(StrictModel):
    """One checkpoint paired with its authoritative append-only journals."""

    checkpoint: RunCheckpoint
    turns: tuple[TurnRecord, ...] = ()
    system_steps: tuple[SystemStepRecord, ...] = ()

    @model_validator(mode="after")
    def validate_journals(self) -> RunRecovery:
        """Validate journal identity, order, continuity, and checkpoint cursors."""
        run_id = self.checkpoint.run_id
        turn_ids = [record.turn.turn_id for record in self.turns]
        _require_unique(turn_ids, "recovery turn_ids")
        _require_unique(
            (record.outcome.apply_sequence for record in self.turns),
            "recovery apply sequences",
        )
        if any(record.run_id != run_id for record in self.turns):
            raise ValueError("turn records must belong to the recovery run")
        if self.turns != tuple(sorted(self.turns, key=_checkpoint_turn_order)):
            raise ValueError("recovery turns must be in deterministic apply order")

        step_ids = [record.entry_id for record in self.system_steps]
        _require_unique(step_ids, "recovery system step ids")
        if any(record.run_id != run_id for record in self.system_steps):
            raise ValueError("system steps must belong to the recovery run")
        if self.system_steps != tuple(sorted(self.system_steps, key=_system_step_order)):
            raise ValueError("recovery system steps must be chronological")

        journal_sequences = [
            sequence
            for sequence in (
                *(record.journal_sequence for record in self.turns),
                *(record.journal_sequence for record in self.system_steps),
            )
            if sequence is not None
        ]
        _require_unique(journal_sequences, "recovery journal sequences")
        if journal_sequences and sorted(journal_sequences) != list(
            range(1, max(journal_sequences) + 1)
        ):
            raise ValueError("recovery journal sequences must be contiguous")

        company_ids = {company.company_id for company in self.checkpoint.economy.companies}
        turn_counts = {company_id: 0 for company_id in company_ids}
        for record in self.turns:
            if record.turn.company_id not in turn_counts:
                raise ValueError("recovery turn company must belong to the active economy")
            turn_counts[record.turn.company_id] += 1
        for cursor in self.checkpoint.cursors:
            if cursor.next_turn_sequence != turn_counts[cursor.company_id] + 1:
                raise ValueError("runtime cursor must follow completed company turns")
        _validate_attention_commitments(self.checkpoint, self.turns)
        return self


class PolicyProfileView(StrictModel):
    """Safe server-side policy configuration exposed to the browser."""

    mode: PolicyKind
    label: str
    available: bool
    provider: str | None = None
    model: str | None = None
    models: tuple[str, ...] = ()
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

    def __add__(self, other: TokenUsage) -> TokenUsage:
        """Add physical provider usage without losing cached-token detail."""
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


class ProviderAttemptOutcome(StrEnum):
    """Physical outcome of one request sent to a model provider."""

    HTTP_ERROR = "http_error"
    PROTOCOL_ERROR = "protocol_error"
    SUCCESS = "success"
    TRANSPORT_ERROR = "transport_error"


class ProviderAttempt(StrictModel):
    """Auditable metadata for one physical provider request."""

    sequence: int = Field(ge=1)
    outcome: ProviderAttemptOutcome
    request_id: str | None = None
    response_id: str | None = None
    usage: TokenUsage = TokenUsage()
    latency_ms: int = Field(default=0, ge=0)
    error_kind: str | None = None
    error_message: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        """Pair errors with failed attempts and keep successes clean."""
        if (self.error_kind is None) != (self.error_message is None):
            raise ValueError("provider attempt error fields must be paired")
        failed = self.outcome is not ProviderAttemptOutcome.SUCCESS
        if failed != (self.error_kind is not None):
            raise ValueError("only failed provider attempts require an error")
        return self


class ProviderCallAudit(StrictModel):
    """Shared aggregate over every physical request in one provider call."""

    usage: TokenUsage = TokenUsage()
    attempts: int = Field(default=1, ge=0)
    attempt_history: tuple[ProviderAttempt, ...] = ()
    latency_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_attempts(self) -> Self:
        """Keep aggregate counters equal to the physical request history."""
        if not self.attempt_history:
            return self
        sequences = tuple(attempt.sequence for attempt in self.attempt_history)
        if sequences != tuple(range(1, len(sequences) + 1)):
            raise ValueError("provider attempts must have contiguous sequences")
        if self.attempts != len(self.attempt_history):
            raise ValueError("attempt count must match attempt_history")
        usage = sum(
            (attempt.usage for attempt in self.attempt_history),
            start=TokenUsage(),
        )
        if self.usage != usage:
            raise ValueError("provider usage must equal physical attempt usage")
        if self.latency_ms != sum(attempt.latency_ms for attempt in self.attempt_history):
            raise ValueError("provider latency must equal physical attempt latency")
        return self


class PolicyInvocation(ProviderCallAudit):
    """Auditable provider call for one V4 atomic company turn."""

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
    domain_turn_id: Identifier | None = None
    sim_minute: int | None = Field(default=None, ge=0)
    state_version: int | None = Field(default=None, ge=0)
    apply_sequence: int | None = Field(default=None, ge=1)
    command: CompanyCommand | None = None
    command_outcome: CommandOutcome | None = None
    error_kind: str | None = None
    error_message: str | None = Field(default=None, max_length=500)
    response_id: str | None = None
    request_id: str | None = None
    @model_validator(mode="after")
    def validate_event_turn(self) -> PolicyInvocation:
        """Keep optional event-driven audit fields complete and consistent."""
        if self.domain_turn_id is not None and (
            self.sim_minute is None
            or self.state_version is None
            or (self.outcome is InvocationOutcome.SUCCESS and self.command is None)
        ):
            raise ValueError("event-driven invocation requires time, state, and command")
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


def _validate_runtime_commitments(checkpoint: RunCheckpoint) -> None:
    """Pair every future economic commitment with exactly one scheduler event."""
    pending = checkpoint.scheduler.pending_events

    def completion_keys(kind: SystemEventKind) -> tuple[tuple[str, str | None, int], ...]:
        keys: list[tuple[str, str | None, int]] = []
        for event in pending:
            if event.kind is not kind:
                continue
            if len(event.reference_ids) != 1:
                raise ValueError("completion events require exactly one reference_id")
            keys.append(
                (
                    event.reference_ids[0],
                    event.company_id,
                    event.at.absolute_minute,
                )
            )
        _require_unique(keys, f"{kind.value} commitments")
        return tuple(keys)

    expected_jobs = {
        (job.job_id, job.company_id, job.completes_at.absolute_minute)
        for job in checkpoint.economy.jobs
    }
    if set(completion_keys(SystemEventKind.OPERATION_COMPLETED)) != expected_jobs:
        raise ValueError("operation jobs and completion events must match")

    expected_deliveries = {
        (
            delivery.delivery_id,
            delivery.buyer_id,
            delivery.arrives_at.absolute_minute,
        )
        for delivery in checkpoint.economy.deliveries
    }
    if set(completion_keys(SystemEventKind.DELIVERY_COMPLETED)) != expected_deliveries:
        raise ValueError("deliveries and completion events must match")

    runtime = checkpoint.economy.scenario.runtime
    if any(
        event.kind is SystemEventKind.COMPANY_WAKE
        and not runtime.open_minute <= event.at.minute_of_day < runtime.close_minute
        for event in pending
    ):
        raise ValueError("company wakes must remain inside business hours")


def _validate_attention_commitments(
    checkpoint: RunCheckpoint,
    turns: tuple[TurnRecord, ...],
) -> None:
    """Pair each armed Wait with its source Turn and fallback wake."""
    attention = AgentAttention()
    turns_by_id = {record.turn.turn_id: record for record in turns}
    latest_by_company: dict[CompanyId, TurnRecord] = {}
    for record in turns:
        latest_by_company[record.turn.company_id] = record

    expected: list[tuple[CompanyId, int, Identifier]] = []
    for cursor in checkpoint.cursors:
        plan = cursor.active_wait
        if plan is None:
            continue
        source = turns_by_id.get(plan.source_turn_id)
        if (
            source is None
            or source.turn.company_id != cursor.company_id
            or latest_by_company.get(cursor.company_id) != source
            or not isinstance(source.envelope.command, Wait)
            or not source.outcome.accepted
            or source.turn.turn_number_today >= source.turn.turn_limit_today
        ):
            raise ValueError("active wait must reference the company's latest accepted Wait")
        try:
            derived = attention.arm(source.envelope.command, source.turn)
        except AttentionRejected as error:
            raise ValueError("active wait source no longer forms a valid plan") from error
        if derived != plan or plan.review_at != source.outcome.next_available_at:
            raise ValueError("active wait must match its source Turn and outcome")
        if plan.review_at is not None:
            expected.append(
                (cursor.company_id, plan.review_at.absolute_minute, plan.source_turn_id)
            )

    actual: list[tuple[CompanyId, int, Identifier]] = []
    for event in checkpoint.scheduler.pending_events:
        if event.kind is not SystemEventKind.COMPANY_WAKE or event.company_id is None:
            continue
        for signal in event.wake_signals:
            if signal.reason is not WakeReason.WAIT_EXPIRED:
                continue
            if signal.source is None or signal.source.entry_type is not JournalEntryKind.TURN:
                raise ValueError("wait-expiry wakes require a source Turn")
            actual.append((event.company_id, event.at.absolute_minute, signal.source.entry_id))
    _require_unique(actual, "wait-expiry commitments")
    if set(actual) != set(expected):
        raise ValueError("armed waits and wait-expiry wakes must match")


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


def _system_step_order(record: SystemStepRecord) -> tuple[int, int, str]:
    """Return the canonical order of one durable system journal entry."""
    return (
        record.occurred_at.absolute_minute,
        record.journal_sequence,
        record.entry_id,
    )
