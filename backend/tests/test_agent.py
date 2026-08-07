"""Tests for one-Agent-per-company policy orchestration."""

import asyncio
from collections.abc import Callable
from decimal import Decimal

import pytest
from pydantic import SecretStr

from company_bench.agents.company import COMMAND_PROMPT_VERSION, LlmCompanyAgent
from company_bench.agents.contracts import (
    CommandGateway,
    CommandModelRequest,
    CommandModelResult,
    ModelCompatibilityError,
    ModelConfigurationError,
    ModelInfrastructureError,
    ModelOutputError,
)
from company_bench.agents.factory import AgentFactory
from company_bench.agents.providers.newapi import NewApiConfig
from company_bench.domain.models import CompanyObservation, PolicyKind, PolicyMetadata
from company_bench.domain.scenario import DAIRY_S9_SCENARIO
from company_bench.runs.coordinator import RunCoordinator
from company_bench.runs.models import InvocationOutcome, RunJob, RunStatus, TokenUsage
from company_bench.runtime.episode import EpisodeRuntime
from company_bench.runtime.models import AgentTurn, SimTime, Wait, WakeReason
from company_bench.storage.store import InMemoryRunStore
from tests.support.fakes import ScriptedModelGateway


def _config() -> NewApiConfig:
    """Return a no-network configuration for injected test gateways."""
    return NewApiConfig(
        api_key=SecretStr("test-key"),
        model="gpt-test-default",
        models=("gpt-test-default", "gemini-test-selected"),
        max_attempts=1,
    )


class _TrackedScriptedGateway(ScriptedModelGateway):
    """Expose adapter closure for lifecycle assertions."""

    def __init__(self) -> None:
        super().__init__(lambda _: Wait())
        self.closed = False

    async def close(self) -> None:
        """Record closure of this company-owned gateway."""
        self.closed = True
        await super().close()


class _RecordingGatewayFactory:
    """Create and retain one observable gateway per company Agent."""

    def __init__(self, create: Callable[[], CommandGateway] = _TrackedScriptedGateway) -> None:
        self._create = create
        self.configs: list[NewApiConfig] = []
        self.gateways: list[CommandGateway] = []

    def __call__(self, config: NewApiConfig) -> CommandGateway:
        """Create a fresh gateway for one company Agent."""
        gateway = self._create()
        self.configs.append(config)
        self.gateways.append(gateway)
        return gateway


def test_model_mode_owns_nine_independent_newapi_gateways() -> None:
    repository = InMemoryRunStore()
    gateway_factory = _RecordingGatewayFactory()
    factory = AgentFactory(
        DAIRY_S9_SCENARIO,
        repository,
        newapi_config=_config(),
        gateway_factory=gateway_factory,
    )

    bundle = factory.create_agents(
        run_id="model_run",
        mode=PolicyKind.MODEL,
        model="gemini-test-selected",
    )
    agents = tuple(bundle.agents.values())
    asyncio.run(bundle.close())

    expected_count = len(DAIRY_S9_SCENARIO.companies)
    assert len(gateway_factory.gateways) == expected_count
    assert all(config.model == "gemini-test-selected" for config in gateway_factory.configs)
    assert len({id(gateway) for gateway in gateway_factory.gateways}) == expected_count
    assert all(gateway.closed for gateway in gateway_factory.gateways)
    assert len({id(agent) for agent in agents}) == expected_count
    assert all(isinstance(agent, LlmCompanyAgent) for agent in agents)
    assert all(agent.metadata.name == "model-company-agent" for agent in agents)
    assert all(agent.metadata.kind is PolicyKind.MODEL for agent in agents)
    assert all(agent.metadata.provider == "newapi" for agent in agents)
    assert all(agent.metadata.model == "gemini-test-selected" for agent in agents)
    assert all(agent.metadata.prompt_version == COMMAND_PROMPT_VERSION for agent in agents)


class _UnavailableGateway:
    """Fail every model call as provider infrastructure."""

    def __init__(self) -> None:
        self.closed = False
        self.command_requests: list[CommandModelRequest] = []

    async def generate_command(
        self,
        request: CommandModelRequest,
    ) -> CommandModelResult:
        self.command_requests.append(request)
        raise ModelInfrastructureError(
            "provider unavailable",
            attempts=3,
            latency_ms=42,
        )

    async def close(self) -> None:
        self.closed = True


class _IncompatibleGateway(_UnavailableGateway):
    """Reject the benchmark's required command protocol."""

    async def generate_command(
        self,
        request: CommandModelRequest,
    ) -> CommandModelResult:
        self.command_requests.append(request)
        raise ModelCompatibilityError("required function call is unsupported")


class _MisconfiguredGateway(_UnavailableGateway):
    """Reject a permanent provider configuration."""

    async def generate_command(
        self,
        request: CommandModelRequest,
    ) -> CommandModelResult:
        self.command_requests.append(request)
        raise ModelConfigurationError("credential rejected")


class _BrokenGateway(_UnavailableGateway):
    """Raise an unclassified implementation failure."""

    async def generate_command(
        self,
        request: CommandModelRequest,
    ) -> CommandModelResult:
        self.command_requests.append(request)
        raise RuntimeError("adapter bug")


class _AuditedOutputErrorGateway:
    """Return a schema failure that still identifies the completed model call."""

    async def generate_command(
        self,
        request: CommandModelRequest,
    ) -> CommandModelResult:
        raise ModelOutputError(
            "invalid output",
            request_id="request_failed",
            response_id="response_failed",
            usage=TokenUsage(total_tokens=17),
            attempts=2,
            latency_ms=123,
        )

    async def close(self) -> None:
        """Release no resources."""


