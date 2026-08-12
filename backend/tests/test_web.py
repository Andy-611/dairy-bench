import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest import MonkeyPatch, raises

import company_bench.application as application_module
from company_bench.agents.factory import AgentFactory
from company_bench.application import BenchmarkApplication
from company_bench.domain.models import (
    EpisodeQuality,
    EpisodeResult,
    PolicyKind,
    ProtocolReport,
)
from company_bench.domain.scenario import DAIRY_S9_SCENARIO
from company_bench.runs.models import (
    ReplaySource,
    RunJob,
    RunStatus,
    RunStopReason,
)
from company_bench.storage.store import InMemoryRunStore
from company_bench.web.app import create_app

TEST_SCENARIO = DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1})


class _TrackedOwnedRepository(InMemoryRunStore):
    """Expose whether the Web lifespan released its owned repository."""

    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    def close(self) -> None:
        """Record application-owned repository cleanup."""
        self.close_calls += 1


def _app(repository: InMemoryRunStore) -> FastAPI:
    """Create an app with deterministic server-side policy availability."""
    factory = AgentFactory(TEST_SCENARIO, repository)
    return create_app(BenchmarkApplication.create(repository, factory))


def _wait_for_terminal_job(
    client: TestClient,
    run_id: str,
    timeout_seconds: float = 120,
) -> RunJob:
    """Poll the public job endpoint until the run reaches a terminal state."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.get(f"/api/run-jobs/{run_id}")
        assert response.status_code == 200
        job = RunJob.model_validate(response.json())
        if job.status.terminal:
            return job
        time.sleep(0.05)
    raise AssertionError(f"run job {run_id} did not finish within {timeout_seconds}s")


def test_run_list_and_detail_http_flow() -> None:
    repository = InMemoryRunStore()
    with TestClient(_app(repository)) as client:
        assert client.get("/api/health").json() == {"status": "ok"}

        created = client.post(
            "/api/runs",
            json={"mode": "baseline", "seed": 42},
        )
        assert created.status_code == 202
        submitted = RunJob.model_validate(created.json())
        assert submitted.status is RunStatus.QUEUED

        completed = _wait_for_terminal_job(client, submitted.run_id)
        assert completed.status is RunStatus.COMPLETED
        assert completed.revision > submitted.revision
        assert completed.current_absolute_day == 7
        assert completed.total_weeks == 1

        detail = client.get(f"/api/runs/{submitted.run_id}")
        assert detail.status_code == 200
        detail_payload = detail.json()
        assert set(detail_payload["score"]) == {
            "score_version",
            "final_score",
            "efficiency_raw",
            "efficiency_reference",
            "efficiency_score",
            "farm_gini",
            "processor_gini",
            "retailer_gini",
            "fairness_score",
            "bankrupt_company_count",
            "bankruptcy_rate",
            "companies",
        }
        result = EpisodeResult.model_validate(detail_payload)
        assert result.seed == 42
        assert result.scenario == TEST_SCENARIO
        assert len(result.scenario.companies) == 9
        assert len(result.snapshots) == 1
        turns = client.get(f"/api/runs/{submitted.run_id}/turns").json()
        assert len(turns) == 6 * len(TEST_SCENARIO.companies)
        assert turns[0]["turn"]["state_version"] == 0
        decision = turns[0]["envelope"]["decision"]
        assert decision["kind"] == "action"
        assert decision["action"]["kind"] in {
            "produce",
            "set_quote_ladder",
            "set_retail_price",
        }
        assert "attention" in decision

        timeline_response = client.get(
            f"/api/runs/{submitted.run_id}/timeline",
            params={"week": 1},
        )
        assert timeline_response.status_code == 200
        timeline = timeline_response.json()
        assert timeline["selected_week"] == 1
        assert len(timeline["week_summaries"]) == 1
        assert [day["sim_day"]["absolute_day"] for day in timeline["days"]] == list(
            range(1, 8)
        )
        assert (
            client.get(
                f"/api/runs/{submitted.run_id}/timeline",
                params={"week": 2},
            ).status_code
            == 422
        )

        assert client.get(f"/api/runs/{result.run_id}/invocations").json() == []
        profiles = client.get("/api/policy-profiles").json()
        assert [profile["mode"] for profile in profiles] == [
            "baseline",
            "model",
            "replay",
        ]
        assert (
            next(profile for profile in profiles if profile["mode"] == "model")["available"]
            is False
        )
        assert next(profile for profile in profiles if profile["mode"] == "replay")["label"] == (
            "Completed Run Replay"
        )

        replayed = client.post(
            "/api/runs",
            json={"mode": "replay", "source_run_id": result.run_id},
        )
        assert replayed.status_code == 202
        replay_job = _wait_for_terminal_job(
            client,
            RunJob.model_validate(replayed.json()).run_id,
        )
        replay = EpisodeResult.model_validate(client.get(f"/api/runs/{replay_job.run_id}").json())
        assert replay.seed == result.seed
        assert replay.events == result.events
        assert replay.snapshots == result.snapshots
        assert replay.score == result.score
        assert all(policy.source_run_id == result.run_id for policy in replay.policies)

        assert client.get("/api/runs/missing").status_code == 404
        assert client.get("/api/run-jobs/missing").status_code == 404
        assert client.get("/api/runs/missing/invocations").status_code == 404


def test_run_job_history_http_lists_all_states_newest_first() -> None:
    repository = InMemoryRunStore()
    submitted_at = datetime(2026, 1, 1, tzinfo=UTC)

    with TestClient(_app(repository)) as client:
        jobs = tuple(
            RunJob(
                run_id=f"http_history_{status.value}",
                mode=PolicyKind.BASELINE,
                status=status,
                seed=sequence,
                scenario_id=DAIRY_S9_SCENARIO.scenario_id,
                total_weeks=DAIRY_S9_SCENARIO.weeks,
                submitted_at=submitted_at + timedelta(minutes=sequence),
                quality=_clean_quality() if status is RunStatus.COMPLETED else None,
            )
            for sequence, status in enumerate(RunStatus)
        )
        for job in reversed(jobs):
            repository.save_job(job)

        history_response = client.get("/api/run-jobs")
        limited_response = client.get("/api/run-jobs", params={"limit": 3})
        offset_response = client.get(
            "/api/run-jobs",
            params={"limit": 2, "offset": 2},
        )

    assert history_response.status_code == 200
    history = tuple(RunJob.model_validate(item) for item in history_response.json())
    assert history == tuple(reversed(jobs))
    assert {job.status for job in history} == set(RunStatus)
    assert limited_response.status_code == 200
    limited = tuple(RunJob.model_validate(item) for item in limited_response.json())
    assert limited == history[:3]
    offset = tuple(RunJob.model_validate(item) for item in offset_response.json())
    assert offset == history[2:4]


def test_stop_run_http_is_idempotent_and_missing_is_not_found() -> None:
    repository = InMemoryRunStore()
    stopped = RunJob(
        run_id="http_stopped",
        mode=PolicyKind.BASELINE,
        status=RunStatus.STOPPED,
        seed=42,
        scenario_id=DAIRY_S9_SCENARIO.scenario_id,
        total_weeks=DAIRY_S9_SCENARIO.weeks,
        submitted_at=datetime(2026, 1, 1, tzinfo=UTC),
        finished_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    repository.save_job(stopped)

    with TestClient(_app(repository)) as client:
        response = client.post(f"/api/run-jobs/{stopped.run_id}/stop")
        missing = client.post("/api/run-jobs/missing/stop")

    assert response.status_code == 200
    assert RunJob.model_validate(response.json()) == stopped
    assert missing.status_code == 404


def test_resume_run_http_preserves_identity_and_rejects_invalid_requests() -> None:
    repository = InMemoryRunStore()
    submitted_at = datetime(2026, 1, 1, tzinfo=UTC)
    stopped = RunJob(
        run_id="http_resume_stopped",
        mode=PolicyKind.BASELINE,
        status=RunStatus.STOPPED,
        seed=42,
        scenario_id=DAIRY_S9_SCENARIO.scenario_id,
        total_weeks=DAIRY_S9_SCENARIO.weeks,
        submitted_at=submitted_at,
        finished_at=submitted_at,
        stop_reason=RunStopReason.USER_REQUESTED,
    )
    failed = stopped.model_copy(update={"run_id": "http_resume_failed", "status": RunStatus.FAILED})
    completed = stopped.model_copy(
        update={
            "run_id": "http_resume_completed",
            "status": RunStatus.COMPLETED,
            "quality": _clean_quality(),
        }
    )
    for job in (stopped, failed, completed):
        repository.save_job(job)

    with TestClient(_app(repository)) as client:
        resumed_response = client.post(f"/api/run-jobs/{stopped.run_id}/resume")
        failed_response = client.post(f"/api/run-jobs/{failed.run_id}/resume")
        completed_response = client.post(f"/api/run-jobs/{completed.run_id}/resume")
        missing_response = client.post("/api/run-jobs/missing/resume")

    resumed = RunJob.model_validate(resumed_response.json())
    assert resumed_response.status_code == 202
    assert resumed.run_id == stopped.run_id
    assert resumed.status is RunStatus.QUEUED
    assert resumed.stop_reason is None
    assert failed_response.status_code == 409
    assert completed_response.status_code == 409
    assert missing_response.status_code == 404


def test_replay_sources_http_lists_all_completed_runs_newest_first() -> None:
    repository = InMemoryRunStore()
    submitted_at = datetime(2026, 1, 1, tzinfo=UTC)
    completed_jobs = tuple(
        RunJob(
            run_id=f"replay_source_{sequence}",
            mode=PolicyKind.BASELINE,
            status=RunStatus.COMPLETED,
            seed=sequence,
            scenario_id=DAIRY_S9_SCENARIO.scenario_id,
            total_weeks=DAIRY_S9_SCENARIO.weeks,
            submitted_at=submitted_at + timedelta(minutes=sequence),
            quality=_clean_quality(),
        )
        for sequence in range(501)
    )
    for job in completed_jobs:
        repository.save_job(job)
    repository.save_job(
        completed_jobs[-1].model_copy(
            update={
                "run_id": "newer_failed_run",
                "status": RunStatus.FAILED,
                "quality": None,
                "submitted_at": submitted_at + timedelta(minutes=501),
            }
        )
    )

    with TestClient(_app(repository)) as client:
        response = client.get("/api/replay-sources")

    assert response.status_code == 200
    sources = tuple(ReplaySource.model_validate(item) for item in response.json())
    assert len(sources) == 501
    assert sources == tuple(
        ReplaySource(
            run_id=job.run_id,
            submitted_at=job.submitted_at,
            benchmark_eligible=True,
        )
        for job in reversed(completed_jobs)
    )


def _clean_quality() -> EpisodeQuality:
    """Build protocol-clean quality metadata for lifecycle-only HTTP tests."""
    return EpisodeQuality(
        benchmark_eligible=True,
        protocol=ProtocolReport(total_turn_count=0, invalid_turn_count=0),
    )


def test_http_validation_and_localhost_cors() -> None:
    repository = InMemoryRunStore()
    with TestClient(_app(repository)) as client:
        assert (
            client.post(
                "/api/runs",
                json={"seed": 42, "unknown": True},
            ).status_code
            == 422
        )
        for invalid_seed in (-1, True, 2_147_483_648):
            assert (
                client.post(
                    "/api/runs",
                    json={"seed": invalid_seed},
                ).status_code
                == 422
            )
        assert (
            client.post(
                "/api/runs",
                json={"mode": "replay"},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/runs",
                json={"mode": "replay", "seed": 42, "source_run_id": "missing"},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/runs",
                json={"mode": "replay", "source_run_id": "missing"},
            ).status_code
            == 422
        )
        for retired_mode in ("codex", "openai", "claude"):
            assert (
                client.post(
                    "/api/runs",
                    json={"mode": retired_mode, "seed": 42},
                ).status_code
                == 422
            )
        assert client.post("/api/runs", json={"mode": "model", "seed": 42}).status_code == 422
        assert (
            client.post(
                "/api/runs",
                json={"mode": "model", "model": "gpt-test", "seed": 42},
            ).status_code
            == 409
        )
        assert client.get("/api/run-jobs", params={"limit": 0}).status_code == 422
        assert client.get("/api/run-jobs", params={"offset": -1}).status_code == 422

        response = client.options(
            "/api/runs",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_default_repository_uses_configured_database(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    database = tmp_path / "data" / "runs-v6.sqlite3"
    monkeypatch.setenv("DAIRY_BENCH_HOME", str(tmp_path))
    monkeypatch.delenv("DAIRY_BENCH_NEWAPI_MODEL", raising=False)
    monkeypatch.delenv("DAIRY_BENCH_NEWAPI_MODELS", raising=False)
    monkeypatch.delenv("NEWAPI_API_KEY", raising=False)

    with TestClient(create_app()) as client:
        assert client.get("/api/health").status_code == 200

    assert database.is_file()


def test_default_app_runs_the_v6_scenario(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("DAIRY_BENCH_NEWAPI_MODEL", raising=False)
    monkeypatch.delenv("DAIRY_BENCH_NEWAPI_MODELS", raising=False)
    monkeypatch.delenv("NEWAPI_API_KEY", raising=False)
    repository = InMemoryRunStore()

    with TestClient(create_app(BenchmarkApplication.create(repository))) as client:
        submitted = RunJob.model_validate(
            client.post(
                "/api/runs",
                json={"mode": "baseline", "seed": 42},
            ).json()
        )
        completed = _wait_for_terminal_job(client, submitted.run_id)
        result = EpisodeResult.model_validate(client.get(f"/api/runs/{submitted.run_id}").json())
        turns = client.get(f"/api/runs/{submitted.run_id}/turns").json()

    assert completed.status is RunStatus.COMPLETED
    assert result.scenario == DAIRY_S9_SCENARIO
    assert len(turns) > (DAIRY_S9_SCENARIO.weeks * len(DAIRY_S9_SCENARIO.companies))


def test_model_profile_exposes_the_full_newapi_catalog_without_credentials(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEWAPI_API_KEY", "secret-newapi-key")
    monkeypatch.setenv("DAIRY_BENCH_NEWAPI_MODEL", "gpt-default-model")
    monkeypatch.setenv(
        "DAIRY_BENCH_NEWAPI_MODELS",
        "gpt-default-model,claude-model,gemini-model,deepseek-model",
    )
    repository = InMemoryRunStore()

    with TestClient(create_app(BenchmarkApplication.create(repository))) as client:
        profiles = client.get("/api/policy-profiles").json()

    model = next(profile for profile in profiles if profile["mode"] == "model")
    assert model == {
        "mode": "model",
        "label": "Model agents via NewAPI",
        "available": True,
        "provider": "newapi",
        "model": "gpt-default-model",
        "models": [
            "gpt-default-model",
            "claude-model",
            "gemini-model",
            "deepseek-model",
        ],
        "description": "Each company is controlled by an independent model through NewAPI.",
        "unavailable_reason": None,
    }
    assert "secret-newapi-key" not in str(profiles)


def test_lifespan_reclaims_owned_resources_when_coordinator_close_fails(
    monkeypatch: MonkeyPatch,
) -> None:
    repository = _TrackedOwnedRepository()

    async def fail_coordinator_close(_: object) -> None:
        raise RuntimeError("coordinator close failed")

    monkeypatch.setattr(application_module, "SQLiteRunStore", lambda _: repository)
    monkeypatch.setattr(application_module.RunCoordinator, "close", fail_coordinator_close)

    with (
        raises(RuntimeError, match="coordinator close failed"),
        TestClient(create_app()),
    ):
        assert repository.close_calls == 0

    assert repository.close_calls == 1
