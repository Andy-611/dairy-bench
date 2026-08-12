"""Typed NewAPI protocol adapters for every model-backed company Agent."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from time import monotonic
from typing import ClassVar, Literal, Protocol, Self
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
    ModelCallError,
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
    ModelOutputError | ModelConfigurationError | ModelInfrastructureError | ModelQuotaExhaustedError
)

DEFAULT_NEWAPI_BASE_URL = "https://newapi.deepwisdom.ai/v1"
DEFAULT_NEWAPI_ENV_PREFIX = "DAIRY_BENCH_NEWAPI_MODEL"
_DECISION_ADAPTER = TypeAdapter(CompanyDecision)
_JSON_OBJECT_ADAPTER = TypeAdapter(JsonObject)
_DEFAULT_MAX_INPUT_TOKENS = 128_000
_DISCOVERY_MAX_OUTPUT_TOKENS = 2_147_483_647
_CAPABILITY_PROBE_TIMEOUT_SECONDS = 30.0
_PROTOCOL_ATTEMPTS = 2
_TOKEN_ESTIMATE_BYTES = 2
_MAX_RETRY_DELAY_SECONDS = 2.0
_PROXY_UPSTREAM_ERROR_TYPE = "bad_response_status_code"
_SAFE_STREAM_ERROR_TYPES = frozenset(
    {
        "api_error",
        "internal_error",
        "rate_limit_error",
        "server_error",
        "timeout_error",
        "upstream_error",
    }
)
_SAFE_PROVIDER_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
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


class NewApiWireProtocol(StrEnum):
    """Supported NewAPI wire formats behind the decision gateway seam."""

    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"


class NewApiConfig(StrictModel):
    """Validated server-only configuration for one NewAPI policy profile."""

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
    def from_environment(
        cls,
        prefix: str = DEFAULT_NEWAPI_ENV_PREFIX,
    ) -> NewApiConfig | None:
        """Load one profile-scoped NewAPI credential and model catalog."""
        prefix = prefix.strip()
        if not prefix:
            raise ValueError("NewAPI environment prefix cannot be empty")
        api_key = os.getenv(f"{prefix}_API_KEY", "").strip()
        models = _model_ids(
            os.getenv(f"{prefix}_MODELS", ""),
            os.getenv(f"{prefix}_MODEL", ""),
        )
        if not api_key or not models:
            return None
        return cls(
            api_key=SecretStr(api_key),
            model=models[0],
            models=models,
            timeout_seconds=float(os.getenv(f"{prefix}_TIMEOUT_SECONDS", "300")),
            max_attempts=int(os.getenv(f"{prefix}_MAX_ATTEMPTS", "3")),
            max_input_tokens=int(
                os.getenv(
                    f"{prefix}_MAX_INPUT_TOKENS",
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
            **self.model_dump(exclude={"model", "max_output_tokens"}),
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


class _ResponsesTokenDetails(_ExternalModel):
    cached_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)


class _ResponsesUsage(_ExternalModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    input_tokens_details: _ResponsesTokenDetails = _ResponsesTokenDetails()
    output_tokens_details: _ResponsesTokenDetails = _ResponsesTokenDetails()


class _ResponsesOutputItem(_ExternalModel):
    type: str
    name: str | None = None
    arguments: str | JsonObject | None = None


class _ResponsesCompletion(_ExternalModel):
    id: str | None = None
    model: str | None = None
    status: str | None = None
    output: tuple[_ResponsesOutputItem, ...] = ()
    usage: _ResponsesUsage = _ResponsesUsage()


class _AnthropicContentBlock(_ExternalModel):
    type: str
    name: str | None = None
    input: JsonObject | None = None


class _AnthropicUsage(_ExternalModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)


class _AnthropicCompletion(_ExternalModel):
    id: str | None = None
    model: str | None = None
    stop_reason: str | None = None
    content: tuple[_AnthropicContentBlock, ...] = ()
    usage: _AnthropicUsage = _AnthropicUsage()


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


class _ResponsesTool(StrictModel):
    type: Literal["function"] = "function"
    name: str
    description: str
    parameters: JsonObject
    strict: Literal[True] = True


class _AnthropicTool(StrictModel):
    name: str
    description: str
    input_schema: JsonObject


class _AnthropicMessage(StrictModel):
    role: Literal["user"] = "user"
    content: str


class _ResponsesInputMessage(StrictModel):
    role: Literal["user"] = "user"
    content: str


class _AnthropicToolChoice(StrictModel):
    type: Literal["any"] = "any"
    disable_parallel_tool_use: Literal[True] = True


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


class _ResponsesRequest(StrictModel):
    model: str
    instructions: str
    input: tuple[_ResponsesInputMessage, ...]
    tools: tuple[_ResponsesTool, ...]
    tool_choice: Literal["required"] = "required"
    parallel_tool_calls: Literal[False] = False
    max_output_tokens: int
    store: Literal[False] = False
    stream: Literal[True] = True

    def repair(self, violation: str) -> Self:
        """Append one bounded decision-protocol correction to the input."""
        return self.model_copy(
            update={
                "input": (
                    *self.input,
                    _ResponsesInputMessage(content=_repair_instruction(violation)),
                )
            }
        )


class _AnthropicRequest(StrictModel):
    model: str
    system: str
    messages: tuple[_AnthropicMessage, ...]
    tools: tuple[_AnthropicTool, ...]
    tool_choice: _AnthropicToolChoice = _AnthropicToolChoice()
    max_tokens: int
    stream: Literal[False] = False

    def repair(self, violation: str) -> Self:
        """Append one bounded decision-protocol correction as a user message."""
        return self.model_copy(
            update={
                "messages": (
                    *self.messages,
                    _AnthropicMessage(content=_repair_instruction(violation)),
                )
            }
        )


type _WireRequest = _ChatRequest | _ResponsesRequest | _AnthropicRequest


class NewApiRequestFeatures(StrictModel):
    """Negotiated optional hints for the benchmark decision protocol."""

    tool_choice_supported: bool = True

    def prepare(self, payload: _WireRequest) -> _WireRequest:
        """Apply the negotiated optional hints to one request."""
        if self.tool_choice_supported or not isinstance(payload, _ChatRequest):
            return payload
        return payload.without_tool_choice()

    def without_tool_choice(self) -> NewApiRequestFeatures:
        """Remember that this model rejects the optional forcing hint."""
        return self.model_copy(update={"tool_choice_supported": False})


@dataclass(frozen=True, slots=True)
class _NormalizedToolCall:
    """One provider tool call normalized without decoding its domain payload."""

    name: str
    arguments: str | JsonObject


@dataclass(frozen=True, slots=True)
class _NormalizedCompletion:
    """Provider-neutral result of one successful wire response."""

    response_id: str | None
    calls: tuple[_NormalizedToolCall, ...]
    usage: TokenUsage
    truncated: bool = False
    output_limit: int = 0
    invalid_response: str | None = None


class _DecisionWireInput(Protocol):
    """Request fields consumed by every wire adapter."""

    instructions: str
    input_text: str
    allowed_tools: tuple[DecisionToolName, ...]


class _CapabilityDecisionRequest(StrictModel):
    """Minimal canonical-tool request used only for readiness probing."""

    instructions: str = "Return exactly one authorized idle tool call."
    input_text: str = '{"probe":"newapi_decision_protocol"}'
    allowed_tools: tuple[DecisionToolName, ...] = ("idle",)


class _WireAdapter(Protocol):
    """Internal adapter for one NewAPI request and response representation."""

    protocol: ClassVar[NewApiWireProtocol]
    adapter_version: ClassVar[str]

    def request(
        self,
        config: NewApiModelConfig,
        request: _DecisionWireInput,
    ) -> _WireRequest:
        """Encode one provider-neutral decision request."""
        ...

    def repair(self, payload: _WireRequest, violation: str) -> _WireRequest:
        """Return one request carrying a bounded protocol correction."""
        ...

    def parse(
        self,
        response: httpx.Response,
        attempts: int,
        latency_ms: int,
        max_output_tokens: int,
    ) -> _NormalizedCompletion:
        """Normalize one successful physical response."""
        ...


class _ChatCompletionsAdapter:
    protocol = NewApiWireProtocol.CHAT_COMPLETIONS
    adapter_version = "newapi-chat-completions-v1"

    def request(
        self,
        config: NewApiModelConfig,
        request: _DecisionWireInput,
    ) -> _ChatRequest:
        return _chat_request(config, request)

    def repair(self, payload: _WireRequest, violation: str) -> _ChatRequest:
        if not isinstance(payload, _ChatRequest):
            raise TypeError("Chat Completions adapter received another wire request")
        return payload.repair(violation)

    def parse(
        self,
        response: httpx.Response,
        attempts: int,
        latency_ms: int,
        max_output_tokens: int,
    ) -> _NormalizedCompletion:
        completion = _parse_chat_completion(response, attempts, latency_ms)
        calls: tuple[_NormalizedToolCall, ...] = ()
        if len(completion.choices) == 1:
            calls = tuple(
                _NormalizedToolCall(call.function.name, call.function.arguments)
                for call in completion.choices[0].message.tool_calls
            )
        return _NormalizedCompletion(
            response_id=completion.id,
            calls=calls,
            usage=_chat_token_usage(completion.usage),
            truncated=_chat_truncated(completion, max_output_tokens),
            output_limit=max_output_tokens,
            invalid_response=(
                None
                if len(completion.choices) == 1
                else f"expected one choice, received {len(completion.choices)}"
            ),
        )


class _ResponsesAdapter:
    protocol = NewApiWireProtocol.RESPONSES
    adapter_version = "newapi-responses-v2"

    def request(
        self,
        config: NewApiModelConfig,
        request: _DecisionWireInput,
    ) -> _ResponsesRequest:
        return _responses_request(config, request)

    def repair(self, payload: _WireRequest, violation: str) -> _ResponsesRequest:
        if not isinstance(payload, _ResponsesRequest):
            raise TypeError("Responses adapter received another wire request")
        return payload.repair(violation)

    def parse(
        self,
        response: httpx.Response,
        attempts: int,
        latency_ms: int,
        max_output_tokens: int,
    ) -> _NormalizedCompletion:
        completion = _parse_responses_completion(response, attempts, latency_ms)
        if completion.status in {"cancelled", "failed"}:
            raise ModelInfrastructureError(
                f"NewAPI Responses request ended with status {completion.status}",
                request_id=_request_id(response),
                response_id=completion.id,
                attempts=attempts,
                latency_ms=latency_ms,
            )
        calls = tuple(
            _NormalizedToolCall(item.name, item.arguments)
            for item in completion.output
            if item.type == "function_call" and item.name is not None and item.arguments is not None
        )
        return _NormalizedCompletion(
            response_id=completion.id,
            calls=calls,
            usage=_responses_token_usage(completion.usage),
            truncated=completion.status in {"incomplete", "max_output_tokens"},
            output_limit=max_output_tokens,
        )


class _AnthropicMessagesAdapter:
    protocol = NewApiWireProtocol.ANTHROPIC_MESSAGES
    adapter_version = "newapi-anthropic-messages-v1"

    def request(
        self,
        config: NewApiModelConfig,
        request: _DecisionWireInput,
    ) -> _AnthropicRequest:
        return _anthropic_request(config, request)

    def repair(self, payload: _WireRequest, violation: str) -> _AnthropicRequest:
        if not isinstance(payload, _AnthropicRequest):
            raise TypeError("Anthropic Messages adapter received another wire request")
        return payload.repair(violation)

    def parse(
        self,
        response: httpx.Response,
        attempts: int,
        latency_ms: int,
        max_output_tokens: int,
    ) -> _NormalizedCompletion:
        completion = _parse_anthropic_completion(response, attempts, latency_ms)
        calls = tuple(
            _NormalizedToolCall(block.name, block.input)
            for block in completion.content
            if block.type == "tool_use" and block.name is not None and block.input is not None
        )
        return _NormalizedCompletion(
            response_id=completion.id,
            calls=calls,
            usage=_anthropic_token_usage(completion.usage),
            truncated=completion.stop_reason == "max_tokens",
            output_limit=max_output_tokens,
        )


_WIRE_ADAPTERS: Mapping[NewApiWireProtocol, _WireAdapter] = {
    NewApiWireProtocol.CHAT_COMPLETIONS: _ChatCompletionsAdapter(),
    NewApiWireProtocol.RESPONSES: _ResponsesAdapter(),
    NewApiWireProtocol.ANTHROPIC_MESSAGES: _AnthropicMessagesAdapter(),
}


@dataclass(frozen=True, slots=True)
class _GatewayResponse:
    """Validated response plus complete audit data for one logical call."""

    request_id: str | None
    completion: _NormalizedCompletion
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
        request_slots: asyncio.Semaphore | None = None,
    ) -> None:
        max_concurrent_requests = require_parallelism(
            max_concurrent_requests,
            "max_concurrent_requests",
        )
        self._api_key = config.api_key.get_secret_value()
        self._base_url = config.base_url
        self._owns_client = client is None
        self._client = client
        self._timeout = config.timeout_seconds
        self._limits = httpx.Limits(
            max_connections=max_concurrent_requests,
            max_keepalive_connections=max_concurrent_requests,
        )
        self._request_slots = request_slots or asyncio.Semaphore(max_concurrent_requests)

    async def post(
        self,
        payload: BaseModel,
        *,
        protocol: NewApiWireProtocol = NewApiWireProtocol.CHAT_COMPLETIONS,
    ) -> httpx.Response:
        """Send one request while holding a permit only for network I/O."""
        headers = (
            {
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }
            if protocol is NewApiWireProtocol.ANTHROPIC_MESSAGES
            else {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
        )
        endpoint = {
            NewApiWireProtocol.CHAT_COMPLETIONS: "chat/completions",
            NewApiWireProtocol.RESPONSES: "responses",
            NewApiWireProtocol.ANTHROPIC_MESSAGES: "messages",
        }[protocol]
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout, limits=self._limits)
            self._client = client
        async with self._request_slots:
            return await client.post(
                f"{self._base_url}/{endpoint}",
                headers=headers,
                json=payload.model_dump(mode="json", exclude_none=True),
            )

    async def close(self) -> None:
        """Close only the application-owned HTTP client."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()


