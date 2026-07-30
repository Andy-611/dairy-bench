"""Safety tests for Dairy Bench Codex Session lifecycle management."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from openai_codex.types import SortDirection, ThreadSortKey

from company_bench.codex_sessions import (
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

    async def thread_list(self, **options: object) -> SimpleNamespace:
        """Return one deterministic archived page."""
        self.list_calls.append(options)
        return self._pages[options["cursor"]]


def test_session_manager_archives_then_prunes_only_expired_owned_sessions() -> None:
    client = _RetentionClient()
    manager = CodexSessionManager(
        CodexSessionRetention(minimum_runs=3),
        wall_clock=lambda: _NOW,
        interval_clock=lambda: 100.0,
    )

    asyncio.run(manager.archive(client, "current-thread"))
    asyncio.run(manager.archive(client, "another-thread"))

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
