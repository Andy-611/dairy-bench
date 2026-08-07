"""NewAPI Claude Messages adapter behind the provider-neutral model gateway."""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Mapping
from time import monotonic
from urllib.parse import urlsplit

from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
    RateLimitError,
)
from anthropic.types import Message, MessageParam, ToolChoiceAnyParam, ToolParam, ToolUseBlock
from pydantic import BaseModel, Field, SecretStr, TypeAdapter, ValidationError, field_validator

from company_bench.agent_models import (
    CommandModelRequest,
    CommandModelResult,
    CommandName,
    DecisionModel,
    DecisionSubmission,
    ModelInfrastructureError,
    ModelOutputError,
    ModelRequest,
    ModelResult,
    command_model,
)
from company_bench.diagnostics import bounded_error
from company_bench.models import StrictModel
from company_bench.precision import require_numeric_economic_schema
from company_bench.run_models import TokenUsage
from company_bench.runtime_models import CompanyCommand

DEFAULT_NEWAPI_BASE_URL = "https://newapi.deepwisdom.ai"
_DECISION_TOOL_NAME = "submit_decision"
_COMMAND_ADAPTER = TypeAdapter(CompanyCommand)
_TOOL_CHOICE: ToolChoiceAnyParam = {
    "type": "any",
    "disable_parallel_tool_use": True,
}
_MAX_RETRY_DELAY_SECONDS = 2.0
_PROXY_UPSTREAM_ERROR_TYPE = "bad_response_status_code"
_UNSUPPORTED_STRICT_SCHEMA_KEYWORDS = frozenset(
    {
        "default",
        "discriminator",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "multipleOf",
        "pattern",
        "uniqueItems",
    }
)


class NewApiClaudeConfig(StrictModel):
    """Validated server-only configuration for Claude through NewAPI."""

    api_key: SecretStr
    model: str = Field(min_length=1)
    models: tuple[str, ...] = ()
    base_url: str = Field(default=DEFAULT_NEWAPI_BASE_URL, min_length=1)
    max_output_tokens: int = Field(default=2048, ge=128, le=32768)
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    max_attempts: int = Field(default=10, ge=1, le=10)

    @field_validator("base_url")
    @classmethod
    def require_https_root(cls, value: str) -> str:
        """Keep credentials on one HTTPS origin with no embedded user information."""
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("NewAPI base_url must be an HTTPS origin")
        return value.rstrip("/")

    @classmethod
    def from_environment(cls) -> NewApiClaudeConfig | None:
        """Load NewAPI only when the key and at least one model are configured."""
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
            max_output_tokens=int(os.getenv("DAIRY_BENCH_NEWAPI_MAX_OUTPUT_TOKENS", "2048")),
            timeout_seconds=float(os.getenv("DAIRY_BENCH_NEWAPI_TIMEOUT_SECONDS", "60")),
            max_attempts=int(os.getenv("DAIRY_BENCH_NEWAPI_MAX_ATTEMPTS", "10")),
        )

    @property
    def fingerprint(self) -> str:
        """Identify behavior-affecting settings without exposing the API key."""
        payload = self.model_dump_json(exclude={"api_key", "models"})
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @property
    def available_models(self) -> tuple[str, ...]:
        """Return the ordered models this credential may select in the UI."""
        return self.models or (self.model,)

    def select_model(self, model: str | None) -> NewApiClaudeConfig:
        """Return one run-specific config after validating the selected model."""
        selected = model or self.model
        if selected not in self.available_models:
            raise ValueError(f"Claude model is not configured: {selected}")
        return self.model_copy(update={"model": selected})


