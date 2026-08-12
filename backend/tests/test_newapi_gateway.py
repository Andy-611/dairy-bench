"""Contract tests for the provider-neutral NewAPI adapter."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from company_bench.agents.contracts import (
    DecisionModelRequest,
    DecisionModelResult,
    ModelCompatibilityError,
    ModelConfigurationError,
    ModelInfrastructureError,
    ModelOutputError,
    ModelQuotaExhaustedError,
)
from company_bench.agents.providers import newapi as newapi_module
from company_bench.agents.providers.capabilities import ModelCapabilityError
from company_bench.agents.providers.newapi import (
    DEFAULT_NEWAPI_BASE_URL,
    NewApiAnthropicMessagesGateway,
    NewApiCapabilityProbe,
    NewApiConfig,
    NewApiModelConfig,
    NewApiModelGateway,
    NewApiResponsesGateway,
    NewApiTransport,
    NewApiWireProtocol,
)
from company_bench.domain.models import CompanyObservation, ProductId, ProtocolIssueKind
from company_bench.runs.models import ProviderAttemptOutcome
from company_bench.runtime.models import (
    ActionDecision,
    AgentTurn,
    AttentionPlan,
    MarketSide,
    QuoteLevel,
    SetQuoteLadder,
    SimDay,
    WakeReason,
)

type ResponseHandler = Callable[[httpx.Request], httpx.Response]


def _assert_strict_object_schemas(schema: object) -> None:
    """Require every nested object to satisfy the Responses strict contract."""
    if isinstance(schema, list):
        for item in schema:
            _assert_strict_object_schemas(item)
        return
    if not isinstance(schema, dict):
        return

    properties = schema.get("properties")
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False
    if isinstance(properties, dict):
        assert schema.get("required") == list(properties)
    for value in schema.values():
        _assert_strict_object_schemas(value)


def _config(
    *,
    max_attempts: int = 1,
    max_input_tokens: int = 128_000,
    max_output_tokens: int = 128_000,
) -> NewApiModelConfig:
    """Return a no-secret-leak test configuration."""
    return NewApiModelConfig(
        api_key=SecretStr("test-key"),
        model="gpt-test",
        models=("gpt-test", "claude-test", "gemini-test", "deepseek-test"),
        base_url="https://newapi.test/v1",
        max_attempts=max_attempts,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
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
        sim_day=SimDay(absolute_day=540),
        state_version=0,
        turn_number_this_week=1,
        turn_limit_this_week=observation.runtime.max_turns_per_company_week,
        wake_reasons=(WakeReason.WEEK_OPEN,),
        observation=observation,
        available_cash=observation.cash,
        marked_surplus=Decimal(),
    )


def _decision_request(observation: CompanyObservation) -> DecisionModelRequest:
    """Build one atomic decision request."""
    turn = _turn(observation)
    return DecisionModelRequest.model_construct(
        invocation_id="newapi_test.farm_a.t1.provider",
        run_id="newapi_test",
        turn=turn,
        instructions="Call exactly one authorized tool.",
        input_text=turn.model_dump_json(),
        allowed_tools=("produce", "set_quote_ladder", "idle"),
    )


async def _with_gateway[ResultT](
    handler: ResponseHandler,
    operation: Callable[[NewApiModelGateway], Awaitable[ResultT]],
    *,
    max_attempts: int = 1,
    max_input_tokens: int = 128_000,
) -> ResultT:
    """Run one gateway operation over an in-memory HTTP transport."""
    config = _config(
        max_attempts=max_attempts,
        max_input_tokens=max_input_tokens,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = NewApiModelGateway(config, NewApiTransport(config, client=client))
        return await operation(gateway)


async def _probe_limit(
    handler: ResponseHandler,
    candidate: int | None,
    protocol: NewApiWireProtocol = NewApiWireProtocol.CHAT_COMPLETIONS,
) -> int:
    """Calibrate one model over an in-memory NewAPI transport."""
    config = _config()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = NewApiTransport(config, client=client)
        return await NewApiCapabilityProbe(config, transport, protocol).max_output_tokens(
            "gemini-test",
            candidate,
        )


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


def _responses_sse_response(
    request: httpx.Request,
    calls: list[dict[str, object]],
) -> httpx.Response:
    """Return one native Responses SSE stream."""
    frames = [
        (
            "response.output_item.done",
            {"type": "response.output_item.done", "item": call},
        )
        for call in calls
    ]
    frames.append(
        (
            "response.completed",
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_newapi",
                    "model": "gpt-test",
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 10,
                        "input_tokens_details": {"cached_tokens": 3},
                        "output_tokens": 4,
                        "output_tokens_details": {"reasoning_tokens": 2},
                        "total_tokens": 14,
                    },
                },
            },
        )
    )
    content = "".join(f"event: {event}\ndata: {json.dumps(data)}\n\n" for event, data in frames)
    return httpx.Response(
        200,
        request=request,
        headers={
            "content-type": "text/event-stream",
            "x-request-id": "req_responses",
        },
        content=content.encode(),
    )


def _responses_call(name: str, arguments: dict[str, object]) -> dict[str, object]:
    """Build one native Responses function-call item."""
    if "attention" not in arguments:
        arguments = {**arguments, "attention": {}}
    return {
        "id": f"fc_{name}",
        "type": "function_call",
        "call_id": f"call_{name}",
        "name": name,
        "arguments": json.dumps(arguments),
        "status": "completed",
    }


def _anthropic_response(
    request: httpx.Request,
    calls: list[dict[str, object]],
) -> httpx.Response:
    """Return one Anthropic Messages payload through NewAPI."""
    return httpx.Response(
        200,
        request=request,
        headers={"request-id": "req_anthropic"},
        json={
            "id": "msg_newapi",
            "type": "message",
            "model": "claude-test",
            "role": "assistant",
            "content": calls,
            "stop_reason": "tool_use",
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 2,
                "cache_read_input_tokens": 3,
                "output_tokens": 4,
            },
        },
    )


def _anthropic_call(name: str, arguments: dict[str, object]) -> dict[str, object]:
    """Build one Anthropic tool-use block."""
    if "attention" not in arguments:
        arguments = {**arguments, "attention": {}}
    return {
        "id": f"toolu_{name}",
        "type": "tool_use",
        "name": name,
        "input": arguments,
    }


def _tool_call(name: str, arguments: object) -> dict[str, object]:
    """Build one OpenAI-compatible function call."""
    if isinstance(arguments, dict) and "attention" not in arguments:
        arguments = {**arguments, "attention": {}}
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
    prefix = "DAIRY_BENCH_NEWAPI_TEST"
    monkeypatch.delenv(f"{prefix}_API_KEY", raising=False)
    monkeypatch.delenv(f"{prefix}_MODEL", raising=False)
    monkeypatch.delenv(f"{prefix}_MODELS", raising=False)
    monkeypatch.delenv(f"{prefix}_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv(f"{prefix}_MAX_ATTEMPTS", raising=False)
    assert NewApiConfig.from_environment() is None

    monkeypatch.setenv(f"{prefix}_API_KEY", "first-secret")
    monkeypatch.setenv(
        f"{prefix}_MODELS",
        "gpt-test,claude-test,gemini-test,deepseek-test,gpt-test",
    )
    loaded = NewApiConfig.from_environment(prefix)
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
    selected = loaded.select_model("gemini-test", max_output_tokens=65_536)
    assert selected.model == "gemini-test"
    assert selected.max_output_tokens == 65_536
    with pytest.raises(ValueError, match="not configured"):
        loaded.select_model("missing-test", max_output_tokens=1)
    other_key = loaded.model_copy(update={"api_key": SecretStr("second-secret")})
    assert loaded.fingerprint == other_key.fingerprint
    assert "first-secret" not in loaded.fingerprint
    assert (
        selected.fingerprint
        != selected.model_copy(update={"max_output_tokens": 32_768}).fingerprint
    )


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


def test_capability_probe_extracts_gemini_exclusive_output_limit() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://newapi.test/v1/chat/completions"
        requests.append(json.loads(request.content))
        if len(requests) == 2:
            return _completion_response(request, [_tool_call("idle", {})])
        return _error_response(
            request,
            400,
            "invalid_request_error",
            "Unable to submit request because it has a maxOutputTokens value of 128000 "
            "but the supported range is from 1 (inclusive) to 65537 (exclusive).",
        )

    assert asyncio.run(_probe_limit(handler, 128_000)) == 65_536
    assert len(requests) == 2
    assert requests[0]["model"] == "gemini-test"
    assert requests[0]["max_tokens"] == 128_000
    assert [tool["function"]["name"] for tool in requests[0]["tools"]] == ["idle"]
    assert requests[1]["max_tokens"] == 65_536


def test_capability_probe_confirms_documented_candidate_on_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _completion_response(request, [_tool_call("idle", {})])

    assert asyncio.run(_probe_limit(handler, 16_384)) == 16_384


def test_capability_probe_rejects_unknown_limit_when_gateway_silently_accepts_ceiling() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _completion_response(request, [_tool_call("idle", {})])

    with pytest.raises(ModelCapabilityError, match="without reporting its exact"):
        asyncio.run(_probe_limit(handler, None))


def test_gateway_sends_common_tools_and_parses_decision() -> None:
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
        return await gateway.generate_decision(_decision_request(observation))

    result = asyncio.run(_with_gateway(handler, operation))

    assert result.decision == ActionDecision(
        action=SetQuoteLadder(
            product=ProductId.RAW_MILK,
            side=MarketSide.SELL,
            levels=(
                QuoteLevel(quantity=Decimal("8"), limit_price=Decimal("1.2")),
                QuoteLevel(quantity=Decimal("3"), limit_price=Decimal("1.4")),
            ),
        ),
        attention=AttentionPlan(),
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
    assert payload["parallel_tool_calls"] is False
    assert payload["max_tokens"] == 128_000
    assert payload["n"] == 1
    assert payload["stream"] is False
    tools = payload["tools"]
    assert isinstance(tools, list)
    assert {tool["function"]["name"] for tool in tools} == {
        "produce",
        "set_quote_ladder",
        "idle",
    }
    ladder = next(tool for tool in tools if tool["function"]["name"] == "set_quote_ladder")
    assert ladder["type"] == "function"
    parameters = ladder["function"]["parameters"]
    assert parameters["additionalProperties"] is False
    assert parameters["$defs"]["OrderQuantity"]["type"] == "number"
    assert parameters["$defs"]["PositiveMoney"]["type"] == "number"


def test_responses_gateway_uses_native_sse_and_parses_one_tool() -> None:
    observation = _observation()
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://newapi.test/v1/responses"
        assert request.headers["authorization"] == "Bearer test-key"
        requests.append(json.loads(request.content))
        return _responses_sse_response(
            request,
            [_responses_call("idle", {})],
        )

    async def operation() -> DecisionModelResult:
        config = _config()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            transport = NewApiTransport(config, client=client)
            return await NewApiResponsesGateway(config, transport).generate_decision(
                _decision_request(observation)
            )

    result = asyncio.run(operation())

    assert result.decision.kind == "idle"
    assert result.response_id == "resp_newapi"
    assert result.request_id == "req_responses"
    assert result.usage.input_tokens == 10
    assert result.usage.cached_tokens == 3
    assert result.usage.output_tokens == 4
    assert result.usage.reasoning_tokens == 2
    assert result.usage.total_tokens == 14
    assert NewApiResponsesGateway.wire_protocol is NewApiWireProtocol.RESPONSES
    assert NewApiResponsesGateway.adapter_version == "newapi-responses-v2"

    payload = requests[0]
    assert payload["instructions"] == "Call exactly one authorized tool."
    assert payload["input"] == [{"role": "user", "content": _turn(observation).model_dump_json()}]
    assert payload["stream"] is True
    assert payload["store"] is False
    assert payload["tool_choice"] == "required"
    assert payload["parallel_tool_calls"] is False
    assert payload["max_output_tokens"] == 128_000
    assert {tool["name"] for tool in payload["tools"]} == {
        "produce",
        "set_quote_ladder",
        "idle",
    }
    for tool in payload["tools"]:
        _assert_strict_object_schemas(tool["parameters"])
    idle = next(tool for tool in payload["tools"] if tool["name"] == "idle")
    attention = idle["parameters"]["$defs"]["AttentionPlan"]
    assert attention["required"] == ["review_after_days", "alerts"]


def test_anthropic_gateway_uses_messages_and_normalizes_cached_usage() -> None:
    observation = _observation()
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://newapi.test/v1/messages"
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in request.headers
        requests.append(json.loads(request.content))
        return _anthropic_response(request, [_anthropic_call("idle", {})])

    async def operation() -> DecisionModelResult:
        config = _config().model_copy(update={"model": "claude-test"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            transport = NewApiTransport(config, client=client)
            return await NewApiAnthropicMessagesGateway(
                config,
                transport,
            ).generate_decision(_decision_request(observation))

    result = asyncio.run(operation())

    assert result.decision.kind == "idle"
    assert result.model == "claude-test"
    assert result.response_id == "msg_newapi"
    assert result.request_id == "req_anthropic"
    assert result.usage.input_tokens == 15
    assert result.usage.cached_tokens == 3
    assert result.usage.output_tokens == 4
    assert result.usage.total_tokens == 19
    assert NewApiAnthropicMessagesGateway.wire_protocol is NewApiWireProtocol.ANTHROPIC_MESSAGES
    assert NewApiAnthropicMessagesGateway.adapter_version == "newapi-anthropic-messages-v1"

    payload = requests[0]
    assert payload["system"] == "Call exactly one authorized tool."
    assert payload["messages"] == [
        {"role": "user", "content": _turn(observation).model_dump_json()}
    ]
    assert payload["tool_choice"] == {
        "type": "any",
        "disable_parallel_tool_use": True,
    }
    assert payload["stream"] is False
    assert payload["max_tokens"] == 128_000
    assert {tool["name"] for tool in payload["tools"]} == {
        "produce",
        "set_quote_ladder",
        "idle",
    }


@pytest.mark.parametrize(
    "protocol",
    (NewApiWireProtocol.RESPONSES, NewApiWireProtocol.ANTHROPIC_MESSAGES),
)
def test_capability_probe_uses_the_selected_wire_protocol(
    protocol: NewApiWireProtocol,
) -> None:
    seen_url = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_url
        seen_url = str(request.url)
        if protocol is NewApiWireProtocol.RESPONSES:
            return _responses_sse_response(request, [_responses_call("idle", {})])
        return _anthropic_response(request, [_anthropic_call("idle", {})])

    assert asyncio.run(_probe_limit(handler, 16_384, protocol)) == 16_384
    expected_endpoint = "responses" if protocol is NewApiWireProtocol.RESPONSES else "messages"
    assert seen_url == f"https://newapi.test/v1/{expected_endpoint}"


def test_responses_stream_failure_is_audited_as_infrastructure() -> None:
    observation = _observation()

    def handler(request: httpx.Request) -> httpx.Response:
        content = (
            "event: response.failed\n"
            'data: {"type":"response.failed","response":'
            '{"error":{"type":"server_error","message":"do not persist me"}}}\n\n'
        )
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            content=content.encode(),
        )

    async def operation() -> object:
        config = _config(max_attempts=2)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            transport = NewApiTransport(config, client=client)
            return await NewApiResponsesGateway(config, transport).generate_decision(
                _decision_request(observation)
            )

    with pytest.raises(ModelInfrastructureError, match="server_error") as raised:
        asyncio.run(operation())
    assert raised.value.attempts == 2
    assert all(
        attempt.outcome is ProviderAttemptOutcome.HTTP_ERROR
        for attempt in raised.value.attempt_history
    )
    assert "do not persist me" not in str(raised.value)


def test_anthropic_gateway_classifies_http_configuration_error() -> None:
    observation = _observation()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://newapi.test/v1/messages"
        return _error_response(request, 401, "authentication_error", "invalid profile key")

    async def operation() -> object:
        config = _config().model_copy(update={"model": "claude-test"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            transport = NewApiTransport(config, client=client)
            return await NewApiAnthropicMessagesGateway(
                config,
                transport,
            ).generate_decision(_decision_request(observation))

    with pytest.raises(ModelConfigurationError, match="invalid profile key"):
        asyncio.run(operation())


def test_missing_required_tool_call_gets_one_audited_protocol_repair() -> None:
    observation = _observation()
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _completion_response(request, [])

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_decision(_decision_request(observation))

    with pytest.raises(ModelOutputError, match="required function call") as raised:
        asyncio.run(_with_gateway(handler, operation))
    assert raised.value.response_id == "chatcmpl_newapi"
    assert raised.value.issue_kind is ProtocolIssueKind.MISSING_TOOL_CALL
    assert raised.value.attempts == 2
    assert raised.value.usage.total_tokens == 28
    assert len(requests[1]["messages"]) == 3


def test_multiple_tool_calls_repair_to_one_decision_and_charge_both_responses() -> None:
    observation = _observation()
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        calls = (
            [_tool_call("idle", {}), _tool_call("produce", {"product": "raw_milk", "quantity": 1})]
            if len(requests) == 1
            else [_tool_call("idle", {})]
        )
        return _completion_response(request, calls)

    async def operation(gateway: NewApiModelGateway) -> DecisionModelResult:
        return await gateway.generate_decision(_decision_request(observation))

    result = asyncio.run(_with_gateway(handler, operation))

    assert result.decision.kind == "idle"
    assert result.attempts == 2
    assert result.usage.total_tokens == 28
    assert tuple(attempt.outcome for attempt in result.attempt_history) == (
        ProviderAttemptOutcome.PROTOCOL_ERROR,
        ProviderAttemptOutcome.SUCCESS,
    )
    assert all(request["parallel_tool_calls"] is False for request in requests)


def test_complete_request_budget_counts_messages_and_tool_schemas() -> None:
    observation = _observation()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _completion_response(request, [_tool_call("idle", {})])

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_decision(_decision_request(observation))

    with pytest.raises(ModelOutputError, match="complete NewAPI request estimate") as raised:
        asyncio.run(_with_gateway(handler, operation, max_input_tokens=1))
    assert raised.value.issue_kind is ProtocolIssueKind.CONTEXT_TOO_LARGE
    assert raised.value.attempts == 0
    assert raised.value.attempt_history == ()
    assert raised.value.usage.total_tokens == 0
    assert calls == 0


def test_repair_budget_failure_preserves_the_first_physical_attempt() -> None:
    observation = _observation()
    request = _decision_request(observation)
    config = _config()
    payload = newapi_module._chat_request(config, request)
    initial_budget = (len(payload.model_dump_json(exclude_none=True).encode()) + 1) // 2
    calls = 0

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _completion_response(http_request, [])

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_decision(request)

    with pytest.raises(ModelOutputError, match="complete NewAPI request estimate") as raised:
        asyncio.run(_with_gateway(handler, operation, max_input_tokens=initial_budget))

    assert calls == 1
    assert raised.value.issue_kind is ProtocolIssueKind.CONTEXT_TOO_LARGE
    assert raised.value.attempts == 1
    assert raised.value.usage.total_tokens == 14
    assert raised.value.attempt_history[0].outcome is ProviderAttemptOutcome.PROTOCOL_ERROR


def test_request_timeout_budget_includes_the_protocol_repair_cycle() -> None:
    config = _config(max_attempts=1).model_copy(update={"timeout_seconds": 2.0})

    assert config.request_budget_seconds == 4.0


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
        return _completion_response(request, [_tool_call("idle", {})])

    async def operation(gateway: NewApiModelGateway) -> tuple[object, object]:
        decision_request = _decision_request(observation)
        return (
            await gateway.generate_decision(decision_request),
            await gateway.generate_decision(decision_request),
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
        return await gateway.generate_decision(_decision_request(observation))

    with pytest.raises(ModelOutputError, match="required function call") as raised:
        asyncio.run(_with_gateway(handler, operation, max_attempts=2))
    assert raised.value.attempts == 3
    assert calls == 3


def test_gateway_reports_an_exhausted_fixed_output_budget() -> None:
    observation = _observation()

    def handler(request: httpx.Request) -> httpx.Response:
        return _completion_response(
            request,
            [],
            finish_reason="length",
            completion_tokens=128_000,
            reasoning_tokens=128_000,
        )

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_decision(_decision_request(observation))

    with pytest.raises(ModelCompatibilityError, match="128000-token response budget") as raised:
        asyncio.run(_with_gateway(handler, operation))
    assert raised.value.usage.output_tokens == 128_000
    assert raised.value.usage.reasoning_tokens == 128_000


@pytest.mark.parametrize(
    "calls",
    [
        [_tool_call("idle", {}), _tool_call("produce", {"product": "raw_milk", "quantity": 1})],
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
        [_tool_call("idle", "not-json")],
    ],
    ids=("multiple", "unauthorized", "invalid", "overprecision", "kind", "malformed"),
)
def test_gateway_rejects_invalid_decision_output(calls: list[dict[str, object]]) -> None:
    observation = _observation()

    def handler(request: httpx.Request) -> httpx.Response:
        return _completion_response(request, calls)

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_decision(_decision_request(observation))

    with pytest.raises(ModelOutputError, match="invalid NewAPI tool call") as raised:
        asyncio.run(_with_gateway(handler, operation))
    assert raised.value.response_id == "chatcmpl_newapi"
    assert raised.value.request_id == "req_newapi"


def test_gateway_does_not_echo_model_values_from_invalid_tool_arguments() -> None:
    observation = _observation()
    sentinel = "LEAK_SENTINEL_123"

    def handler(request: httpx.Request) -> httpx.Response:
        return _completion_response(
            request,
            [_tool_call("produce", {"product": "raw_milk", "quantity": sentinel})],
        )

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_decision(_decision_request(observation))

    with pytest.raises(ModelOutputError) as raised:
        asyncio.run(_with_gateway(handler, operation))
    assert sentinel not in str(raised.value)
    assert "schema validation issue" in str(raised.value)


def test_gateway_does_not_echo_unknown_tool_names() -> None:
    observation = _observation()
    sentinel = "LEAK_SENTINEL_123"

    def handler(request: httpx.Request) -> httpx.Response:
        return _completion_response(request, [_tool_call(sentinel, {})])

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_decision(_decision_request(observation))

    with pytest.raises(ModelOutputError) as raised:
        asyncio.run(_with_gateway(handler, operation))
    assert sentinel not in str(raised.value)
    assert all(
        sentinel not in (attempt.error_message or "") for attempt in raised.value.attempt_history
    )


def test_gateway_drops_provider_identifiers_that_echo_the_credential() -> None:
    observation = _observation()

    def handler(request: httpx.Request) -> httpx.Response:
        response = _completion_response(request, [_tool_call("idle", {})])
        body = response.json()
        body["id"] = "test-key"
        body["model"] = "test-key"
        return httpx.Response(
            200,
            request=request,
            headers={"x-request-id": "request-test-key"},
            json=body,
        )

    async def operation(gateway: NewApiModelGateway) -> DecisionModelResult:
        return await gateway.generate_decision(_decision_request(observation))

    result = asyncio.run(_with_gateway(handler, operation))

    assert result.model == "gpt-test"
    assert result.request_id is None
    assert result.response_id is None
    assert result.attempt_history[0].request_id is None
    assert result.attempt_history[0].response_id is None


def test_gateway_drops_key_shaped_provider_identifiers() -> None:
    observation = _observation()

    def handler(request: httpx.Request) -> httpx.Response:
        response = _completion_response(request, [_tool_call("idle", {})])
        body = response.json()
        body["id"] = "response-sk-foreign-secret"
        return httpx.Response(
            200,
            request=request,
            headers={"x-request-id": "request-sk-foreign-secret"},
            json=body,
        )

    async def operation(gateway: NewApiModelGateway) -> DecisionModelResult:
        return await gateway.generate_decision(_decision_request(observation))

    result = asyncio.run(_with_gateway(handler, operation))

    assert result.request_id is None
    assert result.response_id is None
    assert result.attempt_history[0].request_id is None
    assert result.attempt_history[0].response_id is None


def test_gateway_sanitizes_provider_identifiers_on_parse_failure() -> None:
    observation = _observation()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"x-request-id": "request-test-key"},
            json={
                "id": "test-key",
                "model": "gpt-test",
                "status": "failed",
                "output": [],
            },
        )

    async def operation() -> object:
        config = _config()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            transport = NewApiTransport(config, client=client)
            return await NewApiResponsesGateway(config, transport).generate_decision(
                _decision_request(observation)
            )

    with pytest.raises(ModelInfrastructureError) as raised:
        asyncio.run(operation())

    assert raised.value.request_id is None
    assert raised.value.response_id is None
    assert raised.value.attempt_history[0].request_id is None
    assert raised.value.attempt_history[0].response_id is None


def test_responses_stream_failure_rejects_untrusted_error_type() -> None:
    observation = _observation()
    sentinel = "LEAK_SENTINEL_123"

    def handler(request: httpx.Request) -> httpx.Response:
        content = (
            "event: response.failed\n"
            f'data: {{"type":"response.failed","error":{{"type":"{sentinel} secret"}}}}\n\n'
        )
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            content=content.encode(),
        )

    async def operation() -> object:
        config = _config()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            transport = NewApiTransport(config, client=client)
            return await NewApiResponsesGateway(config, transport).generate_decision(
                _decision_request(observation)
            )

    with pytest.raises(ModelInfrastructureError, match="unknown_error") as raised:
        asyncio.run(operation())
    assert sentinel not in str(raised.value)


def test_gateway_fails_fast_for_an_incompatible_endpoint() -> None:
    observation = _observation()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _error_response(request, 400, "invalid_request_error", "tools unsupported")

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_decision(_decision_request(observation))

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
        return await gateway.generate_decision(_decision_request(observation))

    with pytest.raises(ModelConfigurationError, match="NewAPI HTTP 401"):
        asyncio.run(_with_gateway(handler, operation, max_attempts=3))
    assert calls == 1


@pytest.mark.parametrize("status_code", (403, 429))
def test_gateway_classifies_balance_exhaustion_without_retrying(
    status_code: int,
) -> None:
    observation = _observation()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _error_response(
            request,
            status_code,
            "insufficient_quota",
            "预扣费额度失败, 用户剩余额度: $0.10, 需要预扣费额度: $0.30",
        )

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_decision(_decision_request(observation))

    with pytest.raises(ModelQuotaExhaustedError, match="预扣费额度失败") as raised:
        asyncio.run(_with_gateway(handler, operation, max_attempts=3))
    assert calls == 1
    assert raised.value.request_id == "req_error"
    assert raised.value.attempts == 1


def test_gateway_keeps_unrelated_403_as_configuration_failure() -> None:
    observation = _observation()

    def handler(request: httpx.Request) -> httpx.Response:
        return _error_response(request, 403, "permission_denied", "model access denied")

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_decision(_decision_request(observation))

    with pytest.raises(ModelConfigurationError, match="model access denied"):
        asyncio.run(_with_gateway(handler, operation, max_attempts=3))


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
        return _completion_response(request, [_tool_call("idle", {})])

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_decision(_decision_request(observation))

    result = asyncio.run(_with_gateway(handler, operation, max_attempts=2))
    assert result.attempts == 2
    assert calls == 2


def test_gateway_classifies_exhausted_wrapped_failure_as_infrastructure() -> None:
    observation = _observation()

    def handler(request: httpx.Request) -> httpx.Response:
        return _error_response(request, 400, "bad_response_status_code")

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_decision(_decision_request(observation))

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
        return await gateway.generate_decision(_decision_request(observation))

    with pytest.raises(ModelInfrastructureError, match="test timeout") as raised:
        asyncio.run(_with_gateway(handler, operation, max_attempts=2))
    assert calls == 2
    assert raised.value.attempts == 2


def test_gateway_redacts_key_echoed_by_provider() -> None:
    observation = _observation()

    def handler(request: httpx.Request) -> httpx.Response:
        return _error_response(request, 401, "authentication_error", "echoed test-key")

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_decision(_decision_request(observation))

    with pytest.raises(ModelConfigurationError) as raised:
        asyncio.run(_with_gateway(handler, operation))
    assert "test-key" not in str(raised.value)
    assert "[REDACTED]" in str(raised.value)


def test_gateway_redacts_key_before_bounding_provider_diagnostics() -> None:
    observation = _observation()
    key_prefix = "test-ke"

    def handler(request: httpx.Request) -> httpx.Response:
        return _error_response(
            request,
            401,
            "authentication_error",
            f"{'x' * 225}test-key",
        )

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_decision(_decision_request(observation))

    with pytest.raises(ModelConfigurationError) as raised:
        asyncio.run(_with_gateway(handler, operation))

    assert "test-key" not in str(raised.value)
    assert key_prefix not in str(raised.value)
    assert "test-key" not in (raised.value.attempt_history[0].error_message or "")
    assert key_prefix not in (raised.value.attempt_history[0].error_message or "")


def test_gateway_does_not_echo_secret_from_invalid_success_payload() -> None:
    observation = _observation()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "response_invalid",
                "model": "gpt-test",
                "choices": [{"message": {"tool_calls": "test-key"}}],
                "usage": {},
            },
        )

    async def operation(gateway: NewApiModelGateway) -> object:
        return await gateway.generate_decision(_decision_request(observation))

    with pytest.raises(ModelOutputError) as raised:
        asyncio.run(_with_gateway(handler, operation))
    assert "test-key" not in str(raised.value)
    assert "schema validation issue" in str(raised.value)


def test_gateway_closes_only_its_owned_transport() -> None:
    owned_gateway = NewApiModelGateway(_config())
    assert owned_gateway._transport._client is None
    asyncio.run(owned_gateway.close())
    assert owned_gateway._transport._client is None

    owned_client = httpx.AsyncClient()
    owned_gateway._transport._client = owned_client
    asyncio.run(owned_gateway.close())
    assert owned_client.is_closed

    async def verify_shared_transport() -> None:
        client = httpx.AsyncClient()
        transport = NewApiTransport(_config(), client=client)
        gateway = NewApiModelGateway(_config(), transport)
        await gateway.close()
        assert not client.is_closed
        await transport.close()
        assert not client.is_closed
        await client.aclose()

    asyncio.run(verify_shared_transport())


class _BlockingHttpTransport(httpx.AsyncBaseTransport):
    """Hold provider requests to expose the shared concurrency boundary."""

    def __init__(self, saturation: int) -> None:
        self.active = 0
        self.peak = 0
        self.release = asyncio.Event()
        self.saturated = asyncio.Event()
        self._saturation = saturation

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Record one active request until the test releases the pool."""
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.active == self._saturation:
            self.saturated.set()
        try:
            await self.release.wait()
            return _completion_response(
                request,
                [_tool_call("produce", {"product": "raw_milk", "quantity": 1})],
            )
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_shared_transport_starts_100_requests_and_queues_the_101st() -> None:
    config = _config()
    blocker = _BlockingHttpTransport(saturation=100)
    request = _decision_request(_observation())
    async with httpx.AsyncClient(transport=blocker) as client:
        transport = NewApiTransport(config, max_concurrent_requests=100, client=client)
        gateway = NewApiModelGateway(config, transport)
        tasks = tuple(asyncio.create_task(gateway.generate_decision(request)) for _ in range(101))
        try:
            async with asyncio.timeout(2):
                await blocker.saturated.wait()
            await asyncio.sleep(0)
            assert blocker.active == 100
            assert blocker.peak == 100
            assert sum(not task.done() for task in tasks) == 101
        finally:
            blocker.release.set()
            results = await asyncio.gather(*tasks)

    assert len(results) == 101


