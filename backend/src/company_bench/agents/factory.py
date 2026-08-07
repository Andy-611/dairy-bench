"""Construct fresh, isolated company policies for each benchmark run."""

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
from company_bench.agents.providers.claude import NewApiClaudeConfig, NewApiClaudeGateway
from company_bench.agents.providers.codex.gateway import CodexAgentConfig, CodexModelGateway
from company_bench.agents.providers.openai import OpenAIAgentConfig, OpenAIModelGateway
from company_bench.domain.models import PolicyKind, PolicyMetadata, ScenarioSpec
from company_bench.runs.models import PolicyAuditSink, PolicyProfileView
from company_bench.runtime.models import TurnRecord

type OpenAIGatewayFactory = Callable[[OpenAIAgentConfig], CommandGateway]
type CodexGatewayFactory = Callable[[CodexAgentConfig, str], CommandGateway]
type NewApiClaudeGatewayFactory = Callable[[NewApiClaudeConfig], CommandGateway]
type CompanyGatewayFactory = Callable[[str], CommandGateway]
# Each call must return a fresh gateway owned by exactly one company.
_OPENAI_UNAVAILABLE = "OpenAI Agent is unavailable: configure OPENAI_API_KEY on the backend"
_CLAUDE_UNAVAILABLE = (
    "Claude Agent is unavailable: run `start.cmd --configure-newapi`, then restart `start.cmd`"
)
_CODEX_UNAVAILABLE = (
    "Codex Agent is unavailable: run `codex login`, then set "
    "DAIRY_BENCH_CODEX_ENABLED=true before starting the backend"
)


class PolicyUnavailableError(RuntimeError):
    """The requested server-side policy profile is not configured."""


@dataclass(slots=True)
class AgentBundle:
    """V4 company actors and their independently owned provider gateways."""

    agents: Mapping[str, CompanyAgent]
    _gateways: tuple[CommandGateway, ...] = ()

    async def close(self) -> None:
        """Release every company gateway even when they close concurrently."""
        await _close_gateways(self)


class AgentFactory:
    """Create isolated V4 company Agents for each benchmark run."""

    def __init__(
        self,
        scenario: ScenarioSpec,
        audit_sink: PolicyAuditSink,
        *,
        claude_config: NewApiClaudeConfig | None = None,
        codex_config: CodexAgentConfig | None = None,
        openai_config: OpenAIAgentConfig | None = None,
        claude_gateway_factory: NewApiClaudeGatewayFactory = NewApiClaudeGateway,
        codex_gateway_factory: CodexGatewayFactory = CodexModelGateway,
        gateway_factory: OpenAIGatewayFactory = OpenAIModelGateway,
    ) -> None:
        self._scenario = scenario
        self._audit_sink = audit_sink
        self._claude_config = claude_config
        self._claude_gateway_factory = claude_gateway_factory
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
        claude = self._claude_config
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
                mode=PolicyKind.CLAUDE,
                label="Claude via NewAPI",
                available=claude is not None,
                provider="newapi",
                model=claude.model if claude else None,
                models=claude.available_models if claude else (),
                description=(
                    "Each company is controlled by an independent Claude model through NewAPI."
                ),
                unavailable_reason=(
                    None
                    if claude
                    else "The local NewAPI credential and Claude model catalog are not configured."
                ),
            ),
            PolicyProfileView(
                mode=PolicyKind.REPLAY,
                label="Exact replay",
                available=True,
                description="Replay a completed persisted run without calling agents.",
            ),
        )

    @property
    def policy_timeout_seconds(self) -> float:
        """Leave enough time for the gateway to exhaust its own retries."""
        configured = tuple(
            config
            for config in (
                self._claude_config,
                self._codex_config,
                self._openai_config,
            )
            if config is not None
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
        """Create fresh event-driven company actors for one V4 episode."""
        if mode is not PolicyKind.CLAUDE and model is not None:
            raise ValueError("model is only valid for Claude Agents")
        if mode is PolicyKind.BASELINE:
            return AgentBundle(
                {company.company_id: BaselineCompanyAgent() for company in self._scenario.companies}
            )
        if mode is PolicyKind.REPLAY:
            if not source_turns:
                raise ValueError("V4 replay requires a source turn journal")
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
        checkpoint_by_company = {checkpoint.company_id: checkpoint for checkpoint in checkpoints}
        gateway_factory: CompanyGatewayFactory
        if mode is PolicyKind.CLAUDE:
            config = self._claude_config_for(model)
            metadata = _claude_metadata(config)

            def gateway_factory(_: str) -> CommandGateway:
                return self._claude_gateway_factory(config)

        elif mode is PolicyKind.OPENAI:
            config = self._openai_config
            if config is None:
                raise PolicyUnavailableError(_OPENAI_UNAVAILABLE)
            metadata = self._agent_metadata(mode)

            def gateway_factory(_: str) -> CommandGateway:
                return self._gateway_factory(config)

        elif mode is PolicyKind.CODEX:
            config = self._codex_config
            if config is None:
                raise PolicyUnavailableError(_CODEX_UNAVAILABLE)
            metadata = self._agent_metadata(mode)

            def gateway_factory(company_id: str) -> CommandGateway:
                return self._codex_gateway_factory(config, company_id)

        else:
            raise ValueError(f"unsupported V4 policy mode: {mode.value}")

        agents: dict[str, CompanyAgent] = {}
        gateways: list[CommandGateway] = []
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
        """Build provider metadata shared by equivalent V4 company Agents."""
        if mode is PolicyKind.OPENAI and self._openai_config is not None:
            config = self._openai_config
            return PolicyMetadata(
                name="llm-company-agent",
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
                kind=mode,
                provider="codex",
                model=config.model,
                prompt_version=COMMAND_PROMPT_VERSION,
                config_fingerprint=config.fingerprint,
            )
        unavailable = {
            PolicyKind.CODEX: _CODEX_UNAVAILABLE,
            PolicyKind.OPENAI: _OPENAI_UNAVAILABLE,
        }.get(mode)
        if unavailable is None:
            raise ValueError(f"unsupported V4 policy mode: {mode.value}")
        raise PolicyUnavailableError(unavailable)

    def ensure_available(self, mode: PolicyKind, model: str | None = None) -> None:
        """Reject a disabled profile before a run is queued."""
        if mode is PolicyKind.CLAUDE:
            self._claude_config_for(model)
            return
        if model is not None:
            raise ValueError("model is only valid for Claude Agents")
        if mode is PolicyKind.CODEX and self._codex_config is None:
            raise PolicyUnavailableError(_CODEX_UNAVAILABLE)
        if mode is PolicyKind.OPENAI and self._openai_config is None:
            raise PolicyUnavailableError(_OPENAI_UNAVAILABLE)

    def _claude_config_for(self, model: str | None) -> NewApiClaudeConfig:
        """Resolve one allowed Claude model without exposing the shared credential."""
        if self._claude_config is None:
            raise PolicyUnavailableError(_CLAUDE_UNAVAILABLE)
        return self._claude_config.select_model(model)


def _policy_timeout(timeout_seconds: float, max_attempts: int) -> float:
    """Include provider attempts, retry delays, and orchestration overhead."""
    retry_delays = sum(0.25 * 2**index for index in range(max_attempts - 1))
    return timeout_seconds * max_attempts + retry_delays + 5


def _claude_metadata(config: NewApiClaudeConfig) -> PolicyMetadata:
    """Build auditable metadata for one selected Claude model."""
    return PolicyMetadata(
        name="claude-company-agent",
        kind=PolicyKind.CLAUDE,
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
