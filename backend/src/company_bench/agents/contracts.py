"""Provider-neutral types for one-company Agent decisions."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, Field

from company_bench.domain.models import Identifier, ProtocolIssueKind, StrictModel
from company_bench.runs.models import ProviderAttempt, ProviderCallAudit, TokenUsage
from company_bench.runtime.models import (
    AgentTurn,
    CompanyCommand,
    Produce,
    SetQuoteLadder,
    SetRetailPrice,
    Transform,
    Wait,
)

type CommandName = Literal[
    "produce",
    "transform",
    "set_quote_ladder",
    "set_retail_price",
    "wait",
]
_COMMAND_MODELS: dict[CommandName, type[BaseModel]] = {
    "produce": Produce,
    "transform": Transform,
    "set_quote_ladder": SetQuoteLadder,
    "set_retail_price": SetRetailPrice,
    "wait": Wait,
}


def command_model(name: CommandName) -> type[BaseModel]:
    """Return the canonical Pydantic model for one command tool."""
    return _COMMAND_MODELS[name]


class CommandModelRequest(StrictModel):
    """Complete provider-neutral request for one atomic company command."""

    invocation_id: Identifier
    run_id: Identifier
    turn: AgentTurn
    instructions: str
    input_text: str
    allowed_commands: tuple[CommandName, ...] = Field(min_length=1)


class CommandModelResult(ProviderCallAudit):
    """Validated atomic command plus provider observability metadata."""

    command: CompanyCommand
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
    """The Agent returned no schema-valid command."""

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
    """The selected model cannot satisfy the required command protocol."""


class ModelConfigurationError(ModelCallError):
    """The provider permanently rejected the configured request or credential."""


class ModelInfrastructureError(ModelCallError):
    """The provider could not reliably execute the request."""


class PolicyTerminalError(RuntimeError):
    """A policy failure that cannot be fixed by retrying the same run automatically."""


class PolicyCompatibilityError(PolicyTerminalError):
    """The selected policy cannot satisfy the benchmark command protocol."""


class PolicyConfigurationError(PolicyTerminalError):
    """The selected policy has a permanently rejected provider configuration."""


class PolicyExecutionError(PolicyTerminalError):
    """The policy implementation failed outside a recognized provider condition."""


class PolicyInfrastructureError(RuntimeError):
    """A transient provider failure that interrupts a recoverable run."""


class CommandGateway(Protocol):
    """External model seam for one native or adapted atomic command."""

    async def generate_command(
        self,
        request: CommandModelRequest,
    ) -> CommandModelResult:
        """Return exactly one validated command."""
        ...

    async def close(self) -> None:
        """Release owned provider resources."""
        ...
