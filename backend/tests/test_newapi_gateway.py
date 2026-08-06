"""Contract tests for the NewAPI Anthropic Messages adapter."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from decimal import Decimal

import httpx
import pytest
from anthropic import AsyncAnthropic
from pydantic import SecretStr

import company_bench.newapi_gateway as gateway_module
from company_bench.agent_models import (
    CommandModelRequest,
    ModelInfrastructureError,
    ModelOutputError,
    ModelRequest,
)
from company_bench.models import CompanyObservation, FarmDecision, ProductId
from company_bench.newapi_gateway import (
    DEFAULT_NEWAPI_BASE_URL,
    NewApiClaudeConfig,
    NewApiClaudeGateway,
)
from company_bench.runtime_models import (
    AgentTurn,
    MarketSide,
    QuoteLevel,
    SetQuoteLadder,
    SimTime,
    WakeReason,
)

type ResponseHandler = Callable[[httpx.Request], httpx.Response]


def _config(*, max_attempts: int = 1) -> NewApiClaudeConfig:
    """Return a no-secret-leak test configuration."""
    return NewApiClaudeConfig(
        api_key=SecretStr("test-key"),
        model="claude-test",
        base_url="https://newapi.test",
        max_attempts=max_attempts,
    )


def _observation() -> CompanyObservation:
    """Build the minimal typed observation needed by an adapter contract test."""
    return CompanyObservation.model_construct(
        observation_id="newapi_test.observation",
        scenario_id="newapi_test",
        scenario_days=30,
        day=1,
        company_id="farm_a",
        cash=Decimal("100"),
    )


def _turn(observation: CompanyObservation) -> AgentTurn:
    """Build one farm turn accepted by the provider-neutral seam."""
    return AgentTurn.model_construct(
        turn_id="newapi_test.farm_a.t1",
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


def _command_request(observation: CompanyObservation) -> CommandModelRequest:
    """Build one atomic command request."""
    turn = _turn(observation)
    return CommandModelRequest.model_construct(
        invocation_id="newapi_test.farm_a.t1.provider",
        run_id="newapi_test",
        turn=turn,
        instructions="Call exactly one authorized tool.",
        input_text=turn.model_dump_json(),
        allowed_commands=("produce", "set_quote_ladder", "wait"),
    )


def _model_request(observation: CompanyObservation) -> ModelRequest:
    """Build one legacy daily-decision request."""
    return ModelRequest.model_construct(
        invocation_id="newapi_test.farm_a.daily",
        run_id="newapi_test",
        observation=observation,
        instructions="Return one structured farm decision.",
        input_text=observation.model_dump_json(),
    )


async def _with_gateway[ResultT](
    handler: ResponseHandler,
    operation: Callable[[NewApiClaudeGateway], Awaitable[ResultT]],
    *,
    max_attempts: int = 1,
) -> ResultT:
    """Run one gateway operation over the real SDK and an in-memory transport."""
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = AsyncAnthropic(
            api_key="test-key",
            base_url="https://newapi.test",
            http_client=http_client,
            max_retries=0,
        )
        gateway = NewApiClaudeGateway(_config(max_attempts=max_attempts), client)
        return await operation(gateway)


def _message_response(
    request: httpx.Request,
    content: list[dict[str, object]],
) -> httpx.Response:
    """Return one complete Anthropic Messages payload."""
    return httpx.Response(
        200,
        request=request,
        headers={"request-id": "req_newapi"},
        json={
            "id": "msg_newapi",
            "type": "message",
            "role": "assistant",
            "model": "claude-test",
            "content": content,
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 5,
                "cache_creation_input_tokens": 2,
                "cache_read_input_tokens": 3,
                "output_tokens": 4,
            },
        },
    )


def _tool_use(name: str, arguments: object) -> dict[str, object]:
    """Build one Anthropic tool-use content block."""
    return {
        "type": "tool_use",
        "id": f"tool_{name}",
        "name": name,
        "input": arguments,
    }


def _error_response(
    request: httpx.Request,
    status_code: int,
    error_type: str,
    message: str = "test error",
) -> httpx.Response:
    """Return one Anthropic error envelope."""
    return httpx.Response(
        status_code,
        request=request,
        headers={"request-id": "req_error"},
        json={"type": "error", "error": {"type": error_type, "message": message}},
    )


def _assert_strict_schema(node: object) -> None:
    """Assert recursive normalization required by NewAPI strict tools."""
    if isinstance(node, dict):
        assert "pattern" not in node
        assert "default" not in node
        assert "discriminator" not in node
        assert "oneOf" not in node
        properties = node.get("properties")
        if isinstance(properties, dict):
            assert node["required"] == list(properties)
            assert node["additionalProperties"] is False
        for child in node.values():
            _assert_strict_schema(child)
    elif isinstance(node, list):
        for child in node:
            _assert_strict_schema(child)


def test_config_loads_allowed_models_and_fingerprint_hides_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEWAPI_API_KEY", raising=False)
    monkeypatch.delenv("DAIRY_BENCH_NEWAPI_MODEL", raising=False)
    monkeypatch.delenv("DAIRY_BENCH_NEWAPI_MODELS", raising=False)
    assert NewApiClaudeConfig.from_environment() is None

    monkeypatch.setenv("NEWAPI_API_KEY", "first-secret")
    assert NewApiClaudeConfig.from_environment() is None

    monkeypatch.setenv("DAIRY_BENCH_NEWAPI_MODELS", "claude-a, claude-b,claude-a")
    monkeypatch.setenv("DAIRY_BENCH_NEWAPI_MODEL", "claude-legacy")
    loaded = NewApiClaudeConfig.from_environment()
    assert loaded is not None
    assert loaded.base_url == DEFAULT_NEWAPI_BASE_URL
    assert loaded.model == "claude-a"
    assert loaded.available_models == ("claude-a", "claude-b", "claude-legacy")
    assert loaded.select_model("claude-b").model == "claude-b"
    with pytest.raises(ValueError, match="not configured"):
        loaded.select_model("claude-missing")
    other_key = loaded.model_copy(update={"api_key": SecretStr("second-secret")})
    assert loaded.fingerprint == other_key.fingerprint
    assert "first-secret" not in loaded.fingerprint


@pytest.mark.parametrize(
    "base_url",
    (
        "http://newapi.test",
        "https://user:password@newapi.test",
        "https://newapi.test/v1",
    ),
)
def test_config_rejects_unsafe_provider_origins(base_url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS origin"):
        NewApiClaudeConfig(
            api_key=SecretStr("test-key"),
            model="claude-test",
            base_url=base_url,
        )


def test_newapi_gateway_sends_native_tools_and_parses_command() -> None:
    observation = _observation()
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://newapi.test/v1/messages"
        requests.append(json.loads(request.content))
        return _message_response(
            request,
            [
                _tool_use(
                    "set_quote_ladder",
                    {
                        "product": "raw_milk",
                        "side": "sell",
                        "levels": [
                            {"quantity": "8", "limit_price": "1.20"},
                            {"quantity": "3", "limit_price": "1.40"},
                        ],
                    },
                )
            ],
        )

    async def operation(gateway: NewApiClaudeGateway) -> object:
        return await gateway.generate_command(_command_request(observation))

    result = asyncio.run(_with_gateway(handler, operation))

    assert result.command == SetQuoteLadder(
        product=ProductId.RAW_MILK,
        side=MarketSide.SELL,
        levels=(
            QuoteLevel(quantity=Decimal("8"), limit_price=Decimal("1.20")),
            QuoteLevel(quantity=Decimal("3"), limit_price=Decimal("1.40")),
        ),
    )
    assert result.provider == "newapi"
    assert result.response_id == "msg_newapi"
    assert result.request_id == "req_newapi"
    assert result.usage.input_tokens == 10
    assert result.usage.cached_tokens == 3
    assert result.usage.output_tokens == 4
    assert result.usage.total_tokens == 14

    payload = requests[0]
    assert payload["model"] == "claude-test"
    assert payload["max_tokens"] == 2048
    assert payload["system"] == "Call exactly one authorized tool."
    assert payload["messages"] == [
        {"role": "user", "content": _turn(observation).model_dump_json()}
    ]
    assert payload["tool_choice"] == {
        "type": "any",
        "disable_parallel_tool_use": True,
    }
    assert {tool["name"] for tool in payload["tools"]} == {
        "produce",
        "set_quote_ladder",
        "wait",
    }
    for tool in payload["tools"]:
        assert tool["strict"] is True
        _assert_strict_schema(tool["input_schema"])


def test_newapi_gateway_generates_typed_daily_decision() -> None:
    observation = _observation()

    def handler(request: httpx.Request) -> httpx.Response:
        tool = json.loads(request.content)["tools"][0]
        assert tool["strict"] is True
        _assert_strict_schema(tool["input_schema"])
        return _message_response(
            request,
            [
                _tool_use(
                    "submit_decision",
                    {
                        "decision": {
                            "kind": "farm",
                            "produce_quantity": "10",
                            "raw_offer_quantity": "8",
                            "minimum_raw_price": "1.40",
                        }
                    },
                )
            ],
        )

    async def operation(gateway: NewApiClaudeGateway) -> object:
        return await gateway.generate(_model_request(observation), FarmDecision)

    result = asyncio.run(_with_gateway(handler, operation))
    assert result.decision == FarmDecision(
        produce_quantity="10",
        raw_offer_quantity="8",
        minimum_raw_price="1.40",
    )


@pytest.mark.parametrize(
    "content",
    [
        [],
        [_tool_use("wait", {}), _tool_use("produce", {"product": "raw_milk", "quantity": "1"})],
        [
            _tool_use(
                "transform",
                {
                    "input_product": "raw_milk",
                    "output_product": "bottled_milk",
                    "input_quantity": "1",
                },
            )
        ],
        [_tool_use("produce", {"product": "raw_milk", "quantity": "-1"})],
        [_tool_use("produce", {"kind": "produce", "product": "raw_milk", "quantity": "1"})],
    ],
    ids=("missing", "multiple", "unauthorized", "invalid", "model-supplied-kind"),
)
def test_newapi_gateway_rejects_invalid_command_output(
    content: list[dict[str, object]],
) -> None:
    observation = _observation()

    def handler(request: httpx.Request) -> httpx.Response:
        return _message_response(request, content)

    async def operation(gateway: NewApiClaudeGateway) -> object:
        return await gateway.generate_command(_command_request(observation))

    with pytest.raises(ModelOutputError, match="invalid Claude tool call") as raised:
        asyncio.run(_with_gateway(handler, operation))
    assert raised.value.response_id == "msg_newapi"
    assert raised.value.request_id == "req_newapi"


def test_newapi_gateway_does_not_retry_authentication_failure() -> None:
    observation = _observation()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _error_response(request, 401, "authentication_error")

    async def operation(gateway: NewApiClaudeGateway) -> object:
        return await gateway.generate_command(_command_request(observation))

    with pytest.raises(ModelInfrastructureError, match="AuthenticationError"):
        asyncio.run(_with_gateway(handler, operation, max_attempts=3))
    assert calls == 1


def test_newapi_gateway_redacts_key_echoed_by_provider() -> None:
    observation = _observation()

    def handler(request: httpx.Request) -> httpx.Response:
        return _error_response(
            request,
            401,
            "authentication_error",
            "provider echoed test-key",
        )

    async def operation(gateway: NewApiClaudeGateway) -> object:
        return await gateway.generate_command(_command_request(observation))

    with pytest.raises(ModelInfrastructureError) as raised:
        asyncio.run(_with_gateway(handler, operation))
    assert "test-key" not in str(raised.value)
    assert "[REDACTED]" in str(raised.value)


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [(429, "rate_limit_error"), (500, "api_error")],
)
def test_newapi_gateway_retries_temporary_statuses(
    status_code: int,
    error_type: str,
) -> None:
    observation = _observation()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _error_response(request, status_code, error_type)
        return _message_response(request, [_tool_use("wait", {"until": None, "alerts": []})])

    async def operation(gateway: NewApiClaudeGateway) -> object:
        return await gateway.generate_command(_command_request(observation))

    result = asyncio.run(_with_gateway(handler, operation, max_attempts=2))
    assert result.attempts == 2
    assert calls == 2


def test_newapi_gateway_retries_timeout_then_fails() -> None:
    observation = _observation()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("test timeout", request=request)

    async def operation(gateway: NewApiClaudeGateway) -> object:
        return await gateway.generate_command(_command_request(observation))

    with pytest.raises(ModelInfrastructureError, match="APITimeoutError"):
        asyncio.run(_with_gateway(handler, operation, max_attempts=2))
    assert calls == 2


def test_newapi_gateway_closes_only_owned_client(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        """Minimal close-observable AsyncAnthropic substitute."""

        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    owned = FakeClient()
    monkeypatch.setattr(gateway_module, "AsyncAnthropic", lambda **_: owned)
    owned_gateway = NewApiClaudeGateway(_config())
    asyncio.run(owned_gateway.close())
    assert owned.closed is True

    injected = FakeClient()
    injected_gateway = NewApiClaudeGateway(_config(), injected)  # type: ignore[arg-type]
    asyncio.run(injected_gateway.close())
    assert injected.closed is False