class NewApiClaudeGateway:
    """Generate typed decisions through NewAPI's Anthropic Messages endpoint."""

    provider = "newapi"

    def __init__(
        self,
        config: NewApiClaudeConfig,
        client: AsyncAnthropic | None = None,
    ) -> None:
        self.config = config
        self._owns_client = client is None
        self._client = client or AsyncAnthropic(
            api_key=config.api_key.get_secret_value(),
            base_url=config.base_url,
            max_retries=0,
            timeout=config.timeout_seconds,
        )

    async def generate(
        self,
        request: ModelRequest,
        output_type: type[DecisionModel],
    ) -> ModelResult:
        """Return one schema-valid daily decision through a forced tool call."""
        submission_type = DecisionSubmission[output_type]
        response, attempt, latency_ms = await self._request(
            request,
            (_decision_tool(submission_type),),
        )
        try:
            name, arguments = _single_tool_use(response)
            if name != _DECISION_TOOL_NAME:
                raise ValueError(f"unknown decision tool: {name}")
            submission = submission_type.model_validate(arguments)
        except (ValidationError, ValueError) as error:
            raise _output_error(
                "invalid Claude decision",
                error,
                response,
                attempt,
                latency_ms,
            ) from error
        return ModelResult(
            decision=submission.decision,
            provider=self.provider,
            model=response.model or self.config.model,
            response_id=response.id,
            request_id=getattr(response, "_request_id", None),
            usage=_token_usage(response),
            attempts=attempt,
            latency_ms=latency_ms,
        )

    async def generate_command(
        self,
        request: CommandModelRequest,
    ) -> CommandModelResult:
        """Return exactly one role-authorized atomic command."""
        response, attempt, latency_ms = await self._request(
            request,
            tuple(_command_tool(name) for name in request.allowed_commands),
        )
        try:
            name, arguments = _single_tool_use(response)
            if name not in request.allowed_commands:
                raise ValueError(f"unknown or unauthorized command: {name}")
            if "kind" in arguments:
                raise ValueError("tool input must not supply a command kind")
            command = _COMMAND_ADAPTER.validate_python({"kind": name, **arguments})
        except (ValidationError, ValueError) as error:
            raise _output_error(
                "invalid Claude tool call",
                error,
                response,
                attempt,
                latency_ms,
            ) from error
        return CommandModelResult(
            command=command,
            provider=self.provider,
            model=response.model or self.config.model,
            response_id=response.id,
            request_id=getattr(response, "_request_id", None),
            usage=_token_usage(response),
            attempts=attempt,
            latency_ms=latency_ms,
        )

    async def close(self) -> None:
        """Close the client created by this adapter."""
        if self._owns_client:
            await self._client.close()

    async def _request(
        self,
        request: ModelRequest | CommandModelRequest,
        tools: tuple[ToolParam, ...],
    ) -> tuple[Message, int, int]:
        """Call Messages with bounded retries and return audit timing."""
        started = monotonic()
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                messages: tuple[MessageParam, ...] = (
                    {"role": "user", "content": request.input_text},
                )
                response = await self._client.messages.create(
                    model=self.config.model,
                    max_tokens=self.config.max_output_tokens,
                    system=request.instructions,
                    messages=messages,
                    tools=tools,
                    tool_choice=_TOOL_CHOICE,
                )
                return response, attempt, int((monotonic() - started) * 1000)
            except (APIConnectionError, APITimeoutError, RateLimitError) as error:
                if attempt == self.config.max_attempts:
                    raise _infrastructure_error(error, self.config.api_key) from error
            except APIStatusError as error:
                if not _retryable_status_error(error) or attempt == self.config.max_attempts:
                    raise _infrastructure_error(error, self.config.api_key) from error
            await asyncio.sleep(_retry_delay_seconds(attempt))
        raise AssertionError("bounded retry loop did not terminate")


def _decision_tool(submission_type: type[BaseModel]) -> ToolParam:
    """Expose one forced tool whose input is the typed decision envelope."""
    schema = submission_type.model_json_schema()
    require_numeric_economic_schema(schema)
    _normalize_schema_node(schema)
    return {
        "name": _DECISION_TOOL_NAME,
        "description": "Submit exactly one structured company decision.",
        "input_schema": schema,
        "strict": True,
    }


