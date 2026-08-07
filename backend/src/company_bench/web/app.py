"""FastAPI adapter for asynchronous benchmark run use cases."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Self

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, model_validator

from company_bench.agents.factory import PolicyUnavailableError
from company_bench.agents.providers.codex.artifacts import CodexArtifactView
from company_bench.application import BenchmarkApplication
from company_bench.domain.models import (
    MAX_SEED,
    EpisodeResult,
    PolicyKind,
)
from company_bench.runs.models import (
    PolicyInvocation,
    PolicyProfileView,
    ReplaySource,
    RunJob,
)
from company_bench.runtime.models import TurnRecord
from company_bench.timeline.models import TimelineDay, TimelineDetail
from company_bench.timeline.projector import (
    TimelineNotFoundError,
    TimelineUnsupportedError,
)


class RunRequest(BaseModel):
    """Input for a new rule, Agent, or replay episode."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: PolicyKind = PolicyKind.BASELINE
    model: str | None = Field(default=None, min_length=1, max_length=256)
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
        if self.mode is PolicyKind.CLAUDE and self.model is None:
            raise ValueError("Claude mode requires model")
        if self.mode is not PolicyKind.CLAUDE and self.model is not None:
            raise ValueError("model is only valid in Claude mode")
        return self


def create_app(
    application: BenchmarkApplication | None = None,
) -> FastAPI:
    """Create the thin HTTP Adapter around one application facade."""
    active_application = application or BenchmarkApplication.create()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await active_application.start()
        try:
            yield
        finally:
            await active_application.close()

    app = FastAPI(
        title="Dairy Bench API",
        version="0.4.0",
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
        return active_application.profiles()

    @app.post(
        "/api/runs",
        response_model=RunJob,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["runs"],
    )
    async def run_benchmark(request: RunRequest) -> RunJob:
        try:
            return await active_application.submit(
                mode=request.mode,
                model=request.model,
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
        "/api/run-jobs",
        response_model=tuple[RunJob, ...],
        tags=["runs"],
    )
    def list_jobs(
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> tuple[RunJob, ...]:
        return active_application.jobs(limit)

    @app.get(
        "/api/replay-sources",
        response_model=tuple[ReplaySource, ...],
        tags=["runs"],
    )
    def list_replay_sources() -> tuple[ReplaySource, ...]:
        return active_application.replay_sources()

    @app.get(
        "/api/run-jobs/{run_id}",
        response_model=RunJob,
        tags=["runs"],
    )
    def get_job(run_id: str) -> RunJob:
        job = active_application.job(run_id)
        if job is None:
            raise _not_found("Run job", run_id)
        return job

    @app.post(
        "/api/run-jobs/{run_id}/stop",
        response_model=RunJob,
        tags=["runs"],
    )
    async def stop_job(run_id: str) -> RunJob:
        try:
            return await active_application.stop(run_id)
        except LookupError as error:
            raise _not_found("Run job", run_id) from error

    @app.get(
        "/api/runs/{run_id}/invocations",
        response_model=tuple[PolicyInvocation, ...],
        tags=["runs"],
    )
    def list_invocations(run_id: str) -> tuple[PolicyInvocation, ...]:
        if active_application.job(run_id) is None:
            raise _not_found("Run job", run_id)
        return active_application.invocations(run_id)

    @app.get(
        "/api/runs/{run_id}/turns",
        response_model=tuple[TurnRecord, ...],
        tags=["runs"],
    )
    def list_turns(run_id: str) -> tuple[TurnRecord, ...]:
        if active_application.job(run_id) is None:
            raise _not_found("Run job", run_id)
        return active_application.turns(run_id)

    @app.get(
        "/api/runs/{run_id}/timeline",
        response_model=TimelineDay,
        tags=["runs"],
    )
    def read_timeline_day(
        run_id: str,
        day: Annotated[int, Query(ge=1)],
    ) -> TimelineDay:
        try:
            return active_application.timeline_day(run_id, day)
        except (TimelineNotFoundError, TimelineUnsupportedError, ValueError) as error:
            raise _timeline_http_error(error) from error

    @app.get(
        "/api/runs/{run_id}/timeline/{entry_id}",
        response_model=TimelineDetail,
        tags=["runs"],
    )
    def read_timeline_detail(run_id: str, entry_id: str) -> TimelineDetail:
        try:
            return active_application.timeline_detail(run_id, entry_id)
        except (TimelineNotFoundError, TimelineUnsupportedError, ValueError) as error:
            raise _timeline_http_error(error) from error

    @app.get(
        "/api/runs/{run_id}/invocations/{invocation_id}/artifacts",
        response_model=CodexArtifactView,
        tags=["runs"],
    )
    def get_invocation_artifacts(
        run_id: str,
        invocation_id: str,
    ) -> CodexArtifactView:
        if active_application.job(run_id) is None:
            raise _not_found("Run job", run_id)
        invocation = next(
            (
                candidate
                for candidate in active_application.invocations(run_id)
                if candidate.invocation_id == invocation_id
            ),
            None,
        )
        if invocation is None:
            raise _not_found("Codex artifacts", invocation_id)
        artifacts = active_application.artifacts(invocation)
        if artifacts is None:
            raise _not_found("Codex artifacts", invocation_id)
        return artifacts

    @app.get(
        "/api/runs/{run_id}",
        response_model=EpisodeResult,
        tags=["runs"],
    )
    def get_run(run_id: str) -> EpisodeResult:
        result = active_application.run(run_id)
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


def _timeline_http_error(error: ValueError | LookupError) -> HTTPException:
    """Map timeline domain failures consistently across both read Interfaces."""
    if isinstance(error, TimelineNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    if isinstance(error, TimelineUnsupportedError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    return HTTPException(status_code=422, detail=str(error))
