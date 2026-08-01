"""Deterministic single-clock scheduler for the event-driven runtime."""

from __future__ import annotations

import heapq
from collections.abc import Iterable
from typing import Self

from pydantic import Field, model_validator

from company_bench.models import CompanyId, Identifier, StrictModel
from company_bench.runtime_models import (
    JournalEntryReference,
    SimTime,
    SystemEventKind,
    WakeReason,
    WakeSignal,
)

__all__ = ["ScheduledEvent", "Scheduler", "SchedulerCheckpoint"]


class ScheduledEvent(StrictModel):
    """One immutable event ordered by time and stable insertion sequence."""

    event_id: Identifier
    at: SimTime
    sequence: int = Field(ge=1)
    kind: SystemEventKind
    company_id: CompanyId | None = None
    wake_signals: tuple[WakeSignal, ...] = ()
    reference_ids: tuple[Identifier, ...] = ()

    @property
    def wake_reasons(self) -> tuple[WakeReason, ...]:
        """Return unique reasons in causal-signal order."""
        return tuple(dict.fromkeys(signal.reason for signal in self.wake_signals))

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        """Keep wake-only fields exclusive to company wake events."""
        is_wake = self.kind is SystemEventKind.COMPANY_WAKE
        if is_wake and (self.company_id is None or not self.wake_signals):
            raise ValueError("company wake events require company_id and wake_signals")
        if not is_wake and self.wake_signals:
            raise ValueError("wake_signals are only valid on company wake events")
        if is_wake and self.reference_ids:
            raise ValueError("company wake references belong to their wake_signals")
        if len(self.reference_ids) != len(set(self.reference_ids)):
            raise ValueError("reference_ids must be unique")
        return self


class SchedulerCheckpoint(StrictModel):
    """Complete serializable state needed to resume a scheduler."""

    now: SimTime
    next_sequence: int = Field(ge=1)
    pending_events: tuple[ScheduledEvent, ...] = ()

    @model_validator(mode="after")
    def validate_pending_events(self) -> Self:
        """Reject checkpoints that could break monotonic deterministic replay."""
        ids = [event.event_id for event in self.pending_events]
        sequences = [event.sequence for event in self.pending_events]
        if len(ids) != len(set(ids)):
            raise ValueError("pending event ids must be unique")
        if len(sequences) != len(set(sequences)):
            raise ValueError("pending event sequences must be unique")
        if any(
            event.at.absolute_minute < self.now.absolute_minute for event in self.pending_events
        ):
            raise ValueError("pending events cannot precede checkpoint time")
        if sequences and self.next_sequence <= max(sequences):
            raise ValueError("next_sequence must exceed every pending sequence")

        wake_keys = [
            (event.at.absolute_minute, event.company_id)
            for event in self.pending_events
            if event.kind is SystemEventKind.COMPANY_WAKE
        ]
        if len(wake_keys) != len(set(wake_keys)):
            raise ValueError("company wakes at the same time must be coalesced")
        return self


