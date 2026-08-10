"""Construct isolated company policies for each benchmark run."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from company_bench.agents.company import (
    COMMAND_PROMPT_VERSION,
    BaselineCompanyAgent,
    CompanyAgent,
    LlmCompanyAgent,
    ReplayCompanyAgent,
)
from company_bench.agents.contracts import CommandGateway
from company_bench.agents.memory import AgentCheckpoint
from company_bench.agents.providers.capabilities import ModelCapabilityCatalog
from company_bench.agents.providers.newapi import (
    NewApiConfig,
    NewApiModelConfig,
    NewApiModelGateway,
)
from company_bench.domain.models import PolicyKind, PolicyMetadata, ScenarioSpec
from company_bench.runs.models import PolicyAuditSink, PolicyProfileView
from company_bench.runtime.models import TurnRecord

type NewApiGatewayFactory = Callable[[NewApiModelConfig], CommandGateway]

_MODEL_UNAVAILABLE = (
    "Model Agents are unavailable: run `start.cmd --configure-newapi`, then restart `start.cmd`"
)


class PolicyUnavailableError(RuntimeError):
    """The requested server-side policy profile is not configured."""


@dataclass(slots=True)
class AgentBundle:
    """Company actors and their independently owned model gateways."""

    agents: Mapping[str, CompanyAgent]
    _gateways: tuple[CommandGateway, ...] = ()

    async def close(self) -> None:
        """Release every company gateway and surface the first failure."""
        await _close_gateways(self)


class AgentFactory:
    """Create baseline, NewAPI model, or replay company Agents."""

    def __init__(
        self,
        scenario: ScenarioSpec,
        audit_sink: PolicyAuditSink,
        *,
        newapi_config: NewApiConfig | None = None,
        model_capabilities: ModelCapabilityCatalog | None = None,
        gateway_factory: NewApiGatewayFactory = NewApiModelGateway,
    ) -> None:
        if (newapi_config is None) != (model_capabilities is None):
            raise ValueError(
                "NewAPI configuration and model capabilities must be provided together"
            )
        self._scenario = scenario
        self._audit_sink = audit_sink
        self._newapi_config = newapi_config
        self._model_capabilities = model_capabilities
        self._gateway_factory = gateway_factory

    @property
    def scenario(self) -> ScenarioSpec:
        """Return the immutable scenario this factory serves."""
        return self._scenario

    def profiles(self) -> tuple[PolicyProfileView, ...]:
        """Return the three browser-safe company-policy choices."""
        config = self._newapi_config
        return (
            PolicyProfileView(
                mode=PolicyKind.BASELINE,
                label="Rule baseline",
                available=True,
                description="Transparent, deterministic operating rules.",
            ),
            PolicyProfileView(
                mode=PolicyKind.MODEL,
                label="Model agents via NewAPI",
                available=config is not None,
                provider="newapi",
                model=config.model if config else None,
                models=config.available_models if config else (),
                description="Each company is controlled by an independent model through NewAPI.",
                unavailable_reason=(
                    None
                    if config
                    else "The local NewAPI credential and model catalog are not configured."
                ),
            ),
            PolicyProfileView(
                mode=PolicyKind.REPLAY,
                label="Completed Run Replay",
                available=True,
                description="Deterministically replay a completed run without calling models.",
            ),
        )

    @property
    def policy_timeout_seconds(self) -> float:
        """Leave enough time for the configured gateway retries."""
        config = self._newapi_config
        return 5.0 if config is None else config.request_budget_seconds + 5

    def create_agents(
        self,
        *,
        run_id: str,
        mode: PolicyKind,
        model: str | None = None,
        source_turns: tuple[TurnRecord, ...] = (),
        checkpoints: tuple[AgentCheckpoint, ...] = (),
        completed_turns: tuple[TurnRecord, ...] = (),
    ) -> AgentBundle:
        """Create fresh event-driven company actors for one episode."""
        if mode is not PolicyKind.MODEL and model is not None:
            raise ValueError("model is only valid for Model Agents")
        if mode is PolicyKind.BASELINE:
            return AgentBundle(
                {company.company_id: BaselineCompanyAgent() for company in self._scenario.companies}
            )
        if mode is PolicyKind.REPLAY:
            return self._replay_agents(source_turns, completed_turns)
        if mode is not PolicyKind.MODEL:
            raise ValueError(f"unsupported policy mode: {mode.value}")

        config = self._model_config(model)
        metadata = _model_metadata(config)
        checkpoint_by_company = {checkpoint.company_id: checkpoint for checkpoint in checkpoints}
        agents: dict[str, CompanyAgent] = {}
        gateways: list[CommandGateway] = []
        for company in self._scenario.companies:
            gateway = self._gateway_factory(config)
            gateways.append(gateway)
            agents[company.company_id] = LlmCompanyAgent(
                run_id=run_id,
                company_id=company.company_id,
                gateway=gateway,
                audit_sink=self._audit_sink,
                metadata=metadata,
                checkpoint=checkpoint_by_company.get(company.company_id),
                memory_token_budget=self._scenario.runtime.compaction_trigger_tokens,
            )
        return AgentBundle(agents, tuple(gateways))

    async def ensure_available(self, mode: PolicyKind, model: str | None = None) -> None:
        """Calibrate and reject unavailable model profiles before queueing a run."""
        if mode is PolicyKind.MODEL:
            config, capabilities = self._newapi_dependencies()
            await capabilities.ensure(config.require_model(model))
        elif model is not None:
            raise ValueError("model is only valid for Model Agents")

    def _model_config(self, model: str | None) -> NewApiModelConfig:
        """Resolve one allowed NewAPI model without exposing credentials."""
        config, capabilities = self._newapi_dependencies()
        selected = config.require_model(model)
        capability = capabilities.require(selected)
        return config.select_model(
            selected,
            max_output_tokens=capability.max_output_tokens,
        )

    def _newapi_dependencies(self) -> tuple[NewApiConfig, ModelCapabilityCatalog]:
        """Return the complete configured NewAPI seam or one availability error."""
        if self._newapi_config is None or self._model_capabilities is None:
            raise PolicyUnavailableError(_MODEL_UNAVAILABLE)
        return self._newapi_config, self._model_capabilities

    def _replay_agents(
        self,
        source_turns: tuple[TurnRecord, ...],
        completed_turns: tuple[TurnRecord, ...],
    ) -> AgentBundle:
        """Build replay actors at the persisted per-company cursor."""
        if not source_turns:
            raise ValueError("Completed Run Replay requires a source turn journal")
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


def _model_metadata(config: NewApiModelConfig) -> PolicyMetadata:
    """Build auditable metadata for one selected NewAPI model."""
    return PolicyMetadata(
        name="model-company-agent",
        kind=PolicyKind.MODEL,
        provider="newapi",
        model=config.model,
        prompt_version=COMMAND_PROMPT_VERSION,
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
