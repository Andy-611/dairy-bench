"""Typed NewAPI Chat Completions adapter for every model-backed company Agent."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Literal, Self
from urllib.parse import urlsplit

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from company_bench.agents.contracts import (
    CommandGateway,
    CommandModelRequest,
    CommandModelResult,
    CommandName,
    ModelCompatibilityError,
    ModelConfigurationError,
    ModelInfrastructureError,
    ModelOutputError,
    command_model,
)
from company_bench.diagnostics import bounded_error
from company_bench.domain.models import StrictModel
from company_bench.domain.precision import require_numeric_economic_schema
from company_bench.runs.models import TokenUsage
from company_bench.runtime.models import CompanyCommand

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

DEFAULT_NEWAPI_BASE_URL = "https://newapi.deepwisdom.ai/v1"
_COMMAND_ADAPTER = TypeAdapter(CompanyCommand)
_JSON_OBJECT_ADAPTER = TypeAdapter(JsonObject)
_FIXED_MAX_OUTPUT_TOKENS = 128_000
_MAX_RETRY_DELAY_SECONDS = 2.0
_PROXY_UPSTREAM_ERROR_TYPE = "bad_response_status_code"


class NewApiConfig(StrictModel):
    """Validated server-only configuration for the single model provider."""

    api_key: SecretStr
    model: str = Field(min_length=1)
    models: tuple[str, ...] = ()
    base_url: str = Field(default=DEFAULT_NEWAPI_BASE_URL, min_length=1)
    timeout_seconds: float = Field(default=300.0, gt=0, le=600)
    max_attempts: int = Field(default=3, ge=1, le=10)

    @field_validator("base_url")
    @classmethod
    def require_newapi_endpoint(cls, value: str) -> str:
        """Keep credentials on one HTTPS NewAPI-compatible `/v1` endpoint."""
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != "/v1"
        ):
            raise ValueError("NewAPI base_url must be an HTTPS /v1 endpoint")
        return value.rstrip("/")

    @classmethod
    def from_environment(cls) -> NewApiConfig | None:
        """Load NewAPI only when the key and model catalog are configured."""
        api_key = os.getenv("NEWAPI_API_KEY", "").strip()
        models = _model_ids(
            os.getenv("DAIRY_BENCH_NEWAPI_MODELS", ""),
            os.getenv("DAIRY_BENCH_NEWAPI_MODEL", ""),
        )
        if not api_key or not models:
            return None
        return cls(
            api_key=SecretStr(api_key),
            model=models[0],
            models=models,
            timeout_seconds=float(os.getenv("DAIRY_BENCH_NEWAPI_TIMEOUT_SECONDS", "300")),
            max_attempts=int(os.getenv("DAIRY_BENCH_NEWAPI_MAX_ATTEMPTS", "3")),
        )

    @property
    def fingerprint(self) -> str:
        """Identify behavior-affecting settings without exposing credentials."""
        payload = self.model_dump_json(exclude={"api_key", "models"})
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @property
    def available_models(self) -> tuple[str, ...]:
        """Return the ordered models exposed by the configured credential."""
        return self.models or (self.model,)

    @property
    def request_budget_seconds(self) -> float:
        """Return the maximum configured request and retry duration."""
        retry_delays = sum(_retry_delay_seconds(attempt) for attempt in range(1, self.max_attempts))
        return self.timeout_seconds * self.max_attempts + retry_delays

    def select_model(self, model: str | None) -> NewApiConfig:
        """Return one run-specific configuration after catalog validation."""
        selected = model or self.model
        if selected not in self.available_models:
            raise ValueError(f"NewAPI model is not configured: {selected}")
        return self.model_copy(update={"model": selected})


class _ExternalModel(BaseModel):
    """Typed provider payload that tolerates additive NewAPI fields."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class _FunctionCall(_ExternalModel):
    name: str
    arguments: str | JsonObject


class _ToolCall(_ExternalModel):
    type: Literal["function"]
    function: _FunctionCall


class _AssistantMessage(_ExternalModel):
    tool_calls: tuple[_ToolCall, ...] = ()


class _Choice(_ExternalModel):
    message: _AssistantMessage
    finish_reason: str | None = None


class _TokenDetails(_ExternalModel):
    cached_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)


