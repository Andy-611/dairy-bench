"""Tests for one-Agent-per-company policy orchestration."""

import asyncio
from collections.abc import Callable

import pytest
from pydantic import SecretStr

from company_bench.agent_gateway import (
    OpenAIAgentConfig,
    ScriptedModelGateway,
)
from company_bench.agent_models import (
    DecisionFactory,
    DecisionModel,
    ModelGateway,
    ModelInfrastructureError,
    ModelOutputError,
    ModelRequest,
    ModelResult,
)
from company_bench.agent_policy import LlmCompanyPolicy
from company_bench.application import DairyBenchmark
from company_bench.codex_gateway import CodexAgentConfig
from company_bench.coordinator import RunCoordinator
from company_bench.dairy_scenario import DAIRY_V1_SCENARIO
from company_bench.models import (
    CompanyDecision,
    CompanyObservation,
    NoOpDecision,
    PolicyFailedEvent,
    PolicyKind,
    PolicyMetadata,
    RetailerDecision,
)
from company_bench.policy_factory import PolicyFactory
from company_bench.repository import MemoryRunRepository
from company_bench.run_models import InvocationOutcome, RunStatus, TokenUsage


def _config() -> OpenAIAgentConfig:
    """Return a no-network configuration for injected test gateways."""
    return OpenAIAgentConfig(
        api_key=SecretStr("test-key"),
        model="test-model",
        max_attempts=1,
    )


def _factory(
    repository: MemoryRunRepository,
    gateway_factory: Callable[[OpenAIAgentConfig], ModelGateway],
) -> PolicyFactory:
    """Inject deterministic company gateways behind the production factory."""
    return PolicyFactory(
        DAIRY_V1_SCENARIO,
        repository,
        openai_config=_config(),
        gateway_factory=gateway_factory,
    )


class _TrackedScriptedGateway(ScriptedModelGateway):
    """Expose adapter closure for lifecycle assertions."""

    def __init__(self, decision: DecisionFactory) -> None:
        super().__init__(decision)
        self.closed = False

    async def close(self) -> None:
        """Record closure of this company-owned gateway."""
        self.closed = True
        await super().close()


class _RecordingGatewayFactory:
    """Create and retain one observable gateway per company Agent."""

    def __init__(self, create: Callable[[], ModelGateway]) -> None:
        self._create = create
        self.gateways: list[ModelGateway] = []

    def __call__(self, _: OpenAIAgentConfig) -> ModelGateway:
        """Create a fresh gateway for one company Agent."""
        gateway = self._create()
        self.gateways.append(gateway)
        return gateway


class _RecordingCodexGatewayFactory:
    """Create one observable Codex gateway per company identity."""

    def __init__(self) -> None:
        self.company_ids: list[str] = []
        self.gateways: list[_TrackedScriptedGateway] = []

    def __call__(
        self,
        _: CodexAgentConfig,
        company_id: str,
    ) -> _TrackedScriptedGateway:
        """Create a fresh scripted substitute for one Codex runtime."""
        gateway = _TrackedScriptedGateway(
            lambda request: NoOpDecision(reason=request.observation.company_id)
        )
        self.company_ids.append(company_id)
        self.gateways.append(gateway)
        return gateway


def test_six_isolated_agents_make_180_calls_and_output_error_becomes_no_op() -> None:
    repository = MemoryRunRepository()

    def decision(request: ModelRequest) -> CompanyDecision:
        if request.observation.day == 1 and request.observation.company_id == "farm_a":
            return RetailerDecision(
                bottled_bid_quantity="1",
                maximum_bottled_price="1",
                retail_price="1",
            )
        return NoOpDecision(reason="scripted")

    gateway_factory = _RecordingGatewayFactory(lambda: _TrackedScriptedGateway(decision))
    factory = _factory(repository, gateway_factory)
    bundle = factory.create(run_id="agent_run", mode=PolicyKind.OPENAI)
    policies = tuple(bundle.policies.values())
    assert len({id(policy) for policy in policies}) == 6
    assert all(isinstance(policy, LlmCompanyPolicy) for policy in policies)
    assert len(gateway_factory.gateways) == 6
    assert len({id(gateway) for gateway in gateway_factory.gateways}) == 6

    async def run_episode():
        try:
            return await DairyBenchmark().run(
                bundle.policies,
                seed=42,
                run_id="agent_run",
            )
        finally:
            await bundle.close()

    result = asyncio.run(run_episode())

    gateways = tuple(
        gateway
        for gateway in gateway_factory.gateways
        if isinstance(gateway, _TrackedScriptedGateway)
    )
    assert len(gateways) == 6
    assert all(len(gateway.requests) == 30 for gateway in gateways)
    assert all(gateway.closed for gateway in gateways)
    assert {
        (request.observation.day, request.observation.company_id)
        for gateway in gateways
        for request in gateway.requests
    } == {
        (day, company.company_id) for day in range(1, 31) for company in DAIRY_V1_SCENARIO.companies
    }
    assert all(
        len({request.observation.company_id for request in gateway.requests}) == 1
        for gateway in gateways
    )
    assert len(result.decisions) == 6 * 30
    failed_decision = next(
        recorded
        for recorded in result.decisions
        if recorded.day == 1 and recorded.company_id == "farm_a"
    )
    assert failed_decision.decision == NoOpDecision(reason="policy_failed")
    failures = [
        record.event for record in result.events if isinstance(record.event, PolicyFailedEvent)
    ]
    assert len(failures) == 1
    assert failures[0].company_id == "farm_a"

    invocations = repository.list_invocations(result.run_id)
    assert len(invocations) == 6 * 30
    assert (
        sum(invocation.outcome is InvocationOutcome.AGENT_ERROR for invocation in invocations) == 1
    )
    assert all(
        len(policy.memory()) == 7 for policy in policies if isinstance(policy, LlmCompanyPolicy)
    )


