"""Provider-neutral types for one-company Agent decisions."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, Field

from company_bench.domain.models import Identifier, ProtocolIssueKind, StrictModel
from company_bench.runs.models import ProviderAttempt, ProviderCallAudit, TokenUsage
from company_bench.runtime.models import (
    AgentTurn,
    AttentionPlan,
    CompanyDecision,
    IdleDecision,
    Produce,
    SetQuoteLadder,
    SetRetailPrice,
    Transform,
)

type DecisionToolName = Literal[
    "produce",
    "transform",
    "set_quote_ladder",
    "set_retail_price",
    "idle",
]


class ProduceDecisionInput(Produce):
    """Produce, then arm the supplied attention plan."""

    attention: AttentionPlan


class TransformDecisionInput(Transform):
    """Transform inventory, then arm the supplied attention plan."""

    attention: AttentionPlan


class QuoteDecisionInput(SetQuoteLadder):
    """Set a quote ladder, then arm the supplied attention plan."""

    attention: AttentionPlan


class RetailPriceDecisionInput(SetRetailPrice):
    """Set a retail price, then arm the supplied attention plan."""

    attention: AttentionPlan


_DECISION_TOOL_MODELS: dict[DecisionToolName, type[BaseModel]] = {
    "produce": ProduceDecisionInput,
    "transform": TransformDecisionInput,
    "set_quote_ladder": QuoteDecisionInput,
    "set_retail_price": RetailPriceDecisionInput,
    "idle": IdleDecision,
}


def decision_tool_model(name: DecisionToolName) -> type[BaseModel]:
    """Return the canonical Pydantic model for one decision tool."""
    return _DECISION_TOOL_MODELS[name]


class DecisionModelRequest(StrictModel):
    """Complete provider-neutral request for one atomic company decision."""

    invocation_id: Identifier
    run_id: Identifier
    turn: AgentTurn
    instructions: str
    input_text: str
    allowed_tools: tuple[DecisionToolName, ...] = Field(min_length=1)


class DecisionModelResult(ProviderCallAudit):
    """Validated decision plus provider observability metadata."""

    decision: CompanyDecision
    provider: str
    model: str
    response_id: str | None = None
    request_id: str | None = None
    attempts: int = Field(default=1, ge=1)


class ModelCallError(RuntimeError):
    """A model call failed after producing auditable provider metadata."""

    def __init__(
        self,
        message: str,
        *,
        request_id: str | None = None,
        response_id: str | None = None,
        usage: TokenUsage | None = None,
        attempts: int = 1,
        attempt_history: tuple[ProviderAttempt, ...] = (),
        latency_ms: int = 0,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.response_id = response_id
        self.usage = usage or TokenUsage()
        self.attempt_history = attempt_history
        self.attempts = len(attempt_history) or attempts
        self.latency_ms = latency_ms


class ModelOutputError(ModelCallError):
    """The Agent returned no schema-valid decision."""

    def __init__(
        self,
        message: str,
        *,
        issue_kind: ProtocolIssueKind = ProtocolIssueKind.INVALID_RESPONSE,
        **metadata: object,
    ) -> None:
        super().__init__(message, **metadata)
        self.issue_kind = issue_kind


class ModelCompatibilityError(ModelOutputError):
    """The selected model cannot satisfy the required decision protocol."""


class ModelConfigurationError(ModelCallError):
    """The provider permanently rejected the configured request or credential."""


class ModelQuotaExhaustedError(ModelCallError):
    """The provider rejected the request because paid quota was exhausted."""


class ModelInfrastructureError(ModelCallError):
    """The provider could not reliably execute the request."""


class PolicyTerminalError(RuntimeError):
    """A policy failure that cannot be fixed by retrying the same run automatically."""


class PolicyCompatibilityError(PolicyTerminalError):
    """The selected policy cannot satisfy the benchmark decision protocol."""


class PolicyConfigurationError(PolicyTerminalError):
    """The selected policy has a permanently rejected provider configuration."""


class PolicyExecutionError(PolicyTerminalError):
    """The policy implementation failed outside a recognized provider condition."""


class PolicyRecoverableError(RuntimeError):
    """A policy failure that preserves enough state to continue the same run."""


class PolicyQuotaExhaustedError(PolicyRecoverableError):
    """Paid provider quota was exhausted and requires an explicit resume."""


class PolicyInfrastructureError(PolicyRecoverableError):
    """A transient provider failure that interrupts a recoverable run."""


class DecisionGateway(Protocol):
    """External model seam for one atomic company decision."""

    async def generate_decision(
        self,
        request: DecisionModelRequest,
    ) -> DecisionModelResult:
        """Return exactly one validated decision."""
        ...

    async def close(self) -> None:
        """Release owned provider resources."""
        ...
