"""One-company Agent policy and exact recorded-decision replay."""

from __future__ import annotations

import hashlib
from collections import deque
from datetime import UTC, datetime
from typing import Final

from pydantic import Field

from company_bench.agent_models import (
    ModelGateway,
    ModelInfrastructureError,
    ModelOutputError,
    ModelRequest,
    PolicyInfrastructureError,
)
from company_bench.diagnostics import bounded_error
from company_bench.models import (
    CompanyDecision,
    CompanyId,
    CompanyObservation,
    FarmDecision,
    FarmOperation,
    InventoryPosition,
    MarketSummary,
    Money,
    NoOpDecision,
    PolicyMetadata,
    ProcessorDecision,
    ProcessorOperation,
    RecordedDecision,
    RetailerDecision,
    RetailerOperation,
    StrictModel,
)
from company_bench.policies import CompanyPolicy
from company_bench.run_models import (
    InvocationOutcome,
    PolicyAuditSink,
    PolicyInvocation,
    TokenUsage,
)

PROMPT_VERSION: Final = "dairy-company-v1"
_HISTORY_DAYS: Final = 7


class AgentMemoryEntry(StrictModel):
    """Compact private memory owned by exactly one company Agent."""

    day: int = Field(ge=1)
    cash: Money
    inventory: tuple[InventoryPosition, ...]
    previous_markets: tuple[MarketSummary, ...]
    decision: CompanyDecision


class AgentInput(StrictModel):
    """Typed context sent to a company Agent on one day."""

    recent_history: tuple[AgentMemoryEntry, ...]
    observation: CompanyObservation


class LlmCompanyPolicy(CompanyPolicy):
    """Use one isolated LLM Agent to control one economic company."""

    name = "llm-company-agent"
    version = "1"

    def __init__(
        self,
        *,
        run_id: str,
        company_id: CompanyId,
        gateway: ModelGateway,
        audit_sink: PolicyAuditSink,
        metadata: PolicyMetadata,
        memory: tuple[AgentMemoryEntry, ...] = (),
    ) -> None:
        self.company_id = company_id
        self.metadata = metadata
        self._run_id = run_id
        self._gateway = gateway
        self._audit_sink = audit_sink
        self._memory = deque(memory, maxlen=_HISTORY_DAYS)

    async def decide(
        self,
        observation: CompanyObservation,
    ) -> CompanyDecision:
        """Generate and audit this company's decision for one day."""
        if observation.company_id != self.company_id:
            raise ValueError("an Agent cannot control another company")
        output_type = _decision_type(observation)
        agent_input = AgentInput(
            recent_history=tuple(self._memory),
            observation=observation,
        )
        instructions = _instructions(output_type)
        request = ModelRequest(
            invocation_id=_invocation_id(
                self._run_id,
                observation.day,
                self.company_id,
            ),
            run_id=self._run_id,
            observation=observation,
            instructions=instructions,
            input_text=agent_input.model_dump_json(),
        )
        started_at = datetime.now(UTC)
        try:
            result = await self._gateway.generate(request, output_type)
            if not isinstance(result.decision, (output_type, NoOpDecision)):
                raise ModelOutputError(f"{self.company_id} returned a decision for another role")
        except ModelOutputError as error:
            self._record_failure(
                request,
                started_at,
                InvocationOutcome.AGENT_ERROR,
                error,
            )
            raise
        except ModelInfrastructureError as error:
            self._record_failure(
                request,
                started_at,
                InvocationOutcome.INFRASTRUCTURE_ERROR,
                error,
            )
            raise PolicyInfrastructureError(str(error)) from error
        except Exception as error:
            self._record_failure(
                request,
                started_at,
                InvocationOutcome.INFRASTRUCTURE_ERROR,
                error,
            )
            raise PolicyInfrastructureError(
                f"unexpected model gateway failure: {type(error).__name__}"
            ) from error

        self._audit_sink.record_invocation(
            PolicyInvocation(
                **_invocation_fields(
                    request,
                    self.metadata,
                    started_at,
                ),
                finished_at=datetime.now(UTC),
                outcome=InvocationOutcome.SUCCESS,
                decision=result.decision,
                response_id=result.response_id,
                request_id=result.request_id,
                usage=result.usage,
                attempts=result.attempts,
                latency_ms=result.latency_ms,
            )
        )
        self._memory.append(
            AgentMemoryEntry(
                day=observation.day,
                cash=observation.cash,
                inventory=observation.inventory,
                previous_markets=observation.previous_markets,
                decision=result.decision,
            )
        )
        return result.decision

    def memory(self) -> tuple[AgentMemoryEntry, ...]:
        """Export bounded state for diagnostics or future checkpoints."""
        return tuple(self._memory)

    def _record_failure(
        self,
        request: ModelRequest,
        started_at: datetime,
        outcome: InvocationOutcome,
        error: Exception,
    ) -> None:
        """Persist a sanitized failure at the same audit seam."""
        model_error = error if isinstance(error, ModelOutputError) else None
        self._audit_sink.record_invocation(
            PolicyInvocation(
                **_invocation_fields(
                    request,
                    self.metadata,
                    started_at,
                ),
                finished_at=datetime.now(UTC),
                outcome=outcome,
                error_kind=type(error).__name__,
                error_message=bounded_error(error),
                response_id=model_error.response_id if model_error else None,
                request_id=model_error.request_id if model_error else None,
                usage=model_error.usage if model_error else TokenUsage(),
                attempts=model_error.attempts if model_error else 1,
                latency_ms=model_error.latency_ms if model_error else 0,
            )
        )


