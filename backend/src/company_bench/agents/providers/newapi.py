"""Typed NewAPI Chat Completions adapter for every model-backed company Agent."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
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
    DecisionGateway,
    DecisionModelRequest,
    DecisionModelResult,
    DecisionToolName,
    ModelCompatibilityError,
    ModelConfigurationError,
    ModelInfrastructureError,
    ModelOutputError,
    ModelQuotaExhaustedError,
    decision_tool_model,
)
from company_bench.agents.providers.capabilities import ModelCapabilityError
from company_bench.agents.quota import QUOTA_EXHAUSTION_MATCHER
from company_bench.diagnostics import bounded_error
from company_bench.domain.models import ProtocolIssueKind, StrictModel
from company_bench.domain.precision import require_numeric_economic_schema
from company_bench.runs.models import (
    ProviderAttempt,
    ProviderAttemptOutcome,
    TokenUsage,
)
from company_bench.runtime.models import CompanyDecision
from company_bench.settings import MAX_PARALLELISM, require_parallelism

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type AuditedModelError = (
    ModelOutputError
    | ModelConfigurationError
    | ModelInfrastructureError
    | ModelQuotaExhaustedError
)

DEFAULT_NEWAPI_BASE_URL = "https://newapi.deepwisdom.ai/v1"
_DECISION_ADAPTER = TypeAdapter(CompanyDecision)
_JSON_OBJECT_ADAPTER = TypeAdapter(JsonObject)
_DEFAULT_MAX_INPUT_TOKENS = 128_000
_DISCOVERY_MAX_OUTPUT_TOKENS = 2_147_483_647
_CAPABILITY_PROBE_TIMEOUT_SECONDS = 30.0
_PROTOCOL_ATTEMPTS = 2
_TOKEN_ESTIMATE_BYTES = 2
_MAX_RETRY_DELAY_SECONDS = 2.0
_PROXY_UPSTREAM_ERROR_TYPE = "bad_response_status_code"
_EXCLUSIVE_OUTPUT_LIMIT = re.compile(
    r"supported range is from \d[\d,]*\s*\(inclusive\)\s*to\s*"
    r"(?P<limit>\d[\d,]*)\s*\(exclusive\)",
    re.IGNORECASE,
)
_LESS_THAN_OUTPUT_LIMIT = re.compile(
    r"(?:max[_ ]?tokens|max[_ ]?output[_ ]?tokens|maxOutputTokens)\s+"
    r"must be less than\s+(?P<limit>\d[\d,]*)",
    re.IGNORECASE,
)
_INCLUSIVE_OUTPUT_LIMITS = (
    re.compile(r"supports? at most\s+(?P<limit>\d[\d,]*)", re.IGNORECASE),
    re.compile(
        r"max(?:imum)?(?:[_ ]output|[_ ]completion)?[_ ]tokens?[^\d]{0,32}"
        r"(?:is|of|<=|less than or equal to)\s*(?P<limit>\d[\d,]*)",
        re.IGNORECASE,
    ),
)


class NewApiConfig(StrictModel):
    """Validated server-only configuration for the single model provider."""

    api_key: SecretStr
    model: str = Field(min_length=1)
    models: tuple[str, ...] = ()
    base_url: str = Field(default=DEFAULT_NEWAPI_BASE_URL, min_length=1)
    timeout_seconds: float = Field(default=300.0, gt=0, le=600)
    max_attempts: int = Field(default=3, ge=1, le=10)
    max_input_tokens: int = Field(default=_DEFAULT_MAX_INPUT_TOKENS, ge=1)

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
            max_input_tokens=int(
                os.getenv(
                    "DAIRY_BENCH_NEWAPI_MAX_INPUT_TOKENS",
                    str(_DEFAULT_MAX_INPUT_TOKENS),
                )
            ),
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
        transport_cycle = self.timeout_seconds * self.max_attempts + retry_delays
        return transport_cycle * _PROTOCOL_ATTEMPTS

    def select_model(
        self,
        model: str | None,
        *,
        max_output_tokens: int,
    ) -> NewApiModelConfig:
        """Return one fully resolved model configuration after catalog validation."""
        selected = self.require_model(model)
        return NewApiModelConfig(
            **self.model_dump(exclude={"model"}),
            model=selected,
            max_output_tokens=max_output_tokens,
        )

    def require_model(self, model: str | None) -> str:
        """Return one allowed model ID without resolving its capabilities."""
        selected = model or self.model
        if selected not in self.available_models:
            raise ValueError(f"NewAPI model is not configured: {selected}")
        return selected


class NewApiModelConfig(NewApiConfig):
    """Run-specific NewAPI configuration with a confirmed output limit."""

    max_output_tokens: int = Field(gt=0)


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
    parallel_tool_calls: Literal[False] = False
    max_tokens: int
    n: Literal[1] = 1
    stream: Literal[False] = False

    def without_tool_choice(self) -> Self:
        """Omit an unsupported tool-choice hint while retaining all tools."""
        return self.model_copy(update={"tool_choice": None})

    def repair(self, violation: str) -> Self:
        """Append one bounded correction without replaying hidden model text."""
        correction = (
            "Your previous response violated the decision protocol: "
            f"{violation[:240]}. Return exactly one function call from the supplied tools, "
            "with one valid JSON argument object and no additional tool calls."
        )
        return self.model_copy(
            update={"messages": (*self.messages, _Message(role="user", content=correction))}
        )


class _CapabilityProbeRequest(StrictModel):
    model: str
    messages: tuple[_Message, ...] = (
        _Message(role="user", content="Reply with exactly OK and nothing else."),
    )
    max_tokens: int = Field(gt=0)
    n: Literal[1] = 1
    stream: Literal[False] = False


class NewApiRequestFeatures(StrictModel):
    """Negotiated optional hints for the benchmark decision protocol."""

    tool_choice_supported: bool = True

    def prepare(self, payload: _ChatRequest) -> _ChatRequest:
        """Apply the negotiated optional hints to one request."""
        return payload if self.tool_choice_supported else payload.without_tool_choice()

    def without_tool_choice(self) -> NewApiRequestFeatures:
        """Remember that this model rejects the optional forcing hint."""
        return self.model_copy(update={"tool_choice_supported": False})


@dataclass(frozen=True, slots=True)
class _GatewayResponse:
    """Validated response plus complete audit data for one logical call."""

    response: httpx.Response
    completion: _ChatCompletion
    attempt_history: tuple[ProviderAttempt, ...]


class _ProtocolViolation(ValueError):
    """Typed internal failure produced while decoding one model completion."""

    def __init__(
        self,
        kind: ProtocolIssueKind,
        message: str,
        *,
        repairable: bool = True,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.repairable = repairable


class NewApiTransport:
    """Share one bounded HTTP connection pool across every NewAPI gateway."""

    def __init__(
        self,
        config: NewApiConfig,
        *,
        max_concurrent_requests: int = MAX_PARALLELISM,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        max_concurrent_requests = require_parallelism(
            max_concurrent_requests,
            "max_concurrent_requests",
        )
        self._api_key = config.api_key.get_secret_value()
        self._base_url = config.base_url
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=config.timeout_seconds,
            limits=httpx.Limits(
                max_connections=max_concurrent_requests,
                max_keepalive_connections=max_concurrent_requests,
            ),
        )
        self._request_slots = asyncio.Semaphore(max_concurrent_requests)

    async def post(
        self,
        payload: _ChatRequest | _CapabilityProbeRequest,
    ) -> httpx.Response:
        """Send one request while holding a permit only for network I/O."""
        async with self._request_slots:
            return await self._client.post(
                f"{self._base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload.model_dump(mode="json", exclude_none=True),
            )

    async def close(self) -> None:
        """Close only the application-owned HTTP client."""
        if self._owns_client:
            await self._client.aclose()


class NewApiCapabilityProbe:
    """Validate one documented limit or discover it from a bounded gateway rejection."""

    def __init__(self, config: NewApiConfig, transport: NewApiTransport) -> None:
        self._config = config
        self._transport = transport

    async def max_output_tokens(self, model_id: str, candidate: int | None) -> int:
        """Return one limit confirmed against the active NewAPI route."""
        requested = candidate or _DISCOVERY_MAX_OUTPUT_TOKENS
        payload = _CapabilityProbeRequest(model=model_id, max_tokens=requested)
        try:
            async with asyncio.timeout(_CAPABILITY_PROBE_TIMEOUT_SECONDS):
                response = await self._transport.post(payload)
        except (TimeoutError, httpx.TimeoutException, httpx.TransportError) as error:
            raise ModelCapabilityError(
                f"NewAPI output-token calibration failed for '{model_id}': "
                f"{bounded_error(error, 240, secrets=(self._config.api_key.get_secret_value(),))}"
            ) from error

        if response.is_success:
            if candidate is None:
                raise ModelCapabilityError(
                    f"NewAPI accepted the discovery ceiling for '{model_id}' without "
                    "reporting its exact output-token limit; add a manual capability record"
                )
            return candidate

        discovered = _response_output_limit(response, requested)
        if discovered is not None:
            return discovered
        failure = _response_error(response, self._config.api_key, 1, 0)
        raise ModelCapabilityError(
            f"NewAPI output-token calibration failed for '{model_id}': {failure}"
        ) from failure


class NewApiModelGateway(DecisionGateway):
    """Generate typed company decisions through NewAPI's common chat interface."""

    provider = "newapi"

    def __init__(
        self,
        config: NewApiModelConfig,
        transport: NewApiTransport | None = None,
    ) -> None:
        self.config = config
        self._owns_transport = transport is None
        self._transport = transport or NewApiTransport(config)
        self._features = NewApiRequestFeatures()

    async def generate_decision(
        self,
        request: DecisionModelRequest,
    ) -> DecisionModelResult:
        """Return exactly one role-authorized atomic company decision."""
        payload = _chat_request(self.config, request)
        history: tuple[ProviderAttempt, ...] = ()
        for protocol_attempt in range(_PROTOCOL_ATTEMPTS):
            try:
                _require_input_budget(payload, self.config.max_input_tokens)
            except ModelOutputError as error:
                _audited_error(error, history)
                raise
            try:
                result = await self._request(payload, sequence_offset=len(history))
            except ModelCompatibilityError:
                raise
            except ModelOutputError as error:
                history = (*history, *error.attempt_history)
                if protocol_attempt + 1 < _PROTOCOL_ATTEMPTS:
                    payload = payload.repair(str(error))
                    continue
                _audited_error(error, history)
                raise

            completion = result.completion
            try:
                decision = _decode_decision(
                    completion,
                    request.allowed_tools,
                    self.config.max_output_tokens,
                )
            except _ProtocolViolation as violation:
                failed_history = _mark_protocol_error(result.attempt_history, violation)
                history = (*history, *failed_history)
                if violation.repairable and protocol_attempt + 1 < _PROTOCOL_ATTEMPTS:
                    payload = payload.repair(str(violation))
                    continue
                error_class = ModelOutputError if violation.repairable else ModelCompatibilityError
                error = error_class(
                    f"invalid NewAPI tool call: {violation}",
                    issue_kind=violation.kind,
                    request_id=_request_id(result.response),
                    response_id=completion.id,
                )
                raise _audited_error(error, history) from violation

            history = (*history, *result.attempt_history)
            usage = _attempt_usage(history)
            return DecisionModelResult(
                decision=decision,
                provider=self.provider,
                model=completion.model or self.config.model,
                response_id=completion.id,
                request_id=_request_id(result.response),
                usage=usage,
                attempts=len(history),
                attempt_history=history,
                latency_ms=_attempt_latency(history),
            )
        raise AssertionError("bounded protocol repair loop did not terminate")

    async def close(self) -> None:
        """Close only a transport created privately by this gateway."""
        if self._owns_transport:
            await self._transport.close()

    async def _request(
        self,
        payload: _ChatRequest,
        *,
        sequence_offset: int,
    ) -> _GatewayResponse:
        """Call NewAPI with bounded transport retries and physical audit records."""
        history: list[ProviderAttempt] = []
        payload = self._features.prepare(payload)
        for attempt in range(1, self.config.max_attempts + 1):
            started = monotonic()
            sequence = sequence_offset + len(history) + 1
            try:
                response = await self._transport.post(payload)
            except (httpx.TimeoutException, httpx.TransportError) as error:
                history.append(
                    ProviderAttempt(
                        sequence=sequence,
                        outcome=ProviderAttemptOutcome.TRANSPORT_ERROR,
                        latency_ms=_elapsed_ms(started),
                        error_kind=type(error).__name__,
                        error_message=bounded_error(
                            error,
                            300,
                            secrets=(self.config.api_key.get_secret_value(),),
                        ),
                    )
                )
                if attempt == self.config.max_attempts:
                    failure = _transport_error(error, self.config.api_key, attempt, 0)
                    raise _audited_error(failure, tuple(history)) from error
            else:
                latency_ms = _elapsed_ms(started)
                if response.is_success:
                    try:
                        completion = _parse_completion(response, attempt, latency_ms)
                    except ModelOutputError as error:
                        history.append(
                            ProviderAttempt(
                                sequence=sequence,
                                outcome=ProviderAttemptOutcome.PROTOCOL_ERROR,
                                request_id=_request_id(response),
                                latency_ms=latency_ms,
                                error_kind=error.issue_kind.value,
                                error_message=bounded_error(error, 300),
                            )
                        )
                        _audited_error(error, tuple(history))
                        raise
                    history.append(
                        ProviderAttempt(
                            sequence=sequence,
                            outcome=ProviderAttemptOutcome.SUCCESS,
                            request_id=_request_id(response),
                            response_id=completion.id,
                            usage=_token_usage(completion.usage),
                            latency_ms=latency_ms,
                        )
                    )
                    return _GatewayResponse(
                        response=response,
                        completion=completion,
                        attempt_history=tuple(history),
                    )
                failure = _response_error(response, self.config.api_key, attempt, latency_ms)
                history.append(
                    ProviderAttempt(
                        sequence=sequence,
                        outcome=ProviderAttemptOutcome.HTTP_ERROR,
                        request_id=_request_id(response),
                        latency_ms=latency_ms,
                        error_kind=type(failure).__name__,
                        error_message=bounded_error(failure, 300),
                    )
                )
                if payload.tool_choice and _rejects_tool_choice(response):
                    self._features = self._features.without_tool_choice()
                    if attempt < self.config.max_attempts:
                        payload = payload.without_tool_choice()
                        continue
                if not _retryable_response(response) or attempt == self.config.max_attempts:
                    raise _audited_error(failure, tuple(history))
            await asyncio.sleep(_retry_delay_seconds(attempt))
        raise AssertionError("bounded retry loop did not terminate")