class _Usage(_ExternalModel):
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    prompt_tokens_details: _TokenDetails = _TokenDetails()
    completion_tokens_details: _TokenDetails = _TokenDetails()


class _ChatCompletion(_ExternalModel):
    id: str | None = None
    model: str | None = None
    choices: tuple[_Choice, ...] = ()
    usage: _Usage = _Usage()


class _Message(StrictModel):
    role: Literal["system", "user"]
    content: str


class _FunctionTool(StrictModel):
    name: str
    description: str
    parameters: JsonObject


class _Tool(StrictModel):
    type: Literal["function"] = "function"
    function: _FunctionTool


class _ChatRequest(StrictModel):
    model: str
    messages: tuple[_Message, ...]
    tools: tuple[_Tool, ...]
    tool_choice: Literal["required"] | None = "required"
    max_tokens: int
    n: Literal[1] = 1
    stream: Literal[False] = False

    def without_tool_choice(self) -> Self:
        """Omit an unsupported tool-choice hint while retaining all tools."""
        return self.model_copy(update={"tool_choice": None})


@dataclass(frozen=True, slots=True)
class _GatewayResponse:
    """Validated response plus complete audit data for one logical call."""

    response: httpx.Response
    completion: _ChatCompletion
    usage: TokenUsage
    attempts: int
    latency_ms: int


class NewApiModelGateway(CommandGateway):
    """Generate typed company commands through NewAPI's common chat interface."""

    provider = "newapi"

    def __init__(
        self,
        config: NewApiConfig,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=config.timeout_seconds)
        self._omit_tool_choice = False

    async def generate_command(
        self,
        request: CommandModelRequest,
    ) -> CommandModelResult:
        """Return exactly one role-authorized atomic company command."""
        result = await self._request(_chat_request(self.config, request))
        completion = result.completion
        try:
            name, arguments = _single_tool_call(completion)
            if name not in request.allowed_commands:
                raise ValueError(f"unknown or unauthorized command: {name}")
            if "kind" in arguments:
                raise ValueError("tool input must not supply a command kind")
            command = _COMMAND_ADAPTER.validate_python({"kind": name, **arguments})
        except ModelCompatibilityError as error:
            raise ModelCompatibilityError(
                str(error),
                request_id=_request_id(result.response),
                response_id=completion.id,
                usage=result.usage,
                attempts=result.attempts,
                latency_ms=result.latency_ms,
            ) from error
        except (json.JSONDecodeError, ValidationError, ValueError) as error:
            raise _output_error(
                "invalid NewAPI tool call",
                error,
                completion,
                result,
            ) from error
        return CommandModelResult(
            command=command,
            provider=self.provider,
            model=completion.model or self.config.model,
            response_id=completion.id,
            request_id=_request_id(result.response),
            usage=result.usage,
            attempts=result.attempts,
            latency_ms=result.latency_ms,
        )

    async def close(self) -> None:
        """Close only the HTTP client owned by this adapter."""
        if self._owns_client:
            await self._client.aclose()

    async def _request(
        self,
        payload: _ChatRequest,
    ) -> _GatewayResponse:
        """Call NewAPI with bounded retries and return audit timing."""
        started = monotonic()
        api_key = self.config.api_key.get_secret_value()
        if self._omit_tool_choice:
            payload = payload.without_tool_choice()
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                response = await self._client.post(
                    f"{self.config.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload.model_dump(mode="json", exclude_none=True),
                )
            except (httpx.TimeoutException, httpx.TransportError) as error:
                if attempt == self.config.max_attempts:
                    raise _transport_error(
                        error,
                        self.config.api_key,
                        attempt,
                        _elapsed_ms(started),
                    ) from error
            else:
                if response.is_success:
                    latency_ms = _elapsed_ms(started)
                    completion = _parse_completion(response, attempt, latency_ms)
                    return _GatewayResponse(
                        response=response,
                        completion=completion,
                        usage=_token_usage(completion.usage),
                        attempts=attempt,
                        latency_ms=latency_ms,
                    )
                if payload.tool_choice and _rejects_tool_choice(response):
                    self._omit_tool_choice = True
                    if attempt < self.config.max_attempts:
                        payload = payload.without_tool_choice()
                        continue
                if not _retryable_response(response) or attempt == self.config.max_attempts:
                    raise _response_error(
                        response,
                        self.config.api_key,
                        attempt,
                        _elapsed_ms(started),
                    )
            await asyncio.sleep(_retry_delay_seconds(attempt))
        raise AssertionError("bounded retry loop did not terminate")


