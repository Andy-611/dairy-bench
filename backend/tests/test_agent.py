"""Tests for one-Agent-per-company policy orchestration."""

import asyncio
from collections.abc import Callable
from decimal import Decimal

import pytest
from pydantic import SecretStr

from company_bench.agents.company import DECISION_PROMPT_VERSION, LlmCompanyAgent
from company_bench.agents.contracts import (
    DecisionGateway,
    DecisionModelRequest,
    DecisionModelResult,
    ModelCompatibilityError,
    ModelConfigurationError,
    ModelInfrastructureError,
    ModelOutputError,
    ModelQuotaExhaustedError,
)
from company_bench.agents.factory import AgentFactory, ModelGatewayFactory, ModelPolicyProfile
from company_bench.agents.providers.capabilities import ModelCapabilityCatalog
from company_bench.agents.providers.newapi import (
    NewApiConfig,
    NewApiModelConfig,
    NewApiWireProtocol,
)
from company_bench.domain.models import (
    CompanyObservation,
    PolicyKind,
    PolicyMetadata,
    PolicyProfileId,
    ProtocolIssueKind,
)
from company_bench.domain.scenario import DAIRY_S9_SCENARIO
from company_bench.runs.coordinator import RunCoordinator
from company_bench.runs.models import (
    InvocationOutcome,
    RunJob,
    RunStatus,
    RunStopReason,
    TokenUsage,
)
from company_bench.runtime.episode import EpisodeRuntime
from company_bench.runtime.models import (
    PROTOCOL_ERROR_PREFIX,
    AgentTurn,
    AttentionPlan,
    DecisionEnvelope,
    DecisionOutcome,
    DecisionStatus,
    IdleDecision,
    RejectionCategory,
    TurnRecord,
    WakeReason,
)
from company_bench.storage.memory import InMemoryRunRepository
from tests.support.fakes import (
    ScriptedDecisionGateway,
    company_decision,
    model_capability_catalog,
)


def _config() -> NewApiConfig:
    """Return a no-network configuration for injected test gateways."""
    return NewApiConfig(
        api_key=SecretStr("test-key"),
        model="gpt-test-default",
        models=("gpt-test-default", "gemini-test-selected"),
        max_attempts=1,
    )


def _capabilities() -> ModelCapabilityCatalog:
    """Return confirmed limits for both injected test models."""
    return model_capability_catalog(
        {
            "gpt-test-default": 128_000,
            "gemini-test-selected": 65_536,
        }
    )


def _model_profile(
    gateway_factory: ModelGatewayFactory,
    *,
    profile_id: PolicyProfileId = PolicyProfileId.NEWAPI_MODEL,
) -> ModelPolicyProfile:
    """Build one configured generic NewAPI profile for Agent tests."""
    return ModelPolicyProfile(
        profile_id=profile_id,
        label=profile_id.value,
        description="test profile",
        gateway_factory=gateway_factory,
        config=_config(),
        capabilities=_capabilities(),
    )


@pytest.mark.parametrize(
    "selected_profile",
    (
        PolicyProfileId.NEWAPI_MODEL,
        PolicyProfileId.NEWAPI_CODEX,
        PolicyProfileId.NEWAPI_CLAUDE_CODE,
    ),
)
def test_model_profile_registry_routes_only_to_the_selected_adapter(
    selected_profile: PolicyProfileId,
) -> None:
    repository = InMemoryRunRepository()
    factories = {
        profile_id: _RecordingGatewayFactory()
        for profile_id in (
            PolicyProfileId.NEWAPI_MODEL,
            PolicyProfileId.NEWAPI_CODEX,
            PolicyProfileId.NEWAPI_CLAUDE_CODE,
        )
    }
    factory = AgentFactory(
        DAIRY_S9_SCENARIO,
        repository,
        model_profiles=tuple(
            _model_profile(gateway_factory, profile_id=profile_id)
            for profile_id, gateway_factory in factories.items()
        ),
    )

    policy = asyncio.run(factory.resolve_policy(selected_profile, "gpt-test-default"))
    bundle = factory.create_agents(
        run_id="profile_route",
        policy=policy,
    )
    asyncio.run(bundle.close())

    expected_count = len(DAIRY_S9_SCENARIO.companies)
    assert len(factories[selected_profile].gateways) == expected_count
    assert all(
        not gateway_factory.gateways
        for profile_id, gateway_factory in factories.items()
        if profile_id is not selected_profile
    )
    assert {agent.metadata.profile_id for agent in bundle.agents.values()} == {selected_profile}


class _TrackedScriptedGateway(ScriptedDecisionGateway):
    """Expose adapter closure for lifecycle assertions."""

    def __init__(self) -> None:
        super().__init__(lambda _: company_decision())
        self.closed = False

    async def close(self) -> None:
        """Record closure of this company-owned gateway."""
        self.closed = True
        await super().close()