class NewApiCapabilityProbe:
    """Validate a model's output limit and canonical tool protocol."""

    def __init__(
        self,
        config: NewApiConfig,
        transport: NewApiTransport,
        protocol: NewApiWireProtocol = NewApiWireProtocol.CHAT_COMPLETIONS,
    ) -> None:
        self._config = config
        self._transport = transport
        self._protocol = protocol

    async def max_output_tokens(self, model_id: str, candidate: int | None) -> int:
        """Return one limit confirmed against the active NewAPI route."""
        requested = candidate or _DISCOVERY_MAX_OUTPUT_TOKENS
        request = _capability_decision_request()
        adapter = _WIRE_ADAPTERS[self._protocol]
        payload = adapter.request(
            self._config.select_model(model_id, max_output_tokens=requested),
            request,
        )
        response = await self._post(model_id, payload)

        if response.is_success:
            self._validate_decision_protocol(
                model_id,
                adapter,
                response,
                request,
                requested,
            )
            if candidate is not None:
                return candidate
            raise ModelCapabilityError(
                f"NewAPI accepted the discovery ceiling for '{model_id}' without "
                "reporting its exact output-token limit; add a manual capability record"
            )

        discovered = _response_output_limit(response, requested)
        if discovered is None:
            failure = _response_error(response, self._config.api_key, 1, 0)
            raise ModelCapabilityError(
                f"NewAPI output-token calibration failed for '{model_id}': {failure}"
            ) from failure

        verification = adapter.request(
            self._config.select_model(model_id, max_output_tokens=discovered),
            request,
        )
        response = await self._post(model_id, verification)
        if not response.is_success:
            failure = _response_error(response, self._config.api_key, 1, 0)
            raise ModelCapabilityError(
                f"NewAPI decision-protocol calibration failed for '{model_id}': {failure}"
            ) from failure
        self._validate_decision_protocol(
            model_id,
            adapter,
            response,
            request,
            discovered,
        )
        return discovered

    async def _post(self, model_id: str, payload: _WireRequest) -> httpx.Response:
        """Send one bounded readiness request on the selected wire protocol."""
        try:
            async with asyncio.timeout(_CAPABILITY_PROBE_TIMEOUT_SECONDS):
                return await self._transport.post(payload, protocol=self._protocol)
        except (TimeoutError, httpx.TimeoutException, httpx.TransportError) as error:
            raise ModelCapabilityError(
                f"NewAPI output-token calibration failed for '{model_id}': "
                f"{bounded_error(error, 240, secrets=(self._config.api_key.get_secret_value(),))}"
            ) from error

    @staticmethod
    def _validate_decision_protocol(
        model_id: str,
        adapter: _WireAdapter,
        response: httpx.Response,
        request: _CapabilityDecisionRequest,
        max_output_tokens: int,
    ) -> None:
        """Require one schema-valid canonical idle call before readiness."""
        try:
            completion = adapter.parse(response, 1, 0, max_output_tokens)
            _decode_normalized_decision(completion, request.allowed_tools)
        except (ModelCallError, _ProtocolViolation) as error:
            raise ModelCapabilityError(
                f"NewAPI decision-protocol calibration failed for '{model_id}': {error}"
            ) from error


