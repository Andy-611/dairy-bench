"""Contract tests for persisted NewAPI model capability calibration."""

import asyncio
from pathlib import Path

from company_bench.agents.providers.capabilities import (
    CapabilityDocument,
    CapabilitySource,
    ModelCapabilityCatalog,
)


class _RecordingProbe:
    """Return one deterministic limit while retaining calibration calls."""

    def __init__(self, confirmed: int) -> None:
        self.confirmed = confirmed
        self.calls: list[tuple[str, int | None]] = []

    async def max_output_tokens(self, model_id: str, candidate: int | None) -> int:
        self.calls.append((model_id, candidate))
        await asyncio.sleep(0)
        return self.confirmed


def test_catalog_calibrates_builtin_once_and_reloads_confirmed_value(tmp_path: Path) -> None:
    path = tmp_path / "newapi-model-capabilities.json"
    probe = _RecordingProbe(65_536)
    catalog = ModelCapabilityCatalog.production(path, probe)

    capability = asyncio.run(catalog.ensure("gemini-2.5-pro"))

    assert probe.calls == [("gemini-2.5-pro", 65_536)]
    assert capability.max_output_tokens == 65_536
    assert capability.source is CapabilitySource.GATEWAY
    assert capability.verified_at is not None
    persisted = CapabilityDocument.model_validate_json(path.read_text(encoding="utf-8"))
    assert persisted.models == (capability,)

    unused_probe = _RecordingProbe(1)
    reloaded = ModelCapabilityCatalog.production(path, unused_probe)
    assert asyncio.run(reloaded.ensure("gemini-2.5-pro")) == capability
    assert unused_probe.calls == []


def test_catalog_single_flights_concurrent_calibration(tmp_path: Path) -> None:
    probe = _RecordingProbe(32_768)
    catalog = ModelCapabilityCatalog.production(tmp_path / "capabilities.json", probe)

    async def calibrate() -> tuple[int, ...]:
        capabilities = await asyncio.gather(
            *(catalog.ensure("unlisted-model") for _ in range(8))
        )
        return tuple(capability.max_output_tokens for capability in capabilities)

    assert asyncio.run(calibrate()) == (32_768,) * 8
    assert probe.calls == [("unlisted-model", None)]
