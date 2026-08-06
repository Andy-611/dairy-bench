"""Strongly typed commands and runtime records for event-driven episodes."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import Field, model_validator

from company_bench.models import (
    ZERO,
    CompanyId,
    CompanyObservation,
    DomainEvent,
    EventRecord,
    Identifier,
    Money,
    OperationQuantity,
    OrderQuantity,
    PositiveMoney,
    PositiveQuantity,
    ProductId,
    Quantity,
    StrictModel,
)

__all__ = [
    "PROTOCOL_ERROR_PREFIX",
    "AgentTurn",
    "CommandEnvelope",
    "CommandOutcome",
    "CommandStatus",
    "CompanyCommand",
    "DeliveryExpiryBucket",
    "IncomingDeliveryView",
    "InventoryExpiryBucket",
    "JournalEntryKind",
    "JournalEntryReference",
    "MarketSide",
    "OpenOrderView",
    "OperationJobView",
    "OrderBookView",
    "PriceLevelView",
    "Produce",
    "QuoteAlert",
    "QuoteLadder",
    "QuoteLadderResult",
    "QuoteLevel",
    "QuoteLevelAction",
    "QuoteLevelResult",
    "ScheduledCompletion",
    "SetQuoteLadder",
    "SetRetailPrice",
    "SimTime",
    "SystemEventKind",
    "SystemStepRecord",
    "Transform",
    "TurnRecord",
    "TurnReplayOrigin",
    "Wait",
    "WakeReason",
    "WakeSignal",
    "system_step_id",
]

MINUTES_PER_DAY = 24 * 60
PROTOCOL_ERROR_PREFIX: Final = "agent protocol error: "


class SimTime(StrictModel):
    """One absolute, monotonic minute on the simulation clock."""

    absolute_minute: int = Field(ge=0)

    @classmethod
    def at(cls, *, day: int, hour: int = 0, minute: int = 0) -> SimTime:
        """Build time from a zero-based day and wall-clock minute."""
        if day < 0:
            raise ValueError("day must be non-negative")
        if not 0 <= hour < 24:
            raise ValueError("hour must be between 0 and 23")
        if not 0 <= minute < 60:
            raise ValueError("minute must be between 0 and 59")
        return cls(absolute_minute=day * MINUTES_PER_DAY + hour * 60 + minute)

    @property
    def day(self) -> int:
        """Return the zero-based simulation day."""
        return self.absolute_minute // MINUTES_PER_DAY

    @property
    def hour(self) -> int:
        """Return the wall-clock hour."""
        return self.minute_of_day // 60

    @property
    def minute(self) -> int:
        """Return the wall-clock minute within the hour."""
        return self.absolute_minute % 60

    @property
    def minute_of_day(self) -> int:
        """Return the minute offset within the current day."""
        return self.absolute_minute % MINUTES_PER_DAY

    def plus(self, minutes: int) -> SimTime:
        """Return a later time without mutating this value."""
        if minutes < 0:
            raise ValueError("minutes must be non-negative")
        return SimTime(absolute_minute=self.absolute_minute + minutes)


class WakeReason(StrEnum):
    """Why a company is receiving a new Agent turn."""

    DAY_OPEN = "day_open"
    CONTINUE = "continue"
    WAIT_EXPIRED = "wait_expired"
    PRICE_ALERT = "price_alert"
    TRADE_EXECUTED = "trade_executed"
    COMMAND_REJECTED = "command_rejected"
    OPERATION_COMPLETED = "operation_completed"
    DELIVERY_COMPLETED = "delivery_completed"
    EXTERNAL_EVENT = "external_event"


class JournalEntryKind(StrEnum):
    """Kinds that can own causal links in the turn journal."""

    TURN = "turn"
    SYSTEM_STEP = "system_step"


class JournalEntryReference(StrictModel):
    """Stable causal pointer to another immutable journal entry."""

    entry_id: Identifier
    entry_type: JournalEntryKind


class WakeSignal(StrictModel):
    """One typed wake reason with its causal journal source."""

    reason: WakeReason
    source: JournalEntryReference | None = None
    reference_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        """Reject duplicated opaque references."""
        if len(self.reference_ids) != len(set(self.reference_ids)):
            raise ValueError("wake signal reference_ids must be unique")
        return self


class SystemEventKind(StrEnum):
    """Kinds understood by the simulation runtime."""

    DAY_OPEN = "day_open"
    DAY_CLOSE = "day_close"
    COMPANY_WAKE = "company_wake"
    OPERATION_COMPLETED = "operation_completed"
    DELIVERY_COMPLETED = "delivery_completed"
    MARKET_CLOSE = "market_close"
    CONSUMER_SALES = "consumer_sales"
    TURN_LIMIT_REACHED = "turn_limit_reached"
    AGENT_WAKE_SUPPRESSED = "agent_wake_suppressed"

    @property
    def priority(self) -> int:
        """Define economic ordering for events sharing one virtual minute."""
        return {
            self.DAY_OPEN: 10,
            self.OPERATION_COMPLETED: 20,
            self.DELIVERY_COMPLETED: 20,
            self.MARKET_CLOSE: 30,
            self.CONSUMER_SALES: 40,
            self.DAY_CLOSE: 50,
            self.TURN_LIMIT_REACHED: 90,
            self.AGENT_WAKE_SUPPRESSED: 90,
            self.COMPANY_WAKE: 100,
        }[self]


class MarketSide(StrEnum):
    """A company's intent in the order book."""

    BUY = "buy"
    SELL = "sell"


