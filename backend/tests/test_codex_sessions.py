"""Safety tests for Dairy Bench Codex Session lifecycle management."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from openai_codex.types import SortDirection, ThreadSortKey

from company_bench.agents.providers.codex.sessions import (
    CodexSessionManager,
    CodexSessionRetention,
)

_NOW = 2_000_000_000
_DAY = 86_400


def _session(
    thread_id: str,
    run_id: str,
    updated_at: int,
    *,
    owned: bool = True,
) -> SimpleNamespace:
    """Build one archived Session listing."""
    name = (
        f"Dairy Bench | run={run_id} | company=farm_a"
        if owned
        else f"Personal task | {run_id}"
    )
    return SimpleNamespace(id=thread_id, name=name, updated_at=updated_at)


class _RetentionClient:
    """Record archive, list, and hard-delete calls."""

    def __init__(self) -> None:
        self.archived: list[str] = []
        self.deleted: list[str] = []
        self.list_calls: list[dict[str, object]] = []
        self.deleted_expired = asyncio.Event()
        self._pages = {
            None: SimpleNamespace(
                data=[
                    _session("a-new", "run_a", _NOW - 10),
                    _session("b-new", "run_b", _NOW - 20),
                    _session("c-new", "run_c", _NOW - 30),
                    _session("personal-old", "notes", _NOW - 100 * _DAY, owned=False),
                ],
                next_cursor="page-2",
            ),
            "page-2": SimpleNamespace(
                data=[
                    _session("a-old", "run_a", _NOW - 100 * _DAY),
                    _session("expired", "run_old", _NOW - 61 * _DAY),
                    _session("recent", "run_recent", _NOW - 59 * _DAY),
                ],
                next_cursor=None,
            ),
        }

    async def thread_archive(self, thread_id: str) -> SimpleNamespace:
        """Record an archive."""
        self.archived.append(thread_id)
        return SimpleNamespace()

    async def thread_delete(self, thread_id: str) -> None:
        """Record a hard deletion."""
        self.deleted.append(thread_id)
        if thread_id == "expired":
            self.deleted_expired.set()

    async def thread_list(self, **options: object) -> SimpleNamespace:
        """Return one deterministic archived page."""
        self.list_calls.append(options)
        return self._pages[options["cursor"]]


class _BlockingRetentionClient(_RetentionClient):
    """Hold maintenance open so its scheduling and ownership are observable."""

    def __init__(self) -> None:
        super().__init__()
        self.maintenance_started = asyncio.Event()
        self.release_maintenance = asyncio.Event()

    async def thread_list(self, **options: object) -> SimpleNamespace:
        """Block the first retention page until the test releases it."""
        self.list_calls.append(options)
        self.maintenance_started.set()
        await self.release_maintenance.wait()
        return self._pages[options["cursor"]]


class _FailingRetentionClient(_RetentionClient):
    """Fail only background retention after a successful archive."""

    def __init__(self) -> None:
        super().__init__()
        self.maintenance_attempted = asyncio.Event()

    async def thread_list(self, **options: object) -> SimpleNamespace:
        """Expose the attempt before simulating an app-server failure."""
        self.list_calls.append(options)
        self.maintenance_attempted.set()
        raise RuntimeError("retention unavailable")


def test_session_manager_archives_then_prunes_only_expired_owned_sessions() -> None:
    async def exercise() -> None:
        client = _RetentionClient()
        manager = CodexSessionManager(
            CodexSessionRetention(minimum_runs=3),
            wall_clock=lambda: _NOW,
            interval_clock=lambda: 100.0,
        )

        await manager.archive(client, "current-thread")
        await asyncio.wait_for(client.deleted_expired.wait(), timeout=1)
        await manager.archive(client, "another-thread")
        await manager.close()

        assert client.archived == ["current-thread", "another-thread"]
        assert client.deleted == ["expired"]
        assert len(client.list_calls) == 2
        first_call = client.list_calls[0]
        assert first_call == {
            "archived": True,
            "cursor": None,
            "limit": 100,
            "sort_direction": SortDirection.desc,
            "sort_key": ThreadSortKey.updated_at,
        }

    asyncio.run(exercise())


def test_session_maintenance_never_blocks_archive_and_has_one_task() -> None:
    async def exercise() -> None:
        client = _BlockingRetentionClient()
        manager = CodexSessionManager(
            CodexSessionRetention(),
            interval_clock=lambda: 100.0,
        )

        await manager.archive(client, "first-thread")
        await asyncio.wait_for(client.maintenance_started.wait(), timeout=1)
        await manager.archive(client, "second-thread")

        assert client.archived == ["first-thread", "second-thread"]
        assert len(client.list_calls) == 1

        client.release_maintenance.set()
        await manager.close()

    asyncio.run(exercise())


def test_session_manager_releases_only_the_borrowed_client() -> None:
    async def exercise() -> None:
        client = _BlockingRetentionClient()
        other_client = _RetentionClient()
        manager = CodexSessionManager(CodexSessionRetention())

        await manager.archive(client, "borrowed-thread")
        await asyncio.wait_for(client.maintenance_started.wait(), timeout=1)
        await manager.release(other_client)
        release = asyncio.create_task(manager.release(client))
        await asyncio.sleep(0)
        assert not release.done()

        client.release_maintenance.set()
        await release
        await manager.close()

    asyncio.run(exercise())


def test_session_maintenance_failure_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def exercise() -> None:
        client = _FailingRetentionClient()
        manager = CodexSessionManager(CodexSessionRetention())

        await manager.archive(client, "safe-thread")
        await asyncio.wait_for(client.maintenance_attempted.wait(), timeout=1)
        await asyncio.sleep(0)
        await manager.close()

        assert client.archived == ["safe-thread"]

    asyncio.run(exercise())

    assert "Codex Session retention failed" in caplog.text