def _chat_request(
    config: NewApiModelConfig,
    request: DecisionModelRequest,
) -> _ChatRequest:
    """Build one provider request from the provider-neutral decision contract."""
    return _ChatRequest(
        model=config.model,
        messages=(
            _Message(role="system", content=request.instructions),
            _Message(role="user", content=request.input_text),
        ),
        tools=tuple(_decision_tool(name) for name in request.allowed_tools),
        max_tokens=config.max_output_tokens,
    )


def _decision_tool(name: DecisionToolName) -> _Tool:
    """Build one Chat Completions function from the canonical decision input."""
    model = decision_tool_model(name)
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


def _single_tool_call(
    completion: _ChatCompletion,
    max_output_tokens: int,
) -> tuple[str, JsonObject]:
    """Return exactly one tool call or one typed protocol violation."""
    if len(completion.choices) != 1:
        raise _ProtocolViolation(
            ProtocolIssueKind.INVALID_RESPONSE,
            f"expected one choice, received {len(completion.choices)}",
        )
    calls = completion.choices[0].message.tool_calls
    if not calls:
        truncated = _truncated_without_tool_call(completion, max_output_tokens)
        message = (
            f"selected NewAPI model exhausted its {max_output_tokens}-token response budget "
            "before returning a function call"
            if truncated
            else "selected NewAPI model did not return a required function call"
        )
        raise _ProtocolViolation(
            ProtocolIssueKind.MISSING_TOOL_CALL,
            message,
            repairable=not truncated,
        )
    if len(calls) != 1:
        raise _ProtocolViolation(
            ProtocolIssueKind.MULTIPLE_TOOL_CALLS,
            f"expected one function call, received {len(calls)}",
        )
    call = calls[0].function
    try:
        arguments: object = (
            json.loads(call.arguments) if isinstance(call.arguments, str) else call.arguments
        )
    except json.JSONDecodeError as error:
        raise _ProtocolViolation(
            ProtocolIssueKind.INVALID_ARGUMENTS,
            f"function arguments are not valid JSON: {error}",
        ) from error
    if not isinstance(arguments, dict):
        raise _ProtocolViolation(
            ProtocolIssueKind.INVALID_ARGUMENTS,
            "function arguments must be a JSON object",
        )
    try:
        return call.name, _JSON_OBJECT_ADAPTER.validate_python(arguments)
    except ValidationError as error:
        raise _ProtocolViolation(
            ProtocolIssueKind.INVALID_ARGUMENTS,
            str(error),
        ) from error


