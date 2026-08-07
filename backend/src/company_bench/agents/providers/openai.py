"""OpenAI and scripted adapters behind the model gateway seam."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from time import monotonic
from typing import Literal

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)
from pydantic import Field, SecretStr, TypeAdapter, ValidationError

from company_bench.agents.contracts import (
    CommandGateway,
    CommandModelRequest,
    CommandModelResult,
    CommandName,
    ModelInfrastructureError,
    ModelOutputError,
    command_model,
)
from company_bench.diagnostics import bounded_error
from company_bench.domain.models import StrictModel
from company_bench.domain.precision import require_numeric_economic_schema
from company_bench.runs.models import TokenUsage
from company_bench.runtime.models import CompanyCommand

type ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh"]
_COMMAND_ADAPTER = TypeAdapter(CompanyCommand)


class OpenAIAgentConfig(StrictModel):
    """Validated server-only configuration for the OpenAI adapter."""

    api_key: SecretStr
    model: str = Field(default="gpt-5.6-terra", min_length=1)
    reasoning_effort: ReasoningEffort = "medium"
    max_output_tokens: int = Field(default=2048, ge=128, le=32768)
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    max_attempts: int = Field(default=3, ge=1, le=5)
    base_url: str | None = None

    @classmethod
    def from_environment(cls) -> OpenAIAgentConfig | None:
        """Load a profile only when the backend has an API key."""
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            return None
        return cls(
            api_key=SecretStr(api_key),
            model=os.getenv(
                "DAIRY_BENCH_OPENAI_MODEL",
                "gpt-5.6-terra",
            ),
            reasoning_effort=os.getenv(
                "DAIRY_BENCH_OPENAI_REASONING_EFFORT",
                "medium",
            ),
            max_output_tokens=int(os.getenv("DAIRY_BENCH_OPENAI_MAX_OUTPUT_TOKENS", "2048")),
            timeout_seconds=float(os.getenv("DAIRY_BENCH_OPENAI_TIMEOUT_SECONDS", "60")),
            max_attempts=int(os.getenv("DAIRY_BENCH_OPENAI_MAX_ATTEMPTS", "3")),
            base_url=os.getenv("DAIRY_BENCH_OPENAI_BASE_URL") or None,
        )

    @property
    def fingerprint(self) -> str:
        """Identify behavior-affecting settings without exposing the key."""
        payload = self.model_dump_json(exclude={"api_key"})
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


class OpenAIModelGateway(CommandGateway):
    """Generate typed company commands through the Responses API."""

    provider = "openai"

    def __init__(
        self,
        config: OpenAIAgentConfig,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.config = config
        self._owns_client = client is None
        self._client = client or AsyncOpenAI(
            api_key=config.api_key.get_secret_value(),
            base_url=config.base_url,
            max_retries=0,
            timeout=config.timeout_seconds,
        )

    async def generate_command(
        self,
        request: CommandModelRequest,
    ) -> CommandModelResult:
        """Call native function tools and require exactly one atomic command."""
        started = monotonic()
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                response = await self._client.responses.create(
                    model=self.config.model,
                    instructions=request.instructions,
                    input=request.input_text,
                    tools=_command_tools(request.allowed_commands),
                    tool_choice="required",
                    parallel_tool_calls=False,
                    max_tool_calls=1,
                    max_output_tokens=self.config.max_output_tokens,
                    reasoning={"effort": self.config.reasoning_effort},
                    store=False,
                )
                try:
                    command = _parse_command(response, request.allowed_commands)
                except (json.JSONDecodeError, ValidationError, ValueError) as error:
                    raise ModelOutputError(
                        f"invalid native tool call: {error}",
                        request_id=response._request_id,
                        response_id=response.id,
                        usage=_token_usage(response.usage),
                        attempts=attempt,
                        latency_ms=int((monotonic() - started) * 1000),
                    ) from error
                return CommandModelResult(
                    command=command,
                    provider=self.provider,
                    model=response.model or self.config.model,
                    response_id=response.id,
                    request_id=response._request_id,
                    usage=_token_usage(response.usage),
                    attempts=attempt,
                    latency_ms=int((monotonic() - started) * 1000),
                )
            except ModelOutputError:
                raise
            except (APIConnectionError, APITimeoutError, RateLimitError) as error:
                if attempt == self.config.max_attempts:
                    raise _infrastructure_error(error) from error
            except APIStatusError as error:
                if (
                    error.status_code not in {408, 409, 429} and error.status_code < 500
                ) or attempt == self.config.max_attempts:
                    raise _infrastructure_error(error) from error
            await asyncio.sleep(0.25 * 2 ** (attempt - 1))
        raise AssertionError("bounded retry loop did not terminate")

    async def close(self) -> None:
        """Close the client created by this adapter."""
        if self._owns_client:
            await self._client.close()


def _token_usage(usage: object | None) -> TokenUsage:
    """Normalize optional SDK token counters."""
    if usage is None:
        return TokenUsage()
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    return TokenUsage(
        input_tokens=getattr(usage, "input_tokens", 0),
        cached_tokens=getattr(input_details, "cached_tokens", 0),
        output_tokens=getattr(usage, "output_tokens", 0),
        reasoning_tokens=getattr(output_details, "reasoning_tokens", 0),
        total_tokens=getattr(usage, "total_tokens", 0),
    )


def _command_tools(names: tuple[CommandName, ...]) -> list[dict[str, object]]:
    """Build strict native function schemas from the canonical command models."""
    tools: list[dict[str, object]] = []
    for name in names:
        model = command_model(name)
        schema = model.model_json_schema()
        require_numeric_economic_schema(schema)
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            properties.pop("kind", None)
            schema["required"] = list(properties)
        schema["additionalProperties"] = False
        tools.append(
            {
                "type": "function",
                "name": name,
                "description": model.__doc__ or f"Execute {name}.",
                "parameters": schema,
                "strict": True,
            }
        )
    return tools


def _parse_command(
    response: object,
    allowed_commands: tuple[CommandName, ...],
) -> CompanyCommand:
    """Normalize exactly one provider function call into a domain command."""
    output = getattr(response, "output", ())
    calls = tuple(item for item in output if getattr(item, "type", None) == "function_call")
    if len(calls) != 1:
        raise ValueError(f"expected exactly one function call, received {len(calls)}")
    call = calls[0]
    name = getattr(call, "name", "")
    if name not in allowed_commands:
        raise ValueError(f"unknown or unauthorized command: {name}")
    arguments = json.loads(getattr(call, "arguments", ""))
    if not isinstance(arguments, dict):
        raise ValueError("function arguments must be a JSON object")
    return _COMMAND_ADAPTER.validate_python({"kind": name, **arguments})


def _infrastructure_error(error: Exception) -> ModelInfrastructureError:
    """Return a bounded provider diagnostic without request payloads."""
    return ModelInfrastructureError(bounded_error(error, 300))
