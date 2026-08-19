"""Shared HTTP transport and provider failure classification for NewAPI."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Final

import httpx
from pydantic import BaseModel, SecretStr

from company_bench.agents.contracts import (
    ModelCallError,
    ModelCompatibilityError,
    ModelConfigurationError,
    ModelInfrastructureError,
    ModelOutputError,
    ModelQuotaExhaustedError,
)
from company_bench.agents.providers.newapi.config import NewApiConfig, NewApiWireProtocol
from company_bench.diagnostics import bounded_error
from company_bench.settings import MAX_PARALLELISM, require_parallelism

type AuditedModelError = (
    ModelOutputError | ModelConfigurationError | ModelInfrastructureError | ModelQuotaExhaustedError
)

_PROXY_UPSTREAM_ERROR_TYPE = "bad_response_status_code"
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


@dataclass(frozen=True, slots=True)
class _QuotaExhaustionMatcher:
    """Match only diagnostics that explicitly describe an exhausted balance."""

    markers: tuple[str, ...]

    def matches(self, message: str) -> bool:
        """Return whether a diagnostic explicitly reports exhausted paid quota."""
        normalized = " ".join(message.casefold().split())
        return any(marker in normalized for marker in self.markers)


_QUOTA_EXHAUSTION_MATCHER: Final = _QuotaExhaustionMatcher(
    markers=(
        "预扣费额度失败",
        "用户剩余额度",
        "余额不足",
        "额度不足",
        "insufficient balance",
        "insufficient credit",
        "insufficient quota",
        "credit balance is too low",
        "not enough balance",
        "billing hard limit has been reached",
    )
)


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
    if response.status_code not in {400, 413, 422, 500}:
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
    explicit_diagnostic = message is not None and _QUOTA_EXHAUSTION_MATCHER.matches(message)
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
