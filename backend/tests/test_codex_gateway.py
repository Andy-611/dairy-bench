"""Contract tests for the isolated Codex runtime adapter."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from openai_codex import ApprovalMode, Sandbox
from openai_codex.types import TurnStatus

import company_bench.codex_gateway as codex_gateway_module
from company_bench.agent_models import (
    CommandModelRequest,
    ModelInfrastructureError,
    ModelOutputError,
    ModelRequest,
)
from company_bench.codex_artifacts import (
    CodexArtifactIdentity,
    CodexArtifactView,
)
from company_bench.codex_gateway import (
    DEFAULT_CODEX_HOME,
    CodexAgentConfig,
    CodexModelGateway,
)
from company_bench.codex_sessions import (
    CodexSessionClient,
    CodexSessionManager,
    CodexSessionRetention,
)
from company_bench.models import CompanyObservation, FarmDecision, ProductId
from company_bench.precision import ECONOMIC_QUANTUM
from company_bench.runtime_models import (
    AgentTurn,
    MarketSide,
    QuoteLevel,
    SetQuoteLadder,
    SimTime,
    WakeReason,
)


def _economic_schema_nodes(value: object) -> list[dict[str, object]]:
    """Collect schema nodes governed by the shared economic quantum."""
    if isinstance(value, dict):
        nodes = (
            [value]
            if value.get("multipleOf") == float(ECONOMIC_QUANTUM)
            else []
        )
        return nodes + [
            node
            for child in value.values()
            for node in _economic_schema_nodes(child)
        ]
    if isinstance(value, list):
        return [
            node
            for child in value
            for node in _economic_schema_nodes(child)
        ]
    return []


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
        self.names: list[str] = []
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
            thread=SimpleNamespace(path=str(self._session_path) if self._session_path else None)
        )

    async def set_name(self, name: str) -> SimpleNamespace:
        """Record the compact benchmark task name."""
        self.names.append(name)
        return SimpleNamespace()


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
        self.archived: list[str] = []
        self.deleted: list[str] = []
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

    async def thread_archive(self, thread_id: str) -> SimpleNamespace:
        """Record one immediate Session archive."""
        self.archived.append(thread_id)
        return SimpleNamespace()

    async def thread_delete(self, thread_id: str) -> None:
        """Record one retention deletion."""
        self.deleted.append(thread_id)

    async def thread_list(self, **options: object) -> SimpleNamespace:
        """Return an empty archived Session page."""
        return SimpleNamespace(data=[], next_cursor=None)

    async def close(self) -> None:
        """Record runtime shutdown."""
        self.closed = True


class _FailingArchiveCodex(_FakeCodex):
    """Fail only after the model result has been exported."""

    async def thread_archive(self, thread_id: str) -> SimpleNamespace:
        raise TimeoutError("Session archive timed out")


class _TrackedSessionManager(CodexSessionManager):
    """Record only the ownership interactions required by the gateway."""

    def __init__(self, retention: CodexSessionRetention) -> None:
        super().__init__(retention)
        self.closed = False
        self.released: list[CodexSessionClient] = []
        self.release_started = asyncio.Event()
        self.release_gate = asyncio.Event()
        self.release_gate.set()

    async def release(self, client: CodexSessionClient) -> None:
        """Record borrowed-client release."""
        self.released.append(client)
        self.release_started.set()
        await self.release_gate.wait()
        await super().release(client)

    async def close(self) -> None:
        """Record manager reclamation."""
        self.closed = True
        await super().close()


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


def _command_request(observation: CompanyObservation) -> CommandModelRequest:
    """Build one event-driven farm command request."""
    turn = AgentTurn(
        turn_id="codex_test.farm_a.t1",
        company_id="farm_a",
        sim_time=SimTime(absolute_minute=540),
        state_version=0,
        turn_number_today=1,
        turn_limit_today=observation.runtime.max_turns_per_company_day,
        wake_reasons=(WakeReason.DAY_OPEN,),
        observation=observation,
        available_cash=observation.cash,
        marked_surplus=Decimal(),
    )
    return CommandModelRequest(
        invocation_id="codex_test.farm_a.t1.provider",
        run_id="codex_test",
        turn=turn,
        instructions="Return one command.",
        input_text=turn.model_dump_json(),
        allowed_commands=("produce", "set_quote_ladder", "wait"),
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
    assert identity.domain_turn_id is None
    assert identity.thread_id == "thread_test"
    assert identity.turn_id == "turn_test"
    assert session_path == tmp_path / "session.jsonl"

    thread_options = client.thread_calls[0]
    assert thread_options["approval_mode"] is ApprovalMode.deny_all
    assert "reasoning summary in English" in thread_options["base_instructions"]
    assert thread_options["ephemeral"] is False
    assert thread_options["sandbox"] is Sandbox.read_only
    assert client.thread.names == ["Dairy Bench | run=codex_test | company=farm_a"]
    assert client.archived == ["thread_test"]
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
    economic_nodes = _economic_schema_nodes(output_schema)
    assert economic_nodes
    assert all(node.get("type") == "number" for node in economic_nodes)
    assert all("anyOf" not in node for node in economic_nodes)
    assert run_options["summary"].root.value == "detailed"


def test_codex_runtime_requests_all_public_reasoning() -> None:
    config = CodexAgentConfig()
    runtime = config.runtime_config(Path("."))
    overrides = runtime.config_overrides

    assert "hide_agent_reasoning=false" in overrides
    assert "show_raw_agent_reasoning=true" in overrides
    assert config.codex_home == DEFAULT_CODEX_HOME.resolve()
    assert runtime.env == {"CODEX_HOME": str(DEFAULT_CODEX_HOME.resolve())}


def test_codex_gateway_closes_its_owned_session_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managers: list[_TrackedSessionManager] = []

    def create_manager(retention: CodexSessionRetention) -> _TrackedSessionManager:
        manager = _TrackedSessionManager(retention)
        managers.append(manager)
        return manager

    monkeypatch.setattr(codex_gateway_module, "CodexSessionManager", create_manager)
    client = _FakeCodex(_valid_response())
    gateway = CodexModelGateway(CodexAgentConfig(model="test-model"), "farm_a", client)

    asyncio.run(gateway.close())

    assert len(managers) == 1
    assert managers[0].closed is True
    assert managers[0].released == []
    assert client.closed is True


def test_codex_gateway_leaves_an_injected_session_manager_open() -> None:
    async def exercise() -> None:
        client = _FakeCodex(_valid_response())
        manager = _TrackedSessionManager(CodexSessionRetention())
        manager.release_gate.clear()
        gateway = CodexModelGateway(
            CodexAgentConfig(model="test-model"),
            "farm_a",
            client,
            session_manager=manager,
        )

        closing = asyncio.create_task(gateway.close())
        await asyncio.wait_for(manager.release_started.wait(), timeout=1)
        assert client.closed is False

        manager.release_gate.set()
        await closing

        assert manager.closed is False
        assert manager.released == [client]
        assert client.closed is True

    asyncio.run(exercise())


def test_codex_config_rejects_the_personal_history_directory() -> None:
    with pytest.raises(ValueError, match="dedicated Codex home"):
        CodexAgentConfig(codex_home=Path.home() / ".codex")


def test_codex_gateway_adapts_one_structured_atomic_command(
    first_observation: CompanyObservation,
) -> None:
    client = _FakeCodex(
        '{"command":{"kind":"set_quote_ladder","product":"raw_milk",'
        '"side":"sell","levels":[{"quantity":"8","limit_price":"1.20"},'
        '{"quantity":"3","limit_price":"1.40"},'
        '{"quantity":"1","limit_price":"1.60"}]}}'
    )
    artifacts = _FakeArtifacts()
    gateway = CodexModelGateway(
        CodexAgentConfig(model="test-model"),
        "farm_a",
        client,
        artifacts,
    )

    result = asyncio.run(gateway.generate_command(_command_request(first_observation)))

    assert result.command == SetQuoteLadder(
        product=ProductId.RAW_MILK,
        side=MarketSide.SELL,
        levels=(
            QuoteLevel(quantity=Decimal("8"), limit_price=Decimal("1.20")),
            QuoteLevel(quantity=Decimal("3"), limit_price=Decimal("1.40")),
            QuoteLevel(quantity=Decimal("1"), limit_price=Decimal("1.60")),
        ),
    )
    assert artifacts.calls[0][0].domain_turn_id == "codex_test.farm_a.t1"
    output_schema = client.thread.run_calls[0][1]["output_schema"]
    assert set(output_schema["required"]) == set(output_schema["properties"])
    serialized_schema = json.dumps(output_schema)
    assert '"anyOf"' in serialized_schema
    assert '"oneOf"' not in serialized_schema
    assert '"discriminator"' not in serialized_schema
    assert '"default"' not in serialized_schema
    assert '"maxItems": 3' in serialized_schema
    asyncio.run(gateway.close())


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ("{}", "invalid Codex structured output"),
        (
            json.dumps(
                {
                    "command": {
                        "kind": "transform",
                        "input_product": "raw_milk",
                        "output_product": "bottled_milk",
                        "input_quantity": "12",
                    }
                }
            ),
            "unauthorized command",
        ),
    ],
    ids=("missing-command", "unauthorized-command"),
)
def test_codex_gateway_rejects_invalid_atomic_commands(
    first_observation: CompanyObservation,
    response: str,
    message: str,
) -> None:
    gateway = CodexModelGateway(
        CodexAgentConfig(model="test-model"),
        "farm_a",
        _FakeCodex(response),
        _FakeArtifacts(),
    )

    with pytest.raises(ModelOutputError, match=message):
        asyncio.run(gateway.generate_command(_command_request(first_observation)))

    asyncio.run(gateway.close())


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
    invalid_client = _FakeCodex('{"decision":{"kind":"farm"}}')
    invalid_output = CodexModelGateway(
        CodexAgentConfig(model="test-model"),
        "farm_a",
        invalid_client,
        artifacts,
    )
    with pytest.raises(ModelOutputError) as caught:
        asyncio.run(invalid_output.generate(_request(first_observation), FarmDecision))
    assert len(artifacts.calls) == 1
    assert invalid_client.archived == ["thread_test"]
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
    assert client.archived == []
    asyncio.run(gateway.close())


def test_archive_failure_never_repeats_the_export_or_model_call(
    first_observation: CompanyObservation,
) -> None:
    client = _FailingArchiveCodex(_valid_response())
    artifacts = _FakeArtifacts()
    gateway = CodexModelGateway(
        CodexAgentConfig(model="test-model", max_attempts=3),
        "farm_a",
        client,
        artifacts,
    )

    with pytest.raises(ModelInfrastructureError, match="Session archive timed out"):
        asyncio.run(gateway.generate(_request(first_observation), FarmDecision))

    assert len(client.thread.run_calls) == 1
    assert len(artifacts.calls) == 1
    asyncio.run(gateway.close())
