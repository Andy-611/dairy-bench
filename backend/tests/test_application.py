"""Composition tests for application-owned resources."""

import asyncio
from pathlib import Path
from typing import cast

import httpx
import pytest

import company_bench.application as application_module
from company_bench.agents.factory import AgentFactory
from company_bench.agents.providers.capabilities import (
    CapabilityDocument,
)
from company_bench.agents.providers.newapi import (
    NewApiAnthropicMessagesGateway,
    NewApiModelGateway,
    NewApiResponsesGateway,
    NewApiTransport,
)
from company_bench.application import BenchmarkApplication
from company_bench.domain.models import PolicyProfileId
from company_bench.domain.scenario import DAIRY_S9_SCENARIO
from company_bench.runs.coordinator import RunCoordinator
from company_bench.storage.memory import InMemoryRunRepository
from tests.support.fakes import verified_capabilities


def test_application_shares_one_newapi_transport_across_gateways(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    models = ("gpt-5.6-sol", "gemini-2.5-pro")
    monkeypatch.setenv("DAIRY_BENCH_NEWAPI_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("DAIRY_BENCH_NEWAPI_MODEL_MODELS", ",".join(models))
    monkeypatch.setenv("DAIRY_BENCH_HOME", str(tmp_path))
    capability_path = tmp_path / "credentials" / "newapi-model" / "model-capabilities.json"
    capability_path.parent.mkdir(parents=True)
    capability_path.write_text(
        CapabilityDocument(
            models=verified_capabilities({"gpt-5.6-sol": 128_000, "gemini-2.5-pro": 65_536})
        ).model_dump_json(),
        encoding="utf-8",
    )
    repository = InMemoryRunRepository()
    application = BenchmarkApplication.create(repository)
    transport = application._owned_transports[0]
    factory = application._coordinator._policy_factory

    policy = asyncio.run(factory.resolve_policy(PolicyProfileId.NEWAPI_MODEL, "gemini-2.5-pro"))
    bundle = factory.create_agents(
        run_id="shared_transport",
        policy=policy,
    )
    gateways = tuple(
        gateway for gateway in bundle._gateways if isinstance(gateway, NewApiModelGateway)
    )

    assert len(gateways) == len(factory.scenario.companies)
    assert all(gateway._transport is transport for gateway in gateways)
    asyncio.run(bundle.close())
    assert transport._client is None
    client = httpx.AsyncClient()
    transport._client = client
    assert not transport._client.is_closed
    asyncio.run(application.close())
    assert transport._client.is_closed


def test_application_routes_each_profile_through_its_declared_wire_adapter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    specs = (
        (
            PolicyProfileId.NEWAPI_MODEL,
            "DAIRY_BENCH_NEWAPI_MODEL",
            "chat-test",
            NewApiModelGateway,
        ),
        (
            PolicyProfileId.NEWAPI_CODEX,
            "DAIRY_BENCH_NEWAPI_CODEX",
            "codex-test",
            NewApiResponsesGateway,
        ),
        (
            PolicyProfileId.NEWAPI_CLAUDE_CODE,
            "DAIRY_BENCH_NEWAPI_CLAUDE",
            "claude-test",
            NewApiAnthropicMessagesGateway,
        ),
    )
    monkeypatch.setenv("DAIRY_BENCH_HOME", str(tmp_path))
    for profile_id, prefix, model, _ in specs:
        monkeypatch.setenv(f"{prefix}_API_KEY", "test-key")
        monkeypatch.setenv(f"{prefix}_MODELS", model)
        capability_path = tmp_path / "credentials" / profile_id.value / "model-capabilities.json"
        capability_path.parent.mkdir(parents=True)
        capability_path.write_text(
            CapabilityDocument(models=verified_capabilities({model: 128_000})).model_dump_json(),
            encoding="utf-8",
        )

    repository = InMemoryRunRepository()
    application = BenchmarkApplication.create(repository)
    factory = application._coordinator._policy_factory
    try:
        for profile_id, _, model, gateway_type in specs:
            policy = asyncio.run(factory.resolve_policy(profile_id, model))
            bundle = factory.create_agents(
                run_id=f"route_{profile_id.value}",
                policy=policy,
            )
            try:
                assert all(isinstance(gateway, gateway_type) for gateway in bundle._gateways)
                assert all(
                    agent.metadata.wire_protocol == gateway_type.wire_protocol.value
                    and agent.metadata.adapter_version == gateway_type.adapter_version
                    for agent in bundle.agents.values()
                )
            finally:
                asyncio.run(bundle.close())
    finally:
        asyncio.run(application.close())


class _ClosingTransport:
    """Expose exhaustive application cleanup without allocating HTTP resources."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.calls = 0
        self._failure = failure

    async def close(self) -> None:
        self.calls += 1
        if self._failure is not None:
            raise self._failure


class _BlockedClosingTransport:
    """Hold cleanup until cancellation reaches the application caller."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = False
        self.cancelled = False

    async def close(self) -> None:
        self.started.set()
        try:
            await self.release.wait()
            self.finished = True
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _BlockedClosingCoordinator:
    """Expose cancellation while application-level coordinator cleanup is pending."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = False
        self.cancelled = False

    async def close(self) -> None:
        self.started.set()
        try:
            await self.release.wait()
            self.finished = True
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def test_application_closes_every_transport_when_one_close_fails() -> None:
    repository = InMemoryRunRepository()
    first = _ClosingTransport(RuntimeError("first transport close failed"))
    second = _ClosingTransport()
    application = BenchmarkApplication(
        repository,
        AgentFactory(DAIRY_S9_SCENARIO, repository),
        owned_transports=cast(tuple[NewApiTransport, ...], (first, second)),
    )

    with pytest.raises(RuntimeError, match="first transport close failed"):
        asyncio.run(application.close())

    assert first.calls == 1
    assert second.calls == 1


def test_application_finishes_transport_cleanup_before_propagating_cancellation() -> None:
    async def exercise() -> _BlockedClosingTransport:
        repository = InMemoryRunRepository()
        transport = _BlockedClosingTransport()
        application = BenchmarkApplication(
            repository,
            AgentFactory(DAIRY_S9_SCENARIO, repository),
            owned_transports=cast(tuple[NewApiTransport, ...], (transport,)),
        )
        task = asyncio.create_task(application.close())
        await transport.started.wait()
        task.cancel()
        transport.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return transport

    transport = asyncio.run(exercise())
    assert transport.finished
    assert not transport.cancelled


def test_application_finishes_coordinator_cleanup_before_propagating_cancellation() -> None:
    async def exercise() -> tuple[_BlockedClosingCoordinator, _ClosingTransport]:
        repository = InMemoryRunRepository()
        coordinator = _BlockedClosingCoordinator()
        transport = _ClosingTransport()
        application = BenchmarkApplication(
            repository,
            AgentFactory(DAIRY_S9_SCENARIO, repository),
            owned_transports=cast(tuple[NewApiTransport, ...], (transport,)),
        )
        application._coordinator = cast(RunCoordinator, coordinator)
        task = asyncio.create_task(application.close())
        await coordinator.started.wait()
        task.cancel()
        coordinator.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return coordinator, transport

    coordinator, transport = asyncio.run(exercise())
    assert coordinator.finished
    assert not coordinator.cancelled
    assert transport.calls == 1


def test_application_preflights_all_profile_configs_before_opening_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAIRY_BENCH_NEWAPI_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("DAIRY_BENCH_NEWAPI_MODEL_MODELS", "gpt-test")
    monkeypatch.setenv("DAIRY_BENCH_NEWAPI_CODEX_API_KEY", "test-key")
    monkeypatch.setenv("DAIRY_BENCH_NEWAPI_CODEX_MODELS", "codex-test")
    monkeypatch.setenv("DAIRY_BENCH_NEWAPI_CODEX_TIMEOUT_SECONDS", "not-a-number")
    opened = False

    def open_store(_: Path) -> None:
        nonlocal opened
        opened = True

    monkeypatch.setattr(application_module, "SQLiteRunRepository", open_store)

    with pytest.raises(ValueError):
        BenchmarkApplication.create()

    assert not opened


def test_failed_profile_composition_closes_created_transports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAIRY_BENCH_NEWAPI_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("DAIRY_BENCH_NEWAPI_MODEL_MODELS", "gpt-test")
    transports: list[NewApiTransport] = []

    original_init = application_module.NewApiTransport.__init__

    def track_transport(transport: NewApiTransport, *args: object, **kwargs: object) -> None:
        original_init(transport, *args, **kwargs)
        transports.append(transport)

    def fail_catalog(*_: object) -> None:
        raise RuntimeError("invalid capability catalog")

    monkeypatch.setattr(application_module.NewApiTransport, "__init__", track_transport)
    monkeypatch.setattr(
        application_module.ModelCapabilityCatalog,
        "production",
        fail_catalog,
    )

    with pytest.raises(RuntimeError, match="invalid capability catalog"):
        BenchmarkApplication.create(InMemoryRunRepository())

    assert len(transports) == 1
    assert transports[0]._client is None
