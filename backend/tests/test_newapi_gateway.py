"""Contract tests for the provider-neutral NewAPI adapter."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from company_bench.agents.contracts import (
    CommandModelRequest,
    ModelCompatibilityError,
    ModelInfrastructureError,
    ModelOutputError,
)
from company_bench.agents.providers.newapi import (
    DEFAULT_NEWAPI_BASE_URL,
    NewApiConfig,
    NewApiModelGateway,
)
from company_bench.domain.models import CompanyObservation, ProductId
from company_bench.runtime.models import (
    AgentTurn,
    MarketSide,
    QuoteLevel,
    SetQuoteLadder,
    SimTime,
    WakeReason,
)

type ResponseHandler = Callable[[httpx.Request], httpx.Response]


def _config(*, max_attempts: int = 1) -> NewApiConfig:
    """Return a no-secret-leak test configuration."""
    return NewApiConfig(
        api_key=SecretStr("test-key"),
        model="gpt-test",
        models=("gpt-test", "claude-test", "gemini-test", "deepseek-test"),
        base_url="https://newapi.test/v1",
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


async def _with_gateway[ResultT](
    handler: ResponseHandler,
    operation: Callable[[NewApiModelGateway], Awaitable[ResultT]],
    *,
    max_attempts: int = 1,
) -> ResultT:
    """Run one gateway operation over an in-memory HTTP transport."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = NewApiModelGateway(_config(max_attempts=max_attempts), client)
        return await operation(gateway)


def _completion_response(
    request: httpx.Request,
    calls: list[dict[str, object]],
    *,
    finish_reason: str = "tool_calls",
    completion_tokens: int = 4,
    reasoning_tokens: int = 2,
) -> httpx.Response:
    """Return one OpenAI-compatible Chat Completions payload."""
    return httpx.Response(
        200,
        request=request,
        headers={"x-request-id": "req_newapi"},
        json={
            "id": "chatcmpl_newapi",
            "object": "chat.completion",
            "model": "gpt-test",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": calls,
                    },
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": completion_tokens,
                "total_tokens": 10 + completion_tokens,
                "prompt_tokens_details": {"cached_tokens": 3},
                "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
            },
        },
    )


def _tool_call(name: str, arguments: object) -> dict[str, object]:
    """Build one OpenAI-compatible function call."""
    return {
        "id": f"call_{name}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
        },
    }


def _error_response(
    request: httpx.Request,
    status_code: int,
    error_type: str,
    message: str = "test error",
) -> httpx.Response:
    """Return one NewAPI-compatible error envelope."""
    return httpx.Response(
        status_code,
        request=request,
        headers={"x-request-id": "req_error"},
        json={"error": {"type": error_type, "message": message}},
    )