class _RecordingGatewayFactory:
    """Create and retain one observable gateway per company Agent."""

    wire_protocol = NewApiWireProtocol.CHAT_COMPLETIONS
    adapter_version = "test-chat-completions-v1"

    def __init__(self, create: Callable[[], DecisionGateway] = _TrackedScriptedGateway) -> None:
        self._create = create
        self.configs: list[NewApiModelConfig] = []
        self.gateways: list[DecisionGateway] = []

    def __call__(self, config: NewApiModelConfig) -> DecisionGateway:
        """Create a fresh gateway for one company Agent."""
        gateway = self._create()
        self.configs.append(config)
        self.gateways.append(gateway)
        return gateway


def test_model_mode_owns_nine_independent_newapi_gateways() -> None:
    repository = InMemoryRunRepository()
    gateway_factory = _RecordingGatewayFactory()
    factory = AgentFactory(
        DAIRY_S9_SCENARIO,
        repository,
        model_profiles=(_model_profile(gateway_factory),),
    )

    policy = asyncio.run(
        factory.resolve_policy(PolicyProfileId.NEWAPI_MODEL, "gemini-test-selected")
    )
    bundle = factory.create_agents(
        run_id="model_run",
        policy=policy,
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
    assert all(
        agent.metadata.wire_protocol == gateway_factory.wire_protocol.value for agent in agents
    )
    assert all(
        agent.metadata.adapter_version == gateway_factory.adapter_version for agent in agents
    )
    assert all(agent.metadata.prompt_version == DECISION_PROMPT_VERSION for agent in agents)


class _UnavailableGateway:
    """Fail every model call as provider infrastructure."""

    def __init__(self) -> None:
        self.closed = False
        self.decision_requests: list[DecisionModelRequest] = []

    async def generate_decision(
        self,
        request: DecisionModelRequest,
    ) -> DecisionModelResult:
        self.decision_requests.append(request)
        raise ModelInfrastructureError(
            "provider unavailable",
            attempts=3,
            latency_ms=42,
        )

    async def close(self) -> None:
        self.closed = True


class _IncompatibleGateway(_UnavailableGateway):
    """Reject the benchmark's required decision protocol."""

    async def generate_decision(
        self,
        request: DecisionModelRequest,
    ) -> DecisionModelResult:
        self.decision_requests.append(request)
        raise ModelCompatibilityError("required function call is unsupported")


class _MisconfiguredGateway(_UnavailableGateway):
    """Reject a permanent provider configuration."""

    async def generate_decision(
        self,
        request: DecisionModelRequest,
    ) -> DecisionModelResult:
        self.decision_requests.append(request)
        raise ModelConfigurationError("credential rejected")


class _QuotaExhaustedGateway(_UnavailableGateway):
    """Reject a request whose paid provider balance is exhausted."""

    async def generate_decision(
        self,
        request: DecisionModelRequest,
    ) -> DecisionModelResult:
        self.decision_requests.append(request)
        raise ModelQuotaExhaustedError("paid quota exhausted")


class _BrokenGateway(_UnavailableGateway):
    """Raise an unclassified implementation failure."""

    async def generate_decision(
        self,
        request: DecisionModelRequest,
    ) -> DecisionModelResult:
        self.decision_requests.append(request)
        raise RuntimeError("adapter bug")


class _AuditedOutputErrorGateway:
    """Return a schema failure that still identifies the completed model call."""

    async def generate_decision(
        self,
        request: DecisionModelRequest,
    ) -> DecisionModelResult:
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


def test_output_failure_retains_provider_audit_and_binds_protocol_outcome(
    first_observation: CompanyObservation,
) -> None:
    repository = InMemoryRunRepository()
    turn = AgentTurn(
        turn_id="failed_run.farm_a.t1",
        company_id=first_observation.company_id,
        sim_day=first_observation.sim_day,
        state_version=0,
        turn_number_this_week=1,
        turn_limit_this_week=first_observation.runtime.max_turns_per_company_week,
        wake_reasons=(WakeReason.WEEK_OPEN,),
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
            profile_id=PolicyProfileId.NEWAPI_MODEL,
            provider="newapi",
            model="gpt-test",
            wire_protocol="chat_completions",
            adapter_version="newapi-chat-completions-v1",
            config_fingerprint="test-config",
            prompt_version=DECISION_PROMPT_VERSION,
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
    assert invocation.absolute_day == turn.sim_day.absolute_day
    assert invocation.prompt_version == DECISION_PROMPT_VERSION

    protocol_error = "ModelOutputError: invalid output"
    decision = IdleDecision(attention=AttentionPlan())
    envelope = DecisionEnvelope(
        turn_id=turn.turn_id,
        decision_id="failed_decision",
        company_id=turn.company_id,
        issued_on=turn.sim_day,
        state_version=turn.state_version,
        decision=decision,
    )
    outcome = DecisionOutcome(
        turn_id=turn.turn_id,
        decision_id=envelope.decision_id,
        company_id=turn.company_id,
        occurred_on=turn.sim_day,
        status=DecisionStatus.REJECTED,
        accepted=False,
        rejection_category=RejectionCategory.PROTOCOL,
        reason=f"{PROTOCOL_ERROR_PREFIX}{protocol_error}",
        resulting_state_version=turn.state_version,
        apply_sequence=1,
        next_available_on=turn.sim_day.plus_days(1),
    )
    agent.remember(
        TurnRecord(
            run_id="failed_run",
            turn=turn,
            envelope=envelope,
            outcome=outcome,
            observation_hash="failed_observation",
            protocol_issue_kind=ProtocolIssueKind.INVALID_RESPONSE,
            protocol_error=protocol_error,
        )
    )

    completed = repository.list_invocations("failed_run")[0]
    assert completed.decision == decision
    assert completed.decision_outcome == outcome
    assert completed.apply_sequence == outcome.apply_sequence


async def _run_model_job_until(
    repository: InMemoryRunRepository,
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
            profile_id=PolicyProfileId.NEWAPI_MODEL,
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
    repository = InMemoryRunRepository()
    gateway_factory = _RecordingGatewayFactory(_UnavailableGateway)
    factory = AgentFactory(
        DAIRY_S9_SCENARIO,
        repository,
        model_profiles=(_model_profile(gateway_factory),),
    )

    interrupted = asyncio.run(_run_model_job_until(repository, factory, RunStatus.INTERRUPTED))

    assert interrupted.status is RunStatus.INTERRUPTED
    assert interrupted.current_absolute_day == 0
    assert interrupted.prompt_version == DECISION_PROMPT_VERSION
    assert "PolicyInfrastructureError" in (interrupted.error_message or "")
    assert repository.get(interrupted.run_id) is None
    assert repository.load_recovery(interrupted.run_id) is not None
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
    assert all(len(gateway.decision_requests) == 1 for gateway in gateways)
    assert {gateway.decision_requests[0].turn.company_id for gateway in gateways} == {
        company.company_id for company in DAIRY_S9_SCENARIO.companies
    }


def test_quota_exhaustion_stops_job_and_prompt_drift_blocks_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryRunRepository()
    factory = AgentFactory(
        DAIRY_S9_SCENARIO,
        repository,
        model_profiles=(_model_profile(_RecordingGatewayFactory(_QuotaExhaustedGateway)),),
    )

    stopped = asyncio.run(_run_model_job_until(repository, factory, RunStatus.STOPPED))

    assert stopped.stop_reason is RunStopReason.QUOTA_EXHAUSTED
    assert "PolicyQuotaExhaustedError" in (stopped.error_message or "")
    assert repository.get(stopped.run_id) is None
    assert repository.load_recovery(stopped.run_id) is not None
    assert all(
        invocation.outcome is InvocationOutcome.INFRASTRUCTURE_ERROR
        for invocation in repository.list_invocations(stopped.run_id)
    )

    monkeypatch.setattr(
        "company_bench.agents.factory.DECISION_PROMPT_VERSION",
        "decision-prompt-test-drift",
    )

    async def resume_with_drift() -> None:
        coordinator = RunCoordinator(
            repository,
            factory,
            runtime=EpisodeRuntime(DAIRY_S9_SCENARIO),
        )
        try:
            await coordinator.resume(stopped.run_id)
        finally:
            await coordinator.close()

    with pytest.raises(ValueError, match="policy profile differs"):
        asyncio.run(resume_with_drift())


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
    gateway_type: Callable[[], DecisionGateway],
    expected_error: str,
    outcome: InvocationOutcome,
) -> None:
    repository = InMemoryRunRepository()
    gateway_factory = _RecordingGatewayFactory(gateway_type)
    factory = AgentFactory(
        DAIRY_S9_SCENARIO,
        repository,
        model_profiles=(_model_profile(gateway_factory),),
    )

    failed = asyncio.run(_run_model_job_until(repository, factory, RunStatus.FAILED))

    assert failed.status is RunStatus.FAILED
    assert expected_error in (failed.error_message or "")
    assert repository.get(failed.run_id) is None
    assert repository.load_recovery(failed.run_id) is not None
    assert all(
        invocation.outcome is outcome for invocation in repository.list_invocations(failed.run_id)
    )
