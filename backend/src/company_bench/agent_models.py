"""Provider-neutral types for one-company Agent decisions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

from pydantic import BaseModel, Field

from company_bench.models import (
    CompanyDecision,
    CompanyObservation,
    Identifier,
    NoOpDecision,
    StrictModel,
)
from company_bench.run_models import TokenUsage

DecisionModel = TypeVar("DecisionModel", bound=BaseModel)


class DecisionSubmission[SubmissionModel: BaseModel](StrictModel):
    """Structured-output envelope accepted from a model provider."""

    decision: SubmissionModel | NoOpDecision


class ModelRequest(StrictModel):
    """Complete provider-neutral request for one daily decision."""

    invocation_id: Identifier
    run_id: Identifier
    observation: CompanyObservation
    instructions: str
    input_text: str


class ModelResult(StrictModel):
    """Validated decision plus provider observability metadata."""

    decision: CompanyDecision
    provider: str
    model: str
    response_id: str | None = None
    request_id: str | None = None
    usage: TokenUsage = TokenUsage()
    attempts: int = Field(default=1, ge=1)
    latency_ms: int = Field(default=0, ge=0)


class ModelOutputError(RuntimeError):
    """The Agent returned no schema-valid decision."""

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


class ModelInfrastructureError(RuntimeError):
    """The provider could not reliably execute the request."""


class PolicyInfrastructureError(RuntimeError):
    """A provider failure that must fail, not silently alter, a run."""


class ModelGateway(Protocol):
    """External model seam shared by all company Agent instances."""

    async def generate(
        self,
        request: ModelRequest,
        output_type: type[DecisionModel],
    ) -> ModelResult:
        """Return one validated provider decision."""
        ...

    async def close(self) -> None:
        """Release owned provider resources."""
        ...


type DecisionFactory = Callable[[ModelRequest], CompanyDecision]
