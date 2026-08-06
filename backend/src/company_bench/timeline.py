"""Turn-first operations timeline behind a two-method read interface."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from company_bench.codex_artifacts import (
    CodexArtifactIdentity,
    CodexArtifactStore,
    CodexArtifactView,
)
from company_bench.market_timeline import MarketTimelineProjector
from company_bench.models import (
    CompanyEvent,
    CompanyId,
    ConsumerSaleEvent,
    DomainEvent,
    EpisodeResult,
    EventRecord,
    InventoryExpiredEvent,
    ScenarioSpec,
    TradeExecutedEvent,
)
from company_bench.run_models import (
    PolicyInvocation,
    RunCheckpoint,
    RunJob,
    TokenUsage,
)
from company_bench.runtime_models import (
    Produce,
    QuoteLevelAction,
    SetQuoteLadder,
    SetRetailPrice,
    SimTime,
    SystemEventKind,
    SystemStepRecord,
    Transform,
    TurnRecord,
    TurnReplayOrigin,
    Wait,
    WakeSignal,
)
from company_bench.timeline_models import (
    AgentTraceDetail,
    AgentTracePreview,
    ArtifactStatus,
    ArtifactUnavailableReason,
    CommandDispositionSource,
    DayTimelineSummary,
    InventoryQuantityChange,
    ObservationDelta,
    ObservationFacts,
    OrderCancelledChange,
    OrderPlacedChange,
    OrderReplacedChange,
    RetailPriceChanged,
    SystemTimelineItem,
    TimelineDay,
    TimelineDetail,
    TimelineMoment,
    TimelineRunContext,
    TurnTimelineItem,
)


class TimelineNotFoundError(LookupError):
    """Raised when a run or journal entry does not exist."""


class TimelineUnsupportedError(ValueError):
    """Raised when a run predates V3 continuous-market semantics."""


class TimelineSource(Protocol):
    """Minimal persistence Interface required by the timeline projector."""

    def get(self, run_id: str) -> EpisodeResult | None:
        """Return one completed episode."""

    def get_job(self, run_id: str) -> RunJob | None:
        """Return one lifecycle record."""

    def get_checkpoint(self, run_id: str) -> RunCheckpoint | None:
        """Return one in-progress V3 checkpoint."""

    def list_turns(self, run_id: str) -> tuple[TurnRecord, ...]:
        """Return one immutable Turn journal."""

    def list_system_steps(self, run_id: str) -> tuple[SystemStepRecord, ...]:
        """Return one immutable system-step journal."""

    def list_invocations(self, run_id: str) -> tuple[PolicyInvocation, ...]:
        """Return physical model calls for one run."""


@dataclass(frozen=True, slots=True)
class _TimelineData:
    """Loaded authoritative facts shared by both projector methods."""

    scenario: ScenarioSpec
    result: EpisodeResult | None
    turns: tuple[TurnRecord, ...]
    system_steps: tuple[SystemStepRecord, ...]
    current_invocations: tuple[PolicyInvocation, ...]
    trace_invocations: tuple[PolicyInvocation, ...]
    trace_by_turn: dict[str, tuple[PolicyInvocation, ...]]
    origins: dict[str, TurnReplayOrigin]
    context: TimelineRunContext


@dataclass(frozen=True, slots=True)
class _ArtifactResolution:
    """One optional trace-artifact lookup without leaking storage failures."""

    view: CodexArtifactView | None
    unavailable_reason: ArtifactUnavailableReason | None

    @classmethod
    def available(cls, view: CodexArtifactView) -> _ArtifactResolution:
        """Build a successful artifact resolution."""
        return cls(view=view, unavailable_reason=None)

    @classmethod
    def unavailable(
        cls,
        reason: ArtifactUnavailableReason,
    ) -> _ArtifactResolution:
        """Build a typed unavailable artifact resolution."""
        return cls(view=None, unavailable_reason=reason)


class RunTimelineProjector:
    """Project journals, replay lineage, and traces into one operations view."""

    def __init__(
        self,
        source: TimelineSource,
        artifacts: CodexArtifactStore | None = None,
    ) -> None:
        self._source = source
        self._artifacts = artifacts
        self._market = MarketTimelineProjector()

    def read_day(self, run_id: str, day: int) -> TimelineDay:
        """Return one 1-based simulation day with run-wide summaries."""
        data = self._load(run_id)
        if not 1 <= day <= data.scenario.days:
            raise ValueError(f"day must be between 1 and {data.scenario.days}")
        selected_turn_records = tuple(
            record for record in data.turns if record.turn.sim_time.day + 1 == day
        )
        selected_step_records = tuple(
            record for record in data.system_steps if record.occurred_at.day + 1 == day
        )
        selected_turns = self._turn_items(
            data,
            selected_turn_records,
        )
        selected_steps = self._system_items(selected_step_records)
        minutes = tuple(
            sorted(
                {
                    *(item.sim_time.absolute_minute for item in selected_turns),
                    *(item.sim_time.absolute_minute for item in selected_steps),
                }
            )
        )
        market_by_minute = self._market.project_day(
            data.scenario,
            day,
            selected_turn_records,
            selected_step_records,
            minutes,
        )
        return TimelineDay(
            context=data.context,
            selected_day=day,
            day_summaries=self._day_summaries(data),
            moments=tuple(
                TimelineMoment(
                    sim_time=SimTime(absolute_minute=minute),
                    system_steps=tuple(
                        item for item in selected_steps if item.sim_time.absolute_minute == minute
                    ),
                    turns=tuple(
                        item for item in selected_turns if item.sim_time.absolute_minute == minute
                    ),
                    market=market_by_minute[minute],
                )
                for minute in minutes
            ),
        )

    def read_detail(self, run_id: str, entry_id: str) -> TimelineDetail:
        """Return expanded facts and public trace material for one entry."""
        data = self._load(run_id)
        turn_by_id = {record.turn.turn_id: record for record in data.turns}
        record = turn_by_id.get(entry_id)
        if record is not None:
            item = self._turn_item(
                data,
                record,
                self._previous_turns(data.turns).get(entry_id),
            )
            return TimelineDetail(
                context=data.context,
                item=item,
                turn=record,
                traces=tuple(
                    self._trace_detail(invocation, preview)
                    for invocation, preview in self._trace_pairs(data, record)
                ),
            )
        step = next(
            (candidate for candidate in data.system_steps if candidate.entry_id == entry_id),
            None,
        )
        system_item = self._system_item(step) if step is not None else None
        if system_item is None:
            raise TimelineNotFoundError(
                f"timeline entry '{entry_id}' was not found in run '{run_id}'"
            )
        return TimelineDetail(
            context=data.context,
            item=system_item,
            system_step=step,
        )

    def _load(self, run_id: str) -> _TimelineData:
        result = self._source.get(run_id)
        job = self._source.get_job(run_id)
        checkpoint = self._source.get_checkpoint(run_id)
        if result is None and job is None and checkpoint is None:
            raise TimelineNotFoundError(f"run '{run_id}' was not found")
        scenario = (
            result.scenario
            if result is not None
            else checkpoint.economy.scenario
            if checkpoint is not None
            else None
        )
        if scenario is None:
            raise TimelineNotFoundError(f"run '{run_id}' has no readable scenario")
        if not scenario.uses_event_runtime:
            raise TimelineUnsupportedError("The operations timeline is available for V3 runs only")

        turns = self._source.list_turns(run_id)
        system_steps = tuple(
            sorted(
                self._source.list_system_steps(run_id),
                key=lambda step: (
                    step.occurred_at.absolute_minute,
                    step.journal_sequence,
                    step.entry_id,
                ),
            )
        )

        source_run_id = (
            job.source_run_id
            if job is not None
            else next(
                (
                    policy.source_run_id
                    for policy in (result.policies if result is not None else ())
                    if policy.source_run_id is not None
                ),
                None,
            )
        )
        current_invocations = self._source.list_invocations(run_id)
        trace_run_id, origins = self._resolve_lineage(
            run_id,
            turns,
            source_run_id,
            complete=result is not None,
        )
        trace_invocations = (
            self._source.list_invocations(trace_run_id)
            if trace_run_id != run_id
            else current_invocations
        )
        trace_lists: dict[str, list[PolicyInvocation]] = defaultdict(list)
        for invocation in trace_invocations:
            if invocation.domain_turn_id is not None:
                trace_lists[invocation.domain_turn_id].append(invocation)
        mode = (
            job.mode.value
            if job is not None
            else next(
                (policy.kind.value for policy in result.policies),
                "unknown",
            )
            if result is not None
            else "unknown"
        )
        context = TimelineRunContext(
            run_id=run_id,
            scenario_id=scenario.scenario_id,
            scenario_version=scenario.version,
            total_days=scenario.days,
            mode=mode,
            source_run_id=source_run_id,
            trace_run_id=trace_run_id,
            replay=source_run_id is not None,
            model_call_count=len(current_invocations),
            source_model_call_count=len(trace_invocations),
            current_usage=_sum_usage(current_invocations),
            source_usage=_sum_usage(trace_invocations),
            checkpoint_at=checkpoint.scheduler.now if checkpoint is not None else None,
            checkpoint_state_version=(
                checkpoint.economy.state_version if checkpoint is not None else None
            ),
        )
        return _TimelineData(
            scenario=scenario,
            result=result,
            turns=turns,
            system_steps=system_steps,
            current_invocations=current_invocations,
            trace_invocations=trace_invocations,
            trace_by_turn={
                turn_id: tuple(invocations) for turn_id, invocations in trace_lists.items()
            },
            origins=origins,
            context=context,
        )

    def _turn_items(
        self,
        data: _TimelineData,
        records: tuple[TurnRecord, ...],
    ) -> tuple[TurnTimelineItem, ...]:
        previous_by_turn = self._previous_turns(data.turns)
        return tuple(
            self._turn_item(data, record, previous_by_turn.get(record.turn.turn_id))
            for record in records
        )

    def _turn_item(
        self,
        data: _TimelineData,
        record: TurnRecord,
        previous: TurnRecord | None,
    ) -> TurnTimelineItem:
        observation = record.turn.observation
        origin = data.origins.get(record.turn.turn_id) or record.replay_origin
        signals = record.turn.wake_signals or tuple(
            WakeSignal(reason=reason) for reason in record.turn.wake_reasons
        )
        return TurnTimelineItem(
            entry_id=record.turn.turn_id,
            sim_time=record.turn.sim_time,
            company_id=record.turn.company_id,
            company_name=data.scenario.company(record.turn.company_id).name,
            tier=data.scenario.company(record.turn.company_id).tier,
            state_version=record.turn.state_version,
            apply_sequence=record.outcome.apply_sequence,
            journal_sequence=record.journal_sequence,
            turn_number_today=record.turn.turn_number_today,
            turn_limit_today=record.turn.turn_limit_today,
            wake_signals=signals,
            observation=ObservationFacts(
                cash=record.turn.available_cash,
                reserved_cash=record.turn.reserved_cash,
                marked_surplus=record.turn.marked_surplus,
                inventory=observation.inventory,
                inventory_expiry=record.turn.inventory_expiry,
                retail_price=observation.retail_price,
                open_orders=record.turn.open_orders,
                order_books=record.turn.order_books,
                pending_deliveries=record.turn.pending_deliveries,
                active_operation=record.turn.active_operation,
                remaining_operation_capacity=record.turn.remaining_operation_capacity,
                visible_events=record.turn.visible_events,
                visible_event_count=len(record.turn.visible_events),
            ),
            observation_delta=self._observation_delta(record, previous),
            command=record.envelope.command,
            outcome=record.outcome,
            disposition_source=_disposition_source(record),
            effects=record.outcome.events,
            state_changes=_state_changes(record),
            next_available_at=record.outcome.next_available_at,
            replay_origin=origin,
            traces=tuple(preview for _, preview in self._trace_pairs(data, record)),
            protocol_error=record.protocol_error,
            title=_command_title(record),
            summary=_turn_summary(record),
        )

    def _system_items(
        self,
        records: tuple[SystemStepRecord, ...],
    ) -> tuple[SystemTimelineItem, ...]:
        return tuple(self._system_item(step) for step in records)

    @staticmethod
    def _system_item(
        step: SystemStepRecord,
    ) -> SystemTimelineItem:
        return SystemTimelineItem(
            entry_id=step.entry_id,
            sim_time=step.occurred_at,
            kind=step.kind,
            journal_sequence=step.journal_sequence,
            state_version_before=step.state_version_before,
            state_version_after=step.state_version_after,
            reference_ids=step.reference_ids,
            suppressed_wake_signals=step.suppressed_wake_signals,
            effects=tuple(record.event for record in step.effects),
            affected_company_ids=(
                (step.company_id,)
                if step.company_id is not None
                else _affected_companies(step.effects)
            ),
            title=_system_title(step),
            summary=_system_summary(step),
        )

    def _trace_pairs(
        self,
        data: _TimelineData,
        record: TurnRecord,
    ) -> tuple[tuple[PolicyInvocation, AgentTracePreview], ...]:
        origin = data.origins.get(record.turn.turn_id) or record.replay_origin
        domain_turn_id = origin.source_turn_id if origin is not None else record.turn.turn_id
        invocations = data.trace_by_turn.get(domain_turn_id, ())
        return tuple(
            (
                invocation,
                AgentTracePreview(
                    trace_run_id=data.context.trace_run_id,
                    invocation_id=invocation.invocation_id,
                    source_trace=data.context.replay,
                    provider=invocation.provider,
                    model=invocation.model,
                    outcome=invocation.outcome,
                    usage=invocation.usage,
                    latency_ms=invocation.latency_ms,
                    attempts=invocation.attempts,
                    applied_to_committed_turn=_is_applied_invocation(
                        invocation,
                        record,
                    ),
                ),
            )
            for invocation in invocations
        )

    def _trace_detail(
        self,
        invocation: PolicyInvocation,
        preview: AgentTracePreview,
    ) -> AgentTraceDetail:
        artifact = self._artifact(invocation)
        return AgentTraceDetail(
            preview=preview,
            artifact_status=(
                ArtifactStatus.AVAILABLE
                if artifact.view is not None
                else ArtifactStatus.UNAVAILABLE
            ),
            artifact_unavailable_reason=artifact.unavailable_reason,
            reasoning_markdown=(
                artifact.view.reasoning_markdown if artifact.view is not None else None
            ),
            final_output=artifact.view.final_output if artifact.view is not None else None,
        )

    def _artifact(self, invocation: PolicyInvocation) -> _ArtifactResolution:
        if invocation.provider != "codex":
            return _ArtifactResolution.unavailable(ArtifactUnavailableReason.PROVIDER_NOT_SUPPORTED)
        identity = self._artifact_identity(invocation)
        if identity is None:
            return _ArtifactResolution.unavailable(ArtifactUnavailableReason.IDENTITY_UNAVAILABLE)
        if self._artifacts is None:
            return _ArtifactResolution.unavailable(ArtifactUnavailableReason.STORE_NOT_CONFIGURED)
        try:
            view = self._artifacts.read(identity)
        except (OSError, UnicodeError):
            return _ArtifactResolution.unavailable(ArtifactUnavailableReason.READ_ERROR)
        if view is None:
            return _ArtifactResolution.unavailable(ArtifactUnavailableReason.NOT_FOUND)
        return _ArtifactResolution.available(view)

    @staticmethod
    def _artifact_identity(
        invocation: PolicyInvocation,
    ) -> CodexArtifactIdentity | None:
        if (
            invocation.provider != "codex"
            or invocation.request_id is None
            or (invocation.provider_turn_id or invocation.response_id) is None
        ):
            return None
        return CodexArtifactIdentity(
            invocation_id=invocation.invocation_id,
            run_id=invocation.run_id,
            company_id=invocation.company_id,
            day=invocation.day,
            model=invocation.model,
            thread_id=invocation.request_id,
            turn_id=invocation.provider_turn_id or invocation.response_id,
            domain_turn_id=invocation.domain_turn_id,
        )

    @staticmethod
    def _previous_turns(turns: tuple[TurnRecord, ...]) -> dict[str, TurnRecord]:
        previous_by_company: dict[str, TurnRecord] = {}
        previous_by_turn: dict[str, TurnRecord] = {}
        for record in turns:
            previous = previous_by_company.get(record.turn.company_id)
            if previous is not None:
                previous_by_turn[record.turn.turn_id] = previous
            previous_by_company[record.turn.company_id] = record
        return previous_by_turn

    @staticmethod
    def _observation_delta(
        record: TurnRecord,
        previous: TurnRecord | None,
    ) -> ObservationDelta:
        current = record.turn.observation
        prior = previous.turn.observation if previous is not None else None
        products = tuple(product.product for product in current.products)
        return ObservationDelta(
            cash_before=prior.cash if prior is not None else None,
            cash_after=current.cash,
            cash_change=current.cash - prior.cash if prior is not None else None,
            inventory=tuple(
                InventoryQuantityChange(
                    product=product,
                    before=prior.quantity(product) if prior is not None else None,
                    after=current.quantity(product),
                    change=(
                        current.quantity(product) - prior.quantity(product)
                        if prior is not None
                        else None
                    ),
                )
                for product in products
            ),
            retail_price_before=prior.retail_price if prior is not None else None,
            retail_price_after=current.retail_price,
            open_order_count_before=(
                len(previous.turn.open_orders) if previous is not None else None
            ),
            open_order_count_after=len(record.turn.open_orders),
        )

    def _day_summaries(
        self,
        data: _TimelineData,
    ) -> tuple[DayTimelineSummary, ...]:
        all_events = (
            tuple(record.event for record in data.result.events)
            if data.result is not None
            else tuple(
                (
                    *(event for turn in data.turns for event in turn.outcome.events),
                    *(record.event for step in data.system_steps for record in step.effects),
                )
            )
        )
        return tuple(
            _day_summary(day, data.turns, data.system_steps, all_events)
            for day in range(1, data.scenario.days + 1)
        )

    def _resolve_lineage(
        self,
        run_id: str,
        turns: tuple[TurnRecord, ...],
        source_run_id: str | None,
        *,
        complete: bool,
        visited: frozenset[str] = frozenset(),
    ) -> tuple[str, dict[str, TurnReplayOrigin]]:
        if source_run_id is None:
            return run_id, {}
        if run_id in visited or source_run_id in visited:
            raise ValueError("replay source lineage contains a cycle")
        direct = self._direct_origins(turns, source_run_id, complete=complete)
        source_turns = self._source.list_turns(source_run_id)
        source_source_run_id = self._source_run_id(source_run_id)
        if source_source_run_id is None:
            return source_run_id, direct
        trace_run_id, source_origins = self._resolve_lineage(
            source_run_id,
            source_turns,
            source_source_run_id,
            complete=True,
            visited=visited | {run_id},
        )
        return (
            trace_run_id,
            {
                turn_id: source_origins.get(origin.source_turn_id, origin)
                for turn_id, origin in direct.items()
            },
        )

    def _direct_origins(
        self,
        turns: tuple[TurnRecord, ...],
        source_run_id: str,
        *,
        complete: bool,
    ) -> dict[str, TurnReplayOrigin]:
        """Resolve one replay edge, failing closed on any legacy drift."""
        source_turns = self._source.list_turns(source_run_id)
        source_by_id = {record.turn.turn_id: record for record in source_turns}
        origins: dict[str, TurnReplayOrigin] = {}
        legacy_turns: list[TurnRecord] = []
        for current in turns:
            explicit = current.replay_origin
            if explicit is None:
                legacy_turns.append(current)
                continue
            if explicit.source_run_id != source_run_id:
                raise ValueError("persisted replay origin belongs to another source run")
            source = source_by_id.get(explicit.source_turn_id)
            if source is None:
                raise ValueError("persisted replay origin source Turn was not found")
            _require_replay_pair(current, source)
            origins[current.turn.turn_id] = explicit

        source_by_company: dict[str, list[TurnRecord]] = defaultdict(list)
        current_by_company: dict[str, list[TurnRecord]] = defaultdict(list)
        for record in source_turns:
            source_by_company[record.turn.company_id].append(record)
        for record in legacy_turns:
            current_by_company[record.turn.company_id].append(record)
        if complete and set(source_by_company) != {record.turn.company_id for record in turns}:
            raise ValueError("replay source and current company journals differ")
        if complete:
            for company_id, source_records in source_by_company.items():
                current_count = sum(record.turn.company_id == company_id for record in turns)
                if current_count != len(source_records):
                    raise ValueError(f"replay source turn count differs for company '{company_id}'")
        for company_id, current_records in current_by_company.items():
            source_records = source_by_company.get(company_id)
            if source_records is None or len(current_records) > len(source_records):
                raise ValueError(f"replay source turn count differs for company '{company_id}'")
            for current, source in zip(current_records, source_records, strict=False):
                _require_replay_pair(current, source)
                origins[current.turn.turn_id] = TurnReplayOrigin(
                    source_run_id=source_run_id,
                    source_turn_id=source.turn.turn_id,
                )
        return origins

    def _source_run_id(self, run_id: str) -> str | None:
        job = self._source.get_job(run_id)
        if job is not None:
            return job.source_run_id
        result = self._source.get(run_id)
        if result is None:
            return None
        source_ids = {
            policy.source_run_id for policy in result.policies if policy.source_run_id is not None
        }
        if len(source_ids) > 1:
            raise ValueError("run policies disagree on replay source")
        return next(iter(source_ids), None)


def _sum_usage(invocations: Iterable[PolicyInvocation]) -> TokenUsage:
    """Aggregate physical-call counters without estimating money."""
    usages = tuple(invocation.usage for invocation in invocations)
    return TokenUsage(
        input_tokens=sum(usage.input_tokens for usage in usages),
        cached_tokens=sum(usage.cached_tokens for usage in usages),
        output_tokens=sum(usage.output_tokens for usage in usages),
        reasoning_tokens=sum(usage.reasoning_tokens for usage in usages),
        total_tokens=sum(usage.total_tokens for usage in usages),
    )


def _require_replay_pair(current: TurnRecord, source: TurnRecord) -> None:
    """Fail closed unless one legacy replay Turn exactly reproduces its source."""
    same_outcome = current.outcome.model_dump(
        exclude={"turn_id", "command_id"}
    ) == source.outcome.model_dump(exclude={"turn_id", "command_id"})
    if (
        current.observation_hash != source.observation_hash
        or current.envelope.command != source.envelope.command
        or current.protocol_error != source.protocol_error
        or not same_outcome
    ):
        raise ValueError(f"replay lineage drift at source turn '{source.turn.turn_id}'")


def _is_applied_invocation(
    invocation: PolicyInvocation,
    record: TurnRecord,
) -> bool:
    """Identify the physical call explicitly linked to the committed outcome."""
    linked = invocation.command_outcome
    return (
        linked is not None
        and invocation.command == record.envelope.command
        and linked.apply_sequence == record.outcome.apply_sequence
        and linked.model_dump(exclude={"turn_id", "command_id"})
        == record.outcome.model_dump(exclude={"turn_id", "command_id"})
    )


def _day_summary(
    day: int,
    turns: tuple[TurnRecord, ...],
    steps: tuple[SystemStepRecord, ...],
    events: tuple[DomainEvent, ...],
) -> DayTimelineSummary:
    day_turns = tuple(item for item in turns if item.turn.sim_time.day + 1 == day)
    day_steps = tuple(item for item in steps if item.occurred_at.day + 1 == day)
    day_events = tuple(event for event in events if event.day == day)
    return DayTimelineSummary(
        day=day,
        turn_count=len(day_turns),
        accepted_count=sum(item.outcome.accepted for item in day_turns),
        rejected_count=sum(not item.outcome.accepted for item in day_turns),
        wait_count=sum(isinstance(item.envelope.command, Wait) for item in day_turns),
        system_step_count=len(day_steps),
        event_count=len(day_events),
        trade_quantity=sum(
            (event.quantity for event in day_events if isinstance(event, TradeExecutedEvent)),
            Decimal(),
        ),
        consumer_sales=sum(
            (event.sold_quantity for event in day_events if isinstance(event, ConsumerSaleEvent)),
            Decimal(),
        ),
        expired_quantity=sum(
            (event.quantity for event in day_events if isinstance(event, InventoryExpiredEvent)),
            Decimal(),
        ),
    )


def _affected_companies(records: tuple[EventRecord, ...]) -> tuple[CompanyId, ...]:
    companies: list[CompanyId] = []
    for record in records:
        event = record.event
        if isinstance(event, TradeExecutedEvent):
            companies.extend((event.seller_id, event.buyer_id))
        elif isinstance(event, CompanyEvent):
            companies.append(event.company_id)
    return tuple(dict.fromkeys(companies))


def _command_title(record: TurnRecord) -> str:
    command = record.envelope.command
    if isinstance(command, Produce):
        return f"Produce {command.product.value.replace('_', ' ')}"
    if isinstance(command, Transform):
        return (
            f"Transform {command.input_product.value.replace('_', ' ')} "
            f"into {command.output_product.value.replace('_', ' ')}"
        )
    if isinstance(command, SetQuoteLadder):
        return (
            f"Set {command.side.value} quote ladder for {command.product.value.replace('_', ' ')}"
        )
    if isinstance(command, SetRetailPrice):
        return f"Set retail price for {command.product.value.replace('_', ' ')}"
    return "Wait"


def _disposition_source(record: TurnRecord) -> CommandDispositionSource:
    """Identify the module that produced the persisted command disposition."""
    if record.protocol_error is not None:
        return CommandDispositionSource.RUNTIME_PROTOCOL
    if not record.outcome.accepted and isinstance(record.envelope.command, Wait):
        return CommandDispositionSource.RUNTIME_ATTENTION
    return CommandDispositionSource.ECONOMIC_ENGINE


def _turn_summary(record: TurnRecord) -> str:
    outcome = record.outcome
    if not outcome.accepted:
        return f"Rejected: {outcome.reason}"
    effect_count = len(outcome.events)
    change_count = len(_state_changes(record))
    if effect_count:
        suffix = "effect" if effect_count == 1 else "effects"
        return f"Accepted - {effect_count} economic {suffix}"
    if change_count:
        suffix = "change" if change_count == 1 else "changes"
        return f"Accepted - {change_count} state {suffix}"
    return "Accepted - no economic state change"


def _state_changes(
    record: TurnRecord,
) -> tuple[
    OrderPlacedChange | OrderCancelledChange | OrderReplacedChange | RetailPriceChanged,
    ...,
]:
    """Project accepted non-event mutations from the committed command."""
    if not record.outcome.accepted:
        return ()
    command = record.envelope.command
    if isinstance(command, SetQuoteLadder):
        result = record.outcome.quote_ladder_result
        if result is None:
            return ()
        changes: list[OrderPlacedChange | OrderCancelledChange | OrderReplacedChange] = []
        for level in result.levels:
            if level.action is QuoteLevelAction.PLACE:
                changes.append(
                    OrderPlacedChange(
                        order_id=level.order_id,
                        side=command.side,
                        product=command.product,
                        quantity=level.level.quantity,
                        limit_price=level.level.limit_price,
                    )
                )
            elif level.action is QuoteLevelAction.REPLACE and level.replaced_order_id is not None:
                changes.append(
                    OrderReplacedChange(
                        replaced_order_id=level.replaced_order_id,
                        order_id=level.order_id,
                        quantity=level.level.quantity,
                        limit_price=level.level.limit_price,
                    )
                )
        changes.extend(
            OrderCancelledChange(order_id=order_id) for order_id in result.cancelled_order_ids
        )
        return tuple(changes)
    if isinstance(command, SetRetailPrice):
        return (
            RetailPriceChanged(
                product=command.product,
                before=record.turn.observation.retail_price,
                after=command.unit_price,
            ),
        )
    return ()


def _system_title(step: SystemStepRecord) -> str:
    return {
        SystemEventKind.DAY_OPEN: "Continuous markets opened",
        SystemEventKind.OPERATION_COMPLETED: "Operation completed",
        SystemEventKind.DELIVERY_COMPLETED: "Delivery completed",
        SystemEventKind.MARKET_CLOSE: "Continuous markets closed",
        SystemEventKind.CONSUMER_SALES: "Consumer sales settled",
        SystemEventKind.DAY_CLOSE: "Simulation day closed",
        SystemEventKind.TURN_LIMIT_REACHED: "Daily Agent turn limit reached",
        SystemEventKind.AGENT_WAKE_SUPPRESSED: "Agent wake suppressed",
    }[step.kind]


def _system_summary(step: SystemStepRecord) -> str:
    effect_count = len(step.effects)
    if step.kind is SystemEventKind.DAY_OPEN:
        return "Companies may trade and start operations from 09:00"
    if step.kind is SystemEventKind.MARKET_CLOSE:
        return "Resting DAY orders were cancelled and reserved assets released"
    if step.kind is SystemEventKind.CONSUMER_SALES:
        return f"{effect_count} consumer settlement effect{'s' if effect_count != 1 else ''}"
    if step.kind is SystemEventKind.DAY_CLOSE:
        return f"{effect_count} expiry effect{'s' if effect_count != 1 else ''}"
    if step.kind is SystemEventKind.TURN_LIMIT_REACHED:
        return f"{step.company_id} used its complete daily Agent turn budget"
    if step.kind is SystemEventKind.AGENT_WAKE_SUPPRESSED:
        reasons = ", ".join(signal.reason.value for signal in step.suppressed_wake_signals)
        return f"{step.company_id} was not called at the daily limit: {reasons}"
    reference = "" if not step.reference_ids else f" for {', '.join(step.reference_ids)}"
    return f"{effect_count} economic effect{'s' if effect_count != 1 else ''}{reference}"