class Produce(StrictModel):
    """Request primary production of one product."""

    kind: Literal["produce"] = "produce"
    product: ProductId
    quantity: OperationQuantity


class Transform(StrictModel):
    """Request conversion from one product into another."""

    kind: Literal["transform"] = "transform"
    input_product: ProductId
    output_product: ProductId
    input_quantity: OperationQuantity

    @model_validator(mode="after")
    def validate_products(self) -> Self:
        """Require transformation to change the product identity."""
        if self.input_product == self.output_product:
            raise ValueError("input_product and output_product must differ")
        return self


class QuoteLevel(StrictModel):
    """One independently collateralized price-quantity level."""

    quantity: OrderQuantity
    limit_price: PositiveMoney


class QuoteLadder(StrictModel):
    """Validated target levels for one side of one product market."""

    side: MarketSide
    levels: tuple[QuoteLevel, ...] = Field(default=(), max_length=3)

    @model_validator(mode="after")
    def validate_levels(self) -> Self:
        """Require unique levels ordered from most to least competitive."""
        prices = tuple(level.limit_price for level in self.levels)
        if len(prices) != len(set(prices)):
            raise ValueError("quote ladder prices must be unique")
        expected = tuple(sorted(prices, reverse=self.side is MarketSide.BUY))
        if prices != expected:
            direction = "descending" if self.side is MarketSide.BUY else "ascending"
            raise ValueError(f"quote ladder prices must be {direction}")
        return self


class SetQuoteLadder(QuoteLadder):
    """Atomically set one validated target ladder."""

    kind: Literal["set_quote_ladder"] = "set_quote_ladder"
    product: ProductId


class SetRetailPrice(StrictModel):
    """Set one consumer-facing unit price."""

    kind: Literal["set_retail_price"] = "set_retail_price"
    product: ProductId
    unit_price: PositiveMoney


class QuoteAlert(StrictModel):
    """Wake when one visible best quote crosses a price threshold."""

    product: ProductId
    quote: Literal["best_bid", "best_ask"]
    operator: Literal["at_least", "at_most"]
    price: PositiveMoney


class Wait(StrictModel):
    """Yield until a bounded review time or a visible quote alert."""

    kind: Literal["wait"] = "wait"
    until: SimTime | None = None
    alerts: tuple[QuoteAlert, ...] = Field(default=(), max_length=3)


type CompanyCommand = Annotated[
    Produce | Transform | SetQuoteLadder | SetRetailPrice | Wait,
    Field(discriminator="kind"),
]


