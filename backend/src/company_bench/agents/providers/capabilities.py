"""Persist and calibrate model output limits before benchmark execution."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, ValidationError, model_validator

from company_bench.domain.models import StrictModel

_SCHEMA_VERSION = 1


class ModelCapabilityError(ValueError):
    """The effective NewAPI model limit could not be established safely."""


class CapabilitySource(StrEnum):
    """Provenance for one model capability record."""

    GATEWAY = "gateway_validation"
    OFFICIAL = "official_catalog"


class ModelCapability(StrictModel):
    """One model's effective maximum output-token allowance."""

    model_id: str = Field(min_length=1)
    max_output_tokens: int = Field(gt=0)
    source: CapabilitySource
    verified_at: datetime | None = None

    @property
    def verified(self) -> bool:
        """Return whether this value was confirmed for the active gateway."""
        return self.verified_at is not None


class CapabilityDocument(StrictModel):
    """Versioned on-disk representation of confirmed model capabilities."""

    schema_version: Literal[1] = _SCHEMA_VERSION
    models: tuple[ModelCapability, ...] = ()

    @model_validator(mode="after")
    def require_unique_models(self) -> CapabilityDocument:
        """Reject ambiguous duplicate records."""
        model_ids = tuple(model.model_id for model in self.models)
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("model capability catalog contains duplicate model IDs")
        return self


class CapabilityProbe(Protocol):
    """Calibrate one candidate limit against the active NewAPI route."""

    async def max_output_tokens(self, model_id: str, candidate: int | None) -> int:
        """Return the confirmed effective maximum output-token limit."""
        ...


class ModelCapabilityCatalog:
    """Resolve, single-flight calibrate, and persist model output limits."""

    def __init__(
        self,
        path: Path | None,
        probe: CapabilityProbe | None,
        *,
        seeds: Iterable[ModelCapability] = (),
    ) -> None:
        self._path = path
        self._probe = probe
        self._models = {model.model_id: model for model in seeds}
        self._models.update(_load_document(path))
        self._model_locks: dict[str, asyncio.Lock] = {}
        self._write_lock = asyncio.Lock()

    @classmethod
    def production(cls, path: Path, probe: CapabilityProbe) -> ModelCapabilityCatalog:
        """Open the production catalog with auditable built-in candidates."""
        return cls(path, probe, seeds=_BUILTIN_CAPABILITIES)

    async def ensure(self, model_id: str) -> ModelCapability:
        """Calibrate one model at most once, even across concurrent submissions."""
        capability = self._models.get(model_id)
        if capability is not None and capability.verified:
            return capability
        if self._probe is None:
            raise ModelCapabilityError(
                f"NewAPI model '{model_id}' has no capability probe configured"
            )

        lock = self._model_locks.setdefault(model_id, asyncio.Lock())
        async with lock:
            capability = self._models.get(model_id)
            if capability is not None and capability.verified:
                return capability
            candidate = capability.max_output_tokens if capability is not None else None
            confirmed = await self._probe.max_output_tokens(model_id, candidate)
            if confirmed <= 0:
                raise ModelCapabilityError(
                    f"NewAPI returned an invalid output-token limit for '{model_id}': {confirmed}"
                )
            verified = ModelCapability(
                model_id=model_id,
                max_output_tokens=confirmed,
                source=CapabilitySource.GATEWAY,
                verified_at=datetime.now(UTC),
            )
            self._models[model_id] = verified
            await self._persist()
            return verified

    async def _persist(self) -> None:
        """Atomically persist a stable snapshot of confirmed records."""
        if self._path is None:
            return
        async with self._write_lock:
            models = tuple(
                sorted(
                    (model for model in self._models.values() if model.verified),
                    key=lambda model: model.model_id.casefold(),
                )
            )
            document = CapabilityDocument(models=models)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
            temporary.write_text(f"{document.model_dump_json(indent=2)}\n", encoding="utf-8")
            temporary.replace(self._path)


def _load_document(path: Path | None) -> dict[str, ModelCapability]:
    """Load confirmed local overrides while rejecting corrupt state."""
    if path is None or not path.exists():
        return {}
    try:
        document = CapabilityDocument.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as error:
        raise ModelCapabilityError(f"invalid model capability catalog '{path}': {error}") from error
    unverified = tuple(model.model_id for model in document.models if not model.verified)
    if unverified:
        raise ModelCapabilityError(
            "persisted model capabilities must include verified_at: " + ", ".join(unverified)
        )
    return {model.model_id: model for model in document.models}


def _official(model_id: str, max_output_tokens: int) -> ModelCapability:
    """Declare one documented candidate that still requires gateway validation."""
    return ModelCapability(
        model_id=model_id,
        max_output_tokens=max_output_tokens,
        source=CapabilitySource.OFFICIAL,
    )


_BUILTIN_CAPABILITIES = (
    _official("claude-opus-5", 128_000),
    _official("claude-sonnet-5", 128_000),
    _official("deepseek-v4-pro", 384_000),
    _official("gemini-2.5-pro", 65_536),
    _official("gpt-4o-mini", 16_384),
    _official("gpt-5.6-luna", 128_000),
    _official("gpt-5.6-sol", 128_000),
    _official("gpt-5.6-terra", 128_000),
)
