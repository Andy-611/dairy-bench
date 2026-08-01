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
    CommandGateway,
    CommandModelRequest,
    CommandModelResult,
    ModelInfrastructureError,
    ModelOutputError,
)
from company_bench.agents import COMMAND_PROMPT_VERSION, LlmCompanyAgent
from company_bench.codex_gateway import CodexAgentConfig
from company_bench.coordinator import RunCoordinator
from company_bench.dairy_scenario import DAIRY_S12_V3_SCENARIO
from company_bench.models import (
    CompanyObservation,
    NoOpDecision,
    PolicyKind,
    PolicyMetadata,
)
from company_bench.policy_factory import PolicyFactory
from company_bench.repository import MemoryRunRepository
from company_bench.run_models import InvocationOutcome, RunStatus, TokenUsage
from company_bench.runtime import EpisodeRuntime
from company_bench.runtime_models import AgentTurn, SimTime, Wait, WakeReason


def _config() -> OpenAIAgentConfig:
    """Return a no-network configuration for injected test gateways."""
    return OpenAIAgentConfig(
        api_key=SecretStr("test-key"),
        model="test-model",
        max_attempts=1,
    )


class _TrackedScriptedGateway(ScriptedModelGateway):
    """Expose adapter closure for lifecycle assertions."""

    def __init__(self) -> None:
        super().__init__(
            lambda _: NoOpDecision(),
            command_factory=lambda _: Wait(),
        )
        self.closed = False

    async def close(self) -> None:
        """Record closure of this company-owned gateway."""
        self.closed = True
        await super().close()


class _RecordingGatewayFactory:
    """Create and retain one observable gateway per company Agent."""

    def __init__(self, create: Callable[[], CommandGateway]) -> None:
        self._create = create
        self.gateways: list[CommandGateway] = []

    def __call__(self, _: OpenAIAgentConfig) -> CommandGateway:
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
        gateway = _TrackedScriptedGateway()
        self.company_ids.append(company_id)
        self.gateways.append(gateway)
        return gateway


def test_codex_mode_owns_twelve_independent_company_runtimes() -> None:
    repository = MemoryRunRepository()
    gateway_factory = _RecordingCodexGatewayFactory()
    factory = PolicyFactory(
        DAIRY_S12_V3_SCENARIO,
        repository,
        codex_config=CodexAgentConfig(model="test-codex-model"),
        codex_gateway_factory=gateway_factory,
    )

    bundle = factory.create_agents(run_id="codex_run", mode=PolicyKind.CODEX)
    agents = tuple(bundle.agents.values())
    asyncio.run(bundle.close())

    expected_company_ids = [company.company_id for company in DAIRY_S12_V3_SCENARIO.companies]
    assert gateway_factory.company_ids == expected_company_ids
    assert len({id(gateway) for gateway in gateway_factory.gateways}) == 12
    assert all(gateway.closed for gateway in gateway_factory.gateways)
    assert len({id(agent) for agent in agents}) == 12
    assert all(isinstance(agent, LlmCompanyAgent) for agent in agents)
    assert all(agent.metadata.kind is PolicyKind.CODEX for agent in agents)
    assert all(agent.metadata.provider == "codex" for agent in agents)
    assert all(agent.metadata.version == "3" for agent in agents)
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
        raise ModelInfrastructureError("provider unavailable")

    async def close(self) -> None:
        self.closed = True


class _AuditedOutputErrorGateway:
    """Return a schema failure that still identifies the completed model call."""

    async def generate_command(
        self,
        request: CommandModelRequest,
    ) -> CommandModelResult:
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
    turn = AgentTurn(
        turn_id="failed_run.farm_a.t1",
        company_id=first_observation.company_id,
        sim_time=SimTime(absolute_minute=540),
        state_version=0,
        wake_reasons=(WakeReason.DAY_OPEN,),
        observation=first_observation,
        available_cash=first_observation.cash,
    )
    agent = LlmCompanyAgent(
        run_id="failed_run",
        company_id=first_observation.company_id,
        gateway=_AuditedOutputErrorGateway(),
        audit_sink=repository,
        metadata=PolicyMetadata(
            name="codex-company-agent",
            version="3",
            kind=PolicyKind.CODEX,
            provider="codex",
            model="gpt-5.6-terra",
            prompt_version=COMMAND_PROMPT_VERSION,
        ),
    )

    with pytest.raises(ModelOutputError):
        asyncio.run(agent.act(turn))

    invocation = repository.list_invocations("failed_run")[0]
    assert invocation.request_id == "thread_failed"
    assert invocation.response_id == "turn_failed"
    assert invocation.usage.total_tokens == 17
    assert invocation.attempts == 2
    assert invocation.latency_ms == 123
    assert invocation.domain_turn_id == turn.turn_id
    assert invocation.sim_minute == turn.sim_time.absolute_minute
    assert invocation.prompt_version == COMMAND_PROMPT_VERSION


def test_infrastructure_failure_marks_job_failed_without_result() -> None:
    repository = MemoryRunRepository()
    gateway_factory = _RecordingGatewayFactory(_UnavailableGateway)
    factory = PolicyFactory(
        DAIRY_S12_V3_SCENARIO,
        repository,
        openai_config=_config(),
        gateway_factory=gateway_factory,
    )

    async def run_failed_job():
        coordinator = RunCoordinator(
            repository,
            factory,
            runtime=EpisodeRuntime(DAIRY_S12_V3_SCENARIO),
        )
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
    assert len(gateways) == 12
    assert len({id(gateway) for gateway in gateways}) == 12
    assert all(gateway.closed for gateway in gateways)
    assert all(len(gateway.command_requests) == 1 for gateway in gateways)
    assert {gateway.command_requests[0].turn.company_id for gateway in gateways} == {
        company.company_id for company in DAIRY_S12_V3_SCENARIO.companies
    }