def _chat_request(
    config: NewApiConfig,
    request: CommandModelRequest,
) -> _ChatRequest:
    """Build one provider request from the provider-neutral command contract."""
    return _ChatRequest(
        model=config.model,
        messages=(
            _Message(role="system", content=request.instructions),
            _Message(role="user", content=request.input_text),
        ),
        tools=tuple(_command_tool(name) for name in request.allowed_commands),
        max_tokens=_FIXED_MAX_OUTPUT_TOKENS,
    )


def _command_tool(name: CommandName) -> _Tool:
    """Build one Chat Completions function from the canonical command model."""
    model = command_model(name)
    schema = model.model_json_schema()
    properties = schema.get("properties")
    if isinstance(properties, dict):
        properties.pop("kind", None)
        schema["required"] = list(properties)
    schema["additionalProperties"] = False
    require_numeric_economic_schema(schema)
    return _Tool(
        function=_FunctionTool(
            name=name,
            description=model.__doc__ or f"Execute {name}.",
            parameters=_JSON_OBJECT_ADAPTER.validate_python(schema),
        )
    )


def _parse_completion(
    response: httpx.Response,
    attempts: int,
    latency_ms: int,
) -> _ChatCompletion:
    """Validate the external response while retaining additive compatibility."""
    try:
        return _ChatCompletion.model_validate(response.json())
    except (ValueError, ValidationError) as error:
        raise ModelOutputError(
            f"invalid NewAPI response: {error}",
            request_id=_request_id(response),
            attempts=attempts,
            latency_ms=latency_ms,
        ) from error


def _single_tool_call(completion: _ChatCompletion) -> tuple[str, JsonObject]:
    """Return exactly one tool call or fail fast for an incompatible model."""
    if len(completion.choices) != 1:
        raise ValueError(f"expected one choice, received {len(completion.choices)}")
    calls = completion.choices[0].message.tool_calls
    if not calls:
        message = (
            "selected NewAPI model exhausted the fixed 128000-token response budget "
            "before returning a function call"
            if _truncated_without_tool_call(completion)
            else "selected NewAPI model did not return a required function call"
        )
        raise ModelCompatibilityError(
            message,
            response_id=completion.id,
            usage=_token_usage(completion.usage),
        )
    if len(calls) != 1:
        raise ValueError(f"expected one function call, received {len(calls)}")
    call = calls[0].function
    arguments: object = (
        json.loads(call.arguments) if isinstance(call.arguments, str) else call.arguments
    )
    if not isinstance(arguments, dict):
        raise ValueError("function arguments must be a JSON object")
    return call.name, _JSON_OBJECT_ADAPTER.validate_python(arguments)


def _token_usage(usage: _Usage) -> TokenUsage:
    """Normalize NewAPI token counters into benchmark audit fields."""
    total = usage.total_tokens or usage.prompt_tokens + usage.completion_tokens
    return TokenUsage(
        input_tokens=usage.prompt_tokens,
        cached_tokens=usage.prompt_tokens_details.cached_tokens,
        output_tokens=usage.completion_tokens,
        reasoning_tokens=usage.completion_tokens_details.reasoning_tokens,
        total_tokens=total,
    )


def _truncated_without_tool_call(completion: _ChatCompletion) -> bool:
    """Identify a model that spent its response budget before choosing a tool."""
    if len(completion.choices) != 1:
        return False
    choice = completion.choices[0]
    return not choice.message.tool_calls and (
        choice.finish_reason == "length"
        or completion.usage.completion_tokens >= _FIXED_MAX_OUTPUT_TOKENS
    )


def _output_error(
    label: str,
    error: Exception,
    completion: _ChatCompletion,
    result: _GatewayResponse,
) -> ModelOutputError:
    """Attach provider observability to one invalid model response."""
    return ModelOutputError(
        f"{label}: {error}",
        request_id=_request_id(result.response),
        response_id=completion.id,
        usage=result.usage,
        attempts=result.attempts,
        latency_ms=result.latency_ms,
    )


