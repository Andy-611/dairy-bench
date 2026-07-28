"""FastAPI adapter for asynchronous benchmark run use cases."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Annotated, Self

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, model_validator

from company_bench.agent_gateway import OpenAIAgentConfig
from company_bench.codex_artifacts import (
    CodexArtifactIdentity,
    CodexArtifactStore,
    CodexArtifactView,
)
from company_bench.codex_gateway import CodexAgentConfig, CodexModelGateway
from company_bench.coordinator import RunCoordinator
from company_bench.dairy_scenario import DAIRY_V1_SCENARIO
from company_bench.models import (
    MAX_SEED,
    EpisodeResult,
    PolicyKind,
    RunSummary,
)
from company_bench.policy_factory import PolicyFactory, PolicyUnavailableError
from company_bench.repository import (
    LifecycleRepository,
    SQLiteRunRepository,
)
from company_bench.run_models import (
    PolicyInvocation,
    PolicyProfileView,
    RunJob,
)

DEFAULT_DATABASE = Path(__file__).resolve().parents[2] / "data" / "dairy_bench.sqlite3"


class RunRequest(BaseModel):
    """Input for a new rule, Agent, or replay episode."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: PolicyKind = PolicyKind.BASELINE
    seed: int | None = Field(default=None, strict=True, ge=0, le=MAX_SEED)
    source_run_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_mode_fields(self) -> Self:
        """Keep replay identity separate from stochastic run inputs."""
        if self.mode is PolicyKind.REPLAY:
            if self.source_run_id is None:
                raise ValueError("replay mode requires source_run_id")
            if self.seed is not None:
                raise ValueError("replay mode inherits the source seed")
        elif self.source_run_id is not None:
            raise ValueError("source_run_id is only valid in replay mode")
        return self


def create_app(
    repository: LifecycleRepository | None = None,
    policy_factory: PolicyFactory | None = None,
    artifact_store: CodexArtifactStore | None = None,
) -> FastAPI:
    """Create the HTTP adapter and its owned run coordinator."""
    if repository is None:
        owned_repository: SQLiteRunRepository | None = SQLiteRunRepository(
            os.getenv("DAIRY_BENCH_DB") or DEFAULT_DATABASE
        )
        active_repository: LifecycleRepository = owned_repository
    else:
        owned_repository = None
        active_repository = repository
    active_artifacts = artifact_store or CodexArtifactStore.from_environment()
    active_factory = policy_factory or PolicyFactory(
        scenario=DAIRY_V1_SCENARIO,
        audit_sink=active_repository,
        codex_config=CodexAgentConfig.from_environment(),
        codex_gateway_factory=partial(
            CodexModelGateway,
            artifact_sink=active_artifacts,
        ),
        openai_config=OpenAIAgentConfig.from_environment(),
    )
    coordinator = RunCoordinator(active_repository, active_factory)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await coordinator.start()
        yield
        await coordinator.close()
        if owned_repository is not None:
            owned_repository.close()

    app = FastAPI(
        title="Dairy Bench API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_credentials=False,
        allow_methods=("GET", "POST"),
        allow_headers=("*",),
    )

    @app.get("/api/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/api/policy-profiles",
        response_model=tuple[PolicyProfileView, ...],
        tags=["policies"],
    )
    def policy_profiles() -> tuple[PolicyProfileView, ...]:
        return coordinator.profiles()

    @app.post(
        "/api/runs",
        response_model=RunJob,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["runs"],
    )
    async def run_benchmark(request: RunRequest) -> RunJob:
        try:
            return await coordinator.submit(
                mode=request.mode,
                seed=request.seed,
                source_run_id=request.source_run_id,
            )
        except PolicyUnavailableError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(error),
            ) from error
        except ValueError as error:
            raise HTTPException(
                status_code=422,
                detail=str(error),
            ) from error

    @app.get(
        "/api/run-jobs/{run_id}",
        response_model=RunJob,
        tags=["runs"],
    )
    def get_job(run_id: str) -> RunJob:
        job = coordinator.get_job(run_id)
        if job is None:
            raise _not_found("Run job", run_id)
        return job

    @app.get(
        "/api/runs",
        response_model=tuple[RunSummary, ...],
        tags=["runs"],
    )
    def list_runs(
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> tuple[RunSummary, ...]:
        return active_repository.list(limit)

    @app.get(
        "/api/runs/{run_id}/invocations",
        response_model=tuple[PolicyInvocation, ...],
        tags=["runs"],
    )
    def list_invocations(run_id: str) -> tuple[PolicyInvocation, ...]:
        if active_repository.get_job(run_id) is None:
            raise _not_found("Run job", run_id)
        return active_repository.list_invocations(run_id)

    @app.get(
        "/api/runs/{run_id}/invocations/{invocation_id}/artifacts",
        response_model=CodexArtifactView,
        tags=["runs"],
    )
    def get_invocation_artifacts(
        run_id: str,
        invocation_id: str,
    ) -> CodexArtifactView:
        if active_repository.get_job(run_id) is None:
            raise _not_found("Run job", run_id)
        invocation = next(
            (
                candidate
                for candidate in active_repository.list_invocations(run_id)
                if candidate.invocation_id == invocation_id
            ),
            None,
        )
        if (
            invocation is None
            or invocation.provider != "codex"
            or invocation.request_id is None
            or invocation.response_id is None
        ):
            raise _not_found("Codex artifacts", invocation_id)
        identity = CodexArtifactIdentity(
            invocation_id=invocation.invocation_id,
            run_id=invocation.run_id,
            company_id=invocation.company_id,
            day=invocation.day,
            model=invocation.model,
            thread_id=invocation.request_id,
            turn_id=invocation.response_id,
        )
        artifacts = active_artifacts.read(identity)
        if artifacts is None:
            raise _not_found("Codex artifacts", invocation_id)
        return artifacts

    @app.get(
        "/api/runs/{run_id}",
        response_model=EpisodeResult,
        tags=["runs"],
    )
    def get_run(run_id: str) -> EpisodeResult:
        result = active_repository.get(run_id)
        if result is None:
            raise _not_found("Completed run", run_id)
        return result

    return app


def _not_found(resource: str, identity: str) -> HTTPException:
    """Build a consistent missing-resource response."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"{resource} '{identity}' was not found.",
    )
