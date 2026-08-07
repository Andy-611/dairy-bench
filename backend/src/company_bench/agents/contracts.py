"""Provider-neutral types for one-company Agent decisions."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, Field

from company_bench.domain.models import Identifier, StrictModel
from company_bench.runs.models import TokenUsage
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


class CommandModelResult(StrictModel):
    """Validated atomic command plus provider observability metadata."""

    command: CompanyCommand
    provider: str
    model: str
    response_id: str | None = None
    request_id: str | None = None
    usage: TokenUsage = TokenUsage()
    attempts: int = Field(default=1, ge=1)
    latency_ms: int = Field(default=0, ge=0)


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
        latency_ms: int = 0,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.response_id = response_id
        self.usage = usage or TokenUsage()
        self.attempts = attempts
        self.latency_ms = latency_ms


class ModelOutputError(ModelCallError):
    """The Agent returned no schema-valid command."""


class ModelCompatibilityError(ModelOutputError):
    """The selected model cannot satisfy the required command protocol."""


class ModelInfrastructureError(ModelCallError):
    """The provider could not reliably execute the request."""


class PolicyInfrastructureError(RuntimeError):
    """A provider failure that must fail, not silently alter, a run."""


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