def _retryable_response(response: httpx.Response) -> bool:
    """Retry temporary states and NewAPI-wrapped upstream failures."""
    return (
        response.status_code in {408, 409, 429}
        or response.status_code >= 500
        or _error_type(response) == _PROXY_UPSTREAM_ERROR_TYPE
    )


def _error_type(response: httpx.Response) -> str | None:
    """Read an error type from wrapped or unwrapped NewAPI envelopes."""
    body = _response_body(response)
    if not isinstance(body, Mapping):
        return None
    detail = body.get("error")
    error_type = detail.get("type") if isinstance(detail, Mapping) else body.get("type")
    return error_type if isinstance(error_type, str) else None


def _rejects_tool_choice(response: httpx.Response) -> bool:
    """Detect an explicit provider rejection of the optional tool-choice hint."""
    if response.status_code not in {400, 422}:
        return False
    message = _provider_error_message(response)
    if message is None:
        return False
    normalized = message.casefold()
    names_tool_choice = "tool_choice" in normalized or "tool choice" in normalized
    rejects_parameter = "not support" in normalized or "unsupported" in normalized
    return names_tool_choice and rejects_parameter


def _response_body(response: httpx.Response) -> object:
    """Return a decoded error body without assuming valid JSON."""
    try:
        return response.json()
    except ValueError:
        return None


def _provider_error_message(response: httpx.Response) -> str | None:
    """Read a provider diagnostic from wrapped or unwrapped error envelopes."""
    body = _response_body(response)
    if not isinstance(body, Mapping):
        return None
    detail = body.get("error")
    raw = detail.get("message") if isinstance(detail, Mapping) else body.get("message")
    return raw if isinstance(raw, str) else None


def _response_error(
    response: httpx.Response,
    api_key: SecretStr,
    attempts: int,
    latency_ms: int,
) -> ModelCompatibilityError | ModelConfigurationError | ModelInfrastructureError:
    """Classify a bounded HTTP failure without exposing credentials."""
    message = _safe_error_message(response, api_key)
    error_type = _error_type(response)
    if error_type != _PROXY_UPSTREAM_ERROR_TYPE and response.status_code in {
        400,
        404,
        405,
        422,
    }:
        return ModelCompatibilityError(
            message,
            request_id=_request_id(response),
            attempts=attempts,
            latency_ms=latency_ms,
        )
    error_class = (
        ModelInfrastructureError
        if error_type == _PROXY_UPSTREAM_ERROR_TYPE
        or response.status_code in {408, 409, 429}
        or response.status_code >= 500
        else ModelConfigurationError
    )
    return error_class(
        message,
        request_id=_request_id(response),
        attempts=attempts,
        latency_ms=latency_ms,
    )


def _safe_error_message(response: httpx.Response, api_key: SecretStr) -> str:
    """Extract one bounded provider diagnostic and redact the credential."""
    provider_message = _provider_error_message(response) or response.reason_phrase
    safe = bounded_error(RuntimeError(provider_message), 240)
    return f"NewAPI HTTP {response.status_code}: {safe}".replace(
        api_key.get_secret_value(),
        "[REDACTED]",
    )


def _transport_error(
    error: Exception,
    api_key: SecretStr,
    attempts: int,
    latency_ms: int,
) -> ModelInfrastructureError:
    """Return a bounded transport diagnostic without prompts or credentials."""
    return ModelInfrastructureError(
        bounded_error(error, 300, secrets=(api_key.get_secret_value(),)),
        attempts=attempts,
        latency_ms=latency_ms,
    )


def _request_id(response: httpx.Response) -> str | None:
    """Read either common NewAPI request identifier header."""
    return response.headers.get("x-request-id") or response.headers.get("request-id")


def _elapsed_ms(started: float) -> int:
    """Measure monotonic provider latency for success and failure audits."""
    return int((monotonic() - started) * 1000)


def _retry_delay_seconds(completed_attempts: int) -> float:
    """Back off briefly without making one benchmark stall indefinitely."""
    return min(0.25 * 2 ** (completed_attempts - 1), _MAX_RETRY_DELAY_SECONDS)


def _model_ids(*values: str) -> tuple[str, ...]:
    """Parse a stable, duplicate-free model catalog from environment text."""
    return tuple(
        dict.fromkeys(
            model
            for value in values
            for candidate in value.split(",")
            if (model := candidate.strip())
        )
    )
