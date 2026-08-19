"""Typed wire adapters for NewAPI Chat, Responses, and Anthropic protocols."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import ClassVar, Literal, Protocol, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from company_bench.agents.contracts import (
    DecisionToolName,
    ModelInfrastructureError,
    ModelOutputError,
    decision_tool_model,
    decode_decision_tool_input,
)
from company_bench.agents.providers.newapi.config import NewApiModelConfig, NewApiWireProtocol
from company_bench.agents.providers.newapi.transport import _request_id
from company_bench.domain.models import ProtocolIssueKind, StrictModel
from company_bench.domain.precision import ECONOMIC_QUANTUM
from company_bench.runs.models import ProviderAttempt, ProviderAttemptOutcome, TokenUsage
from company_bench.runtime.models import CompanyDecision

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

_JSON_OBJECT_ADAPTER = TypeAdapter(JsonObject)
_TOKEN_ESTIMATE_BYTES = 2
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
    _require_numeric_economic_schema(schema)
    return model, _JSON_OBJECT_ADAPTER.validate_python(schema)


def _require_numeric_economic_schema(node: object) -> None:
    """Make provider schemas enforce the quantum on JSON numbers, not strings."""
    if isinstance(node, dict):
        if node.get("multipleOf") == float(ECONOMIC_QUANTUM):
            alternatives = node.get("anyOf") or node.get("oneOf")
            numeric = None
            if isinstance(alternatives, list):
                numeric = next(
                    (
                        alternative
                        for alternative in alternatives
                        if isinstance(alternative, dict) and alternative.get("type") == "number"
                    ),
                    None,
                )
            if numeric is not None:
                node.pop("anyOf", None)
                node.pop("oneOf", None)
                node.update(numeric)
        for child in node.values():
            _require_numeric_economic_schema(child)
    elif isinstance(node, list):
        for child in node:
            _require_numeric_economic_schema(child)


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
        return decode_decision_tool_input(name, arguments)
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
