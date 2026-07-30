"""Strongly typed commands and runtime records for event-driven episodes."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import Field, model_validator

from company_bench.models import (
    CompanyId,
    CompanyObservation,
    DomainEvent,
    EventRecord,
    Identifier,
    PositiveMoney,
    PositiveQuantity,
    ProductId,
    StrictModel,
)

__all__ = [
    "PROTOCOL_ERROR_PREFIX",
    "AgentTurn",
    "CancelOrder",
    "CommandEnvelope",
    "CommandOutcome",
    "CommandStatus",
    "CompanyCommand",
    "JournalEntryKind",
    "JournalEntryReference",
    "MarketSide",
    "OpenOrderView",
    "PlaceOrder",
    "Produce",
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
    ORDER_UPDATED = "order_updated"
    PRODUCTION_COMPLETED = "production_completed"
    TRANSFORMATION_COMPLETED = "transformation_completed"
    MARKET_CLEARED = "market_cleared"
    EXTERNAL_EVENT = "external_event"


class JournalEntryKind(StrEnum):
    """Kinds that can own causal links in the V2 journal."""

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
    MARKET_CLEAR = "market_clear"
    PRODUCTION_COMPLETED = "production_completed"
    TRANSFORMATION_COMPLETED = "transformation_completed"
    CONSUMER_SALES = "consumer_sales"
    RUN_END = "run_end"

    @property
    def priority(self) -> int:
        """Run system mechanics before company wakes at the same minute."""
        return 100 if self is self.COMPANY_WAKE else 0


class MarketSide(StrEnum):
    """A company's intent in the order book."""

    BUY = "buy"
    SELL = "sell"


class Produce(StrictModel):
    """Request primary production of one product."""

    kind: Literal["produce"] = "produce"
    product: ProductId
    quantity: PositiveQuantity


class Transform(StrictModel):
    """Request conversion from one product into another."""

    kind: Literal["transform"] = "transform"
    input_product: ProductId
    output_product: ProductId
    input_quantity: PositiveQuantity

    @model_validator(mode="after")
    def validate_products(self) -> Self:
        """Require transformation to change the product identity."""
        if self.input_product == self.output_product:
            raise ValueError("input_product and output_product must differ")
        return self


class PlaceOrder(StrictModel):
    """Place one limit order without accepting an LLM-supplied identity."""

    kind: Literal["place_order"] = "place_order"
    side: MarketSide
    product: ProductId
    quantity: PositiveQuantity
    limit_price: PositiveMoney


class CancelOrder(StrictModel):
    """Cancel an existing order owned by the runtime-bound company."""

    kind: Literal["cancel_order"] = "cancel_order"
    order_id: Identifier


class SetRetailPrice(StrictModel):
    """Set one consumer-facing unit price."""

    kind: Literal["set_retail_price"] = "set_retail_price"
    product: ProductId
    unit_price: PositiveMoney


class Wait(StrictModel):
    """Yield until a selected time or the next relevant external event."""

    kind: Literal["wait"] = "wait"
    until: SimTime | None = None


type CompanyCommand = Annotated[
    Produce | Transform | PlaceOrder | CancelOrder | SetRetailPrice | Wait,
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
    order_id: Identifier | None = None
    events: tuple[DomainEvent, ...] = ()
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


class AgentTurn(StrictModel):
    """Runtime-bound input metadata for one company decision."""

    turn_id: Identifier
    company_id: CompanyId
    sim_time: SimTime
    state_version: int = Field(ge=0)
    wake_reasons: tuple[WakeReason, ...] = Field(min_length=1)
    wake_signals: tuple[WakeSignal, ...] = ()
    observation: CompanyObservation
    open_orders: tuple[OpenOrderView, ...] = ()
    visible_events: tuple[DomainEvent, ...] = ()
    previous_outcome: CommandOutcome | None = None

    @model_validator(mode="after")
    def validate_turn(self) -> Self:
        """Reject inconsistent identity, version, and wake metadata."""
        if len(self.wake_reasons) != len(set(self.wake_reasons)):
            raise ValueError("wake_reasons must be unique")
        if self.wake_signals:
            signal_reasons = tuple(signal.reason for signal in self.wake_signals)
            if signal_reasons != self.wake_reasons:
                raise ValueError("wake_signals must follow wake_reasons exactly")
        if self.observation.company_id != self.company_id:
            raise ValueError("observation company_id must match the turn")
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
    state_version_before: int = Field(ge=0)
    state_version_after: int = Field(ge=0)
    effects: tuple[EventRecord, ...] = ()
    snapshot_day: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_step(self) -> Self:
        """Keep one system transition chronological and internally consistent."""
        if self.kind is SystemEventKind.COMPANY_WAKE:
            raise ValueError("company wakes are signals, not system journal steps")
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
