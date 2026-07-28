"""Typed lifecycle and audit records for asynchronous benchmark runs."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import Field, model_validator

from company_bench.models import (
    CompanyDecision,
    CompanyId,
    CompanyObservation,
    Identifier,
    PolicyKind,
    StrictModel,
)


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
    """Auditable input and outcome of one company-day Agent decision."""

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
    error_kind: str | None = None
    error_message: str | None = Field(default=None, max_length=500)
    response_id: str | None = None
    request_id: str | None = None
    usage: TokenUsage = TokenUsage()
    attempts: int = Field(default=1, ge=1)
    latency_ms: int = Field(default=0, ge=0)


class PolicyAuditSink(Protocol):
    """Persistence port used by Agent policies without repository coupling."""

    def record_invocation(self, invocation: PolicyInvocation) -> None:
        """Persist or replace one deterministic invocation record."""
        ...
