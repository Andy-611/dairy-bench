"""Construct fresh, isolated company policies for each benchmark run."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from company_bench.agent_gateway import OpenAIAgentConfig, OpenAIModelGateway
from company_bench.agent_models import ModelGateway
from company_bench.agent_policy import PROMPT_VERSION, LlmCompanyPolicy, ReplayPolicy
from company_bench.codex_gateway import CodexAgentConfig, CodexModelGateway
from company_bench.models import (
    EpisodeResult,
    PolicyKind,
    PolicyMetadata,
    ScenarioSpec,
)
from company_bench.policies import BaselinePolicy, CompanyPolicy
from company_bench.run_models import PolicyAuditSink, PolicyProfileView

type OpenAIGatewayFactory = Callable[[OpenAIAgentConfig], ModelGateway]
type CodexGatewayFactory = Callable[[CodexAgentConfig, str], ModelGateway]
type CompanyGatewayFactory = Callable[[str], ModelGateway]
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
    _gateways: tuple[ModelGateway, ...] = ()

    async def close(self) -> None:
        """Release every company gateway even when they close concurrently."""
        gateways, self._gateways = self._gateways, ()
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


class PolicyFactory:
    """Create six separate company controllers from one server profile."""

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

    def profiles(self) -> tuple[PolicyProfileView, ...]:
        """Return browser-safe policy choices."""
        codex = self._codex_config
        openai = self._openai_config
        return (
            PolicyProfileView(
                mode=PolicyKind.BASELINE,
                label="规则基线",
                available=True,
                description="透明、确定性的固定经营规则。",
            ),
            PolicyProfileView(
                mode=PolicyKind.CODEX,
                label="Codex 公司 Agent",
                available=codex is not None,
                provider="codex",
                model=codex.model if codex else None,
                reasoning_effort=codex.reasoning_effort if codex else None,
                description="每家公司由一个独立 Codex runtime 控制。",
                unavailable_reason=(
                    None if codex else "请先登录 Codex 并在后端启用 DAIRY_BENCH_CODEX_ENABLED。"
                ),
            ),
            PolicyProfileView(
                mode=PolicyKind.OPENAI,
                label="OpenAI 公司 Agent",
                available=openai is not None,
                provider="openai",
                model=openai.model if openai else None,
                reasoning_effort=openai.reasoning_effort if openai else None,
                description="每家公司由一个独立 LLM Agent 控制。",
                unavailable_reason=(None if openai else "后端尚未配置 OPENAI_API_KEY。"),
            ),
            PolicyProfileView(
                mode=PolicyKind.REPLAY,
                label="精确回放",
                available=True,
                description="按 source_run_id 重放历史经营决策。",
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
        """Create a new policy object for every configured company."""
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
        gateways: list[ModelGateway] = []
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