class Scheduler:
    """Own the simulation clock and a stable absolute-minute event heap."""

    def __init__(self, start_at: SimTime | None = None) -> None:
        self._now = start_at or SimTime(absolute_minute=0)
        self._next_sequence = 1
        self._heap: list[tuple[int, int, int, str]] = []
        self._events: dict[str, ScheduledEvent] = {}
        self._wake_event_ids: dict[tuple[int, str], str] = {}

    @property
    def now(self) -> SimTime:
        """Return the scheduler's sole clock value."""
        return self._now

    def __len__(self) -> int:
        return len(self._events)

    def __bool__(self) -> bool:
        return bool(self._events)

    def schedule_system(
        self,
        kind: SystemEventKind,
        at: SimTime,
        *,
        event_id: Identifier | None = None,
        company_id: CompanyId | None = None,
        reference_ids: Iterable[Identifier] = (),
    ) -> ScheduledEvent:
        """Schedule a non-wake event, clamping past time to the clock."""
        if kind is SystemEventKind.COMPANY_WAKE:
            raise ValueError("use schedule_wake for company wake events")
        sequence = self._next_sequence
        event = ScheduledEvent(
            event_id=event_id or f"event_{sequence}",
            at=self._clamp(at),
            sequence=sequence,
            kind=kind,
            company_id=company_id,
            reference_ids=self._unique(reference_ids),
        )
        self._insert_new(event)
        return event

    def schedule_wake(
        self,
        company_id: CompanyId,
        at: SimTime,
        reason: WakeReason,
        *,
        source: JournalEntryReference | None = None,
        reference_ids: Iterable[Identifier] = (),
    ) -> ScheduledEvent:
        """Schedule or merge one company's wake at an absolute minute."""
        clamped = self._clamp(at)
        key = (clamped.absolute_minute, company_id)
        existing_id = self._wake_event_ids.get(key)
        signal = WakeSignal(
            reason=reason,
            source=source,
            reference_ids=self._unique(reference_ids),
        )
        if existing_id is not None:
            existing = self._events[existing_id]
            signals = (
                existing.wake_signals
                if signal in existing.wake_signals
                else (*existing.wake_signals, signal)
            )
            merged = ScheduledEvent(
                event_id=existing.event_id,
                at=existing.at,
                sequence=existing.sequence,
                kind=existing.kind,
                company_id=existing.company_id,
                wake_signals=signals,
            )
            self._events[existing_id] = merged
            return merged

        sequence = self._next_sequence
        event = ScheduledEvent(
            event_id=f"wake_{sequence}",
            at=clamped,
            sequence=sequence,
            kind=SystemEventKind.COMPANY_WAKE,
            company_id=company_id,
            wake_signals=(signal,),
        )
        self._insert_new(event)
        self._wake_event_ids[key] = event.event_id
        return event

    def peek_time(self) -> SimTime | None:
        """Return the next event time without advancing the clock."""
        if not self._heap:
            return None
        return SimTime(absolute_minute=self._heap[0][0])

    def cancel_company_wakes(self, company_id: CompanyId) -> None:
        """Cancel superseded future wake timers for one company."""
        event_ids = tuple(
            event_id
            for (_, candidate), event_id in self._wake_event_ids.items()
            if candidate == company_id
        )
        if not event_ids:
            return
        for event_id in event_ids:
            event = self._events.pop(event_id)
            self._wake_event_ids.pop(
                (event.at.absolute_minute, company_id),
                None,
            )
        retained = set(self._events)
        self._heap = [entry for entry in self._heap if entry[3] in retained]
        heapq.heapify(self._heap)

    def pop_bucket(self) -> tuple[ScheduledEvent, ...]:
        """Pop every event at the earliest minute and advance once."""
        if not self._heap:
            return ()

        minute = self._heap[0][0]
        if minute < self._now.absolute_minute:
            raise RuntimeError("scheduler heap moved behind its clock")
        self._now = SimTime(absolute_minute=minute)

        events: list[ScheduledEvent] = []
        while self._heap and self._heap[0][0] == minute:
            _, _, _, event_id = heapq.heappop(self._heap)
            event = self._events.pop(event_id)
            if event.kind is SystemEventKind.COMPANY_WAKE:
                wake_key = (event.at.absolute_minute, event.company_id)
                self._wake_event_ids.pop(wake_key, None)
            events.append(event)
        events.sort(key=lambda event: (event.kind.priority, event.sequence))
        return tuple(events)

    def checkpoint(self) -> SchedulerCheckpoint:
        """Return a stable JSON-serializable scheduler snapshot."""
        pending = tuple(
            sorted(
                self._events.values(),
                key=lambda event: (
                    event.at.absolute_minute,
                    event.kind.priority,
                    event.sequence,
                ),
            )
        )
        return SchedulerCheckpoint(
            now=self._now,
            next_sequence=self._next_sequence,
            pending_events=pending,
        )

    @classmethod
    def restore(cls, checkpoint: SchedulerCheckpoint) -> Self:
        """Restore exactly, preserving pending event sequence identities."""
        scheduler = cls(start_at=checkpoint.now)
        scheduler._next_sequence = checkpoint.next_sequence
        for event in checkpoint.pending_events:
            scheduler._restore_event(event)
        return scheduler

    @classmethod
    def from_checkpoint(cls, checkpoint: SchedulerCheckpoint) -> Self:
        """Alias for restore, useful at persistence seams."""
        return cls.restore(checkpoint)

    def _clamp(self, at: SimTime) -> SimTime:
        if at.absolute_minute >= self._now.absolute_minute:
            return at
        return self._now

    def _insert_new(self, event: ScheduledEvent) -> None:
        if event.event_id in self._events:
            raise ValueError(f"duplicate pending event_id: {event.event_id}")
        self._events[event.event_id] = event
        self._push_heap(event)
        self._next_sequence += 1

    def _restore_event(self, event: ScheduledEvent) -> None:
        self._events[event.event_id] = event
        self._push_heap(event)
        if event.kind is SystemEventKind.COMPANY_WAKE:
            wake_key = (event.at.absolute_minute, event.company_id)
            self._wake_event_ids[wake_key] = event.event_id

    def _push_heap(self, event: ScheduledEvent) -> None:
        heapq.heappush(
            self._heap,
            (
                event.at.absolute_minute,
                event.kind.priority,
                event.sequence,
                event.event_id,
            ),
        )

    @staticmethod
    def _unique[Value](values: Iterable[Value]) -> tuple[Value, ...]:
        return tuple(dict.fromkeys(values))