class CommandEnvelope(StrictModel):
    """Bind an untrusted command to runtime-owned identity and time."""

    turn_id: Identifier
    command_id: Identifier
    company_id: CompanyId
    issued_at: SimTime
    state_version: int = Field(ge=0)
    command: CompanyCommand


class CommandStatus(StrEnum):
    """Immediate disposition of a submitted company command."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


class QuoteLevelAction(StrEnum):
    """How one target quote reconciled with the prior ladder."""

    KEEP = "keep"
    PLACE = "place"
    REPLACE = "replace"


class QuoteLevelResult(StrictModel):
    """Auditable application result for one target quote level."""

    level: QuoteLevel
    action: QuoteLevelAction
    order_id: Identifier
    replaced_order_id: Identifier | None = None
    priority_sequence: int = Field(ge=1)
    remaining_quantity: Quantity

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        """Keep identities and post-match quantity consistent with the action."""
        if self.remaining_quantity > self.level.quantity:
            raise ValueError("remaining quote quantity cannot exceed its target")
        if self.action is QuoteLevelAction.REPLACE:
            if self.replaced_order_id is None or self.replaced_order_id == self.order_id:
                raise ValueError("replacement requires distinct old and new order ids")
        elif self.replaced_order_id is not None:
            raise ValueError("only replacement levels may reference an old order")
        if self.action is QuoteLevelAction.KEEP and self.remaining_quantity != self.level.quantity:
            raise ValueError("kept quote quantity must remain unchanged")
        return self


class QuoteLadderResult(StrictModel):
    """Complete reconciliation result for one atomic target ladder."""

    levels: tuple[QuoteLevelResult, ...] = Field(default=(), max_length=3)
    cancelled_order_ids: tuple[Identifier, ...] = Field(default=(), max_length=3)

    @model_validator(mode="after")
    def validate_identities(self) -> Self:
        """Require each old and new order identity to appear only once."""
        current_ids = [level.order_id for level in self.levels]
        old_ids = [
            level.replaced_order_id for level in self.levels if level.replaced_order_id is not None
        ]
        if len(current_ids) != len(set(current_ids)):
            raise ValueError("quote ladder order ids must be unique")
        retired_ids = [*old_ids, *self.cancelled_order_ids]
        if len(retired_ids) != len(set(retired_ids)):
            raise ValueError("retired quote ladder order ids must be unique")
        if set(current_ids) & set(retired_ids):
            raise ValueError("current and retired quote ladder order ids must be disjoint")
        return self


class ScheduledCompletion(StrictModel):
    """A future economic completion requested by the engine."""

    event_id: Identifier
    kind: SystemEventKind
    at: SimTime
    reference_id: Identifier
    company_id: CompanyId

    @model_validator(mode="after")
    def validate_kind(self) -> Self:
        """Only asynchronous completions may be scheduled by commands."""
        allowed = {
            SystemEventKind.OPERATION_COMPLETED,
            SystemEventKind.DELIVERY_COMPLETED,
        }
        if self.kind not in allowed:
            raise ValueError("command schedules must be operation or delivery completions")
        return self


class CommandOutcome(StrictModel):
    """Auditable immediate result of applying one command."""

    turn_id: Identifier
    command_id: Identifier
    company_id: CompanyId
    occurred_at: SimTime
    status: CommandStatus
    accepted: bool
    reason: str | None = Field(default=None, min_length=1, max_length=500)
    resulting_state_version: int = Field(ge=0)
    apply_sequence: int = Field(ge=1)
    quote_ladder_result: QuoteLadderResult | None = None
    job_id: Identifier | None = None
    events: tuple[DomainEvent, ...] = ()
    scheduled_completions: tuple[ScheduledCompletion, ...] = ()
    next_available_at: SimTime | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        """Keep status, time, and rejection details internally consistent."""
        if self.accepted != (self.status is CommandStatus.ACCEPTED):
            raise ValueError("accepted must agree with status")
        if self.status is CommandStatus.REJECTED and self.reason is None:
            raise ValueError("rejected commands require a reason")
        if (
            self.next_available_at is not None
            and self.next_available_at.absolute_minute < self.occurred_at.absolute_minute
        ):
            raise ValueError("next_available_at cannot precede occurred_at")
        return self


class OpenOrderView(StrictModel):
    """Immutable order-book projection safe to expose to an Agent."""

    order_id: Identifier
    owner_id: CompanyId
    side: MarketSide
    product: ProductId
    remaining_quantity: PositiveQuantity
    limit_price: PositiveMoney
    placed_at: SimTime
    priority_sequence: int = Field(ge=1)
    queue_ahead_quantity: Quantity


class PriceLevelView(StrictModel):
    """Anonymous aggregate resting at one public price level."""

    unit_price: PositiveMoney
    quantity: PositiveQuantity
    order_count: int = Field(ge=1)


class OrderBookView(StrictModel):
    """Complete anonymous order-book depth visible to one Agent."""

    product: ProductId
    bids: tuple[PriceLevelView, ...] = ()
    asks: tuple[PriceLevelView, ...] = ()
    last_trade_price: PositiveMoney | None = None
    daily_volume: Quantity = ZERO

    @model_validator(mode="after")
    def validate_book(self) -> Self:
        """Require unique sorted levels and an uncrossed resting book."""
        bid_prices = tuple(level.unit_price for level in self.bids)
        ask_prices = tuple(level.unit_price for level in self.asks)
        if bid_prices != tuple(sorted(set(bid_prices), reverse=True)):
            raise ValueError("bid levels must have unique descending prices")
        if ask_prices != tuple(sorted(set(ask_prices))):
            raise ValueError("ask levels must have unique ascending prices")
        if bid_prices and ask_prices and bid_prices[0] >= ask_prices[0]:
            raise ValueError("a continuous order book cannot remain crossed")
        return self

    def best_price(self, side: MarketSide) -> PositiveMoney | None:
        """Return the first public price on one side, if present."""
        levels = self.bids if side is MarketSide.BUY else self.asks
        return levels[0].unit_price if levels else None


class InventoryExpiryBucket(StrictModel):
    """Owned spot inventory sharing one product and expiry day."""

    product: ProductId
    expires_end_of_day: int = Field(ge=1)
    available_quantity: Quantity
    reserved_quantity: Quantity

    @model_validator(mode="after")
    def validate_quantity(self) -> Self:
        """Omit economically empty expiry buckets."""
        if self.available_quantity + self.reserved_quantity <= ZERO:
            raise ValueError("an inventory expiry bucket must contain inventory")
        return self


class DeliveryExpiryBucket(StrictModel):
    """Quantity within one incoming delivery sharing an expiry day."""

    quantity: PositiveQuantity
    expires_end_of_day: int = Field(ge=1)


class IncomingDeliveryView(StrictModel):
    """One buyer-visible delivery already guaranteed by a trade."""

    trade_id: Identifier
    product: ProductId
    quantity: PositiveQuantity
    arrives_at: SimTime
    expiry_buckets: tuple[DeliveryExpiryBucket, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_expiry_buckets(self) -> Self:
        """Keep expiry detail ordered, unique, and quantity preserving."""
        days = tuple(bucket.expires_end_of_day for bucket in self.expiry_buckets)
        if days != tuple(sorted(set(days))):
            raise ValueError("delivery expiry buckets must be unique and ordered")
        if sum((bucket.quantity for bucket in self.expiry_buckets), start=ZERO) != (self.quantity):
            raise ValueError("delivery expiry buckets must sum to quantity")
        return self


class OperationJobView(StrictModel):
    """The company's currently active production resource."""

    job_id: Identifier
    kind: Literal["production", "transformation"]
    completes_at: SimTime
    output_product: ProductId
    output_quantity: PositiveQuantity


