"""Read models for the Turn-first operations timeline."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from company_bench.models import (
    CompanyId,
    CompanyTier,
    DomainEvent,
    Identifier,
    InventoryPosition,
    Money,
    ProductId,
    StrictModel,
)
from company_bench.run_models import InvocationOutcome, TokenUsage
from company_bench.runtime_models import (
    CommandOutcome,
    CompanyCommand,
    MarketSide,
    OpenOrderView,
    SimTime,
    SystemEventKind,
    SystemStepRecord,
    TurnRecord,
    TurnReplayOrigin,
    WakeSignal,
)


class TimelineRunContext(StrictModel):
    """Run identity and trace provenance shown above the timeline."""

    run_id: Identifier
    scenario_id: Identifier
    scenario_version: int = Field(ge=2)
    total_days: int = Field(ge=1)
    mode: str
    source_run_id: Identifier | None = None
    trace_run_id: Identifier
    replay: bool
    model_call_count: int = Field(ge=0)
    source_model_call_count: int = Field(ge=0)
    current_usage: TokenUsage
    source_usage: TokenUsage


class DayTimelineSummary(StrictModel):
    """Compact activity and economic totals for one simulation day."""

    day: int = Field(ge=1)
    turn_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    wait_count: int = Field(ge=0)
    system_step_count: int = Field(ge=0)
    event_count: int = Field(ge=0)
    trade_quantity: Decimal = Field(ge=0)
    consumer_sales: Decimal = Field(ge=0)
    expired_quantity: Decimal = Field(ge=0)


class InventoryQuantityChange(StrictModel):
    """One product quantity before and at the current observation."""

    product: ProductId
    before: Decimal | None = Field(default=None, ge=0)
    after: Decimal = Field(ge=0)
    change: Decimal | None = None


class ObservationFacts(StrictModel):
    """Decision-relevant current facts without opaque dictionaries."""

    cash: Money
    inventory: tuple[InventoryPosition, ...]
    retail_price: Money | None = None
    open_orders: tuple[OpenOrderView, ...] = ()
    visible_events: tuple[DomainEvent, ...] = ()
    visible_event_count: int = Field(ge=0)


class ObservationDelta(StrictModel):
    """Typed change from this company's preceding observation."""

    cash_before: Money | None = None
    cash_after: Money
    cash_change: Decimal | None = None
    inventory: tuple[InventoryQuantityChange, ...]
    retail_price_before: Money | None = None
    retail_price_after: Money | None = None
    open_order_count_before: int | None = Field(default=None, ge=0)
    open_order_count_after: int = Field(ge=0)


class AgentTracePreview(StrictModel):
    """Small provider-call reference suitable for timeline cards."""

    trace_run_id: Identifier
    invocation_id: Identifier
    source_trace: bool
    provider: str
    model: str
    outcome: InvocationOutcome
    usage: TokenUsage
    latency_ms: int = Field(ge=0)
    attempts: int = Field(ge=1)
    applied_to_committed_turn: bool


class OrderPlacedChange(StrictModel):
    """Accepted standing-order mutation."""

    change_type: Literal["order_placed"] = "order_placed"
    order_id: Identifier
    side: MarketSide
    product: ProductId
    quantity: Decimal = Field(gt=0)
    limit_price: Decimal = Field(gt=0)


class OrderCancelledChange(StrictModel):
    """Accepted standing-order cancellation."""

    change_type: Literal["order_cancelled"] = "order_cancelled"
    order_id: Identifier


class RetailPriceChanged(StrictModel):
    """Accepted consumer-price mutation."""

    change_type: Literal["retail_price_changed"] = "retail_price_changed"
    product: ProductId
    before: Decimal | None = Field(default=None, gt=0)
    after: Decimal = Field(gt=0)


type CommandStateChange = Annotated[
    OrderPlacedChange | OrderCancelledChange | RetailPriceChanged,
    Field(discriminator="change_type"),
]