def test_codex_mode_owns_six_independent_company_runtimes() -> None:
    repository = MemoryRunRepository()
    gateway_factory = _RecordingCodexGatewayFactory()
    factory = PolicyFactory(
        DAIRY_V1_SCENARIO,
        repository,
        codex_config=CodexAgentConfig(model="test-codex-model"),
        codex_gateway_factory=gateway_factory,
    )

    bundle = factory.create(run_id="codex_run", mode=PolicyKind.CODEX)
    policies = tuple(bundle.policies.values())
    asyncio.run(bundle.close())

    expected_company_ids = [company.company_id for company in DAIRY_V1_SCENARIO.companies]
    assert gateway_factory.company_ids == expected_company_ids
    assert len({id(gateway) for gateway in gateway_factory.gateways}) == 6
    assert all(gateway.closed for gateway in gateway_factory.gateways)
    assert all(isinstance(policy, LlmCompanyPolicy) for policy in policies)
    assert all(policy.metadata.kind is PolicyKind.CODEX for policy in policies)
    assert all(policy.metadata.provider == "codex" for policy in policies)


class _UnavailableGateway:
    """Fail every model call as provider infrastructure."""

    def __init__(self) -> None:
        self.closed = False
        self.requests: list[ModelRequest] = []

    async def generate(
        self,
        request: ModelRequest,
        output_type: type[DecisionModel],
    ) -> ModelResult:
        self.requests.append(request)
        raise ModelInfrastructureError("provider unavailable")

    async def close(self) -> None:
        self.closed = True


class _AuditedOutputErrorGateway:
    """Return a schema failure that still identifies the completed model call."""

    async def generate(
        self,
        request: ModelRequest,
        output_type: type[DecisionModel],
    ) -> ModelResult:
        raise ModelOutputError(
            "invalid output",
            request_id="thread_failed",
            response_id="turn_failed",
            usage=TokenUsage(total_tokens=17),
            attempts=2,
            latency_ms=123,
        )

    async def close(self) -> None:
        """Release no resources."""


def test_output_failure_retains_codex_artifact_coordinates(
    first_observation: CompanyObservation,
) -> None:
    repository = MemoryRunRepository()
    policy = LlmCompanyPolicy(
        run_id="failed_run",
        company_id=first_observation.company_id,
        gateway=_AuditedOutputErrorGateway(),
        audit_sink=repository,
        metadata=PolicyMetadata(
            name="codex-company-agent",
            kind=PolicyKind.CODEX,
            provider="codex",
            model="gpt-5.6-terra",
        ),
    )

    with pytest.raises(ModelOutputError):
        asyncio.run(policy.decide(first_observation))

    invocation = repository.list_invocations("failed_run")[0]
    assert invocation.request_id == "thread_failed"
    assert invocation.response_id == "turn_failed"
    assert invocation.usage.total_tokens == 17
    assert invocation.attempts == 2
    assert invocation.latency_ms == 123


def test_infrastructure_failure_marks_job_failed_without_result() -> None:
    repository = MemoryRunRepository()
    gateway_factory = _RecordingGatewayFactory(_UnavailableGateway)
    factory = _factory(repository, gateway_factory)

    async def run_failed_job():
        coordinator = RunCoordinator(repository, factory)
        await coordinator.start()
        try:
            submitted = await coordinator.submit(
                mode=PolicyKind.OPENAI,
                seed=42,
            )

            async def wait_until_terminal():
                while True:
                    job = repository.get_job(submitted.run_id)
                    if job is not None and job.status.terminal:
                        return job
                    await asyncio.sleep(0)

            return await asyncio.wait_for(wait_until_terminal(), timeout=2)
        finally:
            await coordinator.close()

    failed = asyncio.run(run_failed_job())

    assert failed.status is RunStatus.FAILED
    assert failed.current_day == 0
    assert "PolicyInfrastructureError" in (failed.error_message or "")
    assert repository.get(failed.run_id) is None
    invocations = repository.list_invocations(failed.run_id)
    assert invocations
    assert all(
        invocation.outcome is InvocationOutcome.INFRASTRUCTURE_ERROR for invocation in invocations
    )
    gateways = tuple(
        gateway for gateway in gateway_factory.gateways if isinstance(gateway, _UnavailableGateway)
    )
    assert len(gateways) == 6
    assert len({id(gateway) for gateway in gateways}) == 6
    assert all(gateway.closed for gateway in gateways)
    assert all(len(gateway.requests) == 1 for gateway in gateways)
    assert {gateway.requests[0].observation.company_id for gateway in gateways} == {
        company.company_id for company in DAIRY_V1_SCENARIO.companies
    }
