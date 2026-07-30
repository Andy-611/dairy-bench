import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from company_bench.codex_artifacts import (
    CodexArtifactIdentity,
    CodexArtifactStore,
)
from company_bench.dairy_scenario import DAIRY_V1_SCENARIO, DAIRY_V2_SCENARIO
from company_bench.models import (
    CompanyObservation,
    EpisodeResult,
    NoOpDecision,
    PolicyKind,
)
from company_bench.policy_factory import PolicyFactory
from company_bench.repository import MemoryRunRepository
from company_bench.run_models import (
    InvocationOutcome,
    PolicyInvocation,
    RunJob,
    RunStatus,
)
from company_bench.runtime_models import Produce
from company_bench.web import create_app


def _app(repository: MemoryRunRepository) -> FastAPI:
    """Create an app with deterministic server-side policy availability."""
    factory = PolicyFactory(DAIRY_V1_SCENARIO, repository)
    return create_app(repository, factory)


def _wait_for_terminal_job(
    client: TestClient,
    run_id: str,
    timeout_seconds: float = 5,
) -> RunJob:
    """Poll the public job endpoint until the run reaches a terminal state."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.get(f"/api/run-jobs/{run_id}")
        assert response.status_code == 200
        job = RunJob.model_validate(response.json())
        if job.status.terminal:
            return job
        time.sleep(0.01)
    raise AssertionError(f"run job {run_id} did not finish within {timeout_seconds}s")


def test_run_list_and_detail_http_flow() -> None:
    repository = MemoryRunRepository()
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
        assert completed.current_day == completed.total_days == 30

        detail = client.get(f"/api/runs/{submitted.run_id}")
        assert detail.status_code == 200
        result = EpisodeResult.model_validate(detail.json())
        assert result.seed == 42
        assert len(result.decisions) == 6 * 30
        assert len(result.snapshots) == 30

        summaries = client.get("/api/runs").json()
        assert summaries[0]["run_id"] == result.run_id
        assert summaries[0]["scenario_id"] == "flow.dairy.base.s6.v1"

        assert client.get(f"/api/runs/{result.run_id}/invocations").json() == []
        profiles = client.get("/api/policy-profiles").json()
        assert [profile["mode"] for profile in profiles] == [
            "baseline",
            "codex",
            "openai",
            "replay",
        ]
        assert (
            next(profile for profile in profiles if profile["mode"] == "codex")["available"]
            is False
        )
        assert (
            next(profile for profile in profiles if profile["mode"] == "openai")["available"]
            is False
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
        assert replay.decisions == result.decisions
        assert replay.events == result.events
        assert replay.snapshots == result.snapshots
        assert replay.score == result.score

        assert client.get("/api/runs/missing").status_code == 404
        assert client.get("/api/run-jobs/missing").status_code == 404
        assert client.get("/api/runs/missing/invocations").status_code == 404


def test_v2_http_flow_exposes_atomic_turn_journal() -> None:
    repository = MemoryRunRepository()
    factory = PolicyFactory(DAIRY_V2_SCENARIO, repository)
    with TestClient(create_app(repository, factory)) as client:
        submitted = RunJob.model_validate(
            client.post(
                "/api/runs",
                json={"mode": "baseline", "seed": 42},
            ).json()
        )
        completed = _wait_for_terminal_job(client, submitted.run_id)
        assert completed.status is RunStatus.COMPLETED

        result = EpisodeResult.model_validate(client.get(f"/api/runs/{submitted.run_id}").json())
        turns = client.get(f"/api/runs/{submitted.run_id}/turns").json()

        assert result.scenario.scenario_id == "flow.dairy.base.s6.v2"
        assert result.decisions == ()
        assert len(turns) > DAIRY_V2_SCENARIO.days * len(DAIRY_V2_SCENARIO.companies)
        assert turns[0]["turn"]["state_version"] == 0
        assert turns[0]["envelope"]["command"]["kind"] in {
            "produce",
            "place_order",
        }

        replay_job = _wait_for_terminal_job(
            client,
            RunJob.model_validate(
                client.post(
                    "/api/runs",
                    json={
                        "mode": "replay",
                        "source_run_id": submitted.run_id,
                    },
                ).json()
            ).run_id,
        )
        replay = EpisodeResult.model_validate(client.get(f"/api/runs/{replay_job.run_id}").json())
        assert replay.events == result.events
        assert replay.snapshots == result.snapshots
        assert replay.score == result.score
        assert all(policy.source_run_id == submitted.run_id for policy in replay.policies)


def test_http_validation_and_localhost_cors() -> None:
    repository = MemoryRunRepository()
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
        assert (
            client.post(
                "/api/runs",
                json={"mode": "codex", "seed": 42},
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/runs",
                json={"mode": "openai", "seed": 42},
            ).status_code
            == 409
        )
        assert client.get("/api/runs", params={"limit": 0}).status_code == 422

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
    database = tmp_path / "configured.sqlite3"
    monkeypatch.setenv("DAIRY_BENCH_DB", str(database))
    monkeypatch.delenv("DAIRY_BENCH_CODEX_ENABLED", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with TestClient(create_app()) as client:
        assert client.get("/api/health").status_code == 200

    assert database.is_file()


def test_default_app_runs_the_v2_scenario(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("DAIRY_BENCH_CODEX_ENABLED", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    repository = MemoryRunRepository()

    with TestClient(create_app(repository)) as client:
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
    assert result.scenario == DAIRY_V2_SCENARIO
    assert len(turns) > DAIRY_V2_SCENARIO.days * len(DAIRY_V2_SCENARIO.companies)


def test_codex_profile_can_be_enabled_without_exposing_credentials(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAIRY_BENCH_CODEX_ENABLED", "true")
    repository = MemoryRunRepository()

    with TestClient(create_app(repository)) as client:
        profiles = client.get("/api/policy-profiles").json()

    codex = next(profile for profile in profiles if profile["mode"] == "codex")
    assert codex == {
        "mode": "codex",
        "label": "Codex company agents",
        "available": True,
        "provider": "codex",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "low",
        "description": "Each company is controlled by an independent Codex runtime.",
        "unavailable_reason": None,
    }


def test_codex_artifacts_are_loaded_lazily_from_run_files(
    first_observation: CompanyObservation,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifacts_root = tmp_path / "run_artifacts"
    monkeypatch.setenv("DAIRY_BENCH_ARTIFACTS_DIR", str(artifacts_root))
    repository = MemoryRunRepository()
    now = datetime.now(UTC)
    run_id = "run_trace"
    invocation_id = f"{run_id}.1.{first_observation.company_id}"
    repository.save_job(
        RunJob(
            run_id=run_id,
            mode=PolicyKind.CODEX,
            seed=42,
            scenario_id=DAIRY_V1_SCENARIO.scenario_id,
            total_days=DAIRY_V1_SCENARIO.days,
            submitted_at=now,
        )
    )
    repository.record_invocation(
        PolicyInvocation(
            invocation_id=invocation_id,
            run_id=run_id,
            company_id=first_observation.company_id,
            day=1,
            observation=first_observation,
            provider="codex",
            model="gpt-5.6-terra",
            prompt_version="test",
            prompt_hash="hash",
            started_at=now,
            finished_at=now,
            outcome=InvocationOutcome.SUCCESS,
            decision=NoOpDecision(reason="test"),
            request_id="thread_trace",
            response_id="turn_trace",
        )
    )
    identity = CodexArtifactIdentity(
        invocation_id=invocation_id,
        run_id=run_id,
        company_id=first_observation.company_id,
        day=1,
        model="gpt-5.6-terra",
        thread_id="thread_trace",
        turn_id="turn_trace",
    )
    CodexArtifactStore(artifacts_root).export(
        identity,
        SimpleNamespace(
            final_response='{"decision":{"kind":"no_op","reason":"test"}}',
            items=[],
        ),
        None,
    )

    with TestClient(_app(repository)) as client:
        response = client.get(f"/api/runs/{run_id}/invocations/{invocation_id}/artifacts")

    assert response.status_code == 200
    assert response.json()["thread_id"] == "thread_trace"
    assert response.json()["turn_id"] == "turn_trace"
    assert '"kind": "no_op"' in response.json()["final_output"]


def test_v2_codex_artifacts_are_resolved_by_domain_turn(
    first_observation: CompanyObservation,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifacts_root = tmp_path / "run_artifacts"
    monkeypatch.setenv("DAIRY_BENCH_ARTIFACTS_DIR", str(artifacts_root))
    repository = MemoryRunRepository()
    now = datetime.now(UTC)
    run_id = "run_v2_trace"
    domain_turn_id = f"{run_id}.{first_observation.company_id}.t1"
    invocation_id = f"{domain_turn_id}.provider"
    repository.save_job(
        RunJob(
            run_id=run_id,
            mode=PolicyKind.CODEX,
            seed=42,
            scenario_id=DAIRY_V2_SCENARIO.scenario_id,
            total_days=DAIRY_V2_SCENARIO.days,
            submitted_at=now,
        )
    )
    repository.record_invocation(
        PolicyInvocation(
            invocation_id=invocation_id,
            run_id=run_id,
            company_id=first_observation.company_id,
            day=1,
            observation=first_observation,
            provider="codex",
            model="gpt-5.6-terra",
            prompt_version="test",
            prompt_hash="hash",
            started_at=now,
            finished_at=now,
            outcome=InvocationOutcome.SUCCESS,
            domain_turn_id=domain_turn_id,
            sim_minute=540,
            state_version=0,
            command=Produce(product="raw_milk", quantity="1"),
            request_id="thread_v2_trace",
            provider_turn_id="provider_turn_v2_trace",
        )
    )
    identity = CodexArtifactIdentity(
        invocation_id=invocation_id,
        run_id=run_id,
        company_id=first_observation.company_id,
        day=1,
        model="gpt-5.6-terra",
        thread_id="thread_v2_trace",
        turn_id="provider_turn_v2_trace",
        domain_turn_id=domain_turn_id,
    )
    CodexArtifactStore(artifacts_root).export(
        identity,
        SimpleNamespace(
            final_response='{"command":{"kind":"produce","quantity":"1"}}',
            items=[],
        ),
        None,
    )

    with TestClient(_app(repository)) as client:
        response = client.get(f"/api/runs/{run_id}/invocations/{invocation_id}/artifacts")

    assert response.status_code == 200
    assert response.json()["domain_turn_id"] == domain_turn_id
    assert '"kind": "produce"' in response.json()["final_output"]