class ReplayPolicy(CompanyPolicy):
    """Replay one company's previously recorded decisions exactly."""

    name = "recorded-replay"
    version = "1"

    def __init__(
        self,
        company_id: CompanyId,
        decisions: tuple[RecordedDecision, ...],
        metadata: PolicyMetadata,
    ) -> None:
        self.company_id = company_id
        self.metadata = metadata
        self._by_day = {
            recorded.day: recorded.decision
            for recorded in decisions
            if recorded.company_id == company_id
        }

    async def decide(
        self,
        observation: CompanyObservation,
    ) -> CompanyDecision:
        """Return the source decision matching this company and day."""
        if observation.company_id != self.company_id:
            raise ValueError("a replay policy cannot control another company")
        try:
            return self._by_day[observation.day]
        except KeyError as error:
            raise ValueError(
                f"source run has no day {observation.day} decision for {self.company_id}"
            ) from error


def _decision_type(
    observation: CompanyObservation,
) -> type[FarmDecision] | type[ProcessorDecision] | type[RetailerDecision]:
    """Select the only role-specific schema accepted for an observation."""
    operation = observation.operation
    if isinstance(operation, FarmOperation):
        return FarmDecision
    if isinstance(operation, ProcessorOperation):
        return ProcessorDecision
    if isinstance(operation, RetailerOperation):
        return RetailerDecision
    raise TypeError(f"unsupported operation: {type(operation).__name__}")


def _instructions(output_type: type[StrictModel]) -> str:
    """Build the stable, role-aware benchmark prompt."""
    return (
        "You are the sole decision-making Agent for one dairy company. "
        "Use only the supplied observation and your own recent history; "
        "never assume hidden information. Choose a feasible daily action "
        "that improves this company's sustainable final value while avoiding "
        "waste and respecting the published fairness constraints. "
        "Return exactly one structured decision. Use no_op only when acting "
        f"is genuinely unsafe. Required role schema: {output_type.__name__}."
    )


def _invocation_id(run_id: str, day: int, company_id: str) -> str:
    """Create an idempotent identity for one company-day call."""
    return f"{run_id}.{day}.{company_id}"


def _invocation_fields(
    request: ModelRequest,
    metadata: PolicyMetadata,
    started_at: datetime,
) -> dict[str, object]:
    """Return fields shared by successful and failed audit records."""
    return {
        "invocation_id": request.invocation_id,
        "run_id": request.run_id,
        "company_id": request.observation.company_id,
        "day": request.observation.day,
        "observation": request.observation,
        "provider": metadata.provider or "unknown",
        "model": metadata.model or "unknown",
        "prompt_version": metadata.prompt_version or PROMPT_VERSION,
        "prompt_hash": hashlib.sha256(request.instructions.encode()).hexdigest(),
        "started_at": started_at,
    }
