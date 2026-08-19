"""Validated configuration for NewAPI-backed decision gateways."""

from __future__ import annotations

import hashlib
import os
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator

from company_bench.domain.models import StrictModel

DEFAULT_NEWAPI_BASE_URL = "https://newapi.deepwisdom.ai/v1"
DEFAULT_NEWAPI_ENV_PREFIX = "DAIRY_BENCH_NEWAPI_MODEL"
_DEFAULT_MAX_INPUT_TOKENS = 128_000
_DEFAULT_MAX_OUTPUT_TOKENS = 128_000
_CAPABILITY_PROBE_TIMEOUT_SECONDS = 30.0
_PROTOCOL_ATTEMPTS = 2
_MAX_RETRY_DELAY_SECONDS = 2.0


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
