"""Manage isolated Codex Sessions without touching personal task history."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from time import monotonic, time
from typing import Protocol

from openai_codex import AsyncCodex
from openai_codex.generated.v2_all import Thread, ThreadDeleteResponse
from openai_codex.types import SortDirection, ThreadListResponse, ThreadSortKey
from pydantic import Field

from company_bench.models import CompanyId, Identifier, StrictModel

_LOGGER = logging.getLogger(__name__)
_THREAD_PREFIX = "Dairy Bench | "
_THREAD_NAME = re.compile(r"^Dairy Bench \| run=(?P<run>[^|]+) \| company=")
_SECONDS_PER_DAY = 86_400
_MAINTENANCE_INTERVAL_SECONDS = _SECONDS_PER_DAY


class CodexSessionRetention(StrictModel):
    """Bound raw Session growth while preserving recent benchmark runs."""

    max_age_days: int = Field(default=60, ge=1, le=3650)
    minimum_runs: int = Field(default=3, ge=1, le=100)


class CodexSessionClient(Protocol):
    """Narrow client surface required for archival and retention."""

    async def thread_archive(self, thread_id: str) -> object:
        """Move one Session out of the active task list."""
        ...

    async def thread_delete(self, thread_id: str) -> None:
        """Permanently remove one expired archived Session."""
        ...

    async def thread_list(
        self,
        *,
        archived: bool | None = None,
        cursor: str | None = None,
        limit: int | None = None,
        sort_direction: SortDirection | None = None,
        sort_key: ThreadSortKey | None = None,
    ) -> ThreadListResponse:
        """List stored Sessions for retention."""
        ...


class DairyBenchCodex(AsyncCodex):
    """Expose the app-server delete method omitted by the SDK facade."""

    async def thread_delete(self, thread_id: str) -> None:
        """Delete one archived Session through the typed app-server RPC."""
        await self._ensure_initialized()
        await self._client.request(
            "thread/delete",
            {"threadId": thread_id},
            response_model=ThreadDeleteResponse,
        )


class CodexSessionManager:
    """Archive exported Sessions immediately and prune only owned history."""

    def __init__(
        self,
        retention: CodexSessionRetention,
        *,
        wall_clock: Callable[[], float] = time,
        interval_clock: Callable[[], float] = monotonic,
    ) -> None:
        self._retention = retention
        self._wall_clock = wall_clock
        self._interval_clock = interval_clock
        self._next_maintenance_at = 0.0
        self._maintenance_task: asyncio.Task[None] | None = None
        self._maintenance_client: CodexSessionClient | None = None

    @staticmethod
    def thread_name(run_id: Identifier, company_id: CompanyId) -> str:
        """Build a compact, retention-safe task title."""
        return f"{_THREAD_PREFIX}run={run_id} | company={company_id}"

    async def archive(
        self,
        client: CodexSessionClient,
        thread_id: str,
    ) -> None:
        """Archive one Session and schedule best-effort maintenance."""
        await client.thread_archive(thread_id)
        self._schedule_maintenance(client)

    async def close(self) -> None:
        """Wait for and reclaim the optional background maintenance task."""
        await self._drain_maintenance()

    async def release(self, client: CodexSessionClient) -> None:
        """Finish maintenance before its borrowed client is closed."""
        if self._maintenance_client is client:
            await self._drain_maintenance()

    async def _drain_maintenance(self) -> None:
        """Reclaim the current maintenance task without cancelling SDK work."""
        task = self._maintenance_task
        if task is None:
            return
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        finally:
            if self._maintenance_task is task:
                self._maintenance_task = None
                self._maintenance_client = None

    def _schedule_maintenance(self, client: CodexSessionClient) -> None:
        """Start at most one due maintenance task without blocking a Turn."""
        now = self._interval_clock()
        task = self._maintenance_task
        if now < self._next_maintenance_at or (task is not None and not task.done()):
            return
        self._next_maintenance_at = now + _MAINTENANCE_INTERVAL_SECONDS
        self._maintenance_client = client
        self._maintenance_task = asyncio.create_task(
            self._maintain(client),
            name="dairy-bench-codex-session-maintenance",
        )

    async def _maintain(self, client: CodexSessionClient) -> None:
        """Prune expired owned Sessions without affecting Agent outcomes."""
        try:
            await self._delete_expired(client)
        except Exception:
            _LOGGER.warning("Codex Session retention failed", exc_info=True)

    async def _delete_expired(self, client: CodexSessionClient) -> None:
        """Delete only old Dairy Bench Sessions outside protected runs."""
        sessions = await _list_archived(client)
        owned = tuple(
            (session, run_id)
            for session in sessions
            if (run_id := _run_id(session.name)) is not None
        )
        protected = _newest_runs(owned, self._retention.minimum_runs)
        cutoff = int(self._wall_clock()) - self._retention.max_age_days * _SECONDS_PER_DAY
        for session, run_id in owned:
            if session.updated_at < cutoff and run_id not in protected:
                try:
                    await client.thread_delete(session.id)
                except Exception:
                    _LOGGER.warning(
                        "Could not delete expired Dairy Bench Session %s",
                        session.id,
                        exc_info=True,
                    )


async def _list_archived(client: CodexSessionClient) -> tuple[Thread, ...]:
    """Read every archived Session page in newest-first order."""
    sessions: list[Thread] = []
    cursor: str | None = None
    while True:
        response = await client.thread_list(
            archived=True,
            cursor=cursor,
            limit=100,
            sort_direction=SortDirection.desc,
            sort_key=ThreadSortKey.updated_at,
        )
        sessions.extend(response.data)
        cursor = response.next_cursor
        if cursor is None:
            return tuple(sessions)


def _run_id(name: str | None) -> str | None:
    """Return an owned run id only from an exact Dairy Bench title."""
    if name is None or (matched := _THREAD_NAME.match(name)) is None:
        return None
    return matched.group("run").strip() or None


def _newest_runs(
    sessions: tuple[tuple[Thread, str], ...],
    minimum_runs: int,
) -> frozenset[str]:
    """Protect the newest distinct benchmark runs."""
    protected: list[str] = []
    for _, run_id in sorted(sessions, key=lambda item: item[0].updated_at, reverse=True):
        if run_id not in protected:
            protected.append(run_id)
        if len(protected) == minimum_runs:
            break
    return frozenset(protected)