class _RetryOrderTransport(httpx.AsyncBaseTransport):
    """Expose which gateway enters a one-slot transport during retry backoff."""

    def __init__(self) -> None:
        self.first_attempt_finished = asyncio.Event()
        self.second_gateway_entered = asyncio.Event()
        self._first_attempt = True

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Fail the first gateway once and accept the second immediately."""
        payload = json.loads(request.content)
        marker = payload["messages"][0]["content"]
        if marker == "first" and self._first_attempt:
            self._first_attempt = False
            self.first_attempt_finished.set()
            return _error_response(request, 500, "server_error")
        if marker == "second":
            self.second_gateway_entered.set()
        return _completion_response(
            request,
            [_tool_call("produce", {"product": "raw_milk", "quantity": 1})],
        )


@pytest.mark.asyncio
async def test_retry_backoff_releases_the_shared_request_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(newapi_module, "_retry_delay_seconds", lambda _: 1.0)
    config = _config(max_attempts=2)
    provider = _RetryOrderTransport()
    request = _decision_request(_observation())
    first_request = request.model_copy(update={"instructions": "first"})
    second_request = request.model_copy(update={"instructions": "second"})
    async with httpx.AsyncClient(transport=provider) as client:
        transport = NewApiTransport(config, max_concurrent_requests=1, client=client)
        first = asyncio.create_task(
            NewApiModelGateway(config, transport).generate_decision(first_request)
        )
        second: asyncio.Task[DecisionModelResult] | None = None
        try:
            async with asyncio.timeout(2):
                await provider.first_attempt_finished.wait()
            second = asyncio.create_task(
                NewApiModelGateway(config, transport).generate_decision(second_request)
            )
            async with asyncio.timeout(0.5):
                await provider.second_gateway_entered.wait()
            await second
        finally:
            first.cancel()
            if second is not None:
                second.cancel()
            tasks = (first,) if second is None else (first, second)
            await asyncio.gather(*tasks, return_exceptions=True)
