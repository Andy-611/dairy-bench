"""Contract tests for the OpenAI Responses API adapter."""

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from company_bench.agent_gateway import OpenAIAgentConfig, OpenAIModelGateway
from company_bench.agent_models import (
    ModelInfrastructureError,
    ModelOutputError,
    ModelRequest,
    ModelResult,
)
from company_bench.models import CompanyObservation, FarmDecision

type ResponseHandler = Callable[[httpx.Request], httpx.Response]


def _model_request(observation: CompanyObservation) -> ModelRequest:
    """Build one provider-neutral request for adapter tests."""
    return ModelRequest(
        invocation_id="gateway_test.1.farm_a",
        run_id="gateway_test",
        observation=observation,
        instructions="Return one structured farm decision.",
        input_text=observation.model_dump_json(),
    )


async def _generate(
    handler: ResponseHandler,
    observation: CompanyObservation,
) -> ModelResult:
    """Call the real SDK parser over an in-memory HTTP transport."""
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
        return await gateway.generate(
            _model_request(observation),
            FarmDecision,
        )


def _response(
    request: httpx.Request,
    output_text: str,
) -> httpx.Response:
    """Return the minimum complete Responses API payload parsed by the SDK."""
    return httpx.Response(
        200,
        request=request,
        headers={"x-request-id": "req_test"},
        json={
            "id": "resp_test",
            "object": "response",
            "created_at": 1_700_000_000,
            "model": "test-model",
            "status": "completed",
            "output": [
                {
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": output_text,
                            "annotations": [],
                        }
                    ],
                }
            ],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "usage": {
                "input_tokens": 12,
                "input_tokens_details": {"cached_tokens": 2},
                "output_tokens": 8,
                "output_tokens_details": {"reasoning_tokens": 3},
                "total_tokens": 20,
            },
        },
    )


def test_openai_gateway_uses_structured_output_and_records_usage(
    first_observation: CompanyObservation,
) -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _response(
            request,
            json.dumps(
                {
                    "decision": {
                        "kind": "farm",
                        "produce_quantity": "10",
                        "raw_offer_quantity": "8",
                        "minimum_raw_price": "1.40",
                    }
                }
            ),
        )

    result = asyncio.run(_generate(handler, first_observation))

    assert result.decision == FarmDecision(
        produce_quantity="10",
        raw_offer_quantity="8",
        minimum_raw_price="1.40",
    )
    assert result.response_id == "resp_test"
    assert result.request_id == "req_test"
    assert result.usage.input_tokens == 12
    assert result.usage.cached_tokens == 2
    assert result.usage.reasoning_tokens == 3
    assert requests[0]["model"] == "test-model"
    assert requests[0]["store"] is False
    assert requests[0]["reasoning"] == {"effort": "low"}
    text_format = requests[0]["text"]["format"]
    assert text_format["type"] == "json_schema"
    assert text_format["strict"] is True


def test_openai_gateway_separates_output_and_infrastructure_failures(
    first_observation: CompanyObservation,
) -> None:
    def invalid_output(request: httpx.Request) -> httpx.Response:
        return _response(request, '{"decision":{"kind":"farm"}}')

    with pytest.raises(ModelOutputError):
        asyncio.run(_generate(invalid_output, first_observation))

    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            request=request,
            json={
                "error": {
                    "message": "invalid test key",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "invalid_api_key",
                }
            },
        )

    with pytest.raises(ModelInfrastructureError, match="AuthenticationError"):
        asyncio.run(_generate(unauthorized, first_observation))
