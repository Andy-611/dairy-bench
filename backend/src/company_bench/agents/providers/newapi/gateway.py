"""Audited NewAPI decision gateways and capability calibration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from time import monotonic
from typing import ClassVar

import httpx

from company_bench.agents.contracts import (
    DecisionGateway,
    DecisionModelRequest,
    DecisionModelResult,
    ModelCallError,
    ModelCompatibilityError,
    ModelInfrastructureError,
    ModelOutputError,
)
from company_bench.agents.providers.capabilities import ModelCapabilityError
from company_bench.agents.providers.newapi.config import (
    _CAPABILITY_PROBE_TIMEOUT_SECONDS,
    _DEFAULT_MAX_OUTPUT_TOKENS,
    _PROTOCOL_ATTEMPTS,
    NewApiConfig,
    NewApiModelConfig,
    NewApiWireProtocol,
    _retry_delay_seconds,
)
from company_bench.agents.providers.newapi.transport import (
    AuditedModelError,
    NewApiTransport,
    _elapsed_ms,
    _rejects_tool_choice,
    _request_id,
    _response_error,
    _response_output_limit,
    _retryable_response,
    _safe_provider_identifier,
    _sanitize_model_error,
    _transport_error,
)
from company_bench.agents.providers.newapi.wire import (
    _WIRE_ADAPTERS,
    NewApiRequestFeatures,
    _AnthropicMessagesAdapter,
    _capability_decision_request,
    _CapabilityDecisionRequest,
    _ChatCompletionsAdapter,
    _ChatRequest,
    _decode_normalized_decision,
    _mark_protocol_error,
    _NormalizedCompletion,
    _ProtocolViolation,
    _require_input_budget,
    _ResponsesAdapter,
    _WireAdapter,
    _WireRequest,
)
from company_bench.diagnostics import bounded_error
from company_bench.runs.models import ProviderAttempt, ProviderAttemptOutcome, TokenUsage


@dataclass(frozen=True, slots=True)
class _GatewayResponse:
    """Validated response plus complete audit data for one logical call."""

    request_id: str | None
    completion: _NormalizedCompletion
    attempt_history: tuple[ProviderAttempt, ...]


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
        requested = candidate or _DEFAULT_MAX_OUTPUT_TOKENS
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
            return requested

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
