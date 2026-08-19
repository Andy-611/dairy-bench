"""Application facade for benchmark lifecycle and observer use cases."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from company_bench.agents.factory import AgentFactory, ModelPolicyProfile
from company_bench.agents.providers.capabilities import ModelCapabilityCatalog
from company_bench.agents.providers.newapi import (
    NewApiAnthropicMessagesGateway,
    NewApiCapabilityProbe,
    NewApiConfig,
    NewApiGatewayFactory,
    NewApiGatewayType,
    NewApiModelGateway,
    NewApiResponsesGateway,
    NewApiTransport,
)
from company_bench.domain.models import EpisodeResult, PolicyProfileId, ScenarioSpec
from company_bench.domain.scenario import DAIRY_S9_SCENARIO
from company_bench.economy.oracle import EnterpriseOracle
from company_bench.economy.scoring import Evaluator
from company_bench.runs.coordinator import RunCoordinator
from company_bench.runs.evaluation import RunEvaluationProjector
from company_bench.runs.models import (
    PolicyInvocation,
    PolicyProfileView,
    ReplaySource,
    RunEvaluation,
    RunJob,
)
from company_bench.runtime.episode import EpisodeRuntime
from company_bench.runtime.models import TurnRecord
from company_bench.settings import MAX_PARALLELISM, ExecutionLimits, RuntimePaths
from company_bench.storage.repository import RunRepository
from company_bench.storage.sqlite import SQLiteRunRepository
from company_bench.timeline.models import TimelineDetail, TimelineWeek
from company_bench.timeline.projector import RunTimelineProjector


@dataclass(frozen=True, slots=True)
class _ModelProfileSpec:
    """Static composition data for one selectable NewAPI profile."""

    profile_id: PolicyProfileId
    label: str
    description: str
    environment_prefix: str
    gateway_type: NewApiGatewayType


_MODEL_PROFILE_SPECS = (
    _ModelProfileSpec(
        PolicyProfileId.NEWAPI_MODEL,
        "Model agents via NewAPI",
        "Each company is controlled by an independent model through NewAPI.",
        "DAIRY_BENCH_NEWAPI_MODEL",
        NewApiModelGateway,
    ),
    _ModelProfileSpec(
        PolicyProfileId.NEWAPI_CODEX,
        "Codex via NewAPI",
        "Uses the Codex-compatible NewAPI Responses route; Dairy Bench remains the Agent runtime.",
        "DAIRY_BENCH_NEWAPI_CODEX",
        NewApiResponsesGateway,
    ),
    _ModelProfileSpec(
        PolicyProfileId.NEWAPI_CLAUDE_CODE,
        "Claude Code via NewAPI",
        "Uses the Claude-compatible NewAPI Messages route; Dairy Bench remains the Agent runtime.",
        "DAIRY_BENCH_NEWAPI_CLAUDE",
        NewApiAnthropicMessagesGateway,
    ),
)


def _compose_model_factory(
    store: RunRepository,
    paths: RuntimePaths,
    limits: ExecutionLimits,
    configured_profiles: tuple[tuple[_ModelProfileSpec, NewApiConfig | None], ...],
) -> tuple[AgentFactory, tuple[NewApiTransport, ...]]:
    """Build all model profiles and their shared transport registry."""
    request_slots = asyncio.Semaphore(limits.max_concurrent_newapi_requests)
    transports: list[NewApiTransport] = []
    profiles: list[ModelPolicyProfile] = []
    for spec, config in configured_profiles:
        transport = (
            NewApiTransport(
                config,
                max_concurrent_requests=limits.max_concurrent_newapi_requests,
                request_slots=request_slots,
            )
            if config is not None
            else None
        )
        if transport is not None:
            transports.append(transport)
        capabilities = (
            ModelCapabilityCatalog.production(
                paths.model_capabilities(spec.profile_id),
                NewApiCapabilityProbe(
                    config,
                    transport,
                    spec.gateway_type.wire_protocol,
                ),
            )
            if config is not None and transport is not None
            else None
        )
        profiles.append(
            ModelPolicyProfile(
                profile_id=spec.profile_id,
                label=spec.label,
                description=spec.description,
                gateway_factory=NewApiGatewayFactory(spec.gateway_type, transport),
                config=config,
                capabilities=capabilities,
            )
        )
    return (
        AgentFactory(
            scenario=DAIRY_S9_SCENARIO,
            audit_sink=store,
            model_profiles=tuple(profiles),
        ),
        tuple(transports),
    )


async def _close_transports(
    transports: tuple[NewApiTransport, ...],
) -> tuple[BaseException, ...]:
    """Close all transports and return, rather than short-circuit on, failures."""
    results = await asyncio.gather(
        *(transport.close() for transport in transports),
        return_exceptions=True,
    )
    return tuple(result for result in results if isinstance(result, BaseException))


async def _close_resources(
    coordinator: RunCoordinator,
    transports: tuple[NewApiTransport, ...],
    store: SQLiteRunRepository | None,
) -> tuple[BaseException, ...]:
    """Close every owned resource without allowing one failure to skip the rest."""
    failures: list[BaseException] = []
    try:
        await coordinator.close()
    except BaseException as error:
        failures.append(error)
    failures.extend(await _close_transports(transports))
    if store is not None:
        try:
            store.close()
        except BaseException as error:
            failures.append(error)
    return tuple(failures)


async def _await_cleanup[ResultT](task: asyncio.Task[ResultT]) -> ResultT:
    """Finish cleanup even when the caller is concurrently cancelled."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        task.result()
        raise


