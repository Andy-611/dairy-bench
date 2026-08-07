"""Contract tests for the OpenAI Responses API adapter."""

import asyncio
import json
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from company_bench.agents.contracts import (
    CommandModelRequest,
    CommandModelResult,
    ModelOutputError,
)
from company_bench.agents.providers.openai import OpenAIAgentConfig, OpenAIModelGateway
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


async def _generate_command(
    handler: ResponseHandler,
    observation: CompanyObservation,
) -> CommandModelResult:
    """Call native tool use through the real SDK transport."""
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = AsyncOpenAI(
            api_key="test-key",
            base_url="https://api.openai.test/v1",
            http_client=http_client,
            max_retries=0,
        )
        gateway = OpenAIModelGateway(
            OpenAIAgentConfig(
                api_key=SecretStr("test-key"),
                model="test-model",
                reasoning_effort="low",
                max_attempts=1,
            ),
            client,
        )
        turn = AgentTurn(
            turn_id="gateway_test.farm_a.t1",
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
        return await gateway.generate_command(
            CommandModelRequest(
                invocation_id="gateway_test.farm_a.t1.provider",
                run_id="gateway_test",
                turn=turn,
                instructions="Call exactly one tool.",
                input_text=turn.model_dump_json(),
                allowed_commands=("produce", "set_quote_ladder", "wait"),
            )
        )


def _tool_response(
    request: httpx.Request,
    output: list[dict[str, object]] | None = None,
) -> httpx.Response:
    """Return one complete native function-call response."""
    calls = (
        output
        if output is not None
        else [
            {
                "id": "fc_test",
                "type": "function_call",
                "call_id": "call_test",
                "name": "set_quote_ladder",
                "arguments": (
                    '{"product":"raw_milk","side":"sell","levels":'
                    '[{"quantity":"8","limit_price":"1.20"},'
                    '{"quantity":"3","limit_price":"1.40"},'
                    '{"quantity":"1","limit_price":"1.60"}]}'
                ),
                "status": "completed",
            }
        ]
    )
    return httpx.Response(
        200,
        request=request,
        headers={"x-request-id": "req_tool"},
        json={
            "id": "resp_tool",
            "object": "response",
            "created_at": 1_700_000_000,
            "model": "test-model",
            "status": "completed",
            "output": calls,
            "parallel_tool_calls": False,
            "tool_choice": "required",
            "tools": [],
            "usage": {
                "input_tokens": 5,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 4,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 9,
            },
        },
    )


def test_openai_gateway_uses_exactly_one_native_command_tool(
    first_observation: CompanyObservation,
) -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _tool_response(request)

    result = asyncio.run(_generate_command(handler, first_observation))

    assert result.command == SetQuoteLadder(
        product=ProductId.RAW_MILK,
        side=MarketSide.SELL,
        levels=(
            QuoteLevel(quantity=Decimal("8"), limit_price=Decimal("1.20")),
            QuoteLevel(quantity=Decimal("3"), limit_price=Decimal("1.40")),
            QuoteLevel(quantity=Decimal("1"), limit_price=Decimal("1.60")),
        ),
    )
    assert result.response_id == "resp_tool"
    payload = requests[0]
    assert payload["parallel_tool_calls"] is False
    assert payload["max_tool_calls"] == 1
    assert payload["tool_choice"] == "required"
    assert {tool["name"] for tool in payload["tools"]} == {
        "produce",
        "set_quote_ladder",
        "wait",
    }
    ladder = next(tool for tool in payload["tools"] if tool["name"] == "set_quote_ladder")
    parameters = ladder["parameters"]
    assert set(parameters["required"]) == {"product", "side", "levels"}
    assert parameters["properties"]["levels"]["maxItems"] == 3
    for definition in ("OrderQuantity", "PositiveMoney"):
        economic_schema = parameters["$defs"][definition]
        assert economic_schema["type"] == "number"
        assert economic_schema["multipleOf"] == 0.0001
        assert economic_schema["exclusiveMinimum"] == 0.0
        assert "anyOf" not in economic_schema


@pytest.mark.parametrize(
    "output",
    [
        [],
        [
            {
                "id": "fc_one",
                "type": "function_call",
                "call_id": "call_one",
                "name": "wait",
                "arguments": "{}",
                "status": "completed",
            },
            {
                "id": "fc_two",
                "type": "function_call",
                "call_id": "call_two",
                "name": "produce",
                "arguments": '{"product":"raw_milk","quantity":"12"}',
                "status": "completed",
            },
        ],
        [
            {
                "id": "fc_unauthorized",
                "type": "function_call",
                "call_id": "call_unauthorized",
                "name": "transform",
                "arguments": (
                    '{"input_product":"raw_milk","output_product":"bottled_milk",'
                    '"input_quantity":"12"}'
                ),
                "status": "completed",
            }
        ],
        [
            {
                "id": "fc_invalid",
                "type": "function_call",
                "call_id": "call_invalid",
                "name": "produce",
                "arguments": '{"product":"raw_milk","quantity":"-1"}',
                "status": "completed",
            }
        ],
    ],
    ids=("no-call", "multiple-calls", "unauthorized-command", "invalid-arguments"),
)
def test_openai_gateway_rejects_provider_protocol_violations(
    first_observation: CompanyObservation,
    output: list[dict[str, object]],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _tool_response(request, output)

    with pytest.raises(ModelOutputError, match="invalid native tool call"):
        asyncio.run(_generate_command(handler, first_observation))
