"""Small deterministic Adapters used by contract and integration tests."""

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import TypeAdapter

from company_bench.agents.contracts import (
    DecisionModelRequest,
    DecisionModelResult,
    ModelOutputError,
)
from company_bench.agents.providers.capabilities import (
    CapabilitySource,
    ModelCapability,
    ModelCapabilityCatalog,
)
from company_bench.domain.models import (
    PolicyKind,
    PolicyMetadata,
    PolicyProfileId,
    RetailerOperation,
)
from company_bench.runtime.models import (
    ActionDecision,
    AgentTurn,
    AttentionPlan,
    CompanyDecision,
    DecisionPhase,
    EconomicCommand,
    IdleDecision,
    SetRetailPrice,
)

_DECISION_ADAPTER = TypeAdapter(CompanyDecision)


def model_capability_catalog(limits: dict[str, int]) -> ModelCapabilityCatalog:
    """Return a no-I/O catalog of gateway-confirmed model limits."""
    return ModelCapabilityCatalog(None, None, seeds=verified_capabilities(limits))


def verified_capabilities(limits: dict[str, int]) -> tuple[ModelCapability, ...]:
    """Build gateway-confirmed capability records for tests."""
    verified_at = datetime.now(UTC)
    return tuple(
        ModelCapability(
            model_id=model_id,
            max_output_tokens=max_output_tokens,
            source=CapabilitySource.GATEWAY,
            verified_at=verified_at,
        )
        for model_id, max_output_tokens in limits.items()
    )


class FixedDecisionAgent:
    """Return decisions from one deterministic iterable."""

    metadata = PolicyMetadata(
        name="fixed-decision",
        kind=PolicyKind.BASELINE,
        profile_id=PolicyProfileId.BASELINE,
    )

    def __init__(self, decisions: Iterable[CompanyDecision]) -> None:
        self._decisions = iter(decisions)

    async def act(self, turn: AgentTurn) -> CompanyDecision:
        if turn.phase is DecisionPhase.RETAIL_PRICING:
            operation = turn.observation.operation
            if not isinstance(operation, RetailerOperation):
                raise TypeError("pricing test turn requires a retailer")
            return company_decision(
                SetRetailPrice(
                    product=operation.input_product,
                    unit_price=Decimal("3.5000"),
                )
            )
        try:
            return next(self._decisions)
        except StopIteration as error:
            raise ValueError("fixed Agent decision stream is exhausted") from error


class ScriptedDecisionGateway:
    """Adapt a deterministic decision factory to the provider Gateway Interface."""

    provider = "scripted"

    def __init__(
        self,
        decision_factory: Callable[[DecisionModelRequest], CompanyDecision],
    ) -> None:
        self._decision_factory = decision_factory
        self.decision_requests: list[DecisionModelRequest] = []

    async def generate_decision(self, request: DecisionModelRequest) -> DecisionModelResult:
        self.decision_requests.append(request)
        decision = _DECISION_ADAPTER.validate_python(self._decision_factory(request))
        tool = decision.action.kind if isinstance(decision, ActionDecision) else decision.kind
        if tool not in request.allowed_tools:
            raise ModelOutputError(f"decision tool {tool} is not allowed")
        return DecisionModelResult(
            decision=decision,
            provider=self.provider,
            model="scripted-current",
        )

    async def close(self) -> None:
        """Release no resources."""


def company_decision(
    action: EconomicCommand | None = None,
    *,
    review_after_days: int | None = None,
) -> CompanyDecision:
    """Build one typed test decision with an empty alert set."""
    attention = AttentionPlan(review_after_days=review_after_days)
    return (
        IdleDecision(attention=attention)
        if action is None
        else ActionDecision(action=action, attention=attention)
    )