class BenchmarkApplication:
    """Hide run orchestration, persistence, providers, and projections behind one facade."""

    def __init__(
        self,
        store: RunRepository,
        agent_factory: AgentFactory,
        *,
        max_concurrent_runs: int = MAX_PARALLELISM,
        oracle_cache_directory: Path | None = None,
        owned_store: SQLiteRunRepository | None = None,
        owned_transports: tuple[NewApiTransport, ...] = (),
    ) -> None:
        evaluator = Evaluator(oracle=EnterpriseOracle(oracle_cache_directory))
        runtime = EpisodeRuntime(
            agent_factory.scenario,
            evaluator=evaluator,
            agent_timeout_seconds=agent_factory.policy_timeout_seconds,
        )
        self._store = store
        self._scenario = agent_factory.scenario
        self._coordinator = RunCoordinator(
            store,
            agent_factory,
            runtime,
            max_concurrent_runs=max_concurrent_runs,
        )
        self._evaluation = RunEvaluationProjector(store, evaluator)
        self._timeline = RunTimelineProjector(store)
        self._owned_store = owned_store
        self._owned_transports = owned_transports

    @classmethod
    def create(
        cls,
        store: RunRepository | None = None,
        agent_factory: AgentFactory | None = None,
    ) -> BenchmarkApplication:
        """Compose production Adapters while retaining explicit test seams."""
        paths = RuntimePaths.from_environment()
        limits = ExecutionLimits.from_environment()
        configured_profiles = (
            tuple(
                (spec, NewApiConfig.from_environment(spec.environment_prefix))
                for spec in _MODEL_PROFILE_SPECS
            )
            if agent_factory is None
            else ()
        )
        owned_store = SQLiteRunRepository(paths.database) if store is None else None
        active_store = owned_store or store
        if active_store is None:
            raise AssertionError("application composition requires a RunRepository")
        owned_transports: tuple[NewApiTransport, ...] = ()
        try:
            if agent_factory is None:
                agent_factory, owned_transports = _compose_model_factory(
                    active_store,
                    paths,
                    limits,
                    configured_profiles,
                )
            return cls(
                active_store,
                agent_factory,
                max_concurrent_runs=limits.max_concurrent_runs,
                oracle_cache_directory=paths.oracle_cache,
                owned_store=owned_store,
                owned_transports=owned_transports,
            )
        except BaseException:
            if owned_store is not None:
                with suppress(BaseException):
                    owned_store.close()
            raise

    async def start(self) -> None:
        """Resume eligible jobs and accept new submissions."""
        await self._coordinator.start()

    async def close(self) -> None:
        """Release every application-owned resource even after a partial failure."""
        cleanup = asyncio.create_task(
            _close_resources(
                self._coordinator,
                self._owned_transports,
                self._owned_store,
            )
        )
        try:
            failures = await _await_cleanup(cleanup)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            failures = (error,)
        if failures:
            raise failures[0]

    def profiles(self) -> tuple[PolicyProfileView, ...]:
        return self._coordinator.profiles()

    @property
    def scenario(self) -> ScenarioSpec:
        """Return the sole active benchmark scenario."""
        return self._scenario

    async def submit(
        self,
        *,
        profile_id: PolicyProfileId,
        model: str | None,
        seed: int | None,
        source_run_id: str | None,
    ) -> RunJob:
        return await self._coordinator.submit(
            profile_id=profile_id,
            model=model,
            seed=seed,
            source_run_id=source_run_id,
        )

    async def stop(self, run_id: str) -> RunJob:
        return await self._coordinator.stop(run_id)

    async def resume(self, run_id: str) -> RunJob:
        return await self._coordinator.resume(run_id)

    def job(self, run_id: str) -> RunJob | None:
        return self._coordinator.get_job(run_id)

    def jobs(self, limit: int, offset: int) -> tuple[RunJob, ...]:
        return self._store.list_jobs(limit, offset)

    def replay_sources(self) -> tuple[ReplaySource, ...]:
        return self._store.list_replay_sources()

    def run(self, run_id: str) -> EpisodeResult | None:
        return self._store.get(run_id)

    def evaluation(self, run_id: str) -> RunEvaluation | None:
        return self._evaluation.read(run_id)

    def invocations(self, run_id: str) -> tuple[PolicyInvocation, ...]:
        return self._store.list_invocations(run_id)

    def turns(self, run_id: str) -> tuple[TurnRecord, ...]:
        return self._store.list_turns(run_id)

    def timeline_week(self, run_id: str, week: int) -> TimelineWeek:
        return self._timeline.read_week(run_id, week)

    def timeline_detail(self, run_id: str, entry_id: str) -> TimelineDetail:
        return self._timeline.read_detail(run_id, entry_id)