class TurnTimelineItem(StrictModel):
    """One complete wake-to-outcome decision loop."""

    entry_type: Literal["turn"] = "turn"
    entry_id: Identifier
    sim_time: SimTime
    company_id: CompanyId
    company_name: str
    tier: CompanyTier
    state_version: int = Field(ge=0)
    apply_sequence: int = Field(ge=1)
    journal_sequence: int | None = Field(default=None, ge=1)
    wake_signals: tuple[WakeSignal, ...]
    observation: ObservationFacts
    observation_delta: ObservationDelta
    command: CompanyCommand
    outcome: CommandOutcome
    effects: tuple[DomainEvent, ...]
    state_changes: tuple[CommandStateChange, ...] = ()
    next_available_at: SimTime | None = None
    replay_origin: TurnReplayOrigin | None = None
    traces: tuple[AgentTracePreview, ...] = ()
    protocol_error: str | None = None
    title: str
    summary: str


class SystemTimelineItem(StrictModel):
    """One scheduled system transition and its economic effects."""

    entry_type: Literal["system_step"] = "system_step"
    entry_id: Identifier
    sim_time: SimTime
    kind: SystemEventKind
    journal_sequence: int | None = Field(default=None, ge=1)
    state_version_before: int | None = Field(default=None, ge=0)
    state_version_after: int | None = Field(default=None, ge=0)
    effects: tuple[DomainEvent, ...] = ()
    affected_company_ids: tuple[CompanyId, ...] = ()
    reconstructed: bool = False
    title: str
    summary: str


type TimelineItem = Annotated[
    TurnTimelineItem | SystemTimelineItem,
    Field(discriminator="entry_type"),
]


class TimelineMoment(StrictModel):
    """Every system step and concurrent decision at one simulated minute."""

    sim_time: SimTime
    system_steps: tuple[SystemTimelineItem, ...] = ()
    turns: tuple[TurnTimelineItem, ...] = ()


class TimelineDay(StrictModel):
    """One day of the operations replay plus run-wide day summaries."""

    context: TimelineRunContext
    selected_day: int = Field(ge=1)
    day_summaries: tuple[DayTimelineSummary, ...]
    moments: tuple[TimelineMoment, ...]


class ArtifactStatus(StrEnum):
    """Whether a trace's optional exported artifact is readable."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class ArtifactUnavailableReason(StrEnum):
    """Typed reason why an optional trace artifact cannot be displayed."""

    STORE_NOT_CONFIGURED = "store_not_configured"
    PROVIDER_NOT_SUPPORTED = "provider_not_supported"
    IDENTITY_UNAVAILABLE = "identity_unavailable"
    NOT_FOUND = "not_found"
    READ_ERROR = "read_error"


class AgentTraceDetail(StrictModel):
    """Readable public trace material for one physical provider call."""

    preview: AgentTracePreview
    artifact_status: ArtifactStatus
    artifact_unavailable_reason: ArtifactUnavailableReason | None = None
    reasoning_markdown: str | None = None
    final_output: str | None = None

    @model_validator(mode="after")
    def validate_artifact_state(self) -> Self:
        """Keep artifact availability, reason, and content mutually consistent."""
        available = self.artifact_status is ArtifactStatus.AVAILABLE
        if available and self.artifact_unavailable_reason is not None:
            raise ValueError("available artifacts cannot have an unavailable reason")
        if available and (self.reasoning_markdown is None or self.final_output is None):
            raise ValueError("available artifacts require reasoning and final output")
        if not available and self.artifact_unavailable_reason is None:
            raise ValueError("unavailable artifacts require a reason")
        if not available and (self.reasoning_markdown is not None or self.final_output is not None):
            raise ValueError("unavailable artifacts cannot expose partial content")
        return self


class TimelineDetail(StrictModel):
    """Expanded detail for exactly one Turn or system-step entry."""

    context: TimelineRunContext
    item: TimelineItem
    turn: TurnRecord | None = None
    system_step: SystemStepRecord | None = None
    traces: tuple[AgentTraceDetail, ...] = ()

    @model_validator(mode="after")
    def validate_entry_record(self) -> Self:
        """Expose exactly the immutable journal record represented by the item."""
        if isinstance(self.item, TurnTimelineItem):
            if self.turn is None or self.system_step is not None:
                raise ValueError("turn details require only a TurnRecord")
            return self
        if self.system_step is None or self.turn is not None:
            raise ValueError("system details require only a SystemStepRecord")
        if self.traces:
            raise ValueError("system details cannot contain Agent traces")
        return self