def test_output_failure_retains_provider_audit_coordinates(
    first_observation: CompanyObservation,
) -> None:
    repository = InMemoryRunStore()
    turn = AgentTurn(
        turn_id="failed_run.farm_a.t1",
        company_id=first_observation.company_id,
        sim_time=SimTime(absolute_minute=540),
        state_version=0,
        turn_number_today=1,
        turn_limit_today=first_observation.runtime.max_turns_per_company_day,
        wake_reasons=(WakeReason.DAY_OPEN,),
        observation=first_observation,
        available_cash=first_observation.cash,
        marked_surplus=Decimal(),
    )
    agent = LlmCompanyAgent(
        run_id="failed_run",
        company_id=first_observation.company_id,
        gateway=_AuditedOutputErrorGateway(),
        audit_sink=repository,
        metadata=PolicyMetadata(
            name="model-company-agent",
            kind=PolicyKind.MODEL,
            provider="newapi",
            model="gpt-test",
            prompt_version=COMMAND_PROMPT_VERSION,
        ),
    )

    with pytest.raises(ModelOutputError):
        asyncio.run(agent.act(turn))

    invocation = repository.list_invocations("failed_run")[0]
    assert invocation.request_id == "request_failed"
    assert invocation.response_id == "response_failed"
    assert invocation.usage.total_tokens == 17
    assert invocation.attempts == 2
    assert invocation.latency_ms == 123
    assert invocation.domain_turn_id == turn.turn_id
    assert invocation.sim_minute == turn.sim_time.absolute_minute
    assert invocation.prompt_version == COMMAND_PROMPT_VERSION


async def _run_model_job_until(
    repository: InMemoryRunStore,
    factory: AgentFactory,
    expected_status: RunStatus,
) -> RunJob:
    """Run one model job until it reaches the expected non-active status."""
    coordinator = RunCoordinator(
        repository,
        factory,
        runtime=EpisodeRuntime(DAIRY_S9_SCENARIO),
    )
    await coordinator.start()
    try:
        submitted = await coordinator.submit(
            mode=PolicyKind.MODEL,
            model="gpt-test-default",
            seed=42,
        )
        async with asyncio.timeout(2):
            while True:
                job = repository.get_job(submitted.run_id)
                if job is not None and job.status is expected_status:
                    return job
                await asyncio.sleep(0)
    finally:
        await coordinator.close()


def test_infrastructure_failure_interrupts_job_without_result() -> None:
    repository = InMemoryRunStore()
    gateway_factory = _RecordingGatewayFactory(_UnavailableGateway)
    factory = AgentFactory(
        DAIRY_S9_SCENARIO,
        repository,
        newapi_config=_config(),
        gateway_factory=gateway_factory,
    )

    interrupted = asyncio.run(
        _run_model_job_until(repository, factory, RunStatus.INTERRUPTED)
    )

    assert interrupted.status is RunStatus.INTERRUPTED
    assert interrupted.current_day == 0
    assert "PolicyInfrastructureError" in (interrupted.error_message or "")
    assert repository.get(interrupted.run_id) is None
    assert repository.get_checkpoint(interrupted.run_id) is not None
    invocations = repository.list_invocations(interrupted.run_id)
    assert invocations
    assert all(
        invocation.outcome is InvocationOutcome.INFRASTRUCTURE_ERROR for invocation in invocations
    )
    assert all(invocation.attempts == 3 for invocation in invocations)
    assert all(invocation.latency_ms == 42 for invocation in invocations)
    gateways = tuple(
        gateway for gateway in gateway_factory.gateways if isinstance(gateway, _UnavailableGateway)
    )
    expected_count = len(DAIRY_S9_SCENARIO.companies)
    assert len(gateways) == expected_count
    assert len({id(gateway) for gateway in gateways}) == expected_count
    assert all(gateway.closed for gateway in gateways)
    assert all(len(gateway.command_requests) == 1 for gateway in gateways)
    assert {gateway.command_requests[0].turn.company_id for gateway in gateways} == {
        company.company_id for company in DAIRY_S9_SCENARIO.companies
    }


@pytest.mark.parametrize(
    ("gateway_type", "expected_error", "outcome"),
    (
        (_IncompatibleGateway, "PolicyCompatibilityError", InvocationOutcome.AGENT_ERROR),
        (
            _MisconfiguredGateway,
            "PolicyConfigurationError",
            InvocationOutcome.INFRASTRUCTURE_ERROR,
        ),
        (
            _BrokenGateway,
            "PolicyExecutionError",
            InvocationOutcome.INFRASTRUCTURE_ERROR,
        ),
    ),
)
def test_permanent_model_failure_marks_job_failed_without_result(
    gateway_type: Callable[[], CommandGateway],
    expected_error: str,
    outcome: InvocationOutcome,
) -> None:
    repository = InMemoryRunStore()
    gateway_factory = _RecordingGatewayFactory(gateway_type)
    factory = AgentFactory(
        DAIRY_S9_SCENARIO,
        repository,
        newapi_config=_config(),
        gateway_factory=gateway_factory,
    )

    failed = asyncio.run(_run_model_job_until(repository, factory, RunStatus.FAILED))

    assert failed.status is RunStatus.FAILED
    assert expected_error in (failed.error_message or "")
    assert repository.get(failed.run_id) is None
    assert repository.get_checkpoint(failed.run_id) is not None
    assert all(
        invocation.outcome is outcome
        for invocation in repository.list_invocations(failed.run_id)
    )
