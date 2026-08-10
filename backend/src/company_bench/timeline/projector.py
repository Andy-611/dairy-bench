"""Turn-first operations timeline behind a two-method read interface."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from company_bench.domain.models import (
    CompanyEvent,
    CompanyId,
    ConsumerSaleEvent,
    DaySnapshot,
    DomainEvent,
    EpisodeResult,
    EventRecord,
    InventoryExpiredEvent,
    ScenarioSpec,
    TradeExecutedEvent,
)
from company_bench.domain.precision import EconomicPrecision
from company_bench.runs.models import (
    PolicyInvocation,
    RunJob,
    RunRecovery,
    RunStatus,
    TokenUsage,
)
from company_bench.runtime.attention import AgentAttention
from company_bench.runtime.models import (
    IdleDecision,
    Produce,
    QuoteLevelAction,
    RejectionCategory,
    SetQuoteLadder,
    SetRetailPrice,
    SimTime,
    SystemEventKind,
    SystemStepRecord,
    Transform,
    TurnRecord,
    TurnReplayOrigin,
    WakeSignal,
)
from company_bench.timeline.market import MarketTimelineProjector
from company_bench.timeline.models import (
    AgentTraceDetail,
    AgentTracePreview,
    DayTimelineSummary,
    DecisionDispositionSource,
    InventoryQuantityChange,
    ObservationDelta,
    ObservationFacts,
    OrderCancelledChange,
    OrderPlacedChange,
    OrderReplacedChange,
    RetailPriceChanged,
    RunDiagnostics,
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
    """Raised when a run predates V4 continuous-market semantics."""


class TimelineSource(Protocol):
    """Minimal persistence Interface required by the timeline projector."""

    def get(self, run_id: str) -> EpisodeResult | None:
        """Return one completed episode."""

    def get_job(self, run_id: str) -> RunJob | None:
        """Return one lifecycle record."""

    def load_recovery(self, run_id: str) -> RunRecovery | None:
        """Return one atomic recovery aggregate when the run is incomplete."""

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


class RunTimelineProjector:
    """Project journals, replay lineage, and traces into one operations view."""

    def __init__(self, source: TimelineSource) -> None:
        self._source = source
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
                    AgentTraceDetail(preview=preview)
                    for _, preview in self._trace_pairs(data, record)
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
        recovery = self._source.load_recovery(run_id)
        checkpoint = recovery.checkpoint if recovery is not None else None
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
        turns = recovery.turns if recovery is not None else self._source.list_turns(run_id)
        stored_system_steps = (
            recovery.system_steps
            if recovery is not None
            else self._source.list_system_steps(run_id)
        )
        system_steps = tuple(
            sorted(
                stored_system_steps,
                key=lambda step: (
                    step.occurred_at.absolute_minute,
                    step.journal_sequence,
                    step.entry_id,
                ),
            )
        )

        source_run_id = job.source_run_id if job is not None else None
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
        snapshots = (
            result.snapshots
            if result is not None
            else checkpoint.snapshots
            if checkpoint is not None
            else ()
        )
        event_records = (
            result.events
            if result is not None
            else checkpoint.events
            if checkpoint is not None
            else ()
        )
        context = TimelineRunContext(
            run_id=run_id,
            scenario_id=scenario.scenario_id,
            scenario_version=scenario.version,
            total_days=scenario.days,
            status=job.status if job is not None else RunStatus.COMPLETED,
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
            diagnostics=_run_diagnostics(
                scenario,
                result,
                turns,
                snapshots,
                event_records,
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
            decision=record.envelope.decision,
            outcome=record.outcome,
            disposition_source=_disposition_source(record),
            effects=record.outcome.events,
            state_changes=_state_changes(record),
            next_available_at=record.outcome.next_available_at,
            review_at=_review_at(record),
            replay_origin=origin,
            traces=tuple(preview for _, preview in self._trace_pairs(data, record)),
            protocol_error=record.protocol_error,
            title=_decision_title(record),
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
        """Resolve one replay edge from explicit persisted origins only."""
        source_turns = self._source.list_turns(source_run_id)
        source_by_id = {record.turn.turn_id: record for record in source_turns}
        origins: dict[str, TurnReplayOrigin] = {}
        for current in turns:
            explicit = current.replay_origin
            if explicit is None:
                raise ValueError("replay Turn is missing its persisted source origin")
            if explicit.source_run_id != source_run_id:
                raise ValueError("persisted replay origin belongs to another source run")
            source = source_by_id.get(explicit.source_turn_id)
            if source is None:
                raise ValueError("persisted replay origin source Turn was not found")
            _require_replay_pair(current, source)
            origins[current.turn.turn_id] = explicit
        source_ids = [origin.source_turn_id for origin in origins.values()]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("replay Turns cannot reuse one source Turn")
        if complete and set(source_ids) != set(source_by_id):
            raise ValueError("replay source and current journals differ")
        return origins

    def _source_run_id(self, run_id: str) -> str | None:
        job = self._source.get_job(run_id)
        return job.source_run_id if job is not None else None


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
    """Fail closed unless one replay Turn exactly reproduces its source."""
    same_outcome = current.outcome.model_dump(
        exclude={"turn_id", "decision_id"}
    ) == source.outcome.model_dump(exclude={"turn_id", "decision_id"})
    if (
        current.observation_hash != source.observation_hash
        or current.envelope.decision != source.envelope.decision
        or current.protocol_error != source.protocol_error
        or not same_outcome
    ):
        raise ValueError(f"replay lineage drift at source turn '{source.turn.turn_id}'")


def _is_applied_invocation(
    invocation: PolicyInvocation,
    record: TurnRecord,
) -> bool:
    """Identify the physical call explicitly linked to the committed outcome."""
    linked = invocation.decision_outcome
    return (
        linked is not None
        and invocation.decision == record.envelope.decision
        and linked.apply_sequence == record.outcome.apply_sequence
        and linked.model_dump(exclude={"turn_id", "decision_id"})
        == record.outcome.model_dump(exclude={"turn_id", "decision_id"})
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
        idle_count=sum(isinstance(item.envelope.decision, IdleDecision) for item in day_turns),
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


def _run_diagnostics(
    scenario: ScenarioSpec,
    result: EpisodeResult | None,
    turns: tuple[TurnRecord, ...],
    snapshots: tuple[DaySnapshot, ...],
    event_records: tuple[EventRecord, ...],
) -> RunDiagnostics:
    """Derive protocol and market-health facts from authoritative journals."""
    trade_days = tuple(
        record.event.day for record in event_records if isinstance(record.event, TradeExecutedEvent)
    )
    completed_days = len(snapshots)
    demand = sum((snapshot.consumer_demand for snapshot in snapshots), Decimal())
    sales = sum((snapshot.consumer_sales for snapshot in snapshots), Decimal())
    fill_rate = EconomicPrecision.round(sales / demand) if demand else Decimal()
    initial_cash = {company.company_id: company.initial_cash for company in scenario.companies}
    latest_value = (
        {company.company_id: company.net_worth for company in snapshots[-1].companies}
        if snapshots
        else {
            record.turn.company_id: EconomicPrecision.round(
                record.turn.marked_surplus + initial_cash[record.turn.company_id]
            )
            for record in turns
        }
    )
    near_insolvent = tuple(
        company.company_id
        for company in scenario.companies
        if latest_value.get(company.company_id, company.initial_cash)
        <= EconomicPrecision.round(company.initial_cash * Decimal("0.01"))
    )
    return RunDiagnostics(
        completed_days=completed_days,
        benchmark_eligible=(result.quality.benchmark_eligible if result is not None else None),
        protocol_invalid_turns=(
            result.quality.protocol.invalid_turn_count
            if result is not None
            else sum(record.protocol_error is not None for record in turns)
        ),
        economic_rejections=sum(
            record.outcome.rejection_category is RejectionCategory.ECONOMIC for record in turns
        ),
        attention_rejections=sum(
            record.outcome.rejection_category is RejectionCategory.ATTENTION for record in turns
        ),
        trade_count=len(trade_days),
        last_trade_day=max(trade_days, default=None),
        zero_trade_day_streak=(
            max(0, completed_days - max(trade_days)) if trade_days else completed_days
        ),
        consumer_demand=demand,
        consumer_sales=sales,
        consumer_fill_rate=fill_rate,
        expired_quantity=sum(
            (snapshot.expired_quantity for snapshot in snapshots),
            Decimal(),
        ),
        near_insolvent_company_ids=near_insolvent,
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


def _decision_title(record: TurnRecord) -> str:
    action = record.envelope.action
    if isinstance(action, Produce):
        return f"Produce {action.product.value.replace('_', ' ')}"
    if isinstance(action, Transform):
        return (
            f"Transform {action.input_product.value.replace('_', ' ')} "
            f"into {action.output_product.value.replace('_', ' ')}"
        )
    if isinstance(action, SetQuoteLadder):
        return f"Set {action.side.value} quote ladder for {action.product.value.replace('_', ' ')}"
    if isinstance(action, SetRetailPrice):
        return f"Set retail price for {action.product.value.replace('_', ' ')}"
    return "Idle"


def _review_at(record: TurnRecord) -> SimTime | None:
    """Rebuild the installed fallback review for an accepted decision."""
    if not record.outcome.accepted:
        return None
    return AgentAttention().arm(record.envelope.decision.attention, record.turn).review_at


def _disposition_source(record: TurnRecord) -> DecisionDispositionSource:
    """Identify the module that produced the persisted decision disposition."""
    if record.outcome.rejection_category is RejectionCategory.PROTOCOL:
        return DecisionDispositionSource.RUNTIME_PROTOCOL
    if record.outcome.rejection_category is RejectionCategory.ATTENTION:
        return DecisionDispositionSource.RUNTIME_ATTENTION
    return DecisionDispositionSource.ECONOMIC_ENGINE


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
    """Project accepted non-event mutations from the committed action."""
    if not record.outcome.accepted:
        return ()
    action = record.envelope.action
    if isinstance(action, SetQuoteLadder):
        result = record.outcome.quote_ladder_result
        if result is None:
            return ()
        changes: list[OrderPlacedChange | OrderCancelledChange | OrderReplacedChange] = []
        for level in result.levels:
            if level.action is QuoteLevelAction.PLACE:
                changes.append(
                    OrderPlacedChange(
                        order_id=level.order_id,
                        side=action.side,
                        product=action.product,
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
    if isinstance(action, SetRetailPrice):
        return (
            RetailPriceChanged(
                product=action.product,
                before=record.turn.observation.retail_price,
                after=action.unit_price,
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