def _decode_decision(
    completion: _ChatCompletion,
    allowed_tools: tuple[DecisionToolName, ...],
    max_output_tokens: int,
) -> CompanyDecision:
    """Decode one provider tool call into the canonical company decision."""
    name, arguments = _single_tool_call(completion, max_output_tokens)
    if name not in allowed_tools:
        raise _ProtocolViolation(
            ProtocolIssueKind.UNAUTHORIZED_DECISION_TOOL,
            f"unknown or unauthorized decision tool: {name}",
        )
    if "kind" in arguments:
        raise _ProtocolViolation(
            ProtocolIssueKind.INVALID_ARGUMENTS,
            "tool input must not supply a decision kind",
        )
    try:
        submitted = decision_tool_model(name).model_validate({"kind": name, **arguments})
        payload = submitted.model_dump()
        attention = payload.pop("attention")
        decision = (
            {"kind": "idle", "attention": attention}
            if name == "idle"
            else {"kind": "action", "action": payload, "attention": attention}
        )
        return _DECISION_ADAPTER.validate_python(decision)
    except ValidationError as error:
        raise _ProtocolViolation(
            ProtocolIssueKind.INVALID_ARGUMENTS,
            str(error),
        ) from error


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


def _truncated_without_tool_call(
    completion: _ChatCompletion,
    max_output_tokens: int,
) -> bool:
    """Identify a model that spent its response budget before choosing a tool."""
    if len(completion.choices) != 1:
        return False
    choice = completion.choices[0]
    return not choice.message.tool_calls and (
        choice.finish_reason == "length" or completion.usage.completion_tokens >= max_output_tokens
    )


