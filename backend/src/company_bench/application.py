"""Application facade for benchmark lifecycle and observer use cases."""

from __future__ import annotations

from functools import partial

from company_bench.agents.factory import AgentFactory
from company_bench.agents.providers.claude import NewApiClaudeConfig
from company_bench.agents.providers.codex.artifacts import (
    CodexArtifactIdentity,
    CodexArtifactStore,
    CodexArtifactView,
)
from company_bench.agents.providers.codex.gateway import CodexAgentConfig, CodexModelGateway
from company_bench.agents.providers.codex.sessions import CodexSessionManager
from company_bench.agents.providers.openai import OpenAIAgentConfig
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
        artifacts: CodexArtifactStore,
        *,
        owned_store: SQLiteRunStore | None = None,
        owned_codex_sessions: CodexSessionManager | None = None,
    ) -> None:
        runtime = EpisodeRuntime(
            agent_factory.scenario,
            agent_timeout_seconds=agent_factory.policy_timeout_seconds,
        )
        self._store = store
        self._coordinator = RunCoordinator(store, agent_factory, runtime)
        self._timeline = RunTimelineProjector(store, artifacts)
        self._artifacts = artifacts
        self._owned_store = owned_store
        self._owned_codex_sessions = owned_codex_sessions

    @classmethod
    def create(
        cls,
        store: RunStore | None = None,
        agent_factory: AgentFactory | None = None,
        artifacts: CodexArtifactStore | None = None,
    ) -> BenchmarkApplication:
        """Compose production Adapters while retaining explicit test seams."""
        paths = RuntimePaths.from_environment()
        owned_store = SQLiteRunStore(paths.database) if store is None else None
        active_store = owned_store or store
        if active_store is None:
            raise AssertionError("application composition requires a RunStore")
        active_artifacts = artifacts or CodexArtifactStore.from_environment()
        owned_sessions: CodexSessionManager | None = None
        if agent_factory is None:
            codex_config = CodexAgentConfig.from_environment()
            owned_sessions = (
                CodexSessionManager(codex_config.session_retention)
                if codex_config is not None
                else None
            )
            agent_factory = AgentFactory(
                scenario=DAIRY_S9_SCENARIO,
                audit_sink=active_store,
                claude_config=NewApiClaudeConfig.from_environment(),
                codex_config=codex_config,
                codex_gateway_factory=partial(
                    CodexModelGateway,
                    artifact_sink=active_artifacts,
                    session_manager=owned_sessions,
                ),
                openai_config=OpenAIAgentConfig.from_environment(),
            )
        return cls(
            active_store,
            agent_factory,
            active_artifacts,
            owned_store=owned_store,
            owned_codex_sessions=owned_sessions,
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
                if self._owned_codex_sessions is not None:
                    await self._owned_codex_sessions.close()
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

    def job(self, run_id: str) -> RunJob | None:
        return self._coordinator.get_job(run_id)

    def jobs(self, limit: int) -> tuple[RunJob, ...]:
        return self._store.list_jobs(limit)

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

    def artifacts(self, invocation: PolicyInvocation) -> CodexArtifactView | None:
        """Load Codex evidence only when the invocation carries complete coordinates."""
        turn_id = invocation.provider_turn_id or invocation.response_id
        if invocation.provider != "codex" or invocation.request_id is None or turn_id is None:
            return None
        return self._artifacts.read(
            CodexArtifactIdentity(
                invocation_id=invocation.invocation_id,
                run_id=invocation.run_id,
                company_id=invocation.company_id,
                day=invocation.day,
                model=invocation.model,
                thread_id=invocation.request_id,
                turn_id=turn_id,
                domain_turn_id=invocation.domain_turn_id,
            )
        )