class AgentTurn(StrictModel):
    """Runtime-bound input metadata for one company decision."""

    turn_id: Identifier
    company_id: CompanyId
    sim_time: SimTime
    state_version: int = Field(ge=0)
    turn_number_today: int = Field(ge=1)
    turn_limit_today: int = Field(ge=1)
    wake_reasons: tuple[WakeReason, ...] = Field(min_length=1)
    wake_signals: tuple[WakeSignal, ...] = ()
    observation: CompanyObservation
    available_cash: Money
    reserved_cash: Money = ZERO
    marked_surplus: Decimal = Field(allow_inf_nan=False)
    inventory_expiry: tuple[InventoryExpiryBucket, ...] = ()
    open_orders: tuple[OpenOrderView, ...] = ()
    order_books: tuple[OrderBookView, ...] = ()
    pending_deliveries: tuple[IncomingDeliveryView, ...] = ()
    active_operation: OperationJobView | None = None
    visible_events: tuple[DomainEvent, ...] = ()
    previous_outcome: CommandOutcome | None = None

    @property
    def remaining_operation_capacity(self) -> Quantity | None:
        """Derive remaining capacity from the sole private operation state."""
        operation = self.observation.daily_operation
        return None if operation is None else operation.remaining_capacity

    @model_validator(mode="after")
    def validate_turn(self) -> Self:
        """Reject inconsistent identity, version, and wake metadata."""
        if self.turn_number_today > self.turn_limit_today:
            raise ValueError("turn_number_today cannot exceed turn_limit_today")
        if self.turn_limit_today != self.observation.runtime.max_turns_per_company_day:
            raise ValueError("turn_limit_today must match the runtime turn limit")
        if len(self.wake_reasons) != len(set(self.wake_reasons)):
            raise ValueError("wake_reasons must be unique")
        if self.wake_signals:
            signal_reasons = tuple(dict.fromkeys(signal.reason for signal in self.wake_signals))
            if signal_reasons != self.wake_reasons:
                raise ValueError("wake_signals must cover wake_reasons in order")
        if self.observation.company_id != self.company_id:
            raise ValueError("observation company_id must match the turn")
        if self.observation.cash != self.available_cash:
            raise ValueError("available_cash must match the observed cash")
        if any(order.owner_id != self.company_id for order in self.open_orders):
            raise ValueError("open_orders must be owned by the observed company")
        product_order = {
            product.product: index for index, product in enumerate(self.observation.products)
        }
        expiry_keys = tuple(
            (bucket.product, bucket.expires_end_of_day) for bucket in self.inventory_expiry
        )
        if len(expiry_keys) != len(set(expiry_keys)):
            raise ValueError("inventory_expiry keys must be unique")
        if any(product not in product_order for product, _ in expiry_keys):
            raise ValueError("inventory_expiry contains an unknown product")
        if expiry_keys != tuple(
            sorted(expiry_keys, key=lambda key: (product_order[key[0]], key[1]))
        ):
            raise ValueError("inventory_expiry must follow product and expiry order")
        book_products = tuple(book.product for book in self.order_books)
        if len(book_products) != len(set(book_products)):
            raise ValueError("order_books products must be unique")
        if any(product not in product_order for product in book_products):
            raise ValueError("order_books contains an unknown product")
        if book_products != tuple(sorted(book_products, key=product_order.__getitem__)):
            raise ValueError("order_books must follow scenario product order")
        if self.previous_outcome is not None:
            if self.previous_outcome.company_id != self.company_id:
                raise ValueError("previous outcome company_id must match the turn")
            if self.previous_outcome.resulting_state_version > self.state_version:
                raise ValueError("previous outcome cannot exceed the observed state version")
            if self.previous_outcome.occurred_at.absolute_minute > self.sim_time.absolute_minute:
                raise ValueError("previous outcome cannot occur after the turn")
        return self


