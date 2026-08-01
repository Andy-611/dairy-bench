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
    PositiveMoney,
    PositiveQuantity,
    ProductId,
    Quantity,
    StrictModel,
)
from company_bench.run_models import InvocationOutcome, TokenUsage
from company_bench.runtime_models import (
    CommandOutcome,
    CompanyCommand,
    IncomingDeliveryView,
    MarketSide,
    MarketView,
    OpenOrderView,
    OperationJobView,
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
    checkpoint_at: SimTime | None = None
    checkpoint_state_version: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_checkpoint_reference(self) -> Self:
        """Keep checkpoint time and state version present or absent together."""
        if (self.checkpoint_at is None) != (self.checkpoint_state_version is None):
            raise ValueError("checkpoint time and state version must be paired")
        return self


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
    reserved_cash: Money
    inventory: tuple[InventoryPosition, ...]
    reserved_inventory: tuple[InventoryPosition, ...] = ()
    retail_price: Money | None = None
    open_orders: tuple[OpenOrderView, ...] = ()
    market_views: tuple[MarketView, ...] = ()
    pending_deliveries: tuple[IncomingDeliveryView, ...] = ()
    active_operation: OperationJobView | None = None
    remaining_operation_capacity: Decimal | None = Field(default=None, ge=0)
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


class OrderReplacedChange(StrictModel):
    """Atomic replacement of one standing order."""

    change_type: Literal["order_replaced"] = "order_replaced"
    replaced_order_id: Identifier
    order_id: Identifier
    quantity: Decimal = Field(gt=0)
    limit_price: Decimal = Field(gt=0)


class RetailPriceChanged(StrictModel):
    """Accepted consumer-price mutation."""

    change_type: Literal["retail_price_changed"] = "retail_price_changed"
    product: ProductId
    before: Decimal | None = Field(default=None, gt=0)
    after: Decimal = Field(gt=0)


type CommandStateChange = Annotated[
    OrderPlacedChange | OrderCancelledChange | OrderReplacedChange | RetailPriceChanged,
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
    turn_number_today: int = Field(ge=1)
    turn_limit_today: int = Field(ge=1)
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
    reference_ids: tuple[Identifier, ...] = ()
    suppressed_wake_signals: tuple[WakeSignal, ...] = ()
    effects: tuple[DomainEvent, ...] = ()
    affected_company_ids: tuple[CompanyId, ...] = ()
    title: str
    summary: str


class MarketPriceLevel(StrictModel):
    """One aggregated Bid or Ask price with its active orders."""

    unit_price: PositiveMoney
    size: PositiveQuantity
    orders: tuple[OpenOrderView, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_aggregation(self) -> Self:
        """Keep every displayed level exact and internally homogeneous."""
        if any(order.limit_price != self.unit_price for order in self.orders):
            raise ValueError("price-level orders must share its unit price")
        if sum((order.remaining_quantity for order in self.orders), Decimal()) != self.size:
            raise ValueError("price-level size must equal its remaining order quantities")
        return self


class MarketOrderBook(StrictModel):
    """Observer-only end-of-minute Bid and Ask book for one product."""

    product: ProductId
    bids: tuple[MarketPriceLevel, ...] = ()
    asks: tuple[MarketPriceLevel, ...] = ()
    last_trade_price: PositiveMoney | None = None
    best_bid: PositiveMoney | None = None
    best_ask: PositiveMoney | None = None
    spread: Money | None = None

    @model_validator(mode="after")
    def validate_book(self) -> Self:
        """Require sorted, uncrossed levels and exact market summary values."""
        bid_prices = tuple(level.unit_price for level in self.bids)
        ask_prices = tuple(level.unit_price for level in self.asks)
        if bid_prices != tuple(sorted(bid_prices, reverse=True)):
            raise ValueError("Bid levels must be ordered from highest to lowest")
        if ask_prices != tuple(sorted(ask_prices)):
            raise ValueError("Ask levels must be ordered from lowest to highest")
        if any(
            order.side is not MarketSide.BUY or order.product is not self.product
            for level in self.bids
            for order in level.orders
        ):
            raise ValueError("Bid levels must contain only matching buy orders")
        if any(
            order.side is not MarketSide.SELL or order.product is not self.product
            for level in self.asks
            for order in level.orders
        ):
            raise ValueError("Ask levels must contain only matching sell orders")

        expected_bid = bid_prices[0] if bid_prices else None
        expected_ask = ask_prices[0] if ask_prices else None
        if self.best_bid != expected_bid or self.best_ask != expected_ask:
            raise ValueError("best prices must match the first Bid and Ask levels")
        if expected_bid is not None and expected_ask is not None and expected_bid >= expected_ask:
            raise ValueError("an end-state continuous order book cannot remain crossed")
        expected_spread = (
            expected_ask - expected_bid
            if expected_bid is not None and expected_ask is not None
            else None
        )
        if self.spread != expected_spread:
            raise ValueError("spread must equal best Ask minus best Bid")
        return self


class MarketMatchLeg(StrictModel):
    """One maker-priced fill against an incoming market order."""

    trade_id: Identifier
    maker_order: OpenOrderView
    quantity: PositiveQuantity
    unit_price: PositiveMoney

    @model_validator(mode="after")
    def validate_maker_fill(self) -> Self:
        """Keep each match within its persisted maker commitment."""
        if self.unit_price != self.maker_order.limit_price:
            raise ValueError("match price must equal the maker order price")
        if self.quantity > self.maker_order.remaining_quantity:
            raise ValueError("match quantity cannot exceed the maker order")
        return self


class _AppliedMarketOrder(StrictModel):
    """Shared trace for Place and Replace commands entering the matcher."""

    apply_sequence: int = Field(ge=1)
    incoming_order: OpenOrderView
    matches: tuple[MarketMatchLeg, ...] = ()
    matched_quantity: Quantity
    remaining_quantity: Quantity

    @model_validator(mode="after")
    def validate_application(self) -> Self:
        """Require exact submitted, matched, and resting quantities."""
        incoming = self.incoming_order
        if incoming.priority_sequence != self.apply_sequence:
            raise ValueError("incoming order priority must equal its apply sequence")
        matched = sum((match.quantity for match in self.matches), Decimal())
        if matched != self.matched_quantity:
            raise ValueError("matched quantity must equal the matching trace")
        if matched + self.remaining_quantity != incoming.remaining_quantity:
            raise ValueError("submitted quantity must equal matched plus remaining")
        trade_ids = [match.trade_id for match in self.matches]
        if len(trade_ids) != len(set(trade_ids)):
            raise ValueError("matching trace trade ids must be unique")
        for match in self.matches:
            maker = match.maker_order
            if maker.product is not incoming.product or maker.side is incoming.side:
                raise ValueError("maker order must be from the opposite product book side")
            if maker.owner_id == incoming.owner_id:
                raise ValueError("matching trace cannot contain a self trade")
            crosses = (
                incoming.limit_price >= maker.limit_price
                if incoming.side is MarketSide.BUY
                else incoming.limit_price <= maker.limit_price
            )
            if not crosses:
                raise ValueError("matching trace orders must cross")
        return self


class MarketOrderPlaced(_AppliedMarketOrder):
    """One accepted PlaceOrder and every immediate maker match."""

    action: Literal["place"] = "place"


class MarketOrderReplaced(_AppliedMarketOrder):
    """One accepted atomic ReplaceOrder and every immediate maker match."""

    action: Literal["replace"] = "replace"
    replaced_order: OpenOrderView

    @model_validator(mode="after")
    def validate_replacement(self) -> Self:
        """Keep the old and new orders within one owner and product book."""
        incoming = self.incoming_order
        replaced = self.replaced_order
        if incoming.order_id == replaced.order_id:
            raise ValueError("replacement must receive a new order id")
        if (
            incoming.owner_id != replaced.owner_id
            or incoming.product is not replaced.product
            or incoming.side is not replaced.side
        ):
            raise ValueError("replacement must preserve owner, product, and side")
        return self


class MarketOrderCancelled(StrictModel):
    """One accepted CancelOrder and the active order it removed."""

    action: Literal["cancel"] = "cancel"
    apply_sequence: int = Field(ge=1)
    cancelled_order: OpenOrderView


type MarketOrderFlowItem = Annotated[
    MarketOrderPlaced | MarketOrderReplaced | MarketOrderCancelled,
    Field(discriminator="action"),
]


class TimelineTrade(StrictModel):
    """One persisted trade displayed in the minute that applied it."""

    apply_sequence: int = Field(ge=1)
    trade_id: Identifier
    maker_order_id: Identifier
    taker_order_id: Identifier
    product: ProductId
    seller_id: CompanyId
    buyer_id: CompanyId
    quantity: PositiveQuantity
    unit_price: PositiveMoney
    arrives_at: SimTime


class MarketFrame(StrictModel):
    """Accepted order flow, trade tape, and the exact closing order books."""

    state_version: int = Field(ge=0)
    order_flow: tuple[MarketOrderFlowItem, ...] = ()
    trades: tuple[TimelineTrade, ...] = ()
    closing_order_books: tuple[MarketOrderBook, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_audit_flow(self) -> Self:
        """Require order flow and trade tape to describe the same matches."""
        sequences = tuple(item.apply_sequence for item in self.order_flow)
        if sequences != tuple(sorted(sequences)) or len(sequences) != len(set(sequences)):
            raise ValueError("order flow must have unique ascending apply sequences")

        projected: list[tuple[object, ...]] = []
        for item in self.order_flow:
            if isinstance(item, MarketOrderCancelled):
                continue
            taker = item.incoming_order
            for match in item.matches:
                maker = match.maker_order
                seller_id = taker.owner_id if taker.side is MarketSide.SELL else maker.owner_id
                buyer_id = taker.owner_id if taker.side is MarketSide.BUY else maker.owner_id
                projected.append(
                    (
                        item.apply_sequence,
                        match.trade_id,
                        maker.order_id,
                        taker.order_id,
                        taker.product,
                        seller_id,
                        buyer_id,
                        match.quantity,
                        match.unit_price,
                    )
                )
        actual = [
            (
                trade.apply_sequence,
                trade.trade_id,
                trade.maker_order_id,
                trade.taker_order_id,
                trade.product,
                trade.seller_id,
                trade.buyer_id,
                trade.quantity,
                trade.unit_price,
            )
            for trade in self.trades
        ]
        if projected != actual:
            raise ValueError("matching trace must exactly equal the trade tape")
        return self


type TimelineItem = Annotated[
    TurnTimelineItem | SystemTimelineItem,
    Field(discriminator="entry_type"),
]


class TimelineMoment(StrictModel):
    """Every system step and concurrent decision at one simulated minute."""

    sim_time: SimTime
    system_steps: tuple[SystemTimelineItem, ...] = ()
    turns: tuple[TurnTimelineItem, ...] = ()
    market: MarketFrame


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