def _command_tool(name: CommandName) -> ToolParam:
    """Build one Anthropic tool from the canonical command model."""
    model = command_model(name)
    schema = model.model_json_schema()
    properties = schema.get("properties")
    if isinstance(properties, dict):
        properties.pop("kind", None)
    require_numeric_economic_schema(schema)
    _normalize_schema_node(schema)
    return {
        "name": name,
        "description": model.__doc__ or f"Execute {name}.",
        "input_schema": schema,
        "strict": True,
    }


def _normalize_schema_node(node: object) -> None:
    """Normalize Pydantic output to Anthropic's strict JSON Schema subset."""
    if isinstance(node, dict):
        for keyword in _UNSUPPORTED_STRICT_SCHEMA_KEYWORDS:
            node.pop(keyword, None)
        one_of = node.pop("oneOf", None)
        if one_of is not None:
            node["anyOf"] = one_of
        properties = node.get("properties")
        if isinstance(properties, dict):
            node["required"] = list(properties)
            node["additionalProperties"] = False
        for child in node.values():
            _normalize_schema_node(child)
    elif isinstance(node, list):
        for child in node:
            _normalize_schema_node(child)


def _single_tool_use(response: Message) -> tuple[str, dict[str, object]]:
    """Return exactly one Anthropic tool-use block."""
    calls = tuple(block for block in response.content if isinstance(block, ToolUseBlock))
    if len(calls) != 1:
        raise ValueError(f"expected exactly one tool use, received {len(calls)}")
    call = calls[0]
    name = call.name
    arguments = call.input
    if not isinstance(name, str) or not name:
        raise ValueError("tool use must have a name")
    if not isinstance(arguments, dict):
        raise ValueError("tool input must be a JSON object")
    return name, arguments


def _token_usage(response: Message) -> TokenUsage:
    """Normalize Anthropic token counters into benchmark audit fields."""
    usage = response.usage
    uncached = usage.input_tokens or 0
    cache_creation = usage.cache_creation_input_tokens or 0
    cached = usage.cache_read_input_tokens or 0
    output = usage.output_tokens or 0
    total_input = uncached + cache_creation + cached
    return TokenUsage(
        input_tokens=total_input,
        cached_tokens=cached,
        output_tokens=output,
        total_tokens=total_input + output,
    )


def _output_error(
    label: str,
    error: Exception,
    response: Message,
    attempts: int,
    latency_ms: int,
) -> ModelOutputError:
    """Attach provider observability to one invalid model response."""
    return ModelOutputError(
        f"{label}: {error}",
        request_id=getattr(response, "_request_id", None),
        response_id=getattr(response, "id", None),
        usage=_token_usage(response),
        attempts=attempts,
        latency_ms=latency_ms,
    )


def _retryable_status(status_code: int) -> bool:
    """Retry temporary HTTP states and provider failures."""
    return status_code in {408, 409, 429} or status_code >= 500


def _retryable_status_error(error: APIStatusError) -> bool:
    """Retry temporary HTTP states and NewAPI-wrapped upstream failures."""
    return _retryable_status(error.status_code) or _error_type(error.body) == (
        _PROXY_UPSTREAM_ERROR_TYPE
    )


def _error_type(body: object) -> str | None:
    """Read the error type from either a full or unwrapped provider envelope."""
    if not isinstance(body, Mapping):
        return None
    detail = body.get("error")
    error_type = detail.get("type") if isinstance(detail, Mapping) else body.get("type")
    return error_type if isinstance(error_type, str) else None


def _retry_delay_seconds(completed_attempts: int) -> float:
    """Back off briefly without making a long benchmark stall."""
    return min(0.25 * 2 ** (completed_attempts - 1), _MAX_RETRY_DELAY_SECONDS)


def _infrastructure_error(error: Exception, api_key: SecretStr) -> ModelInfrastructureError:
    """Return a bounded provider diagnostic without prompts or credentials."""
    return ModelInfrastructureError(
        bounded_error(error, 300, secrets=(api_key.get_secret_value(),))
    )


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