class TurnReplayOrigin(StrictModel):
    """Exact source Turn reused by a zero-model-call replay."""

    source_run_id: Identifier
    source_turn_id: Identifier


class SystemStepRecord(StrictModel):
    """One immutable system transition and its exact economic effects."""

    run_id: Identifier
    entry_id: Identifier
    journal_sequence: int = Field(ge=1)
    scheduled_event_id: Identifier
    occurred_at: SimTime
    kind: SystemEventKind
    company_id: CompanyId | None = None
    reference_ids: tuple[Identifier, ...] = ()
    suppressed_wake_signals: tuple[WakeSignal, ...] = ()
    state_version_before: int = Field(ge=0)
    state_version_after: int = Field(ge=0)
    effects: tuple[EventRecord, ...] = ()
    snapshot_day: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_step(self) -> Self:
        """Keep one system transition chronological and internally consistent."""
        if self.kind is SystemEventKind.COMPANY_WAKE:
            raise ValueError("company wakes are signals, not system journal steps")
        company_audit = self.kind in {
            SystemEventKind.TURN_LIMIT_REACHED,
            SystemEventKind.AGENT_WAKE_SUPPRESSED,
        }
        if company_audit != (self.company_id is not None):
            raise ValueError("Agent limit audit steps alone require company_id")
        wake_suppressed = self.kind is SystemEventKind.AGENT_WAKE_SUPPRESSED
        if wake_suppressed != bool(self.suppressed_wake_signals):
            raise ValueError("suppressed-wake steps require their causal signals")
        if self.state_version_after < self.state_version_before:
            raise ValueError("system step cannot move the state version backwards")
        expected_day = self.occurred_at.day + 1
        if any(record.event.day != expected_day for record in self.effects):
            raise ValueError("system step effects must occur on its simulation day")
        if self.snapshot_day is not None and self.snapshot_day != expected_day:
            raise ValueError("system step snapshot_day must match its simulation day")
        return self


