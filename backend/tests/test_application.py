"""Composition tests for application-owned resources."""

import asyncio
from pathlib import Path

import pytest

from company_bench.agents.providers.capabilities import (
    CapabilityDocument,
    verified_capabilities,
)
from company_bench.agents.providers.newapi import NewApiModelGateway
from company_bench.application import BenchmarkApplication
from company_bench.domain.models import PolicyKind
from company_bench.storage.store import InMemoryRunStore


def test_application_shares_one_newapi_transport_across_gateways(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    models = ("gpt-5.6-sol", "gemini-2.5-pro")
    monkeypatch.setenv("NEWAPI_API_KEY", "test-key")
    monkeypatch.setenv("DAIRY_BENCH_NEWAPI_MODELS", ",".join(models))
    monkeypatch.setenv("DAIRY_BENCH_HOME", str(tmp_path))
    capability_path = tmp_path / "credentials" / "newapi-model-capabilities.json"
    capability_path.parent.mkdir(parents=True)
    capability_path.write_text(
        CapabilityDocument(
            models=verified_capabilities(
                {"gpt-5.6-sol": 128_000, "gemini-2.5-pro": 65_536}
            )
        ).model_dump_json(),
        encoding="utf-8",
    )
    repository = InMemoryRunStore()
    application = BenchmarkApplication.create(repository)
    transport = application._owned_transport
    assert transport is not None
    factory = application._coordinator._policy_factory

    bundle = factory.create_agents(
        run_id="shared_transport",
        mode=PolicyKind.MODEL,
        model="gemini-2.5-pro",
    )
    gateways = tuple(
        gateway
        for gateway in bundle._gateways
        if isinstance(gateway, NewApiModelGateway)
    )

    assert len(gateways) == len(factory.scenario.companies)
    assert all(gateway._transport is transport for gateway in gateways)
    asyncio.run(bundle.close())
    assert not transport._client.is_closed
    asyncio.run(application.close())
    assert transport._client.is_closed
