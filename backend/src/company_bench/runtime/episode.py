"""Event-driven episode orchestration behind one small runtime interface."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import TypeAdapter, ValidationError

from company_bench.agents.company import (
    CheckpointMemory,
    CompanyAgent,
    EpisodeCompletionGuard,
    ReplayDriftError,
    ReplayedProtocolError,
    TurnMemory,
    observation_hash,
)
from company_bench.agents.contracts import (
    ModelOutputError,
    PolicyExecutionError,
    PolicyInfrastructureError,
    PolicyTerminalError,
)
from company_bench.domain.models import (
    CompanyEvent,
    CompanyId,
    DaySnapshot,
    DomainEvent,
    EpisodeQuality,
    EpisodeResult,
    EventRecord,
    PolicyDescriptor,
    PolicyKind,
    ProtocolIssueKind,
    ProtocolReport,
    ScenarioSpec,
    TradeExecutedEvent,
)
from company_bench.economy.engine import EconomyEngine, EconomyState
from company_bench.economy.scoring import Evaluator
from company_bench.runs.models import (
    CompanyRuntimeCursor,
    RunCheckpoint,
    RunRecovery,
)
from company_bench.runtime.attention import (
    AgentAttention,
    ArmedAttention,
    AttentionMatch,
    AttentionRejected,
)
from company_bench.runtime.economics import PrivateEconomicsProjector
from company_bench.runtime.models import (
    PROTOCOL_ERROR_PREFIX,
    AgentTurn,
    AttentionPlan,
    CompanyDecision,
    DecisionEnvelope,
    DecisionOutcome,
    DecisionStatus,
    IdleDecision,
    JournalEntryKind,
    JournalEntryReference,
    RejectionCategory,
    SimTime,
    SystemEventKind,
    SystemStepRecord,
    TurnRecord,
    TurnReplayOrigin,
    WakeReason,
    WakeSignal,
    system_step_id,
)
from company_bench.runtime.scheduler import ScheduledEvent, Scheduler

_DECISION_ADAPTER = TypeAdapter(CompanyDecision)
type DayCallback = Callable[[int], Awaitable[None]]
type TurnCallback = Callable[[TurnRecord], Awaitable[None]]


class RuntimeStore(Protocol):
    """Internal persistence seam for atomic turn/checkpoint progress."""

    def save_progress(
        self,
        turns: tuple[TurnRecord, ...],
        system_steps: tuple[SystemStepRecord, ...],
        checkpoint: RunCheckpoint,
    ) -> None:
        """Atomically append journal entries and replace the checkpoint."""
        ...


@dataclass(frozen=True, slots=True)
class EpisodeExecution:
    """Completed episode plus its authoritative per-turn journal."""

    episode: EpisodeResult
    turns: tuple[TurnRecord, ...]


@dataclass(slots=True)
class _Cursor:
    next_turn_sequence: int = 1
    last_visible_event_sequence: int = 0
    available_at: SimTime | None = None
    active_attention: ArmedAttention | None = None


@dataclass(frozen=True, slots=True)
class _PendingTurn:
    turn: AgentTurn
    decision: CompanyDecision
    protocol_issue_kind: ProtocolIssueKind | None = None
    protocol_error: str | None = None
    decision_error: str | None = None
    attention_plan: ArmedAttention | None = None

    @property
    def preflight_accepted(self) -> bool:
        """Return whether the engine may receive this decision."""
        return self.protocol_error is None and self.decision_error is None


class EpisodeRuntime:
    """Run one V4 episode with deterministic scheduling and atomic decisions."""

    def __init__(
        self,
        scenario: ScenarioSpec,
        *,
        engine: EconomyEngine | None = None,
        evaluator: Evaluator | None = None,
        agent_timeout_seconds: float = 180.0,
    ) -> None:
        if agent_timeout_seconds <= 0:
            raise ValueError("agent_timeout_seconds must be positive")
        self.scenario = scenario
        self._engine = engine or EconomyEngine()
        self._attention = AgentAttention()
        self._economics = PrivateEconomicsProjector()
        self._evaluator = evaluator or Evaluator()
        self._agent_timeout_seconds = agent_timeout_seconds

    async def run(
        self,
        agents: Mapping[str, CompanyAgent],
        seed: int,
        *,
        run_id: str,
        started_at: datetime | None = None,
        on_day_completed: DayCallback | None = None,
        on_turn_completed: TurnCallback | None = None,
        store: RuntimeStore | None = None,
        recovery: RunRecovery | None = None,
        replay_source: EpisodeResult | None = None,
    ) -> EpisodeExecution:
        """Run, audit, and score one complete event-driven episode."""
        self._validate_agent_set(agents)
        self._validate_replay_source(agents, seed, replay_source)
        checkpoint = recovery.checkpoint if recovery is not None else None
        episode_started_at = (
            checkpoint.episode_started_at
            if checkpoint is not None
            else started_at or datetime.now(UTC)
        )
        if (
            checkpoint is not None
            and started_at is not None
            and started_at != checkpoint.episode_started_at
        ):
            raise ValueError("started_at does not match the checkpoint")
        initial_state = self._engine.initial_state(self.scenario, seed)
        if recovery is None:
            economy = self._engine.open_day(initial_state)
            scheduler = Scheduler()
            self._schedule_day(scheduler, economy.day)
            turns: list[TurnRecord] = []
            system_steps: list[SystemStepRecord] = []
            event_records: list[EventRecord] = []
            snapshots: list[DaySnapshot] = []
            cursors = {company.company_id: _Cursor() for company in self.scenario.companies}
        else:
            self._validate_recovery(recovery, run_id, seed, agents)
            economy = checkpoint.economy
            scheduler = Scheduler.restore(checkpoint.scheduler)
            turns = list(recovery.turns)
            system_steps = list(recovery.system_steps)
            event_records = list(checkpoint.events)
            snapshots = list(checkpoint.snapshots)
            saved_cursors = {cursor.company_id: cursor for cursor in checkpoint.cursors}
            cursors = {
                company.company_id: self._restore_cursor(
                    company.company_id,
                    saved_cursors,
                )
                for company in self.scenario.companies
            }
        if recovery is None:
            self._save_progress(
                store,
                (),
                (),
                run_id,
                episode_started_at,
                economy,
                scheduler,
                agents,
                turns,
                system_steps,
                event_records,
                snapshots,
                cursors,
            )
        previous_outcomes = self._previous_outcomes(turns)
        turn_counts = self._turn_counts(turns)
        turn_limit_audits: set[tuple[int, CompanyId]] = {
            (step.occurred_at.day + 1, step.company_id)
            for step in system_steps
            if step.kind is SystemEventKind.TURN_LIMIT_REACHED and step.company_id is not None
        }
        next_apply_sequence = (
            max((record.outcome.apply_sequence for record in turns), default=0) + 1
        )
        next_journal_sequence = (
            max(
                (
                    *(record.journal_sequence or 0 for record in turns),
                    *(record.journal_sequence for record in system_steps),
                ),
                default=0,
            )
            + 1
        )

        while scheduler:
            bucket = scheduler.pop_bucket()
            (
                economy,
                completed,
                new_steps,
                next_journal_sequence,
                completed_days,
            ) = await self._apply_system_events(
                run_id,
                economy,
                scheduler,
                bucket,
                system_steps,
                event_records,
                snapshots,
                next_journal_sequence,
            )
            self._expire_attention(bucket, cursors)
            pending_system_steps = list(new_steps)
            pending_completed_days = list(completed_days)
            if completed:
                self._save_progress(
                    store,
                    (),
                    tuple(pending_system_steps),
                    run_id,
                    episode_started_at,
                    economy,
                    scheduler,
                    agents,
                    turns,
                    system_steps,
                    event_records,
                    snapshots,
                    cursors,
                )
                if on_day_completed is not None:
                    for completed_day in pending_completed_days:
                        await on_day_completed(completed_day)
                break

            wake_events = [event for event in bucket if event.kind is SystemEventKind.COMPANY_WAKE]
            while scheduler.peek_time() == scheduler.now:
                same_time = scheduler.pop_bucket()
                (
                    economy,
                    completed,
                    step_delta,
                    next_journal_sequence,
                    day_delta,
                ) = await self._apply_system_events(
                    run_id,
                    economy,
                    scheduler,
                    same_time,
                    system_steps,
                    event_records,
                    snapshots,
                    next_journal_sequence,
                )
                self._expire_attention(same_time, cursors)
                pending_system_steps.extend(step_delta)
                pending_completed_days.extend(day_delta)
                if completed:
                    break
                wake_events.extend(
                    event for event in same_time if event.kind is SystemEventKind.COMPANY_WAKE
                )
            if completed:
                self._save_progress(
                    store,
                    (),
                    tuple(pending_system_steps),
                    run_id,
                    episode_started_at,
                    economy,
                    scheduler,
                    agents,
                    turns,
                    system_steps,
                    event_records,
                    snapshots,
                    cursors,
                )
                if on_day_completed is not None:
                    for completed_day in pending_completed_days:
                        await on_day_completed(completed_day)
                break
            merged_wakes = self._merge_wakes(tuple(wake_events))
            ready_wakes = self._defer_busy_wakes(
                scheduler,
                merged_wakes,
                cursors,
            )
            if not ready_wakes:
                self._save_progress(
                    store,
                    (),
                    tuple(pending_system_steps),
                    run_id,
                    episode_started_at,
                    economy,
                    scheduler,
                    agents,
                    turns,
                    system_steps,
                    event_records,
                    snapshots,
                    cursors,
                )
                if on_day_completed is not None:
                    for completed_day in pending_completed_days:
                        await on_day_completed(completed_day)
                continue
            (
                eligible,
                limit_steps,
                next_journal_sequence,
            ) = self._enforce_turn_limit(
                run_id,
                economy,
                ready_wakes,
                turn_counts,
                turn_limit_audits,
                cursors,
                scheduler,
                next_journal_sequence,
            )
            system_steps.extend(limit_steps)
            pending_system_steps.extend(limit_steps)
            if not eligible:
                self._save_progress(
                    store,
                    (),
                    tuple(pending_system_steps),
                    run_id,
                    episode_started_at,
                    economy,
                    scheduler,
                    agents,
                    turns,
                    system_steps,
                    event_records,
                    snapshots,
                    cursors,
                )
                if on_day_completed is not None:
                    for completed_day in pending_completed_days:
                        await on_day_completed(completed_day)
                continue

            for event in eligible:
                if event.company_id is not None:
                    scheduler.cancel_company_wakes(event.company_id)
                    cursors[event.company_id].active_attention = None
            pending = await self._query_bucket(
                run_id,
                economy,
                eligible,
                agents,
                event_records,
                cursors,
                previous_outcomes,
                turn_counts,
            )
            ordered = tuple(
                self._prepare_attention(item)
                for item in self._application_order(pending, seed, scheduler.now)
            )
            envelopes = tuple(
                DecisionEnvelope(
                    turn_id=item.turn.turn_id,
                    decision_id=f"{item.turn.turn_id}.decision",
                    company_id=item.turn.company_id,
                    issued_at=item.turn.sim_time,
                    state_version=item.turn.state_version,
                    decision=item.decision,
                )
                for item in ordered
            )
            accepted_envelopes = tuple(
                envelope
                for envelope, item in zip(envelopes, ordered, strict=True)
                if item.preflight_accepted
            )
            accepted_sequences = tuple(
                next_apply_sequence + offset
                for offset, item in enumerate(ordered)
                if item.preflight_accepted
            )
            before_event_count = len(economy.events)
            economy, accepted_outcomes = self._engine.apply_batch(
                economy,
                accepted_envelopes,
                first_apply_sequence=next_apply_sequence,
                apply_sequences=accepted_sequences,
            )
            outcomes = self._merge_outcomes(
                ordered,
                envelopes,
                accepted_outcomes,
                next_apply_sequence,
            )
            next_apply_sequence += len(ordered)
            self._schedule_completions(scheduler, outcomes)
            self._append_events(
                event_records,
                economy.events[before_event_count:],
            )

            new_records: list[TurnRecord] = []
            for item, envelope, outcome in zip(
                ordered,
                envelopes,
                outcomes,
                strict=True,
            ):
                record = TurnRecord(
                    run_id=run_id,
                    turn=item.turn,
                    envelope=envelope,
                    outcome=outcome,
                    observation_hash=observation_hash(item.turn),
                    protocol_issue_kind=item.protocol_issue_kind,
                    protocol_error=item.protocol_error,
                    journal_sequence=next_journal_sequence,
                    replay_origin=self._replay_origin(
                        agents[item.turn.company_id],
                        run_id,
                        item.turn,
                    ),
                )
                next_journal_sequence += 1
                turns.append(record)
                new_records.append(record)
                company_id = item.turn.company_id
                turn_counts[(economy.day, company_id)] = (
                    turn_counts.get((economy.day, company_id), 0) + 1
                )
                previous_outcomes[company_id] = outcome
                cursors[company_id].available_at = self._available_after(record)
                agent = agents[company_id]
                if isinstance(agent, TurnMemory):
                    agent.remember(record)
                if on_turn_completed is not None:
                    await on_turn_completed(record)
                self._schedule_rejection_retry(
                    scheduler,
                    record,
                    turn_counts[(economy.day, company_id)],
                )
                self._install_attention(
                    scheduler,
                    cursors[company_id],
                    record,
                    item.attention_plan,
                    turn_counts[(economy.day, company_id)],
                )
                self._schedule_trade_wakes(scheduler, record)
            self._schedule_price_alerts(scheduler, economy, cursors)
            for record in new_records:
                company_id = record.turn.company_id
                audit_key = (economy.day, company_id)
                if (
                    turn_counts[audit_key] != self.scenario.runtime.max_turns_per_company_day
                    or audit_key in turn_limit_audits
                ):
                    continue
                cursors[company_id].active_attention = None
                step = self._turn_limit_step(
                    run_id,
                    economy,
                    record.turn.sim_time,
                    company_id,
                    next_journal_sequence,
                )
                next_journal_sequence += 1
                turn_limit_audits.add(audit_key)
                system_steps.append(step)
                pending_system_steps.append(step)
            self._save_progress(
                store,
                tuple(new_records),
                tuple(pending_system_steps),
                run_id,
                episode_started_at,
                economy,
                scheduler,
                agents,
                turns,
                system_steps,
                event_records,
                snapshots,
                cursors,
            )
            if on_day_completed is not None:
                for completed_day in pending_completed_days:
                    await on_day_completed(completed_day)

        final_state = economy.base_state
        if final_state.day != self.scenario.days:
            raise RuntimeError("scheduler ended before the scenario completed")
        for agent in agents.values():
            if isinstance(agent, EpisodeCompletionGuard):
                agent.ensure_episode_complete()
        protocol_issues = tuple(
            record.protocol_issue_kind for record in turns if record.protocol_issue_kind is not None
        )
        protocol = ProtocolReport.from_issues(len(turns), protocol_issues)
        events = tuple(record.event for record in event_records)
        episode = EpisodeResult(
            run_id=run_id,
            scenario=self.scenario,
            seed=seed,
            started_at=episode_started_at,
            finished_at=datetime.now(UTC),
            policies=_policy_descriptors(self.scenario, agents),
            events=tuple(event_records),
            snapshots=tuple(snapshots),
            score=self._evaluator.evaluate(
                self.scenario,
                initial_state,
                final_state,
                tuple(snapshots),
                events,
            ),
            quality=EpisodeQuality(
                benchmark_eligible=protocol.invalid_turn_count == 0,
                protocol=protocol,
            ),
        )
        if replay_source is not None and (
            episode.events != replay_source.events
            or episode.snapshots != replay_source.snapshots
            or episode.score != replay_source.score
            or episode.quality != replay_source.quality
        ):
            raise ReplayDriftError(
                "replay final events, snapshots, score, or quality drifted from the source run"
            )
        return EpisodeExecution(episode=episode, turns=tuple(turns))

    @staticmethod
    def _merge_wakes(
        wake_events: tuple[ScheduledEvent, ...],
    ) -> tuple[ScheduledEvent, ...]:
        """Coalesce wakes added while same-minute system events execute."""
        merged: dict[str, ScheduledEvent] = {}
        for event in sorted(wake_events, key=lambda item: item.sequence):
            company_id = event.company_id
            if company_id is None:
                raise ValueError("wake event is missing company_id")
            existing = merged.get(company_id)
            if existing is None:
                merged[company_id] = event
                continue
            merged[company_id] = existing.model_copy(
                update={
                    "wake_signals": tuple(
                        dict.fromkeys((*existing.wake_signals, *event.wake_signals))
                    )
                }
            )
        return tuple(merged.values())

    def _defer_busy_wakes(
        self,
        scheduler: Scheduler,
        wake_events: tuple[ScheduledEvent, ...],
        cursors: Mapping[str, _Cursor],
    ) -> tuple[ScheduledEvent, ...]:
        """Move external wakes to the end of a company's active action."""
        ready: list[ScheduledEvent] = []
        for event in wake_events:
            company_id = event.company_id
            if company_id is None:
                raise ValueError("wake event is missing company_id")
            available_at = cursors[company_id].available_at
            if available_at is None or event.at.absolute_minute >= available_at.absolute_minute:
                ready.append(event)
                continue
            target = self._next_business_time(available_at)
            if target is None:
                continue
            for signal in event.wake_signals:
                scheduler.schedule_wake(
                    company_id,
                    target,
                    signal.reason,
                    source=signal.source,
                    reference_ids=signal.reference_ids,
                )
        return tuple(ready)

    def _next_business_time(self, at: SimTime) -> SimTime | None:
        """Move a cooldown boundary into the next valid business window."""
        runtime = self.scenario.runtime
        day = at.day
        minute = at.minute_of_day
        if minute < runtime.open_minute:
            minute = runtime.open_minute
        elif minute >= runtime.close_minute:
            day += 1
            minute = runtime.open_minute
        if day >= self.scenario.days:
            return None
        return SimTime(absolute_minute=day * 24 * 60 + minute)

    @staticmethod
    def _available_after(record: TurnRecord) -> SimTime | None:
        """Return the cooldown boundary; idling does not occupy a company."""
        if isinstance(record.envelope.decision, IdleDecision) and record.outcome.accepted:
            return None
        return record.outcome.next_available_at

    async def _apply_system_events(
        self,
        run_id: str,
        economy: EconomyState,
        scheduler: Scheduler,
        bucket: tuple[ScheduledEvent, ...],
        system_steps: list[SystemStepRecord],
        event_records: list[EventRecord],
        snapshots: list[DaySnapshot],
        next_journal_sequence: int,
    ) -> tuple[
        EconomyState,
        bool,
        tuple[SystemStepRecord, ...],
        int,
        tuple[int, ...],
    ]:
        new_steps: list[SystemStepRecord] = []
        completed_days: list[int] = []
        completed = False
        for event in bucket:
            if event.kind is SystemEventKind.COMPANY_WAKE:
                continue
            before_version = economy.state_version
            before_event_count = len(event_records)
            snapshot_day: int | None = None
            entry_id = system_step_id(run_id, event.event_id)
            if event.kind is SystemEventKind.DAY_OPEN:
                for company in self.scenario.companies:
                    scheduler.schedule_wake(
                        company.company_id,
                        event.at,
                        WakeReason.DAY_OPEN,
                        source=JournalEntryReference(
                            entry_id=entry_id,
                            entry_type=JournalEntryKind.SYSTEM_STEP,
                        ),
                    )
            elif event.kind in {
                SystemEventKind.OPERATION_COMPLETED,
                SystemEventKind.DELIVERY_COMPLETED,
            }:
                reference_id = self._completion_reference(event)
                before = len(economy.events)
                economy = (
                    self._engine.complete_operation(economy, reference_id, event.at)
                    if event.kind is SystemEventKind.OPERATION_COMPLETED
                    else self._engine.complete_delivery(economy, reference_id, event.at)
                )
                self._append_events(event_records, economy.events[before:])
                if event.at.minute_of_day < self.scenario.runtime.close_minute:
                    if event.company_id is None:
                        raise ValueError("completion event is missing company_id")
                    scheduler.schedule_wake(
                        event.company_id,
                        event.at,
                        (
                            WakeReason.OPERATION_COMPLETED
                            if event.kind is SystemEventKind.OPERATION_COMPLETED
                            else WakeReason.DELIVERY_COMPLETED
                        ),
                        source=JournalEntryReference(
                            entry_id=entry_id,
                            entry_type=JournalEntryKind.SYSTEM_STEP,
                        ),
                        reference_ids=(reference_id,),
                    )
            elif event.kind is SystemEventKind.MARKET_CLOSE:
                economy = self._engine.close_markets(economy, event.at)
            elif event.kind is SystemEventKind.CONSUMER_SALES:
                before = len(economy.events)
                economy = self._engine.settle_consumer_sales(economy, event.at)
                self._append_events(event_records, economy.events[before:])
            elif event.kind is SystemEventKind.DAY_CLOSE:
                result = self._engine.close_day(economy, event.at)
                self._append_events(
                    event_records,
                    result.events[len(economy.events) :],
                )
                snapshots.append(result.snapshot)
                snapshot_day = result.snapshot.day
                completed_days.append(result.state.day)
                if result.state.day == self.scenario.days:
                    economy = economy.model_copy(
                        update={
                            "base_state": result.state,
                            "companies": result.state.companies,
                            "events": result.events,
                            "state_version": economy.state_version + 1,
                        }
                    )
                    completed = True
                else:
                    economy = self._engine.open_day(
                        result.state,
                        state_version=economy.state_version + 1,
                    )
                    self._schedule_day(scheduler, economy.day)
            else:
                raise RuntimeError(f"unsupported system event: {event.kind.value}")
            effects = tuple(event_records[before_event_count:])
            step = SystemStepRecord(
                run_id=run_id,
                entry_id=entry_id,
                journal_sequence=next_journal_sequence,
                scheduled_event_id=event.event_id,
                occurred_at=event.at,
                kind=event.kind,
                reference_ids=event.reference_ids,
                state_version_before=before_version,
                state_version_after=economy.state_version,
                effects=effects,
                snapshot_day=snapshot_day,
            )
            system_steps.append(step)
            new_steps.append(step)
            next_journal_sequence += 1
            if completed:
                break
        return (
            economy,
            completed,
            tuple(new_steps),
            next_journal_sequence,
            tuple(completed_days),
        )

    @staticmethod
    def _completion_reference(event: ScheduledEvent) -> str:
        """Return the sole economic entity completed by a system event."""
        if len(event.reference_ids) != 1:
            raise ValueError("completion event requires exactly one reference_id")
        return event.reference_ids[0]

    async def _query_bucket(
        self,
        run_id: str,
        economy: EconomyState,
        wake_events: tuple[ScheduledEvent, ...],
        agents: Mapping[str, CompanyAgent],
        event_records: list[EventRecord],
        cursors: dict[str, _Cursor],
        previous_outcomes: dict[str, DecisionOutcome],
        turn_counts: Mapping[tuple[int, str], int],
    ) -> tuple[_PendingTurn, ...]:
        turns = tuple(
            self._build_turn(
                run_id,
                economy,
                event,
                event_records,
                cursors[event.company_id],
                previous_outcomes.get(event.company_id),
                turn_counts.get((economy.day, event.company_id), 0) + 1,
            )
            for event in sorted(wake_events, key=lambda item: item.company_id or "")
            if event.company_id is not None
        )
        tasks = tuple(
            asyncio.create_task(self._ask_agent(turn, agents[turn.company_id])) for turn in turns
        )
        try:
            return tuple(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    def _build_turn(
        self,
        run_id: str,
        economy: EconomyState,
        wake_event: ScheduledEvent,
        event_records: list[EventRecord],
        cursor: _Cursor,
        previous_outcome: DecisionOutcome | None,
        turn_number_today: int,
    ) -> AgentTurn:
        company_id = wake_event.company_id
        if company_id is None:
            raise ValueError("wake event is missing company_id")
        sequence = cursor.next_turn_sequence
        cursor.next_turn_sequence += 1
        visible = tuple(
            record.event
            for record in event_records[cursor.last_visible_event_sequence :]
            if self._event_is_visible(record.event, company_id)
        )
        cursor.last_visible_event_sequence = len(event_records)
        observation = self._engine.observe_active(economy, company_id)
        order_books = self._engine.order_books(economy, company_id)
        return AgentTurn(
            turn_id=f"{run_id}.{company_id}.t{sequence}",
            company_id=company_id,
            sim_time=wake_event.at,
            state_version=economy.state_version,
            turn_number_today=turn_number_today,
            turn_limit_today=self.scenario.runtime.max_turns_per_company_day,
            wake_reasons=wake_event.wake_reasons,
            wake_signals=wake_event.wake_signals,
            observation=observation,
            available_cash=observation.cash,
            reserved_cash=self._engine.reserved_cash(economy, company_id),
            marked_surplus=self._engine.marked_surplus(economy, company_id),
            inventory_expiry=self._engine.inventory_expiry(economy, company_id),
            open_orders=self._engine.company_orders(economy, company_id),
            order_books=order_books,
            pending_deliveries=self._engine.pending_delivery_views(
                economy,
                company_id,
            ),
            active_operation=self._engine.operation_view(economy, company_id),
            private_economics=self._economics.project(
                company_id,
                observation,
                order_books,
                tuple(record.event for record in event_records),
            ),
            visible_events=visible,
            previous_outcome=previous_outcome,
        )

    @staticmethod
    def _replay_origin(
        agent: CompanyAgent,
        run_id: str,
        turn: AgentTurn,
    ) -> TurnReplayOrigin | None:
        """Persist the exact immediate source Turn for replay provenance."""
        source_run_id = agent.metadata.source_run_id
        if agent.metadata.kind is not PolicyKind.REPLAY or source_run_id is None:
            return None
        prefix = f"{run_id}."
        if not turn.turn_id.startswith(prefix):
            raise ValueError("runtime turn_id does not carry its run_id")
        return TurnReplayOrigin(
            source_run_id=source_run_id,
            source_turn_id=f"{source_run_id}.{turn.turn_id.removeprefix(prefix)}",
        )

    async def _ask_agent(
        self,
        turn: AgentTurn,
        agent: CompanyAgent,
    ) -> _PendingTurn:
        try:
            decision = await asyncio.wait_for(
                agent.act(turn),
                timeout=self._agent_timeout_seconds,
            )
            return _PendingTurn(
                turn=turn,
                decision=_DECISION_ADAPTER.validate_python(decision),
            )
        except (PolicyInfrastructureError, PolicyTerminalError, ReplayDriftError):
            raise
        except TimeoutError as error:
            raise PolicyInfrastructureError(
                f"Agent turn timed out after {self._agent_timeout_seconds:g} seconds"
            ) from error
        except ReplayedProtocolError as error:
            return _PendingTurn(
                turn=turn,
                decision=IdleDecision(attention=AttentionPlan()),
                protocol_issue_kind=error.issue_kind,
                protocol_error=str(error),
            )
        except (ModelOutputError, ValidationError) as error:
            reason = f"{type(error).__name__}: {str(error).strip()}"[:450]
            return _PendingTurn(
                turn=turn,
                decision=IdleDecision(attention=AttentionPlan()),
                protocol_issue_kind=(
                    error.issue_kind
                    if isinstance(error, ModelOutputError)
                    else ProtocolIssueKind.INVALID_ARGUMENTS
                ),
                protocol_error=reason or type(error).__name__,
            )
        except Exception as error:
            raise PolicyExecutionError(
                f"unexpected Agent failure: {type(error).__name__}"
            ) from error

    def _prepare_attention(self, item: _PendingTurn) -> _PendingTurn:
        """Preflight attention semantics before applying the economic decision."""
        if item.protocol_error is not None:
            return item
        try:
            plan = self._attention.arm(item.decision.attention, item.turn)
        except AttentionRejected as error:
            return _PendingTurn(
                turn=item.turn,
                decision=item.decision,
                decision_error=str(error),
            )
        return _PendingTurn(
            turn=item.turn,
            decision=item.decision,
            attention_plan=plan,
        )

    @staticmethod
    def _application_order(
        pending: tuple[_PendingTurn, ...],
        seed: int,
        at: SimTime,
    ) -> tuple[_PendingTurn, ...]:
        """Create a full seed-derived arrival permutation for one minute."""

        def priority(item: _PendingTurn) -> tuple[bytes, str]:
            company_id = item.turn.company_id
            digest = hashlib.sha256(f"{seed}|{at.absolute_minute}|{company_id}".encode()).digest()
            return digest, company_id

        return tuple(sorted(pending, key=priority))

    @staticmethod
    def _merge_outcomes(
        pending: tuple[_PendingTurn, ...],
        envelopes: tuple[DecisionEnvelope, ...],
        accepted_outcomes: tuple[DecisionOutcome, ...],
        first_apply_sequence: int,
    ) -> tuple[DecisionOutcome, ...]:
        successful = iter(accepted_outcomes)
        outcomes: list[DecisionOutcome] = []
        state_version = pending[0].turn.state_version if pending else 0
        for offset, (item, envelope) in enumerate(zip(pending, envelopes, strict=True)):
            if item.preflight_accepted:
                outcome = next(successful)
                outcomes.append(outcome)
                state_version = outcome.resulting_state_version
                continue
            reason = (
                f"{PROTOCOL_ERROR_PREFIX}{item.protocol_error}"
                if item.protocol_error is not None
                else item.decision_error
            )
            if reason is None:
                raise RuntimeError("rejected preflight is missing a reason")
            outcomes.append(
                DecisionOutcome(
                    turn_id=envelope.turn_id,
                    decision_id=envelope.decision_id,
                    company_id=envelope.company_id,
                    occurred_at=envelope.issued_at,
                    status=DecisionStatus.REJECTED,
                    accepted=False,
                    rejection_category=(
                        RejectionCategory.PROTOCOL
                        if item.protocol_error is not None
                        else RejectionCategory.ATTENTION
                    ),
                    reason=reason,
                    resulting_state_version=state_version,
                    apply_sequence=first_apply_sequence + offset,
                    next_available_at=envelope.issued_at.plus(
                        item.turn.observation.runtime.decision_interval_minutes
                    ),
                )
            )
        return tuple(outcomes)

    def _schedule_rejection_retry(
        self,
        scheduler: Scheduler,
        record: TurnRecord,
        turns_today: int,
    ) -> None:
        if (
            record.outcome.accepted
            or turns_today >= self.scenario.runtime.max_turns_per_company_day
        ):
            return
        available = record.outcome.next_available_at
        if (
            available is not None
            and available.day == record.turn.sim_time.day
            and available.minute_of_day < self.scenario.runtime.close_minute
        ):
            scheduler.schedule_wake(
                record.turn.company_id,
                available,
                WakeReason.DECISION_REJECTED,
                source=JournalEntryReference(
                    entry_id=record.turn.turn_id,
                    entry_type=JournalEntryKind.TURN,
                ),
            )

    def _schedule_day(self, scheduler: Scheduler, day: int) -> None:
        day_index = day - 1
        runtime = self.scenario.runtime
        scheduler.schedule_system(
            SystemEventKind.DAY_OPEN,
            SimTime(absolute_minute=day_index * 24 * 60 + runtime.open_minute),
            event_id=f"d{day}.open",
        )
        scheduler.schedule_system(
            SystemEventKind.MARKET_CLOSE,
            SimTime(absolute_minute=day_index * 24 * 60 + runtime.close_minute),
            event_id=f"d{day}.market_close",
        )
        scheduler.schedule_system(
            SystemEventKind.CONSUMER_SALES,
            SimTime(absolute_minute=day_index * 24 * 60 + runtime.close_minute),
            event_id=f"d{day}.consumer_sales",
        )
        scheduler.schedule_system(
            SystemEventKind.DAY_CLOSE,
            SimTime(absolute_minute=day_index * 24 * 60 + runtime.day_close_minute),
            event_id=f"d{day}.close",
        )

    @staticmethod
    def _schedule_completions(
        scheduler: Scheduler,
        outcomes: tuple[DecisionOutcome, ...],
    ) -> None:
        """Persist every accepted asynchronous economic commitment."""
        for outcome in outcomes:
            for completion in outcome.scheduled_completions:
                scheduler.schedule_system(
                    completion.kind,
                    completion.at,
                    event_id=completion.event_id,
                    company_id=completion.company_id,
                    reference_ids=(completion.reference_id,),
                )

    def _install_attention(
        self,
        scheduler: Scheduler,
        cursor: _Cursor,
        record: TurnRecord,
        plan: ArmedAttention | None,
        turns_today: int,
    ) -> None:
        """Persist accepted attention and schedule only its fallback review."""
        if not record.outcome.accepted:
            return
        if plan is None:
            raise RuntimeError("accepted decision is missing its attention plan")
        if turns_today >= self.scenario.runtime.max_turns_per_company_day:
            return
        cursor.active_attention = plan
        if plan.review_at is None:
            return
        scheduler.schedule_wake(
            record.turn.company_id,
            plan.review_at,
            WakeReason.REVIEW_DUE,
            source=self._turn_reference(record.turn.turn_id),
        )

    def _schedule_trade_wakes(
        self,
        scheduler: Scheduler,
        record: TurnRecord,
    ) -> None:
        """Notify only the companies whose own orders actually traded."""
        trades = tuple(
            event for event in record.outcome.events if isinstance(event, TradeExecutedEvent)
        )
        at = record.turn.sim_time.plus(self.scenario.runtime.decision_interval_minutes)
        if not trades or not self._can_wake_on_day(at, record.turn.sim_time.day):
            return
        trade_ids_by_company: dict[CompanyId, list[str]] = {}
        for trade in trades:
            for company_id in (trade.seller_id, trade.buyer_id):
                trade_ids_by_company.setdefault(company_id, []).append(trade.trade_id)
        for company_id, trade_ids in sorted(trade_ids_by_company.items()):
            scheduler.schedule_wake(
                company_id,
                at,
                WakeReason.TRADE_EXECUTED,
                source=(
                    self._turn_reference(record.turn.turn_id)
                    if company_id == record.turn.company_id
                    else None
                ),
                reference_ids=tuple(dict.fromkeys(trade_ids)),
            )

    def _schedule_price_alerts(
        self,
        scheduler: Scheduler,
        economy: EconomyState,
        cursors: Mapping[str, _Cursor],
    ) -> None:
        """Evaluate every plan once against the committed minute-end books."""
        wake_at = scheduler.now.plus(self.scenario.runtime.decision_interval_minutes)
        for company_id, cursor in sorted(cursors.items()):
            plan = cursor.active_attention
            if plan is None:
                continue
            match = self._attention.evaluate(
                plan,
                self._engine.order_books(economy, company_id),
            )
            if match is None:
                continue
            if plan.review_at is not None:
                scheduler.cancel_wake(
                    company_id,
                    plan.review_at,
                    WakeReason.REVIEW_DUE,
                )
            cursor.active_attention = None
            if not self._can_wake_on_day(wake_at, scheduler.now.day):
                continue
            scheduler.schedule_wake(
                company_id,
                wake_at,
                WakeReason.PRICE_ALERT,
                source=self._turn_reference(plan.source_turn_id),
                reference_ids=self._alert_reference_ids(match),
            )

    def _can_wake_on_day(self, at: SimTime, day: int) -> bool:
        """Return whether Agents may still act on this simulation day."""
        return at.day == day and at.minute_of_day < self.scenario.runtime.close_minute

    @staticmethod
    def _turn_reference(turn_id: str) -> JournalEntryReference:
        """Return the canonical causal pointer to one Agent turn."""
        return JournalEntryReference(
            entry_id=turn_id,
            entry_type=JournalEntryKind.TURN,
        )

    @staticmethod
    def _alert_reference_ids(match: AttentionMatch) -> tuple[str, ...]:
        """Build stable opaque identities for the matched rules."""
        return tuple(
            "alert_"
            + hashlib.sha256(
                f"{match.source_turn_id}|{alert.model_dump_json()}".encode()
            ).hexdigest()[:16]
            for alert in match.matched_alerts
        )

    def _enforce_turn_limit(
        self,
        run_id: str,
        economy: EconomyState,
        wakes: tuple[ScheduledEvent, ...],
        counts: Mapping[tuple[int, str], int],
        audited: set[tuple[int, CompanyId]],
        cursors: Mapping[str, _Cursor],
        scheduler: Scheduler,
        next_journal_sequence: int,
    ) -> tuple[tuple[ScheduledEvent, ...], tuple[SystemStepRecord, ...], int]:
        """Admit bounded turns and backfill any missing limit audit."""
        eligible: list[ScheduledEvent] = []
        steps: list[SystemStepRecord] = []
        limit = self.scenario.runtime.max_turns_per_company_day
        for wake in wakes:
            company_id = wake.company_id
            if company_id is None:
                raise ValueError("wake event is missing company_id")
            key = (economy.day, company_id)
            if counts.get(key, 0) < limit:
                eligible.append(wake)
                continue
            cursors[company_id].active_attention = None
            if key not in audited:
                steps.append(
                    self._turn_limit_step(
                        run_id,
                        economy,
                        wake.at,
                        company_id,
                        next_journal_sequence,
                    )
                )
                next_journal_sequence += 1
                audited.add(key)
            step = self._suppressed_wake_step(
                run_id,
                economy,
                wake,
                next_journal_sequence,
            )
            next_journal_sequence += 1
            steps.append(step)
        return tuple(eligible), tuple(steps), next_journal_sequence

    @staticmethod
    def _turn_limit_step(
        run_id: str,
        economy: EconomyState,
        at: SimTime,
        company_id: CompanyId,
        journal_sequence: int,
    ) -> SystemStepRecord:
        """Create one state-neutral audit marker for a daily hard cap."""
        event_id = f"d{economy.day}.turn_limit.{company_id}"
        return EpisodeRuntime._agent_audit_step(
            run_id,
            economy,
            at,
            company_id,
            event_id,
            kind=SystemEventKind.TURN_LIMIT_REACHED,
            journal_sequence=journal_sequence,
        )

    @staticmethod
    def _suppressed_wake_step(
        run_id: str,
        economy: EconomyState,
        wake: ScheduledEvent,
        journal_sequence: int,
    ) -> SystemStepRecord:
        """Persist one causal Wake rejected by the daily Agent limit."""
        if wake.company_id is None:
            raise ValueError("wake event is missing company_id")
        event_id = f"d{economy.day}.wake_suppressed.{wake.company_id}.{wake.event_id}"
        return EpisodeRuntime._agent_audit_step(
            run_id,
            economy,
            wake.at,
            wake.company_id,
            event_id,
            kind=SystemEventKind.AGENT_WAKE_SUPPRESSED,
            journal_sequence=journal_sequence,
            signals=wake.wake_signals,
        )

    @staticmethod
    def _agent_audit_step(
        run_id: str,
        economy: EconomyState,
        at: SimTime,
        company_id: CompanyId,
        event_id: str,
        *,
        kind: SystemEventKind,
        journal_sequence: int,
        signals: tuple[WakeSignal, ...] = (),
    ) -> SystemStepRecord:
        """Build one state-neutral per-company Agent audit record."""
        return SystemStepRecord(
            run_id=run_id,
            entry_id=system_step_id(run_id, event_id),
            journal_sequence=journal_sequence,
            scheduled_event_id=event_id,
            occurred_at=at,
            kind=kind,
            company_id=company_id,
            suppressed_wake_signals=signals,
            state_version_before=economy.state_version,
            state_version_after=economy.state_version,
        )

    @staticmethod
    def _expire_attention(
        events: tuple[ScheduledEvent, ...],
        cursors: Mapping[str, _Cursor],
    ) -> None:
        """Discard same-day plans when the continuous markets close."""
        if not any(event.kind is SystemEventKind.MARKET_CLOSE for event in events):
            return
        for cursor in cursors.values():
            cursor.active_attention = None

    @staticmethod
    def _restore_cursor(
        company_id: CompanyId,
        saved: Mapping[str, CompanyRuntimeCursor],
    ) -> _Cursor:
        cursor = saved.get(company_id)
        return (
            _Cursor(
                next_turn_sequence=cursor.next_turn_sequence,
                last_visible_event_sequence=cursor.last_visible_event_sequence,
                available_at=cursor.available_at,
                active_attention=cursor.active_attention,
            )
            if cursor is not None
            else _Cursor()
        )

    @staticmethod
    def _previous_outcomes(
        turns: list[TurnRecord],
    ) -> dict[str, DecisionOutcome]:
        outcomes: dict[str, DecisionOutcome] = {}
        for record in sorted(turns, key=lambda item: item.outcome.apply_sequence):
            outcomes[record.turn.company_id] = record.outcome
        return outcomes

    @staticmethod
    def _turn_counts(
        turns: list[TurnRecord],
    ) -> dict[tuple[int, str], int]:
        counts: dict[tuple[int, str], int] = {}
        for record in turns:
            key = (record.turn.observation.day, record.turn.company_id)
            counts[key] = counts.get(key, 0) + 1
        return counts

    @staticmethod
    def _save_progress(
        store: RuntimeStore | None,
        new_records: tuple[TurnRecord, ...],
        new_system_steps: tuple[SystemStepRecord, ...],
        run_id: str,
        episode_started_at: datetime,
        economy: EconomyState,
        scheduler: Scheduler,
        agents: Mapping[str, CompanyAgent],
        turns: list[TurnRecord],
        system_steps: list[SystemStepRecord],
        events: list[EventRecord],
        snapshots: list[DaySnapshot],
        cursors: Mapping[str, _Cursor],
    ) -> None:
        if store is None:
            return
        agent_states = tuple(
            agent.checkpoint()
            for _, agent in sorted(agents.items())
            if isinstance(agent, CheckpointMemory)
        )
        checkpoint = RunCheckpoint(
            run_id=run_id,
            episode_started_at=episode_started_at,
            economy=economy,
            scheduler=scheduler.checkpoint(),
            policies=_policy_descriptors(economy.scenario, agents),
            agent_states=agent_states,
            events=tuple(events),
            snapshots=tuple(snapshots),
            cursors=tuple(
                CompanyRuntimeCursor(
                    company_id=company_id,
                    next_turn_sequence=cursor.next_turn_sequence,
                    last_visible_event_sequence=cursor.last_visible_event_sequence,
                    available_at=cursor.available_at,
                    active_attention=cursor.active_attention,
                )
                for company_id, cursor in sorted(cursors.items())
            ),
        )
        store.save_progress(new_records, new_system_steps, checkpoint)

    def _validate_recovery(
        self,
        recovery: RunRecovery,
        run_id: str,
        seed: int,
        agents: Mapping[str, CompanyAgent],
    ) -> None:
        checkpoint = recovery.checkpoint
        if checkpoint.run_id != run_id:
            raise ValueError("checkpoint belongs to another run")
        if checkpoint.economy.scenario != self.scenario:
            raise ValueError("checkpoint uses a different scenario")
        if checkpoint.economy.seed != seed:
            raise ValueError("checkpoint uses a different seed")
        if checkpoint.policies != _policy_descriptors(self.scenario, agents):
            raise ValueError("checkpoint policy metadata differs from the active Agents")
        expected = {company.company_id for company in self.scenario.companies}
        actual = {cursor.company_id for cursor in checkpoint.cursors}
        if actual != expected:
            raise ValueError("checkpoint must contain one cursor per company")

    @staticmethod
    def _append_events(
        records: list[EventRecord],
        events: tuple[DomainEvent, ...],
    ) -> None:
        first_sequence = len(records) + 1
        records.extend(
            EventRecord(sequence=first_sequence + offset, event=event)
            for offset, event in enumerate(events)
        )

    @staticmethod
    def _event_is_visible(event: DomainEvent, company_id: CompanyId) -> bool:
        if isinstance(event, TradeExecutedEvent):
            return company_id in {event.seller_id, event.buyer_id}
        if isinstance(event, CompanyEvent):
            return event.company_id == company_id
        return False

    def _validate_agent_set(self, agents: Mapping[str, CompanyAgent]) -> None:
        expected = {company.company_id for company in self.scenario.companies}
        actual = set(agents)
        if actual != expected:
            raise ValueError(
                "agents must match scenario companies; "
                f"missing={sorted(expected - actual)}, "
                f"unexpected={sorted(actual - expected)}"
            )

    def _validate_replay_source(
        self,
        agents: Mapping[str, CompanyAgent],
        seed: int,
        source: EpisodeResult | None,
    ) -> None:
        """Bind replay Agents to one matching authoritative source result."""
        replay_count = sum(agent.metadata.kind is PolicyKind.REPLAY for agent in agents.values())
        if replay_count not in {0, len(agents)}:
            raise ValueError("an episode cannot mix replay and live Agents")
        if replay_count and source is None:
            raise ValueError("replay Agents require the completed source result")
        if not replay_count and source is not None:
            raise ValueError("a replay source is valid only with replay Agents")
        if source is not None and (source.scenario != self.scenario or source.seed != seed):
            raise ValueError("replay source scenario and seed must match the runtime")
        if source is not None and {agent.metadata.source_run_id for agent in agents.values()} != {
            source.run_id
        }:
            raise ValueError("replay Agent journals must belong to the source run_id")


def _policy_descriptors(
    scenario: ScenarioSpec,
    agents: Mapping[str, CompanyAgent],
) -> tuple[PolicyDescriptor, ...]:
    """Freeze company policy identities in stable scenario order."""
    return tuple(
        PolicyDescriptor(
            company_id=company.company_id,
            **agents[company.company_id].metadata.model_dump(),
        )
        for company in scenario.companies
    )
