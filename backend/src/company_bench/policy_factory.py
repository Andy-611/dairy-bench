"""Construct fresh, isolated company policies for each benchmark run."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from company_bench.agent_gateway import OpenAIAgentConfig, OpenAIModelGateway
from company_bench.agent_models import CompanyModelGateway
from company_bench.agent_policy import PROMPT_VERSION, LlmCompanyPolicy, ReplayPolicy
from company_bench.agents import (
    COMMAND_PROMPT_VERSION,
    BaselineCompanyAgent,
    CompanyAgent,
    LlmCompanyAgent,
    ReplayCompanyAgent,
)
from company_bench.codex_gateway import CodexAgentConfig, CodexModelGateway
from company_bench.memory import AgentCheckpoint
from company_bench.models import (
    EpisodeResult,
    PolicyKind,
    PolicyMetadata,
    ScenarioSpec,
)
from company_bench.policies import BaselinePolicy, CompanyPolicy
from company_bench.run_models import PolicyAuditSink, PolicyProfileView
from company_bench.runtime_models import TurnRecord

type OpenAIGatewayFactory = Callable[[OpenAIAgentConfig], CompanyModelGateway]
type CodexGatewayFactory = Callable[[CodexAgentConfig, str], CompanyModelGateway]
type CompanyGatewayFactory = Callable[[str], CompanyModelGateway]
# Each call must return a fresh gateway owned by exactly one company.
_OPENAI_UNAVAILABLE = "OpenAI Agent is unavailable: configure OPENAI_API_KEY on the backend"
_CODEX_UNAVAILABLE = (
    "Codex Agent is unavailable: run `codex login`, then set "
    "DAIRY_BENCH_CODEX_ENABLED=true before starting the backend"
)


class PolicyUnavailableError(RuntimeError):
    """The requested server-side policy profile is not configured."""


@dataclass(slots=True)
class PolicyBundle:
    """Policies for one episode and their independently owned gateways."""

    policies: Mapping[str, CompanyPolicy]
    _gateways: tuple[CompanyModelGateway, ...] = ()

    async def close(self) -> None:
        """Release every company gateway even when they close concurrently."""
        await _close_gateways(self)


@dataclass(slots=True)
class AgentBundle:
    """V3 company actors and their independently owned provider gateways."""

    agents: Mapping[str, CompanyAgent]
    _gateways: tuple[CompanyModelGateway, ...] = ()

    async def close(self) -> None:
        """Release every company gateway even when they close concurrently."""
        await _close_gateways(self)


class PolicyFactory:
    """Create an isolated controller for every configured company."""

    def __init__(
        self,
        scenario: ScenarioSpec,
        audit_sink: PolicyAuditSink,
        *,
        codex_config: CodexAgentConfig | None = None,
        openai_config: OpenAIAgentConfig | None = None,
        codex_gateway_factory: CodexGatewayFactory = CodexModelGateway,
        gateway_factory: OpenAIGatewayFactory = OpenAIModelGateway,
    ) -> None:
        self._scenario = scenario
        self._audit_sink = audit_sink
        self._codex_config = codex_config
        self._codex_gateway_factory = codex_gateway_factory
        self._openai_config = openai_config
        self._gateway_factory = gateway_factory

    @property
    def scenario(self) -> ScenarioSpec:
        """Return the immutable scenario this factory serves."""
        return self._scenario

    def profiles(self) -> tuple[PolicyProfileView, ...]:
        """Return browser-safe policy choices."""
        codex = self._codex_config
        openai = self._openai_config
        return (
            PolicyProfileView(
                mode=PolicyKind.BASELINE,
                label="Rule baseline",
                available=True,
                description="Transparent, deterministic operating rules.",
            ),
            PolicyProfileView(
                mode=PolicyKind.CODEX,
                label="Codex company agents",
                available=codex is not None,
                provider="codex",
                model=codex.model if codex else None,
                reasoning_effort=codex.reasoning_effort if codex else None,
                description="Each company is controlled by an independent Codex runtime.",
                unavailable_reason=(
                    None if codex else "Log in to Codex and enable DAIRY_BENCH_CODEX_ENABLED."
                ),
            ),
            PolicyProfileView(
                mode=PolicyKind.OPENAI,
                label="OpenAI company agents",
                available=openai is not None,
                provider="openai",
                model=openai.model if openai else None,
                reasoning_effort=openai.reasoning_effort if openai else None,
                description="Each company is controlled by an independent LLM agent.",
                unavailable_reason=(
                    None if openai else "OPENAI_API_KEY is not configured on the backend."
                ),
            ),
            PolicyProfileView(
                mode=PolicyKind.REPLAY,
                label="Exact replay",
                available=True,
                description="Replay a historical run exactly from its source_run_id.",
            ),
        )

    @property
    def policy_timeout_seconds(self) -> float:
        """Leave enough time for the gateway to exhaust its own retries."""
        configured = tuple(
            config for config in (self._codex_config, self._openai_config) if config is not None
        )
        return max(
            (
                5.0,
                *(
                    _policy_timeout(config.timeout_seconds, config.max_attempts)
                    for config in configured
                ),
            )
        )

    def create(
        self,
        *,
        run_id: str,
        mode: PolicyKind,
        source: EpisodeResult | None = None,
    ) -> PolicyBundle:
        """Create daily policies for an explicit pre-V2 scenario."""
        if self._scenario.version >= 2:
            raise ValueError("daily policies do not implement event-driven V3 scenarios")
        if mode is PolicyKind.BASELINE:
            return PolicyBundle(
                {company.company_id: BaselinePolicy() for company in self._scenario.companies}
            )
        if mode is PolicyKind.CODEX:
            return self._codex_bundle(run_id)
        if mode is PolicyKind.OPENAI:
            return self._openai_bundle(run_id)
        if source is None:
            raise ValueError("replay mode requires a completed source run")
        if source.scenario != self._scenario:
            raise ValueError("source run uses a different scenario")
        metadata = PolicyMetadata(
            name="recorded-replay",
            kind=PolicyKind.REPLAY,
            source_run_id=source.run_id,
        )
        return PolicyBundle(
            {
                company.company_id: ReplayPolicy(
                    company.company_id,
                    source.decisions,
                    metadata,
                )
                for company in self._scenario.companies
            }
        )

    def create_agents(
        self,
        *,
        run_id: str,
        mode: PolicyKind,
        source_turns: tuple[TurnRecord, ...] = (),
        checkpoints: tuple[AgentCheckpoint, ...] = (),
        completed_turns: tuple[TurnRecord, ...] = (),
    ) -> AgentBundle:
        """Create fresh event-driven company actors for one V3 episode."""
        if self._scenario.version != 3:
            raise ValueError("event-driven Agents require a V3 scenario")
        if mode is PolicyKind.BASELINE:
            return AgentBundle(
                {company.company_id: BaselineCompanyAgent() for company in self._scenario.companies}
            )
        if mode is PolicyKind.REPLAY:
            if not source_turns:
                raise ValueError("V3 replay requires a source turn journal")
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
        metadata = self._agent_metadata(mode)
        checkpoint_by_company = {checkpoint.company_id: checkpoint for checkpoint in checkpoints}
        gateway_factory: CompanyGatewayFactory
        if mode is PolicyKind.OPENAI:
            config = self._openai_config
            if config is None:
                raise PolicyUnavailableError(_OPENAI_UNAVAILABLE)

            def gateway_factory(_: str) -> CompanyModelGateway:
                return self._gateway_factory(config)

        elif mode is PolicyKind.CODEX:
            config = self._codex_config
            if config is None:
                raise PolicyUnavailableError(_CODEX_UNAVAILABLE)

            def gateway_factory(company_id: str) -> CompanyModelGateway:
                return self._codex_gateway_factory(config, company_id)

        else:
            raise ValueError(f"unsupported V3 policy mode: {mode.value}")

        agents: dict[str, CompanyAgent] = {}
        gateways: list[CompanyModelGateway] = []
        for company in self._scenario.companies:
            gateway = gateway_factory(company.company_id)
            gateways.append(gateway)
            agents[company.company_id] = LlmCompanyAgent(
                run_id=run_id,
                company_id=company.company_id,
                gateway=gateway,
                audit_sink=self._audit_sink,
                metadata=metadata,
                checkpoint=checkpoint_by_company.get(company.company_id),
                memory_token_budget=self._scenario.runtime.compaction_trigger_tokens,
                max_prompt_tokens=self._scenario.runtime.max_prompt_tokens,
            )
        return AgentBundle(agents, tuple(gateways))

    def _agent_metadata(self, mode: PolicyKind) -> PolicyMetadata:
        """Build provider metadata shared by equivalent V3 company Agents."""
        if mode is PolicyKind.OPENAI and self._openai_config is not None:
            config = self._openai_config
            return PolicyMetadata(
                name="llm-company-agent",
                version="3",
                kind=mode,
                provider="openai",
                model=config.model,
                prompt_version=COMMAND_PROMPT_VERSION,
                config_fingerprint=config.fingerprint,
            )
        if mode is PolicyKind.CODEX and self._codex_config is not None:
            config = self._codex_config
            return PolicyMetadata(
                name="codex-company-agent",
                version="3",
                kind=mode,
                provider="codex",
                model=config.model,
                prompt_version=COMMAND_PROMPT_VERSION,
                config_fingerprint=config.fingerprint,
            )
        raise PolicyUnavailableError(
            _OPENAI_UNAVAILABLE if mode is PolicyKind.OPENAI else _CODEX_UNAVAILABLE
        )

    def _openai_bundle(self, run_id: str) -> PolicyBundle:
        """Build one isolated policy and provider client per company."""
        config = self._openai_config
        if config is None:
            raise PolicyUnavailableError(_OPENAI_UNAVAILABLE)
        metadata = PolicyMetadata(
            name="llm-company-agent",
            kind=PolicyKind.OPENAI,
            provider="openai",
            model=config.model,
            prompt_version=PROMPT_VERSION,
            config_fingerprint=config.fingerprint,
        )
        return self._agent_bundle(
            run_id,
            metadata,
            lambda _: self._gateway_factory(config),
        )

    def _codex_bundle(self, run_id: str) -> PolicyBundle:
        """Build one isolated policy and Codex runtime per company."""
        config = self._codex_config
        if config is None:
            raise PolicyUnavailableError(_CODEX_UNAVAILABLE)
        metadata = PolicyMetadata(
            name="codex-company-agent",
            kind=PolicyKind.CODEX,
            provider="codex",
            model=config.model,
            prompt_version=PROMPT_VERSION,
            config_fingerprint=config.fingerprint,
        )
        return self._agent_bundle(
            run_id,
            metadata,
            lambda company_id: self._codex_gateway_factory(config, company_id),
        )

    def _agent_bundle(
        self,
        run_id: str,
        metadata: PolicyMetadata,
        gateway_factory: CompanyGatewayFactory,
    ) -> PolicyBundle:
        """Build one isolated LLM policy and gateway per company."""
        policies: dict[str, CompanyPolicy] = {}
        gateways: list[CompanyModelGateway] = []
        for company in self._scenario.companies:
            gateway = gateway_factory(company.company_id)
            gateways.append(gateway)
            policies[company.company_id] = LlmCompanyPolicy(
                run_id=run_id,
                company_id=company.company_id,
                gateway=gateway,
                audit_sink=self._audit_sink,
                metadata=metadata,
            )
        return PolicyBundle(policies, tuple(gateways))

    def ensure_available(self, mode: PolicyKind) -> None:
        """Reject a disabled profile before a run is queued."""
        if mode is PolicyKind.CODEX and self._codex_config is None:
            raise PolicyUnavailableError(_CODEX_UNAVAILABLE)
        if mode is PolicyKind.OPENAI and self._openai_config is None:
            raise PolicyUnavailableError(_OPENAI_UNAVAILABLE)


def _policy_timeout(timeout_seconds: float, max_attempts: int) -> float:
    """Include provider attempts, retry delays, and orchestration overhead."""
    retry_delays = sum(0.25 * 2**index for index in range(max_attempts - 1))
    return timeout_seconds * max_attempts + retry_delays + 5


async def _close_gateways(bundle: PolicyBundle | AgentBundle) -> None:
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
