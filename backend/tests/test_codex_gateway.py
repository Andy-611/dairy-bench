"""Contract tests for the isolated Codex runtime adapter."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from openai_codex import ApprovalMode, Sandbox
from openai_codex.types import TurnStatus

from company_bench.agent_models import (
    ModelInfrastructureError,
    ModelOutputError,
    ModelRequest,
)
from company_bench.codex_artifacts import (
    CodexArtifactIdentity,
    CodexArtifactView,
)
from company_bench.codex_gateway import CodexAgentConfig, CodexModelGateway
from company_bench.models import CompanyObservation, FarmDecision


class _FakeThread:
    """Record one SDK thread and return a configured turn result."""

    id = "thread_test"

    def __init__(
        self,
        final_response: str,
        session_path: Path | None = None,
    ) -> None:
        self._final_response = final_response
        self._session_path = session_path
        self.run_calls: list[tuple[str, dict[str, object]]] = []

    async def run(self, input: str, **options: object) -> SimpleNamespace:
        """Return one completed fake Codex turn."""
        self.run_calls.append((input, options))
        usage = SimpleNamespace(
            total=SimpleNamespace(
                cached_input_tokens=2,
                input_tokens=12,
                output_tokens=8,
                reasoning_output_tokens=3,
                total_tokens=20,
            )
        )
        return SimpleNamespace(
            error=None,
            final_response=self._final_response,
            id="turn_test",
            items=[],
            status=TurnStatus.completed,
            usage=usage,
        )

    async def read(self, *, include_turns: bool = False) -> SimpleNamespace:
        """Return the persisted Session metadata surface."""
        return SimpleNamespace(
            thread=SimpleNamespace(
                path=str(self._session_path) if self._session_path else None
            )
        )


class _FakeArtifacts:
    """Record exports without touching the real Codex home."""

    def __init__(self) -> None:
        self.calls: list[tuple[CodexArtifactIdentity, SimpleNamespace, Path | None]] = []

    def export(
        self,
        identity: CodexArtifactIdentity,
        result: SimpleNamespace,
        session_path: Path | None,
    ) -> CodexArtifactView:
        self.calls.append((identity, result, session_path))
        return CodexArtifactView(
            **identity.model_dump(),
            reasoning_markdown="",
            final_output=result.final_response,
        )


class _FailingArtifacts:
    """Simulate a local archive failure after the model has completed."""

    def export(
        self,
        identity: CodexArtifactIdentity,
        result: SimpleNamespace,
        session_path: Path | None,
    ) -> CodexArtifactView:
        raise TimeoutError("artifact disk timed out")


class _FakeCodex:
    """Implement the narrow async client surface without starting a process."""

    def __init__(
        self,
        final_response: str,
        *,
        authenticated: bool = True,
        session_path: Path | None = None,
    ) -> None:
        self.authenticated = authenticated
        self.closed = False
        self.entered = False
        self.thread = _FakeThread(final_response, session_path)
        self.thread_calls: list[dict[str, object]] = []

    async def __aenter__(self) -> _FakeCodex:
        self.entered = True
        return self

    async def account(self, *, refresh_token: bool = False) -> SimpleNamespace:
        """Return only whether a local ChatGPT session exists."""
        return SimpleNamespace(account=object() if self.authenticated else None)

    async def thread_start(self, **options: object) -> _FakeThread:
        """Return one isolated fake thread."""
        self.thread_calls.append(options)
        return self.thread

    async def close(self) -> None:
        """Record runtime shutdown."""
        self.closed = True


def _request(observation: CompanyObservation) -> ModelRequest:
    """Build one provider-neutral farm request."""
    return ModelRequest(
        invocation_id="codex_test.1.farm_a",
        run_id="codex_test",
        observation=observation,
        instructions="Return one farm decision.",
        input_text=observation.model_dump_json(),
    )


def _valid_response() -> str:
    """Return one valid decision envelope."""
    return json.dumps(
        {
            "decision": {
                "kind": "farm",
                "produce_quantity": "10",
                "raw_offer_quantity": "8",
                "minimum_raw_price": "1.40",
            }
        }
    )


def test_codex_gateway_isolates_runtime_and_validates_output(
    first_observation: CompanyObservation,
    tmp_path: Path,
) -> None:
    session_path = tmp_path / "session.jsonl"
    client = _FakeCodex(_valid_response(), session_path=session_path)
    artifacts = _FakeArtifacts()
    gateway = CodexModelGateway(
        CodexAgentConfig(model="test-model", reasoning_effort="low"),
        "farm_a",
        client,
        artifacts,
    )

    result = asyncio.run(gateway.generate(_request(first_observation), FarmDecision))
    workspace = Path(client.thread_calls[0]["cwd"])
    asyncio.run(gateway.close())

    assert result.decision == FarmDecision(
        produce_quantity="10",
        raw_offer_quantity="8",
        minimum_raw_price="1.40",
    )
    assert result.provider == "codex"
    assert result.model == "test-model"
    assert result.response_id == "turn_test"
    assert result.request_id == "thread_test"
    assert result.usage.input_tokens == 12
    assert result.usage.cached_tokens == 2
    assert result.usage.reasoning_tokens == 3
    assert client.entered is True
    assert client.closed is True
    assert workspace.is_dir() is False
    assert len(artifacts.calls) == 1
    identity, _, session_path = artifacts.calls[0]
    assert identity.invocation_id == "codex_test.1.farm_a"
    assert identity.thread_id == "thread_test"
    assert identity.turn_id == "turn_test"
    assert session_path == tmp_path / "session.jsonl"

    thread_options = client.thread_calls[0]
    assert thread_options["approval_mode"] is ApprovalMode.deny_all
    assert "reasoning summary in English" in thread_options["base_instructions"]
    assert thread_options["ephemeral"] is False
    assert thread_options["sandbox"] is Sandbox.read_only
    _, run_options = client.thread.run_calls[0]
    assert run_options["approval_mode"] is ApprovalMode.deny_all
    assert run_options["sandbox"] is Sandbox.read_only
    output_schema = run_options["output_schema"]
    assert output_schema["additionalProperties"] is False
    farm_schema = output_schema["$defs"]["FarmDecision"]
    no_op_schema = output_schema["$defs"]["NoOpDecision"]
    assert set(farm_schema["required"]) == set(farm_schema["properties"])
    assert set(no_op_schema["required"]) == set(no_op_schema["properties"])
    assert '"pattern"' not in json.dumps(output_schema)
    assert run_options["summary"].root.value == "detailed"


def test_codex_runtime_requests_all_public_reasoning() -> None:
    overrides = CodexAgentConfig().runtime_config(Path(".")).config_overrides

    assert "hide_agent_reasoning=false" in overrides
    assert "show_raw_agent_reasoning=true" in overrides


def test_codex_gateway_separates_auth_and_output_failures(
    first_observation: CompanyObservation,
) -> None:
    unauthenticated = _FakeCodex(_valid_response(), authenticated=False)
    missing_auth = CodexModelGateway(
        CodexAgentConfig(model="test-model"),
        "farm_a",
        unauthenticated,
        _FakeArtifacts(),
    )
    with pytest.raises(ModelInfrastructureError, match="codex login"):
        asyncio.run(missing_auth.generate(_request(first_observation), FarmDecision))
    assert unauthenticated.closed is True

    artifacts = _FakeArtifacts()
    invalid_output = CodexModelGateway(
        CodexAgentConfig(model="test-model"),
        "farm_a",
        _FakeCodex('{"decision":{"kind":"farm"}}'),
        artifacts,
    )
    with pytest.raises(ModelOutputError) as caught:
        asyncio.run(invalid_output.generate(_request(first_observation), FarmDecision))
    assert len(artifacts.calls) == 1
    assert caught.value.request_id == "thread_test"
    assert caught.value.response_id == "turn_test"
    assert caught.value.usage.total_tokens == 20
    asyncio.run(invalid_output.close())


def test_artifact_failure_never_repeats_the_model_call(
    first_observation: CompanyObservation,
) -> None:
    client = _FakeCodex(_valid_response())
    gateway = CodexModelGateway(
        CodexAgentConfig(model="test-model", max_attempts=3),
        "farm_a",
        client,
        _FailingArtifacts(),
    )

    with pytest.raises(ModelInfrastructureError, match="artifact disk timed out"):
        asyncio.run(gateway.generate(_request(first_observation), FarmDecision))

    assert len(client.thread_calls) == 1
    assert len(client.thread.run_calls) == 1
    asyncio.run(gateway.close())