def _require_input_budget(payload: _ChatRequest, max_input_tokens: int) -> None:
    """Bound the complete serialized request, including every tool schema."""
    encoded = payload.model_dump_json(exclude_none=True).encode()
    estimate = max(1, (len(encoded) + _TOKEN_ESTIMATE_BYTES - 1) // _TOKEN_ESTIMATE_BYTES)
    if estimate > max_input_tokens:
        raise ModelOutputError(
            f"complete NewAPI request estimate {estimate} exceeds {max_input_tokens} tokens",
            issue_kind=ProtocolIssueKind.CONTEXT_TOO_LARGE,
            attempts=0,
        )


def _mark_protocol_error(
    attempts: tuple[ProviderAttempt, ...],
    violation: _ProtocolViolation,
) -> tuple[ProviderAttempt, ...]:
    """Reclassify the final HTTP success as a protocol-invalid completion."""
    if not attempts or attempts[-1].outcome is not ProviderAttemptOutcome.SUCCESS:
        raise ValueError("protocol validation requires one successful provider response")
    failed = attempts[-1].model_copy(
        update={
            "outcome": ProviderAttemptOutcome.PROTOCOL_ERROR,
            "error_kind": violation.kind.value,
            "error_message": str(violation)[:300],
        }
    )
    return (*attempts[:-1], failed)


def _attempt_usage(attempts: tuple[ProviderAttempt, ...]) -> TokenUsage:
    """Sum provider-reported usage across every physical request."""
    return sum((attempt.usage for attempt in attempts), start=TokenUsage())


def _attempt_latency(attempts: tuple[ProviderAttempt, ...]) -> int:
    """Sum physical request latency without counting local validation time."""
    return sum(attempt.latency_ms for attempt in attempts)


def _audited_error(
    error: AuditedModelError,
    attempts: tuple[ProviderAttempt, ...],
) -> AuditedModelError:
    """Attach cumulative immutable provider audit metadata to one failure."""
    error.attempt_history = attempts
    error.attempts = len(attempts) or error.attempts
    error.usage = _attempt_usage(attempts)
    error.latency_ms = _attempt_latency(attempts)
    return error


def _retryable_response(response: httpx.Response) -> bool:
    """Retry temporary states and NewAPI-wrapped upstream failures."""
    return not _is_quota_exhaustion(response) and (
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


def _response_output_limit(response: httpx.Response, requested: int) -> int | None:
    """Extract one explicit output-token ceiling from a provider rejection."""
    if response.status_code not in {400, 422}:
        return None
    message = _provider_error_message(response)
    if message is None or not _names_output_token_parameter(message):
        return None

    exclusive = _EXCLUSIVE_OUTPUT_LIMIT.search(message) or _LESS_THAN_OUTPUT_LIMIT.search(message)
    if exclusive is not None:
        return _valid_discovered_limit(exclusive.group("limit"), requested, adjustment=-1)
    for pattern in _INCLUSIVE_OUTPUT_LIMITS:
        if match := pattern.search(message):
            return _valid_discovered_limit(match.group("limit"), requested)
    return None


def _names_output_token_parameter(message: str) -> bool:
    """Avoid mistaking context or input-token diagnostics for an output limit."""
    normalized = message.casefold().replace("_", "").replace(" ", "")
    return any(
        name in normalized
        for name in (
            "maxtokens",
            "maxoutputtokens",
            "maxcompletiontokens",
            "outputtokens",
            "completiontokens",
        )
    )


def _valid_discovered_limit(
    raw_limit: str,
    requested: int,
    *,
    adjustment: int = 0,
) -> int | None:
    """Accept only a positive ceiling stricter than the rejected request."""
    limit = int(raw_limit.replace(",", "")) + adjustment
    return limit if 0 < limit < requested else None


def _response_error(
    response: httpx.Response,
    api_key: SecretStr,
    attempts: int,
    latency_ms: int,
) -> AuditedModelError:
    """Classify a bounded HTTP failure without exposing credentials."""
    message = _safe_error_message(response, api_key)
    metadata = {
        "request_id": _request_id(response),
        "attempts": attempts,
        "latency_ms": latency_ms,
    }
    if _is_quota_exhaustion(response):
        return ModelQuotaExhaustedError(message, **metadata)
    error_type = _error_type(response)
    if error_type != _PROXY_UPSTREAM_ERROR_TYPE and response.status_code in {
        400,
        404,
        405,
        422,
    }:
        return ModelCompatibilityError(
            message,
            **metadata,
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
        **metadata,
    )


def _is_quota_exhaustion(response: httpx.Response) -> bool:
    """Recognize paid-quota exhaustion without swallowing rate or permission errors."""
    message = _provider_error_message(response)
    explicit_diagnostic = (
        message is not None and QUOTA_EXHAUSTION_MATCHER.matches(message)
    )
    return response.status_code in {402, 403, 429} and (
        explicit_diagnostic
        or _error_type(response) in {"insufficient_balance", "insufficient_quota"}
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