class _NewApiDecisionGateway(DecisionGateway):
    """Shared audited decision gateway parameterized by one wire adapter."""

    provider = "newapi"
    wire_protocol: ClassVar[NewApiWireProtocol]
    adapter_version: ClassVar[str]
    _adapter: ClassVar[_WireAdapter]

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
        payload = self._adapter.request(self.config, request)
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
                    payload = self._adapter.repair(payload, str(error))
                    continue
                _audited_error(error, history)
                raise

            completion = result.completion
            try:
                decision = _decode_normalized_decision(completion, request.allowed_tools)
            except _ProtocolViolation as violation:
                failed_history = _mark_protocol_error(result.attempt_history, violation)
                history = (*history, *failed_history)
                if violation.repairable and protocol_attempt + 1 < _PROTOCOL_ATTEMPTS:
                    payload = self._adapter.repair(payload, str(violation))
                    continue
                error_class = ModelOutputError if violation.repairable else ModelCompatibilityError
                error = error_class(
                    f"invalid NewAPI tool call: {violation}",
                    issue_kind=violation.kind,
                    request_id=result.request_id,
                    response_id=completion.response_id,
                )
                raise _audited_error(error, history) from violation

            history = (*history, *result.attempt_history)
            usage = _attempt_usage(history)
            return DecisionModelResult(
                decision=decision,
                provider=self.provider,
                model=self.config.model,
                response_id=completion.response_id,
                request_id=result.request_id,
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
        payload: _WireRequest,
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
                response = await self._transport.post(
                    payload,
                    protocol=self.wire_protocol,
                )
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
                request_id = _safe_provider_identifier(
                    _request_id(response),
                    self.config.api_key,
                )
                if response.is_success:
                    try:
                        completion = self._adapter.parse(
                            response,
                            attempt,
                            latency_ms,
                            self.config.max_output_tokens,
                        )
                    except (ModelOutputError, ModelInfrastructureError) as error:
                        _sanitize_model_error(
                            error,
                            request_id,
                            self.config.api_key,
                        )
                        issue_kind = (
                            error.issue_kind.value
                            if isinstance(error, ModelOutputError)
                            else type(error).__name__
                        )
                        history.append(
                            ProviderAttempt(
                                sequence=sequence,
                                outcome=(
                                    ProviderAttemptOutcome.PROTOCOL_ERROR
                                    if isinstance(error, ModelOutputError)
                                    else ProviderAttemptOutcome.HTTP_ERROR
                                ),
                                request_id=request_id,
                                response_id=error.response_id,
                                latency_ms=latency_ms,
                                error_kind=issue_kind,
                                error_message=bounded_error(
                                    error,
                                    300,
                                    secrets=(self.config.api_key.get_secret_value(),),
                                ),
                            )
                        )
                        if (
                            isinstance(error, ModelInfrastructureError)
                            and attempt < self.config.max_attempts
                        ):
                            await asyncio.sleep(_retry_delay_seconds(attempt))
                            continue
                        _audited_error(error, tuple(history))
                        raise
                    completion = replace(
                        completion,
                        response_id=_safe_provider_identifier(
                            completion.response_id,
                            self.config.api_key,
                        ),
                    )
                    history.append(
                        ProviderAttempt(
                            sequence=sequence,
                            outcome=ProviderAttemptOutcome.SUCCESS,
                            request_id=request_id,
                            response_id=completion.response_id,
                            usage=completion.usage,
                            latency_ms=latency_ms,
                        )
                    )
                    return _GatewayResponse(
                        request_id=request_id,
                        completion=completion,
                        attempt_history=tuple(history),
                    )
                failure = _response_error(response, self.config.api_key, attempt, latency_ms)
                history.append(
                    ProviderAttempt(
                        sequence=sequence,
                        outcome=ProviderAttemptOutcome.HTTP_ERROR,
                        request_id=failure.request_id,
                        latency_ms=latency_ms,
                        error_kind=type(failure).__name__,
                        error_message=bounded_error(failure, 300),
                    )
                )
                if (
                    isinstance(payload, _ChatRequest)
                    and payload.tool_choice
                    and _rejects_tool_choice(response)
                ):
                    self._features = self._features.without_tool_choice()
                    if attempt < self.config.max_attempts:
                        payload = payload.without_tool_choice()
                        continue
                if not _retryable_response(response) or attempt == self.config.max_attempts:
                    raise _audited_error(failure, tuple(history))
            await asyncio.sleep(_retry_delay_seconds(attempt))
        raise AssertionError("bounded retry loop did not terminate")


class NewApiModelGateway(_NewApiDecisionGateway):
    """Generate typed decisions through NewAPI Chat Completions."""

    wire_protocol = NewApiWireProtocol.CHAT_COMPLETIONS
    adapter_version = _ChatCompletionsAdapter.adapter_version
    _adapter = _WIRE_ADAPTERS[wire_protocol]


class NewApiResponsesGateway(_NewApiDecisionGateway):
    """Generate typed decisions through NewAPI Responses SSE."""

    wire_protocol = NewApiWireProtocol.RESPONSES
    adapter_version = _ResponsesAdapter.adapter_version
    _adapter = _WIRE_ADAPTERS[wire_protocol]


class NewApiAnthropicMessagesGateway(_NewApiDecisionGateway):
    """Generate typed decisions through NewAPI Anthropic Messages."""

    wire_protocol = NewApiWireProtocol.ANTHROPIC_MESSAGES
    adapter_version = _AnthropicMessagesAdapter.adapter_version
    _adapter = _WIRE_ADAPTERS[wire_protocol]


type NewApiGatewayType = type[_NewApiDecisionGateway]


@dataclass(frozen=True, slots=True)
class NewApiGatewayFactory:
    """Bind one typed wire adapter to an optional shared transport."""

    gateway_type: NewApiGatewayType
    transport: NewApiTransport | None = None

    @property
    def wire_protocol(self) -> NewApiWireProtocol:
        """Return the protocol implemented by the constructed gateway."""
        return self.gateway_type.wire_protocol

    @property
    def adapter_version(self) -> str:
        """Return the adapter version implemented by the gateway."""
        return self.gateway_type.adapter_version

    def __call__(self, config: NewApiModelConfig) -> DecisionGateway:
        """Create one company-owned gateway with the bound transport."""
        return self.gateway_type(config, transport=self.transport)


def _chat_request(
    config: NewApiModelConfig,
    request: _DecisionWireInput,
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


def _responses_request(
    config: NewApiModelConfig,
    request: _DecisionWireInput,
) -> _ResponsesRequest:
    """Build one native Responses request with list-form user input."""
    return _ResponsesRequest(
        model=config.model,
        instructions=request.instructions,
        input=(_ResponsesInputMessage(content=request.input_text),),
        tools=tuple(_responses_tool(name) for name in request.allowed_tools),
        max_output_tokens=config.max_output_tokens,
    )


def _anthropic_request(
    config: NewApiModelConfig,
    request: _DecisionWireInput,
) -> _AnthropicRequest:
    """Build one Anthropic Messages request through NewAPI."""
    return _AnthropicRequest(
        model=config.model,
        system=request.instructions,
        messages=(_AnthropicMessage(content=request.input_text),),
        tools=tuple(_anthropic_tool(name) for name in request.allowed_tools),
        max_tokens=config.max_output_tokens,
    )


def _decision_tool(name: DecisionToolName) -> _Tool:
    """Build one Chat Completions function from the canonical decision input."""
    model, schema = _decision_schema(name)
    return _Tool(
        function=_FunctionTool(
            name=name,
            description=model.__doc__ or f"Execute {name}.",
            parameters=schema,
        )
    )


def _responses_tool(name: DecisionToolName) -> _ResponsesTool:
    """Build one native Responses function from the canonical decision input."""
    model, schema = _decision_schema(name)
    _require_all_object_properties(schema)
    return _ResponsesTool(
        name=name,
        description=model.__doc__ or f"Execute {name}.",
        parameters=schema,
    )


def _anthropic_tool(name: DecisionToolName) -> _AnthropicTool:
    """Build one Anthropic tool from the canonical decision input."""
    model, schema = _decision_schema(name)
    return _AnthropicTool(
        name=name,
        description=model.__doc__ or f"Execute {name}.",
        input_schema=schema,
    )


def _decision_schema(name: DecisionToolName) -> tuple[type[BaseModel], JsonObject]:
    """Return one closed numeric schema shared by every wire protocol."""
    model = decision_tool_model(name)
    schema = model.model_json_schema()
    properties = schema.get("properties")
    if isinstance(properties, dict):
        properties.pop("kind", None)
        schema["required"] = list(properties)
    schema["additionalProperties"] = False
    require_numeric_economic_schema(schema)
    return model, _JSON_OBJECT_ADAPTER.validate_python(schema)


def _require_all_object_properties(schema: JsonValue) -> None:
    """Make every nested object compatible with strict Responses tools."""
    if isinstance(schema, list):
        for item in schema:
            _require_all_object_properties(item)
        return
    if not isinstance(schema, dict):
        return

    properties = schema.get("properties")
    if isinstance(properties, dict):
        schema["required"] = list(properties)
    if schema.get("type") == "object":
        schema["additionalProperties"] = False
    for value in tuple(schema.values()):
        _require_all_object_properties(value)


def _parse_chat_completion(
    response: httpx.Response,
    attempts: int,
    latency_ms: int,
) -> _ChatCompletion:
    """Validate the external response while retaining additive compatibility."""
    return _validate_external_response(
        response.json,
        _ChatCompletion,
        response,
        attempts,
        latency_ms,
    )


def _parse_anthropic_completion(
    response: httpx.Response,
    attempts: int,
    latency_ms: int,
) -> _AnthropicCompletion:
    """Validate one Anthropic Messages response."""
    return _validate_external_response(
        response.json,
        _AnthropicCompletion,
        response,
        attempts,
        latency_ms,
    )


def _parse_responses_completion(
    response: httpx.Response,
    attempts: int,
    latency_ms: int,
) -> _ResponsesCompletion:
    """Collect a native Responses JSON or SSE completion."""
    content_type = response.headers.get("content-type", "").casefold()
    if "text/event-stream" not in content_type and not response.text.lstrip().startswith(
        ("event:", "data:")
    ):
        return _validate_external_response(
            response.json,
            _ResponsesCompletion,
            response,
            attempts,
            latency_ms,
        )

    completed: _ResponsesCompletion | None = None
    output_items: list[_ResponsesOutputItem] = []
    for event_name, data in _sse_events(response.text):
        if data == "[DONE]":
            continue
        try:
            event = _JSON_OBJECT_ADAPTER.validate_python(json.loads(data))
        except (json.JSONDecodeError, ValidationError) as error:
            raise _invalid_response(response, attempts, latency_ms, error) from error
        event_type = event.get("type") or event_name
        if event_type == "response.output_item.done":
            item = event.get("item")
            try:
                output_items.append(_ResponsesOutputItem.model_validate(item))
            except ValidationError as error:
                raise _invalid_response(response, attempts, latency_ms, error) from error
        elif event_type == "response.completed":
            payload = event.get("response")
            try:
                completed = _ResponsesCompletion.model_validate(payload)
            except ValidationError as error:
                raise _invalid_response(response, attempts, latency_ms, error) from error
        elif event_type in {"response.failed", "response.error", "error"}:
            raise ModelInfrastructureError(
                f"NewAPI Responses stream failed: {_event_error_kind(event)}",
                request_id=_request_id(response),
                attempts=attempts,
                latency_ms=latency_ms,
            )

    if completed is None:
        raise _invalid_response(
            response,
            attempts,
            latency_ms,
            ValueError("Responses stream ended without response.completed"),
        )
    if not completed.output and output_items:
        completed = completed.model_copy(update={"output": tuple(output_items)})
    return completed


def _validate_external_response[CompletionT: BaseModel](
    load: Callable[[], object],
    model: type[CompletionT],
    response: httpx.Response,
    attempts: int,
    latency_ms: int,
) -> CompletionT:
    """Validate one external JSON response with common audit metadata."""
    try:
        return model.model_validate(load())
    except (ValueError, ValidationError) as error:
        raise _invalid_response(response, attempts, latency_ms, error) from error


def _invalid_response(
    response: httpx.Response,
    attempts: int,
    latency_ms: int,
    error: Exception,
) -> ModelOutputError:
    """Attach common request metadata to one invalid provider response."""
    detail = (
        _validation_issue_summary(error)
        if isinstance(error, ValidationError)
        else f"{type(error).__name__}: {error}"
    )
    return ModelOutputError(
        f"invalid NewAPI response: {detail}",
        request_id=_request_id(response),
        attempts=attempts,
        latency_ms=latency_ms,
    )


def _sse_events(content: str) -> tuple[tuple[str | None, str], ...]:
    """Parse bounded SSE frames from one buffered HTTP response."""
    events: list[tuple[str | None, str]] = []
    event_name: str | None = None
    data_lines: list[str] = []

    def flush() -> None:
        nonlocal event_name, data_lines
        if data_lines:
            events.append((event_name, "\n".join(data_lines)))
        event_name = None
        data_lines = []

    for line in (*content.splitlines(), ""):
        if not line:
            flush()
        elif line.startswith("event:"):
            event_name = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").lstrip())
    return tuple(events)


def _event_error_kind(event: Mapping[str, JsonValue]) -> str:
    """Return a safe Responses failure category without provider text."""
    detail = event.get("error")
    if not isinstance(detail, Mapping):
        response = event.get("response")
        detail = response.get("error") if isinstance(response, Mapping) else None
    error_type = detail.get("type") if isinstance(detail, Mapping) else None
    return error_type if error_type in _SAFE_STREAM_ERROR_TYPES else "unknown_error"


def _validation_issue_summary(error: ValidationError) -> str:
    """Describe external schema failures without echoing model-supplied values."""
    issues = tuple(
        ".".join(str(part) for part in issue["loc"]) + f" ({issue['type']})"
        for issue in error.errors(include_url=False, include_input=False)
    )
    detail = ", ".join(issues[:3])
    remainder = error.error_count() - min(len(issues), 3)
    return f"{error.error_count()} schema validation issue(s): {detail}" + (
        f", plus {remainder} more" if remainder else ""
    )


def _single_normalized_tool_call(
    completion: _NormalizedCompletion,
) -> tuple[str, JsonObject]:
    """Return exactly one tool call or one typed protocol violation."""
    if completion.invalid_response is not None:
        raise _ProtocolViolation(
            ProtocolIssueKind.INVALID_RESPONSE,
            completion.invalid_response,
        )
    calls = completion.calls
    if not calls:
        message = (
            f"selected NewAPI model exhausted its {completion.output_limit}-token "
            "response budget "
            "before returning a function call"
            if completion.truncated
            else "selected NewAPI model did not return a required function call"
        )
        raise _ProtocolViolation(
            ProtocolIssueKind.MISSING_TOOL_CALL,
            message,
            repairable=not completion.truncated,
        )
    if len(calls) != 1:
        raise _ProtocolViolation(
            ProtocolIssueKind.MULTIPLE_TOOL_CALLS,
            f"expected one function call, received {len(calls)}",
        )
    call = calls[0]
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
            _validation_issue_summary(error),
        ) from error


def _decode_normalized_decision(
    completion: _NormalizedCompletion,
    allowed_tools: tuple[DecisionToolName, ...],
) -> CompanyDecision:
    """Decode one provider tool call into the canonical company decision."""
    name, arguments = _single_normalized_tool_call(completion)
    if name not in allowed_tools:
        raise _ProtocolViolation(
            ProtocolIssueKind.UNAUTHORIZED_DECISION_TOOL,
            "model selected an unknown or unauthorized decision tool",
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
            _validation_issue_summary(error),
        ) from error


def _chat_token_usage(usage: _Usage) -> TokenUsage:
    """Normalize Chat Completions token counters into benchmark audit fields."""
    total = usage.total_tokens or usage.prompt_tokens + usage.completion_tokens
    return TokenUsage(
        input_tokens=usage.prompt_tokens,
        cached_tokens=usage.prompt_tokens_details.cached_tokens,
        output_tokens=usage.completion_tokens,
        reasoning_tokens=usage.completion_tokens_details.reasoning_tokens,
        total_tokens=total,
    )


def _responses_token_usage(usage: _ResponsesUsage) -> TokenUsage:
    """Normalize Responses token counters into benchmark audit fields."""
    total = usage.total_tokens or usage.input_tokens + usage.output_tokens
    return TokenUsage(
        input_tokens=usage.input_tokens,
        cached_tokens=usage.input_tokens_details.cached_tokens,
        output_tokens=usage.output_tokens,
        reasoning_tokens=usage.output_tokens_details.reasoning_tokens,
        total_tokens=total,
    )


def _anthropic_token_usage(usage: _AnthropicUsage) -> TokenUsage:
    """Normalize Anthropic cache-aware token counters into benchmark fields."""
    total_input = (
        usage.input_tokens + usage.cache_creation_input_tokens + usage.cache_read_input_tokens
    )
    return TokenUsage(
        input_tokens=total_input,
        cached_tokens=usage.cache_read_input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=total_input + usage.output_tokens,
    )


def _chat_truncated(
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


def _repair_instruction(violation: str) -> str:
    """Build one bounded provider-neutral protocol correction."""
    return (
        "Your previous response violated the decision protocol: "
        f"{violation[:240]}. Return exactly one function call from the supplied tools, "
        "with one valid JSON argument object and no additional tool calls."
    )


def _capability_decision_request() -> _CapabilityDecisionRequest:
    """Return the single canonical idle probe shared by every wire protocol."""
    return _CapabilityDecisionRequest()


def _require_input_budget(payload: _WireRequest, max_input_tokens: int) -> None:
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
        "request_id": _safe_provider_identifier(_request_id(response), api_key),
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
    explicit_diagnostic = message is not None and QUOTA_EXHAUSTION_MATCHER.matches(message)
    return response.status_code in {402, 403, 429} and (
        explicit_diagnostic
        or _error_type(response) in {"insufficient_balance", "insufficient_quota"}
    )


def _safe_error_message(response: httpx.Response, api_key: SecretStr) -> str:
    """Extract one bounded provider diagnostic and redact the credential."""
    provider_message = _provider_error_message(response) or response.reason_phrase
    safe = bounded_error(
        RuntimeError(provider_message),
        240,
        secrets=(api_key.get_secret_value(),),
    )
    return f"NewAPI HTTP {response.status_code}: {safe}"


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


def _safe_provider_identifier(value: str | None, api_key: SecretStr) -> str | None:
    """Keep only bounded provider identifiers that cannot echo the credential."""
    if value is None or api_key.get_secret_value() in value or "sk-" in value.casefold():
        return None
    return value if _SAFE_PROVIDER_IDENTIFIER.fullmatch(value) else None


def _sanitize_model_error(
    error: ModelCallError,
    request_id: str | None,
    api_key: SecretStr,
) -> None:
    """Replace untrusted provider identifiers before an error crosses the gateway."""
    error.request_id = request_id
    error.response_id = _safe_provider_identifier(error.response_id, api_key)


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
