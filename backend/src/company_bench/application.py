"""Application facade for benchmark lifecycle and observer use cases."""

from __future__ import annotations

from company_bench.agents.factory import AgentFactory
from company_bench.agents.providers.newapi import NewApiConfig
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
from company_bench.settings import RuntimePaths
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
        owned_store: SQLiteRunStore | None = None,
    ) -> None:
        runtime = EpisodeRuntime(
            agent_factory.scenario,
            agent_timeout_seconds=agent_factory.policy_timeout_seconds,
        )
        self._store = store
        self._coordinator = RunCoordinator(store, agent_factory, runtime)
        self._timeline = RunTimelineProjector(store)
        self._owned_store = owned_store

    @classmethod
    def create(
        cls,
        store: RunStore | None = None,
        agent_factory: AgentFactory | None = None,
    ) -> BenchmarkApplication:
        """Compose production Adapters while retaining explicit test seams."""
        paths = RuntimePaths.from_environment()
        owned_store = SQLiteRunStore(paths.database) if store is None else None
        active_store = owned_store or store
        if active_store is None:
            raise AssertionError("application composition requires a RunStore")
        if agent_factory is None:
            agent_factory = AgentFactory(
                scenario=DAIRY_S9_SCENARIO,
                audit_sink=active_store,
                newapi_config=NewApiConfig.from_environment(),
            )
        return cls(
            active_store,
            agent_factory,
            owned_store=owned_store,
        )

    async def start(self) -> None:
        """Resume eligible jobs and accept new submissions."""
        await self._coordinator.start()

    async def close(self) -> None:
        """Release every application-owned resource even after a partial failure."""
        try:
            await self._coordinator.close()
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