def system_step_id(run_id: Identifier, scheduled_event_id: Identifier) -> Identifier:
    """Build the stable journal identity for one scheduled system event."""
    return f"{run_id}.system.{scheduled_event_id}"


class TurnRecord(StrictModel):
    """One complete Agent turn with its bound command and outcome."""

    run_id: Identifier
    turn: AgentTurn
    envelope: CommandEnvelope
    outcome: CommandOutcome
    observation_hash: Identifier
    protocol_error: str | None = Field(default=None, min_length=1, max_length=450)
    journal_sequence: int | None = Field(default=None, ge=1)
    replay_origin: TurnReplayOrigin | None = None

    @model_validator(mode="after")
    def validate_runtime_identity(self) -> Self:
        """Require every runtime-owned identity and timestamp to agree."""
        if self.envelope.turn_id != self.turn.turn_id:
            raise ValueError("envelope turn_id must match the turn")
        if self.envelope.company_id != self.turn.company_id:
            raise ValueError("command company_id must match the turn")
        if self.envelope.issued_at != self.turn.sim_time:
            raise ValueError("command issued_at must match the turn time")
        if self.envelope.state_version != self.turn.state_version:
            raise ValueError("command state_version must match the turn")
        if self.outcome.turn_id != self.turn.turn_id:
            raise ValueError("outcome turn_id must match the turn")
        if self.outcome.company_id != self.turn.company_id:
            raise ValueError("outcome company_id must match the turn")
        if self.outcome.command_id != self.envelope.command_id:
            raise ValueError("outcome command_id must match the command")
        if self.outcome.resulting_state_version < self.envelope.state_version:
            raise ValueError("outcome cannot move the state version backwards")
        if self.outcome.occurred_at.absolute_minute < self.envelope.issued_at.absolute_minute:
            raise ValueError("outcome cannot precede the command")
        command = self.envelope.command
        result = self.outcome.quote_ladder_result
        expects_ladder_result = self.outcome.accepted and isinstance(command, SetQuoteLadder)
        if (result is not None) != expects_ladder_result:
            raise ValueError("only an accepted quote ladder requires a ladder result")
        if result is not None and tuple(level.level for level in result.levels) != command.levels:
            raise ValueError("quote ladder result levels must match the command target")
        if (
            self.turn.previous_outcome is not None
            and self.outcome.apply_sequence <= self.turn.previous_outcome.apply_sequence
        ):
            raise ValueError("apply_sequence must advance beyond the previous outcome")
        if self.protocol_error is not None:
            if self.outcome.accepted:
                raise ValueError("a protocol error cannot produce an accepted outcome")
            if not isinstance(self.envelope.command, Wait):
                raise ValueError("protocol errors must normalize to a wait command")
            if self.outcome.reason != f"{PROTOCOL_ERROR_PREFIX}{self.protocol_error}":
                raise ValueError("protocol error must match the outcome reason")
        return self
