"""Observer-only reconstruction of each minute's continuous market state."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from company_bench.models import Identifier, ProductId, ScenarioSpec, TradeExecutedEvent
from company_bench.runtime_models import (
    CancelOrder,
    MarketSide,
    OpenOrderView,
    PlaceOrder,
    ReplaceOrder,
    SystemEventKind,
    SystemStepRecord,
    TurnRecord,
)
from company_bench.timeline_models import (
    MarketFrame,
    MarketMatchLeg,
    MarketOrderBook,
    MarketOrderCancelled,
    MarketOrderFlowItem,
    MarketOrderPlaced,
    MarketOrderReplaced,
    MarketPriceLevel,
    TimelineTrade,
)


class MarketProjectionError(ValueError):
    """Raised when a persisted journal cannot produce a coherent order book."""


@dataclass(frozen=True, slots=True)
class _TurnMarketProjection:
    """Compact market effects emitted by one accepted company turn."""

    flow: MarketOrderFlowItem | None = None
    trades: tuple[TimelineTrade, ...] = ()


@dataclass(slots=True)
class _MarketReplay:
    """Maintain exact active orders from authoritative command and trade facts."""

    scenario: ScenarioSpec
    orders: dict[Identifier, OpenOrderView] = field(default_factory=dict)
    last_trade_price: dict[ProductId, Decimal] = field(default_factory=dict)
    state_version: int = 0

    def reset_day(self) -> None:
        """Open a new empty DAY book and reset daily market statistics."""
        self.orders.clear()
        self.last_trade_price.clear()

    def apply_system_step(self, step: SystemStepRecord) -> None:
        """Apply the only system transitions that mutate the order book."""
        if step.kind is SystemEventKind.DAY_OPEN:
            self.reset_day()
        elif step.kind is SystemEventKind.MARKET_CLOSE:
            self.orders.clear()
        self.state_version = step.state_version_after

    def apply_turn(self, record: TurnRecord) -> _TurnMarketProjection:
        """Apply one accepted order command and return its exact audit projection."""
        self.state_version = record.outcome.resulting_state_version
        if not record.outcome.accepted:
            return _TurnMarketProjection()

        command = record.envelope.command
        incoming: OpenOrderView | None = None
        replaced: OpenOrderView | None = None
        cancelled: OpenOrderView | None = None
        if isinstance(command, PlaceOrder):
            incoming = self._new_order(
                record,
                side=command.side,
                product=command.product,
                quantity=command.quantity,
                limit_price=command.limit_price,
            )
        elif isinstance(command, ReplaceOrder):
            replaced = self._remove(command.order_id)
            incoming = self._new_order(
                record,
                side=replaced.side,
                product=replaced.product,
                quantity=command.quantity,
                limit_price=command.limit_price,
            )
        elif isinstance(command, CancelOrder):
            cancelled = self._remove(command.order_id)

        events = tuple(
            event
            for event in record.outcome.events
            if isinstance(event, TradeExecutedEvent)
        )
        if events and incoming is None:
            raise MarketProjectionError("trade events require an incoming market order")
        matches: tuple[MarketMatchLeg, ...] = ()
        remaining = incoming
        if incoming is not None:
            remaining, matches = self._apply_fills(incoming, events)
            if remaining is not None:
                self._add(remaining)

        remaining_quantity = (
            remaining.remaining_quantity if remaining is not None else Decimal()
        )
        flow: MarketOrderFlowItem | None = None
        if isinstance(command, PlaceOrder) and incoming is not None:
            flow = MarketOrderPlaced(
                apply_sequence=record.outcome.apply_sequence,
                incoming_order=incoming,
                matches=matches,
                matched_quantity=_matched_quantity(matches),
                remaining_quantity=remaining_quantity,
            )
        elif isinstance(command, ReplaceOrder) and incoming is not None and replaced is not None:
            flow = MarketOrderReplaced(
                apply_sequence=record.outcome.apply_sequence,
                incoming_order=incoming,
                matches=matches,
                matched_quantity=_matched_quantity(matches),
                remaining_quantity=remaining_quantity,
                replaced_order=replaced,
            )
        elif isinstance(command, CancelOrder) and cancelled is not None:
            flow = MarketOrderCancelled(
                apply_sequence=record.outcome.apply_sequence,
                cancelled_order=cancelled,
            )

        arrives_at = record.envelope.issued_at.plus(
            self.scenario.runtime.delivery_duration_minutes
        )
        trades = tuple(
            TimelineTrade(
                apply_sequence=record.outcome.apply_sequence,
                trade_id=event.trade_id,
                maker_order_id=event.maker_order_id,
                taker_order_id=event.taker_order_id,
                product=event.product,
                seller_id=event.seller_id,
                buyer_id=event.buyer_id,
                quantity=event.quantity,
                unit_price=event.unit_price,
                arrives_at=arrives_at,
            )
            for event in events
        )
        for event in events:
            self.last_trade_price[event.product] = event.unit_price
        return _TurnMarketProjection(flow=flow, trades=trades)

    def frame(
        self,
        order_flow: tuple[MarketOrderFlowItem, ...],
        trades: tuple[TimelineTrade, ...],
    ) -> MarketFrame:
        """Freeze the current replay into an immutable UI projection."""
        return MarketFrame(
            state_version=self.state_version,
            order_flow=order_flow,
            trades=trades,
            closing_order_books=tuple(
                self._book(product.product) for product in self.scenario.products
            ),
        )

    def _new_order(
        self,
        record: TurnRecord,
        *,
        side: MarketSide,
        product: ProductId,
        quantity: Decimal,
        limit_price: Decimal,
    ) -> OpenOrderView:
        order_id = record.outcome.order_id
        if order_id is None:
            raise MarketProjectionError("accepted order command has no generated order id")
        return OpenOrderView(
            order_id=order_id,
            owner_id=record.turn.company_id,
            side=side,
            product=product,
            remaining_quantity=quantity,
            limit_price=limit_price,
            placed_at=record.envelope.issued_at,
            priority_sequence=record.outcome.apply_sequence,
        )

    def _apply_fills(
        self,
        incoming: OpenOrderView,
        events: tuple[TradeExecutedEvent, ...],
    ) -> tuple[OpenOrderView | None, tuple[MarketMatchLeg, ...]]:
        matches: list[MarketMatchLeg] = []
        for index, event in enumerate(events):
            self._validate_trade(incoming, event)
            resting = self._resting_order(incoming, event)
            matches.append(
                MarketMatchLeg(
                    trade_id=event.trade_id,
                    maker_order=resting,
                    quantity=event.quantity,
                    unit_price=event.unit_price,
                )
            )
            updated_resting = _reduce(resting, event.quantity)
            if updated_resting is None:
                self.orders.pop(resting.order_id)
            else:
                self.orders[resting.order_id] = updated_resting
            incoming = _reduce(incoming, event.quantity)
            if incoming is None and index < len(events) - 1:
                raise MarketProjectionError("an exhausted incoming order has additional fills")
        return incoming, tuple(matches)

    @staticmethod
    def _validate_trade(incoming: OpenOrderView, event: TradeExecutedEvent) -> None:
        if event.taker_order_id != incoming.order_id:
            raise MarketProjectionError("trade taker does not match its incoming order")
        if event.product is not incoming.product:
            raise MarketProjectionError("trade product does not match its incoming order")
        expected_owner = event.buyer_id if incoming.side is MarketSide.BUY else event.seller_id
        if expected_owner != incoming.owner_id:
            raise MarketProjectionError("trade actor does not own its incoming order")

    def _resting_order(
        self,
        incoming: OpenOrderView,
        event: TradeExecutedEvent,
    ) -> OpenOrderView:
        try:
            resting = self.orders[event.maker_order_id]
        except KeyError as error:
            raise MarketProjectionError(
                f"trade '{event.trade_id}' has no active maker order"
            ) from error
        resting_owner = event.seller_id if incoming.side is MarketSide.BUY else event.buyer_id
        if (
            resting.product is not incoming.product
            or resting.side is incoming.side
            or resting.owner_id != resting_owner
            or resting.limit_price != event.unit_price
        ):
            raise MarketProjectionError(
                f"trade '{event.trade_id}' does not match its persisted maker order"
            )
        return resting

    def _add(self, order: OpenOrderView) -> None:
        if order.order_id in self.orders:
            raise MarketProjectionError(f"duplicate active order '{order.order_id}'")
        self.orders[order.order_id] = order

    def _remove(self, order_id: Identifier) -> OpenOrderView:
        try:
            return self.orders.pop(order_id)
        except KeyError as error:
            raise MarketProjectionError(f"active order '{order_id}' does not exist") from error

    def _book(self, product: ProductId) -> MarketOrderBook:
        bids = self._levels(product, MarketSide.BUY)
        asks = self._levels(product, MarketSide.SELL)
        best_bid = bids[0].unit_price if bids else None
        best_ask = asks[0].unit_price if asks else None
        return MarketOrderBook(
            product=product,
            bids=bids,
            asks=asks,
            last_trade_price=self.last_trade_price.get(product),
            best_bid=best_bid,
            best_ask=best_ask,
            spread=(
                best_ask - best_bid
                if best_bid is not None and best_ask is not None
                else None
            ),
        )

    def _levels(
        self,
        product: ProductId,
        side: MarketSide,
    ) -> tuple[MarketPriceLevel, ...]:
        orders_by_price: dict[Decimal, list[OpenOrderView]] = defaultdict(list)
        for order in self.orders.values():
            if order.product is product and order.side is side:
                orders_by_price[order.limit_price].append(order)
        prices = sorted(orders_by_price, reverse=side is MarketSide.BUY)
        return tuple(
            MarketPriceLevel(
                unit_price=price,
                size=sum(
                    (order.remaining_quantity for order in orders_by_price[price]),
                    Decimal(),
                ),
                orders=tuple(sorted(orders_by_price[price], key=_time_priority)),
            )
            for price in prices
        )


class MarketTimelineProjector:
    """Project one day's journal behind a single typed read interface."""

    def project_day(
        self,
        scenario: ScenarioSpec,
        day: int,
        turns: tuple[TurnRecord, ...],
        system_steps: tuple[SystemStepRecord, ...],
        minutes: tuple[int, ...],
    ) -> dict[int, MarketFrame]:
        """Return exact end-state market frames for requested simulation minutes."""
        replay = _MarketReplay(scenario)
        entries: tuple[TurnRecord | SystemStepRecord, ...] = (
            *(record for record in system_steps if record.occurred_at.day + 1 == day),
            *(record for record in turns if record.turn.sim_time.day + 1 == day),
        )
        by_minute: dict[int, list[TurnRecord | SystemStepRecord]] = defaultdict(list)
        for record in sorted(entries, key=_journal_key):
            by_minute[_journal_key(record)[0]].append(record)

        requested_minutes = frozenset(minutes)
        frames: dict[int, MarketFrame] = {}
        for minute in sorted(requested_minutes | by_minute.keys()):
            order_flow: list[MarketOrderFlowItem] = []
            trades: list[TimelineTrade] = []
            for record in by_minute.get(minute, ()):
                if isinstance(record, TurnRecord):
                    projection = replay.apply_turn(record)
                    if projection.flow is not None:
                        order_flow.append(projection.flow)
                    trades.extend(projection.trades)
                else:
                    replay.apply_system_step(record)
            if minute in requested_minutes:
                frames[minute] = replay.frame(tuple(order_flow), tuple(trades))
        return frames


def _journal_key(record: TurnRecord | SystemStepRecord) -> tuple[int, int, int]:
    if isinstance(record, TurnRecord):
        return (
            record.turn.sim_time.absolute_minute,
            record.journal_sequence or record.outcome.apply_sequence,
            record.outcome.apply_sequence,
        )
    return (record.occurred_at.absolute_minute, record.journal_sequence, 0)


def _reduce(order: OpenOrderView, quantity: Decimal) -> OpenOrderView | None:
    if quantity > order.remaining_quantity:
        raise MarketProjectionError("trade quantity exceeds its projected order")
    remaining = order.remaining_quantity - quantity
    return order.model_copy(update={"remaining_quantity": remaining}) if remaining else None


def _matched_quantity(matches: tuple[MarketMatchLeg, ...]) -> Decimal:
    return sum((match.quantity for match in matches), Decimal())


def _time_priority(order: OpenOrderView) -> tuple[int, int, str]:
    return (
        order.placed_at.absolute_minute,
        order.priority_sequence,
        order.order_id,
    )
