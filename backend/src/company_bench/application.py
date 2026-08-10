"""Application facade for benchmark lifecycle and observer use cases."""

from __future__ import annotations

from functools import partial

from company_bench.agents.factory import AgentFactory
from company_bench.agents.providers.capabilities import ModelCapabilityCatalog
from company_bench.agents.providers.newapi import (
    NewApiCapabilityProbe,
    NewApiConfig,
    NewApiModelGateway,
    NewApiTransport,
)
from company_bench.domain.models import EpisodeResult, PolicyKind
from company_bench.domain.scenario import DAIRY_S9_SCENARIO
from company_bench.runs.coordinator import RunCoordinator
from company_bench.runs.models import (
    PolicyInvocation,
    PolicyProfileView,
    ReplaySource,
    RunJob,
)
from company_bench.runtime.episode import EpisodeRuntime
from company_bench.runtime.models import TurnRecord
from company_bench.settings import MAX_PARALLELISM, ExecutionLimits, RuntimePaths
from company_bench.storage.store import RunStore, SQLiteRunStore
from company_bench.timeline.models import TimelineDay, TimelineDetail
from company_bench.timeline.projector import RunTimelineProjector


class BenchmarkApplication:
    """Hide run orchestration, persistence, providers, and projections behind one facade."""

    def __init__(
        self,
        store: RunStore,
        agent_factory: AgentFactory,
        *,
        max_concurrent_runs: int = MAX_PARALLELISM,
        owned_store: SQLiteRunStore | None = None,
        owned_transport: NewApiTransport | None = None,
    ) -> None:
        runtime = EpisodeRuntime(
            agent_factory.scenario,
            agent_timeout_seconds=agent_factory.policy_timeout_seconds,
        )
        self._store = store
        self._coordinator = RunCoordinator(
            store,
            agent_factory,
            runtime,
            max_concurrent_runs=max_concurrent_runs,
        )
        self._timeline = RunTimelineProjector(store)
        self._owned_store = owned_store
        self._owned_transport = owned_transport

    @classmethod
    def create(
        cls,
        store: RunStore | None = None,
        agent_factory: AgentFactory | None = None,
    ) -> BenchmarkApplication:
        """Compose production Adapters while retaining explicit test seams."""
        paths = RuntimePaths.from_environment()
        limits = ExecutionLimits.from_environment()
        owned_store = SQLiteRunStore(paths.database) if store is None else None
        active_store = owned_store or store
        if active_store is None:
            raise AssertionError("application composition requires a RunStore")
        owned_transport: NewApiTransport | None = None
        model_capabilities: ModelCapabilityCatalog | None = None
        if agent_factory is None:
            newapi_config = NewApiConfig.from_environment()
            if newapi_config is not None:
                owned_transport = NewApiTransport(
                    newapi_config,
                    max_concurrent_requests=limits.max_concurrent_newapi_requests,
                )
                model_capabilities = ModelCapabilityCatalog.production(
                    paths.model_capabilities,
                    NewApiCapabilityProbe(newapi_config, owned_transport),
                )
            agent_factory = AgentFactory(
                scenario=DAIRY_S9_SCENARIO,
                audit_sink=active_store,
                newapi_config=newapi_config,
                model_capabilities=model_capabilities,
                gateway_factory=(
                    partial(NewApiModelGateway, transport=owned_transport)
                    if owned_transport is not None
                    else NewApiModelGateway
                ),
            )
        return cls(
            active_store,
            agent_factory,
            max_concurrent_runs=limits.max_concurrent_runs,
            owned_store=owned_store,
            owned_transport=owned_transport,
        )

    async def start(self) -> None:
        """Resume eligible jobs and accept new submissions."""
        await self._coordinator.start()

    async def close(self) -> None:
        """Release every application-owned resource even after a partial failure."""
        try:
            await self._coordinator.close()
        finally:
            try:
                if self._owned_transport is not None:
                    await self._owned_transport.close()
            finally:
                if self._owned_store is not None:
                    self._owned_store.close()

    def profiles(self) -> tuple[PolicyProfileView, ...]:
        return self._coordinator.profiles()

    async def submit(
        self,
        *,
        mode: PolicyKind,
        model: str | None,
        seed: int | None,
        source_run_id: str | None,
    ) -> RunJob:
        return await self._coordinator.submit(
            mode=mode,
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

    def invocations(self, run_id: str) -> tuple[PolicyInvocation, ...]:
        return self._store.list_invocations(run_id)

    def turns(self, run_id: str) -> tuple[TurnRecord, ...]:
        return self._store.list_turns(run_id)

    def timeline_day(self, run_id: str, day: int) -> TimelineDay:
        return self._timeline.read_day(run_id, day)

    def timeline_detail(self, run_id: str, entry_id: str) -> TimelineDetail:
        return self._timeline.read_detail(run_id, entry_id)