def test_config_loads_every_model_family_and_hides_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEWAPI_API_KEY", raising=False)
    monkeypatch.delenv("DAIRY_BENCH_NEWAPI_MODEL", raising=False)
    monkeypatch.delenv("DAIRY_BENCH_NEWAPI_MODELS", raising=False)
    monkeypatch.delenv("DAIRY_BENCH_NEWAPI_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("DAIRY_BENCH_NEWAPI_MAX_ATTEMPTS", raising=False)
    assert NewApiConfig.from_environment() is None

    monkeypatch.setenv("NEWAPI_API_KEY", "first-secret")
    monkeypatch.setenv(
        "DAIRY_BENCH_NEWAPI_MODELS",
        "gpt-test,claude-test,gemini-test,deepseek-test,gpt-test",
    )
    loaded = NewApiConfig.from_environment()
    assert loaded is not None
    assert loaded.base_url == DEFAULT_NEWAPI_BASE_URL
    assert loaded.timeout_seconds == 300
    assert loaded.max_attempts == 3
    assert loaded.available_models == (
        "gpt-test",
        "claude-test",
        "gemini-test",
        "deepseek-test",
    )
    assert loaded.select_model("gemini-test").model == "gemini-test"
    with pytest.raises(ValueError, match="not configured"):
        loaded.select_model("missing-test")
    other_key = loaded.model_copy(update={"api_key": SecretStr("second-secret")})
    assert loaded.fingerprint == other_key.fingerprint
    assert "first-secret" not in loaded.fingerprint


@pytest.mark.parametrize(
    "base_url",
    (
        "http://newapi.test/v1",
        "https://user:password@newapi.test/v1",
        "https://newapi.test",
        "https://newapi.test/v2",
        "https://newapi.test/v1?key=secret",
    ),
)
def test_config_rejects_unsafe_provider_endpoints(base_url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS /v1 endpoint"):
        NewApiConfig(api_key=SecretStr("test-key"), model="gpt-test", base_url=base_url)


def test_gateway_sends_common_tools_and_parses_command() -> None:
    observation = _observation()
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://newapi.test/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        requests.append(json.loads(request.content))
        return _completion_response(
            request,
            [
                _tool_call(
                    "set_quote_ladder",
                    {
                        "product": "raw_milk",
                        "side": "sell",
                        "levels": [
                            {"quantity": 8, "limit_price": 1.2},
                            {"quantity": 3, "limit_price": 1.4},
                        ],
                    },
                )
            ],
        )

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_command(_command_request(observation))

    result = asyncio.run(_with_gateway(handler, operation))

    assert result.command == SetQuoteLadder(
        product=ProductId.RAW_MILK,
        side=MarketSide.SELL,
        levels=(
            QuoteLevel(quantity=Decimal("8"), limit_price=Decimal("1.2")),
            QuoteLevel(quantity=Decimal("3"), limit_price=Decimal("1.4")),
        ),
    )
    assert result.provider == "newapi"
    assert result.response_id == "chatcmpl_newapi"
    assert result.request_id == "req_newapi"
    assert result.usage.input_tokens == 10
    assert result.usage.cached_tokens == 3
    assert result.usage.output_tokens == 4
    assert result.usage.reasoning_tokens == 2
    assert result.usage.total_tokens == 14

    payload = requests[0]
    assert payload["model"] == "gpt-test"
    assert payload["messages"] == [
        {"role": "system", "content": "Call exactly one authorized tool."},
        {"role": "user", "content": _turn(observation).model_dump_json()},
    ]
    assert payload["tool_choice"] == "required"
    assert payload["max_tokens"] == 131_072
    assert payload["n"] == 1
    assert payload["stream"] is False
    tools = payload["tools"]
    assert isinstance(tools, list)
    assert {tool["function"]["name"] for tool in tools} == {
        "produce",
        "set_quote_ladder",
        "wait",
    }
    ladder = next(tool for tool in tools if tool["function"]["name"] == "set_quote_ladder")
    assert ladder["type"] == "function"
    parameters = ladder["function"]["parameters"]
    assert parameters["additionalProperties"] is False
    assert parameters["$defs"]["OrderQuantity"]["type"] == "number"
    assert parameters["$defs"]["PositiveMoney"]["type"] == "number"


def test_missing_required_tool_call_is_a_compatibility_failure() -> None:
    observation = _observation()

    def handler(request: httpx.Request) -> httpx.Response:
        return _completion_response(request, [])

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_command(_command_request(observation))

    with pytest.raises(ModelCompatibilityError, match="required function call") as raised:
        asyncio.run(_with_gateway(handler, operation))
    assert raised.value.response_id == "chatcmpl_newapi"


def test_gateway_negotiates_and_remembers_omitted_tool_choice() -> None:
    observation = _observation()
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return _error_response(
                request,
                400,
                "invalid_request_error",
                "RuntimeError: Thinking mode does not support this tool_choice",
            )
        return _completion_response(request, [_tool_call("wait", {})])

    async def operation(gateway: NewApiModelGateway) -> tuple[object, object]:
        command_request = _command_request(observation)
        return (
            await gateway.generate_command(command_request),
            await gateway.generate_command(command_request),
        )

    first, second = asyncio.run(_with_gateway(handler, operation, max_attempts=2))

    assert first.attempts == 2
    assert second.attempts == 1
    assert requests[0]["tool_choice"] == "required"
    assert all("tool_choice" not in request for request in requests[1:])
    assert all("tools" in request for request in requests)


def test_gateway_still_requires_a_tool_call_after_compatibility_fallback() -> None:
    observation = _observation()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _error_response(
                request,
                400,
                "invalid_request_error",
                "Thinking mode does not support this tool_choice",
            )
        return _completion_response(request, [])

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_command(_command_request(observation))

    with pytest.raises(ModelCompatibilityError, match="required function call") as raised:
        asyncio.run(_with_gateway(handler, operation, max_attempts=2))
    assert raised.value.attempts == 2
    assert calls == 2


def test_gateway_reports_an_exhausted_fixed_output_budget() -> None:
    observation = _observation()

    def handler(request: httpx.Request) -> httpx.Response:
        return _completion_response(
            request,
            [],
            finish_reason="length",
            completion_tokens=131_072,
            reasoning_tokens=131_072,
        )

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_command(_command_request(observation))

    with pytest.raises(ModelCompatibilityError, match="fixed 131072-token") as raised:
        asyncio.run(_with_gateway(handler, operation))
    assert raised.value.usage.output_tokens == 131_072
    assert raised.value.usage.reasoning_tokens == 131_072


@pytest.mark.parametrize(
    "calls",
    [
        [_tool_call("wait", {}), _tool_call("produce", {"product": "raw_milk", "quantity": 1})],
        [
            _tool_call(
                "transform",
                {
                    "input_product": "raw_milk",
                    "output_product": "bottled_milk",
                    "input_quantity": 1,
                },
            )
        ],
        [_tool_call("produce", {"product": "raw_milk", "quantity": -1})],
        [_tool_call("produce", {"product": "raw_milk", "quantity": 1.00001})],
        [_tool_call("produce", {"kind": "produce", "product": "raw_milk", "quantity": 1})],
        [_tool_call("wait", "not-json")],
    ],
    ids=("multiple", "unauthorized", "invalid", "overprecision", "kind", "malformed"),
)
def test_gateway_rejects_invalid_command_output(calls: list[dict[str, object]]) -> None:
    observation = _observation()

    def handler(request: httpx.Request) -> httpx.Response:
        return _completion_response(request, calls)

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_command(_command_request(observation))

    with pytest.raises(ModelOutputError, match="invalid NewAPI tool call") as raised:
        asyncio.run(_with_gateway(handler, operation))
    assert raised.value.response_id == "chatcmpl_newapi"
    assert raised.value.request_id == "req_newapi"


def test_gateway_fails_fast_for_an_incompatible_endpoint() -> None:
    observation = _observation()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _error_response(request, 400, "invalid_request_error", "tools unsupported")

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_command(_command_request(observation))

    with pytest.raises(ModelCompatibilityError, match="tools unsupported"):
        asyncio.run(_with_gateway(handler, operation, max_attempts=3))
    assert calls == 1


def test_gateway_does_not_retry_authentication_failure() -> None:
    observation = _observation()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _error_response(request, 401, "authentication_error")

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_command(_command_request(observation))

    with pytest.raises(ModelInfrastructureError, match="NewAPI HTTP 401"):
        asyncio.run(_with_gateway(handler, operation, max_attempts=3))
    assert calls == 1


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    ((400, "bad_response_status_code"), (429, "rate_limit_error"), (500, "api_error")),
)
def test_gateway_retries_temporary_or_wrapped_failures(
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
        return _completion_response(request, [_tool_call("wait", {})])

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_command(_command_request(observation))

    result = asyncio.run(_with_gateway(handler, operation, max_attempts=2))
    assert result.attempts == 2
    assert calls == 2


def test_gateway_classifies_exhausted_wrapped_failure_as_infrastructure() -> None:
    observation = _observation()

    def handler(request: httpx.Request) -> httpx.Response:
        return _error_response(request, 400, "bad_response_status_code")

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_command(_command_request(observation))

    with pytest.raises(ModelInfrastructureError, match="NewAPI HTTP 400") as raised:
        asyncio.run(_with_gateway(handler, operation, max_attempts=2))
    assert raised.value.attempts == 2


def test_gateway_retries_timeout_then_fails() -> None:
    observation = _observation()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("test timeout", request=request)

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_command(_command_request(observation))

    with pytest.raises(ModelInfrastructureError, match="test timeout") as raised:
        asyncio.run(_with_gateway(handler, operation, max_attempts=2))
    assert calls == 2
    assert raised.value.attempts == 2


def test_gateway_redacts_key_echoed_by_provider() -> None:
    observation = _observation()

    def handler(request: httpx.Request) -> httpx.Response:
        return _error_response(request, 401, "authentication_error", "echoed test-key")

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_command(_command_request(observation))

    with pytest.raises(ModelInfrastructureError) as raised:
        asyncio.run(_with_gateway(handler, operation))
    assert "test-key" not in str(raised.value)
    assert "[REDACTED]" in str(raised.value)


def test_gateway_closes_only_its_owned_client() -> None:
    owned_gateway = NewApiModelGateway(_config())
    owned_client = owned_gateway._client
    asyncio.run(owned_gateway.close())
    assert owned_client.is_closed

    async def verify_injected_client() -> None:
        client = httpx.AsyncClient()
        gateway = NewApiModelGateway(_config(), client)
        await gateway.close()
        assert not client.is_closed
        await client.aclose()

    asyncio.run(verify_injected_client())
