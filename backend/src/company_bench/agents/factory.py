"""Construct isolated company policies for each benchmark run."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from company_bench.agents.company import (
    DECISION_PROMPT_VERSION,
    BaselineCompanyAgent,
    CompanyAgent,
    LlmCompanyAgent,
    ReplayCompanyAgent,
)
from company_bench.agents.contracts import DecisionGateway
from company_bench.agents.memory import AgentCheckpoint
from company_bench.agents.providers.capabilities import ModelCapabilityCatalog
from company_bench.agents.providers.newapi import (
    NewApiConfig,
    NewApiModelConfig,
    NewApiWireProtocol,
)
from company_bench.domain.models import (
    PolicyKind,
    PolicyMetadata,
    PolicyProfileId,
    ScenarioSpec,
)
from company_bench.runs.models import PolicyAuditSink, PolicyProfileView
from company_bench.runtime.models import TurnRecord

_MODEL_UNAVAILABLE = "The selected model policy profile is not configured"


class PolicyUnavailableError(RuntimeError):
    """The requested server-side policy profile is not configured."""


class ModelGatewayFactory(Protocol):
    """Construct gateways while exposing their exact auditable adapter identity."""

    wire_protocol: NewApiWireProtocol
    adapter_version: str

    def __call__(self, config: NewApiModelConfig) -> DecisionGateway:
        """Create one isolated company gateway."""
        ...


@dataclass(slots=True)
class AgentBundle:
    """Company actors and their independently owned model gateways."""

    agents: Mapping[str, CompanyAgent]
    _gateways: tuple[DecisionGateway, ...] = ()

    async def close(self) -> None:
        """Release every company gateway and surface the first failure."""
        await _close_gateways(self)


@dataclass(frozen=True, slots=True)
class ResolvedPolicy:
    """One calibrated policy identity and its optional model configuration."""

    profile_id: PolicyProfileId
    metadata: PolicyMetadata
    model_config: NewApiModelConfig | None = None

    def __post_init__(self) -> None:
        if self.metadata.profile_id is not self.profile_id:
            raise ValueError("resolved policy metadata belongs to another profile")
        if self.metadata.kind is not self.profile_id.kind:
            raise ValueError("resolved policy kind must match its profile")
        model_backed = self.profile_id.kind is PolicyKind.MODEL
        if model_backed != (self.model_config is not None):
            raise ValueError("resolved model profiles require one model configuration")
        if self.model_config is not None and self.metadata.model != self.model_config.model:
            raise ValueError("resolved policy model identity is inconsistent")


@dataclass(frozen=True, slots=True)
class ModelPolicyProfile:
    """One configured model profile behind the provider-neutral Agent seam."""

    profile_id: PolicyProfileId
    label: str
    description: str
    gateway_factory: ModelGatewayFactory
    config: NewApiConfig | None = None
    capabilities: ModelCapabilityCatalog | None = None

    def __post_init__(self) -> None:
        if self.profile_id.kind is not PolicyKind.MODEL:
            raise ValueError("ModelPolicyProfile requires a model profile_id")
        if not self.gateway_factory.adapter_version:
            raise ValueError("model gateway adapter version cannot be empty")
        if (self.config is None) != (self.capabilities is None):
            raise ValueError("profile configuration and capabilities must be provided together")

    @property
    def available(self) -> bool:
        """Return whether this profile has complete server-side dependencies."""
        return self.config is not None

    def view(self) -> PolicyProfileView:
        """Project browser-safe profile metadata without credentials."""
        return PolicyProfileView(
            profile_id=self.profile_id,
            kind=PolicyKind.MODEL,
            label=self.label,
            available=self.available,
            provider="newapi",
            model=self.config.model if self.config else None,
            models=self.config.available_models if self.config else (),
            description=self.description,
            unavailable_reason=None if self.available else _MODEL_UNAVAILABLE,
        )

    async def resolve(self, model: str | None) -> NewApiModelConfig:
        """Calibrate and resolve one selected model and output limit."""
        config, capabilities = self.dependencies()
        selected = config.require_model(model)
        capability = await capabilities.ensure(selected)
        return config.select_model(selected, max_output_tokens=capability.max_output_tokens)

    def dependencies(self) -> tuple[NewApiConfig, ModelCapabilityCatalog]:
        """Return complete profile dependencies or one stable availability error."""
        if self.config is None or self.capabilities is None:
            raise PolicyUnavailableError(f"{_MODEL_UNAVAILABLE}: {self.profile_id.value}")
        return self.config, self.capabilities


class AgentFactory:
    """Resolve stable policy profiles into isolated company Agents."""

    def __init__(
        self,
        scenario: ScenarioSpec,
        audit_sink: PolicyAuditSink,
        *,
        model_profiles: tuple[ModelPolicyProfile, ...] = (),
    ) -> None:
        self._scenario = scenario
        self._audit_sink = audit_sink
        self._model_profiles = {profile.profile_id: profile for profile in model_profiles}
        if len(self._model_profiles) != len(model_profiles):
            raise ValueError("model profile IDs must be unique")

    @property
    def scenario(self) -> ScenarioSpec:
        """Return the immutable scenario this factory serves."""
        return self._scenario

    def profiles(self) -> tuple[PolicyProfileView, ...]:
        """Return stable browser-safe policy choices in registry order."""
        return (
            PolicyProfileView(
                profile_id=PolicyProfileId.BASELINE,
                kind=PolicyKind.BASELINE,
                label="Rule baseline",
                available=True,
                description="Transparent, deterministic operating rules.",
            ),
            *(profile.view() for profile in self._model_profiles.values()),
            PolicyProfileView(
                profile_id=PolicyProfileId.REPLAY,
                kind=PolicyKind.REPLAY,
                label="Exact Replay",
                available=True,
                description="Deterministically replay a completed run without calling models.",
            ),
        )

    @property
    def policy_timeout_seconds(self) -> float:
        """Leave enough time for the configured gateway retries."""
        budgets = (
            profile.config.request_budget_seconds
            for profile in self._model_profiles.values()
            if profile.config is not None
        )
        return max(budgets, default=0) + 5

    def create_agents(
        self,
        *,
        run_id: str,
        policy: ResolvedPolicy,
        source_turns: tuple[TurnRecord, ...] = (),
        checkpoints: tuple[AgentCheckpoint, ...] = (),
        completed_turns: tuple[TurnRecord, ...] = (),
    ) -> AgentBundle:
        """Create fresh event-driven company actors for one episode."""
        profile_id = policy.profile_id
        if profile_id is PolicyProfileId.BASELINE:
            return AgentBundle(
                {company.company_id: BaselineCompanyAgent() for company in self._scenario.companies}
            )
        if profile_id is PolicyProfileId.REPLAY:
            return self._replay_agents(source_turns, completed_turns)

        profile = self._model_profile(profile_id)
        config = policy.model_config
        if config is None:
            raise ValueError("resolved model policy has no model configuration")
        checkpoint_by_company = {checkpoint.company_id: checkpoint for checkpoint in checkpoints}
        agents: dict[str, CompanyAgent] = {}
        gateways: list[DecisionGateway] = []
        for company in self._scenario.companies:
            gateway = profile.gateway_factory(config)
            gateways.append(gateway)
            agents[company.company_id] = LlmCompanyAgent(
                run_id=run_id,
                company_id=company.company_id,
                gateway=gateway,
                audit_sink=self._audit_sink,
                metadata=policy.metadata,
                checkpoint=checkpoint_by_company.get(company.company_id),
                memory_token_budget=self._scenario.runtime.compaction_trigger_tokens,
            )
        return AgentBundle(agents, tuple(gateways))

    async def resolve_policy(
        self,
        profile_id: PolicyProfileId,
        model: str | None = None,
    ) -> ResolvedPolicy:
        """Calibrate once and return the complete policy used by one run."""
        if profile_id.kind is PolicyKind.MODEL:
            profile = self._model_profile(profile_id)
            config = await profile.resolve(model)
            return ResolvedPolicy(
                profile_id=profile_id,
                metadata=_model_metadata(profile, config),
                model_config=config,
            )
        if model is not None:
            raise ValueError("model is only valid for Model Agents")
        return ResolvedPolicy(
            profile_id=profile_id,
            metadata=PolicyMetadata(
                name=(
                    "event-baseline" if profile_id is PolicyProfileId.BASELINE else "turn-replay"
                ),
                kind=profile_id.kind,
                profile_id=profile_id,
            ),
        )

    def _model_profile(self, profile_id: PolicyProfileId) -> ModelPolicyProfile:
        """Return one registered model profile without fallback routing."""
        try:
            return self._model_profiles[profile_id]
        except KeyError as error:
            raise PolicyUnavailableError(f"{_MODEL_UNAVAILABLE}: {profile_id.value}") from error

    def _replay_agents(
        self,
        source_turns: tuple[TurnRecord, ...],
        completed_turns: tuple[TurnRecord, ...],
    ) -> AgentBundle:
        """Build replay actors at the persisted per-company cursor."""
        if not source_turns:
            raise ValueError("Exact Replay requires a completed source turn journal")
        completed_by_company = {
            company.company_id: sum(
                record.turn.company_id == company.company_id for record in completed_turns
            )
            for company in self._scenario.companies
        }
        return AgentBundle(
            {
                company.company_id: ReplayCompanyAgent(
                    company.company_id,
                    source_turns,
                    completed_turns=completed_by_company[company.company_id],
                )
                for company in self._scenario.companies
            }
        )


def _model_metadata(
    profile: ModelPolicyProfile,
    config: NewApiModelConfig,
) -> PolicyMetadata:
    """Build auditable metadata for one selected NewAPI model."""
    return PolicyMetadata(
        name="model-company-agent",
        kind=PolicyKind.MODEL,
        profile_id=profile.profile_id,
        provider="newapi",
        model=config.model,
        wire_protocol=profile.gateway_factory.wire_protocol.value,
        adapter_version=profile.gateway_factory.adapter_version,
        prompt_version=DECISION_PROMPT_VERSION,
        config_fingerprint=config.fingerprint,
    )


async def _close_gateways(bundle: AgentBundle) -> None:
    """Close a bundle's gateways once and surface the first failure."""
    gateways, bundle._gateways = bundle._gateways, ()
    outcomes = await asyncio.gather(
        *(gateway.close() for gateway in gateways),
        return_exceptions=True,
    )
    failure = next(
        (outcome for outcome in outcomes if isinstance(outcome, BaseException)),
        None,
    )
    if failure is not None:
        raise failure
