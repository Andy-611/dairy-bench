"""Observer-only reconstruction of each day's continuous market state."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from company_bench.domain.models import (
    ZERO,
    CompanyBankruptEvent,
    Identifier,
    PositiveMoney,
    ProductId,
    Quantity,
    ScenarioSpec,
    TradeExecutedEvent,
)
from company_bench.economy.market import PriceTimeQueue
from company_bench.runtime.models import (
    MarketSide,
    OpenOrderView,
    QuoteLevelAction,
    QuoteLevelResult,
    SetQuoteLadder,
    SystemEventKind,
    SystemStepRecord,
    TurnRecord,
)
from company_bench.timeline.models import (
    MarketFrame,
    MarketMatchLeg,
    MarketOrderBook,
    MarketOrderCancelled,
    MarketOrderFlowItem,
    MarketOrderPlaced,
    MarketOrderPreserved,
    MarketOrderReplaced,
    MarketPriceLevel,
    TimelineTrade,
)


class MarketProjectionError(ValueError):
    """Raised when a persisted journal cannot produce a coherent order book."""


@dataclass(frozen=True, slots=True)
class _TurnMarketProjection:
    """Compact market effects emitted by one accepted company turn."""

    flows: tuple[MarketOrderFlowItem, ...] = ()
    trades: tuple[TimelineTrade, ...] = ()


@dataclass(slots=True)
class _MarketReplay:
    """Maintain exact active orders from authoritative action and trade facts."""

    scenario: ScenarioSpec
    orders: dict[Identifier, OpenOrderView] = field(default_factory=dict)
    last_trade_price: dict[ProductId, PositiveMoney] = field(default_factory=dict)
    state_version: int = 0

    def reset_week(self) -> None:
        """Open a new empty weekly book and reset market statistics."""
        self.orders.clear()
        self.last_trade_price.clear()

    def apply_system_step(self, step: SystemStepRecord) -> None:
        """Apply the only system transitions that mutate the order book."""
        for record in step.effects:
            if isinstance(record.event, CompanyBankruptEvent):
                self._remove_bankrupt_orders(record.event)
        if step.kind is SystemEventKind.WEEK_OPEN:
            self.reset_week()
        elif step.kind is SystemEventKind.MARKET_CLOSE:
            self.orders.clear()
        self.state_version = step.state_version_after

    def apply_turn(self, record: TurnRecord) -> _TurnMarketProjection:
        """Apply one accepted market action and return its exact audit projection."""
        self.state_version = record.outcome.resulting_state_version
        if not record.outcome.accepted:
            return _TurnMarketProjection()

        action = record.envelope.action
        events = tuple(
            event for event in record.outcome.events if isinstance(event, TradeExecutedEvent)
        )
        bankruptcies = tuple(
            event
            for event in record.outcome.events
            if isinstance(event, CompanyBankruptEvent)
        )
        if not isinstance(action, SetQuoteLadder):
            if events or record.outcome.quote_ladder_result is not None:
                raise MarketProjectionError("only a quote ladder may carry market results")
            return _TurnMarketProjection(
                flows=self._bankruptcy_flows(
                    bankruptcies,
                    record.outcome.apply_sequence,
                )
            )
        result = record.outcome.quote_ladder_result
        if result is None or tuple(level.level for level in result.levels) != action.levels:
            raise MarketProjectionError("accepted quote ladder has no matching result")

        retired_ids = (
            *result.cancelled_order_ids,
            *(
                level.replaced_order_id
                for level in result.levels
                if level.replaced_order_id is not None
            ),
        )
        retired = {order_id: self._remove(order_id) for order_id in retired_ids}
        events_by_order: dict[Identifier, list[TradeExecutedEvent]] = defaultdict(list)
        for event in events:
            events_by_order[event.taker_order_id].append(event)

        flows: list[MarketOrderFlowItem] = []
        for level in result.levels:
            if level.action is QuoteLevelAction.KEEP:
                preserved = self._kept_order(record, level)
                flows.append(
                    MarketOrderPreserved(
                        apply_sequence=record.outcome.apply_sequence,
                        preserved_order=preserved,
                    )
                )
                continue

            incoming = self._new_order(record, level)
            remaining, matches, withdrawn_quantity = self._apply_fills(
                incoming,
                tuple(events_by_order.pop(incoming.order_id, ())),
            )
            remaining_quantity = remaining.remaining_quantity if remaining is not None else ZERO
            if remaining_quantity != level.remaining_quantity:
                raise MarketProjectionError("quote result remaining quantity is inconsistent")
            if remaining is not None:
                self._add(remaining)
            common = {
                "apply_sequence": record.outcome.apply_sequence,
                "incoming_order": incoming,
                "matches": matches,
                "matched_quantity": _matched_quantity(matches),
                "remaining_quantity": remaining_quantity,
                "withdrawn_quantity": withdrawn_quantity,
            }
            if level.action is QuoteLevelAction.PLACE:
                flows.append(MarketOrderPlaced(**common))
            elif level.replaced_order_id is not None:
                flows.append(
                    MarketOrderReplaced(
                        **common,
                        replaced_order=retired[level.replaced_order_id],
                    )
                )
            else:
                raise MarketProjectionError("replacement result has no retired order")

        flows.extend(
            MarketOrderCancelled(
                apply_sequence=record.outcome.apply_sequence,
                cancelled_order=retired[order_id],
            )
            for order_id in result.cancelled_order_ids
        )
        if events_by_order:
            raise MarketProjectionError("trade event references an unknown ladder order")

        arrives_on = record.envelope.issued_on.plus_days(
            self.scenario.runtime.delivery_duration_days
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
                arrives_on=arrives_on,
            )
            for event in events
        )
        for event in events:
            self.last_trade_price[event.product] = event.unit_price
        flows.extend(
            self._bankruptcy_flows(
                bankruptcies,
                record.outcome.apply_sequence,
            )
        )
        return _TurnMarketProjection(flows=tuple(flows), trades=trades)

    def _bankruptcy_flows(
        self,
        events: tuple[CompanyBankruptEvent, ...],
        apply_sequence: int,
    ) -> tuple[MarketOrderFlowItem, ...]:
        """Apply forced exchange exits and expose their cancelled orders."""
        return tuple(
            MarketOrderCancelled(
                apply_sequence=apply_sequence,
                cancelled_order=self._remove(order_id),
            )
            for event in events
            for order_id in event.cancelled_order_ids
        )

    def _remove_bankrupt_orders(self, event: CompanyBankruptEvent) -> None:
        """Apply a system-triggered forced exit without synthesizing a turn flow."""
        for order_id in event.cancelled_order_ids:
            self._remove(order_id)

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
        result: QuoteLevelResult,
    ) -> OpenOrderView:
        action = record.envelope.action
        if not isinstance(action, SetQuoteLadder):
            raise MarketProjectionError("new quote requires a ladder action")
        order = OpenOrderView(
            order_id=result.order_id,
            owner_id=record.turn.company_id,
            side=action.side,
            product=action.product,
            remaining_quantity=result.level.quantity,
            limit_price=result.level.limit_price,
            placed_on=record.envelope.issued_on,
            priority_sequence=result.priority_sequence,
            queue_ahead_quantity=Decimal(),
        )
        return self._snapshot(order)

    def _kept_order(
        self,
        record: TurnRecord,
        result: QuoteLevelResult,
    ) -> OpenOrderView:
        """Validate one exact retained quote against the reconstructed book."""
        action = record.envelope.action
        if not isinstance(action, SetQuoteLadder):
            raise MarketProjectionError("kept quote requires a ladder action")
        try:
            order = self.orders[result.order_id]
        except KeyError as error:
            raise MarketProjectionError("kept quote is not active") from error
        if (
            order.owner_id != record.turn.company_id
            or order.side is not action.side
            or order.product is not action.product
            or order.remaining_quantity != result.level.quantity
            or order.limit_price != result.level.limit_price
            or order.priority_sequence != result.priority_sequence
            or result.remaining_quantity != order.remaining_quantity
        ):
            raise MarketProjectionError("kept quote does not match its target result")
        return self._snapshot(order)

    def _apply_fills(
        self,
        incoming: OpenOrderView,
        events: tuple[TradeExecutedEvent, ...],
    ) -> tuple[OpenOrderView | None, tuple[MarketMatchLeg, ...], Quantity]:
        matches: list[MarketMatchLeg] = []
        withdrawn_quantity = ZERO
        for index, event in enumerate(events):
            self._validate_trade(incoming, event)
            resting = self._resting_order(incoming, event)
            updated_resting, maker_withdrawn = _settled_remainder(
                resting,
                event.quantity,
                event.maker_remaining_quantity,
            )
            matches.append(
                MarketMatchLeg(
                    trade_id=event.trade_id,
                    maker_order=resting,
                    quantity=event.quantity,
                    unit_price=event.unit_price,
                    maker_remaining_quantity=event.maker_remaining_quantity,
                    maker_withdrawn_quantity=maker_withdrawn,
                )
            )
            if updated_resting is None:
                self.orders.pop(resting.order_id)
            else:
                self.orders[resting.order_id] = updated_resting
            incoming, taker_withdrawn = _settled_remainder(
                incoming,
                event.quantity,
                event.taker_remaining_quantity,
            )
            withdrawn_quantity += taker_withdrawn
            if incoming is None and index < len(events) - 1:
                raise MarketProjectionError("an exhausted incoming order has additional fills")
        return incoming, tuple(matches), withdrawn_quantity

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
        expected = PriceTimeQueue.best_crossing(incoming, self.orders.values())
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
        if expected is None or expected.order_id != resting.order_id:
            raise MarketProjectionError(
                f"trade '{event.trade_id}' violates price-time maker priority"
            )
        return self._snapshot(resting)

    def _add(self, order: OpenOrderView) -> None:
        if order.order_id in self.orders:
            raise MarketProjectionError(f"duplicate active order '{order.order_id}'")
        self.orders[order.order_id] = order

    def _remove(self, order_id: Identifier) -> OpenOrderView:
        try:
            order = self.orders[order_id]
        except KeyError as error:
            raise MarketProjectionError(f"active order '{order_id}' does not exist") from error
        snapshot = self._snapshot(order)
        self.orders.pop(order_id)
        return snapshot

    def _snapshot(self, order: OpenOrderView) -> OpenOrderView:
        """Refresh derived queue depth at one timeline exposure boundary."""
        return order.model_copy(
            update={
                "queue_ahead_quantity": PriceTimeQueue.quantity_ahead(
                    order,
                    self.orders.values(),
                )
            }
        )

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
            spread=(best_ask - best_bid if best_bid is not None and best_ask is not None else None),
        )

    def _levels(
        self,
        product: ProductId,
        side: MarketSide,
    ) -> tuple[MarketPriceLevel, ...]:
        orders_by_price: dict[Decimal, list[OpenOrderView]] = defaultdict(list)
        for order in self.orders.values():
            if order.product is product and order.side is side:
                orders_by_price[order.limit_price].append(self._snapshot(order))
        prices = sorted(orders_by_price, reverse=side is MarketSide.BUY)
        return tuple(
            MarketPriceLevel(
                unit_price=price,
                size=sum(
                    (order.remaining_quantity for order in orders_by_price[price]),
                    Decimal(),
                ),
                orders=tuple(sorted(orders_by_price[price], key=PriceTimeQueue.priority)),
            )
            for price in prices
        )


class MarketTimelineProjector:
    """Project one week's journal behind a single typed read interface."""

    def project_week(
        self,
        scenario: ScenarioSpec,
        week: int,
        turns: tuple[TurnRecord, ...],
        system_steps: tuple[SystemStepRecord, ...],
        absolute_days: tuple[int, ...],
    ) -> dict[int, MarketFrame]:
        """Return exact end-state market frames for requested simulation days."""
        replay = _MarketReplay(scenario)
        entries: tuple[TurnRecord | SystemStepRecord, ...] = (
            *(record for record in system_steps if record.occurred_on.week == week),
            *(record for record in turns if record.turn.sim_day.week == week),
        )
        by_day: dict[int, list[TurnRecord | SystemStepRecord]] = defaultdict(list)
        for record in sorted(entries, key=_journal_key):
            by_day[_journal_key(record)[0]].append(record)

        requested_days = frozenset(absolute_days)
        frames: dict[int, MarketFrame] = {}
        for absolute_day in sorted(requested_days | by_day.keys()):
            order_flow: list[MarketOrderFlowItem] = []
            trades: list[TimelineTrade] = []
            for record in by_day.get(absolute_day, ()):
                if isinstance(record, TurnRecord):
                    projection = replay.apply_turn(record)
                    order_flow.extend(projection.flows)
                    trades.extend(projection.trades)
                else:
                    replay.apply_system_step(record)
            if absolute_day in requested_days:
                frames[absolute_day] = replay.frame(tuple(order_flow), tuple(trades))
        return frames


def _journal_key(record: TurnRecord | SystemStepRecord) -> tuple[int, int, int]:
    if isinstance(record, TurnRecord):
        return (
            record.turn.sim_day.absolute_day,
            record.journal_sequence or record.outcome.apply_sequence,
            record.outcome.apply_sequence,
        )
    return (record.occurred_on.absolute_day, record.journal_sequence, 0)


def _settled_remainder(
    order: OpenOrderView,
    quantity: Decimal,
    remaining: Quantity,
) -> tuple[OpenOrderView | None, Quantity]:
    """Apply one fill and preserve any persisted precision-driven withdrawal."""
    if quantity > order.remaining_quantity:
        raise MarketProjectionError("trade quantity exceeds its projected order")
    expected = order.remaining_quantity - quantity
    if remaining > expected:
        raise MarketProjectionError("trade remainder exceeds its projected order")
    updated = order.model_copy(update={"remaining_quantity": remaining}) if remaining else None
    return updated, expected - remaining


def _matched_quantity(matches: tuple[MarketMatchLeg, ...]) -> Decimal:
    return sum((match.quantity for match in matches), ZERO)
