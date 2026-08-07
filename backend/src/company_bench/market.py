"""Fully collateralized continuous spot-market mechanics."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Literal, Protocol, Self, TypeVar

from pydantic import Field, model_validator

from company_bench.models import (
    ZERO,
    CompanyId,
    CompanyState,
    Identifier,
    InventoryLot,
    Money,
    OrderQuantity,
    PositiveMoney,
    PositiveQuantity,
    ProductId,
    Quantity,
    StrictModel,
    WorldState,
)
from company_bench.precision import EconomicPrecision
from company_bench.runtime_models import (
    MarketSide,
    OpenOrderView,
    OrderBookView,
    PriceLevelView,
    QuoteLadder,
    QuoteLadderResult,
    QuoteLevel,
    QuoteLevelAction,
    QuoteLevelResult,
    SimTime,
)

__all__ = [
    "AssetLedger",
    "BuyOrder",
    "ContinuousSpotMarket",
    "LimitOrder",
    "MarketError",
    "MarketState",
    "OrderIdentity",
    "PriceTimeQueue",
    "QuoteLadderExecution",
    "SellOrder",
    "TradeFill",
]

TradeIdFactory = Callable[[int], Identifier]


@dataclass(frozen=True, slots=True)
class OrderIdentity:
    """Engine-owned identity and FIFO sequence for one new order."""

    order_id: Identifier
    priority_sequence: int


OrderIdentityFactory = Callable[[int], OrderIdentity]


class _QueueOrder(Protocol):
    """Structural fields required by price-time queue projection."""

    order_id: Identifier
    product: ProductId
    side: MarketSide
    remaining_quantity: PositiveQuantity
    limit_price: PositiveMoney
    placed_at: SimTime
    priority_sequence: int


_QueueOrderT = TypeVar("_QueueOrderT", bound=_QueueOrder)


class PriceTimeQueue:
    """Own the FIFO rule shared by matching and order-book projections."""

    @staticmethod
    def priority(order: _QueueOrder) -> tuple[int, int, str]:
        """Return one order's time-priority key within a price level."""
        return (
            order.placed_at.absolute_minute,
            order.priority_sequence,
            order.order_id,
        )

    @classmethod
    def quantity_ahead(
        cls,
        order: _QueueOrder,
        candidates: Iterable[_QueueOrder],
    ) -> Quantity:
        """Sum active same-level quantity with earlier time priority."""
        priority = cls.priority(order)
        return sum(
            (
                candidate.remaining_quantity
                for candidate in candidates
                if candidate.product is order.product
                and candidate.side is order.side
                and candidate.limit_price == order.limit_price
                and cls.priority(candidate) < priority
            ),
            start=ZERO,
        )

    @classmethod
    def best_crossing(
        cls,
        incoming: _QueueOrder,
        candidates: Iterable[_QueueOrderT],
    ) -> _QueueOrderT | None:
        """Select the executable maker by price and then FIFO priority."""
        crossing = tuple(
            candidate
            for candidate in candidates
            if candidate.product is incoming.product
            and candidate.side is not incoming.side
            and (
                incoming.limit_price >= candidate.limit_price
                if incoming.side is MarketSide.BUY
                else incoming.limit_price <= candidate.limit_price
            )
        )
        if not crossing:
            return None
        return min(
            crossing,
            key=lambda order: (
                order.limit_price if incoming.side is MarketSide.BUY else -order.limit_price,
                *cls.priority(order),
            ),
        )


class MarketError(ValueError):
    """A typed rejection that leaves the caller's persisted state untouched."""


@dataclass(slots=True)
class _Account:
    company_id: CompanyId
    cash: Money
    inventory: list[InventoryLot]


class AssetLedger:
    """Own exact mutable cash and on-hand inventory inside one transaction."""

    def __init__(
        self,
        companies: Iterable[CompanyState],
        *,
        next_lot_sequence: int = 1,
    ) -> None:
        states = tuple(companies)
        company_ids = [state.company_id for state in states]
        lot_ids = [lot.lot_id for state in states for lot in state.inventory]
        if not states:
            raise MarketError("asset ledger requires at least one company")
        if len(company_ids) != len(set(company_ids)):
            raise MarketError("company ids must be unique")
        if len(lot_ids) != len(set(lot_ids)):
            raise MarketError("inventory lot ids must be globally unique")
        if next_lot_sequence < 1:
            raise MarketError("next_lot_sequence must be positive")

        self._company_order = tuple(company_ids)
        self._accounts = {
            state.company_id: _Account(
                company_id=state.company_id,
                cash=state.cash,
                inventory=list(state.inventory),
            )
            for state in states
        }
        self._known_lot_ids = set(lot_ids)
        self._next_lot_sequence = next_lot_sequence

    @classmethod
    def from_world(cls, world: WorldState, *, next_lot_sequence: int = 1) -> Self:
        """Build a ledger from one immutable world snapshot."""
        return cls(world.companies, next_lot_sequence=next_lot_sequence)

    @classmethod
    def from_companies(
        cls,
        companies: Iterable[CompanyState],
        *,
        next_lot_sequence: int = 1,
    ) -> Self:
        """Build a ledger from immutable company snapshots."""
        return cls(companies, next_lot_sequence=next_lot_sequence)

    @property
    def next_lot_sequence(self) -> int:
        """Return the deterministic counter required by the next transaction."""
        return self._next_lot_sequence

    def cash(self, company_id: CompanyId) -> Money:
        """Return currently available, unreserved cash."""
        return self._account(company_id).cash

    def quantity(self, company_id: CompanyId, product: ProductId) -> Quantity:
        """Return currently available, unreserved on-hand inventory."""
        return sum(
            (lot.quantity for lot in self._account(company_id).inventory if lot.product is product),
            start=ZERO,
        )

    def debit_cash(self, company_id: CompanyId, amount: Money) -> None:
        """Debit an exact affordable amount."""
        amount = EconomicPrecision.normalize_exact(amount)
        account = self._account(company_id)
        if amount < ZERO:
            raise MarketError("cash debit must be nonnegative")
        if amount > account.cash:
            raise MarketError(f"insufficient cash: requested {amount}, available {account.cash}")
        account.cash -= amount

    def credit_cash(self, company_id: CompanyId, amount: Money) -> None:
        """Credit an exact nonnegative amount."""
        amount = EconomicPrecision.normalize_exact(amount)
        if amount < ZERO:
            raise MarketError("cash credit must be nonnegative")
        self._account(company_id).cash += amount

    def reserve_cash(self, company_id: CompanyId, amount: Money) -> None:
        """Move exact cash out of a company's available balance."""
        self.debit_cash(company_id, amount)

    def restore_cash(self, company_id: CompanyId, amount: Money) -> None:
        """Return previously reserved cash to its owner."""
        self.credit_cash(company_id, amount)

    def reserve_inventory(
        self,
        company_id: CompanyId,
        product: ProductId,
        quantity: PositiveQuantity,
    ) -> tuple[InventoryLot, ...]:
        """Remove an exact quantity from on-hand inventory in FEFO order."""
        quantity = EconomicPrecision.normalize_exact(quantity)
        if quantity <= ZERO:
            raise MarketError("reserved inventory quantity must be positive")
        available = self.quantity(company_id, product)
        if quantity > available:
            raise MarketError(
                f"insufficient inventory: requested {quantity}, available {available}"
            )
        account = self._account(company_id)
        reserved, retained = _take_fefo(account.inventory, product, quantity)
        account.inventory = retained
        retained_ids = {lot.lot_id for lot in retained}
        return tuple(
            lot.model_copy(update={"lot_id": self._allocate_lot_id(company_id, "reserve")})
            if lot.lot_id in retained_ids
            else lot
            for lot in reserved
        )

    def restore_inventory(
        self,
        company_id: CompanyId,
        lots: Iterable[InventoryLot],
    ) -> None:
        """Return reserved or delivered lots to one company's on-hand inventory."""
        restored = tuple(lots)
        account = self._account(company_id)
        existing_ids = {lot.lot_id for lot in account.inventory}
        restored_ids = [lot.lot_id for lot in restored]
        if len(restored_ids) != len(set(restored_ids)):
            raise MarketError("restored lot ids must be unique")
        if existing_ids.intersection(restored_ids):
            raise MarketError("restored lot already exists in on-hand inventory")
        account.inventory.extend(restored)
        self._known_lot_ids.update(restored_ids)

    def relabel_for_buyer(
        self,
        buyer_id: CompanyId,
        lots: Iterable[InventoryLot],
    ) -> tuple[InventoryLot, ...]:
        """Give dispatched lots globally unique buyer-side identities."""
        self._account(buyer_id)
        return tuple(
            lot.model_copy(update={"lot_id": self._allocate_lot_id(buyer_id, "trade")})
            for lot in lots
        )

    def track_lots(self, lots: Iterable[InventoryLot]) -> None:
        """Include off-ledger reserved lots in future identity allocation."""
        self._known_lot_ids.update(lot.lot_id for lot in lots)

    def freeze_states(self) -> tuple[CompanyState, ...]:
        """Freeze accounts in their original deterministic company order."""
        return tuple(
            CompanyState(
                company_id=company_id,
                cash=(account := self._account(company_id)).cash,
                inventory=tuple(sorted(account.inventory, key=_lot_priority)),
            )
            for company_id in self._company_order
        )

    def clone(self) -> AssetLedger:
        """Create an independent transaction with every identity reservation."""
        cloned = AssetLedger.from_companies(
            self.freeze_states(),
            next_lot_sequence=self._next_lot_sequence,
        )
        cloned._known_lot_ids = set(self._known_lot_ids)
        return cloned

    def _commit_from(self, staged: AssetLedger) -> None:
        """Atomically adopt a compatible staged transaction."""
        if staged._company_order != self._company_order:
            raise MarketError("staged ledger companies do not match")
        self._accounts = {
            company_id: _Account(
                company_id=company_id,
                cash=(account := staged._account(company_id)).cash,
                inventory=list(account.inventory),
            )
            for company_id in self._company_order
        }
        self._known_lot_ids = set(staged._known_lot_ids)
        self._next_lot_sequence = staged._next_lot_sequence

    def _account(self, company_id: CompanyId) -> _Account:
        try:
            return self._accounts[company_id]
        except KeyError as error:
            raise MarketError(f"unknown company: {company_id}") from error

    def _allocate_lot_id(self, owner_id: CompanyId, source: str) -> Identifier:
        while True:
            candidate = f"{source}_{owner_id}_{self._next_lot_sequence}"
            self._next_lot_sequence += 1
            if candidate not in self._known_lot_ids:
                self._known_lot_ids.add(candidate)
                return candidate


class _BaseOrder(StrictModel):
    """Fields shared by fully collateralized resting orders."""

    order_id: Identifier
    owner_id: CompanyId
    product: ProductId
    remaining_quantity: PositiveQuantity
    limit_price: PositiveMoney
    placed_at: SimTime
    priority_sequence: int = Field(ge=1)


class BuyOrder(_BaseOrder):
    """A bid backed by its full remaining limit-price cash commitment."""

    side: Literal[MarketSide.BUY] = MarketSide.BUY
    reserved_cash: PositiveMoney

    @model_validator(mode="after")
    def validate_reserve(self) -> Self:
        """Require exact full cash collateral."""
        if self.reserved_cash != _cash_value(self.remaining_quantity, self.limit_price):
            raise ValueError("buy reserve must equal the rounded remaining commitment")
        return self


class SellOrder(_BaseOrder):
    """An ask backed by exact FEFO inventory lots."""

    side: Literal[MarketSide.SELL] = MarketSide.SELL
    reserved_lots: tuple[InventoryLot, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_reserve(self) -> Self:
        """Require exact full inventory collateral of one product."""
        if any(lot.product is not self.product for lot in self.reserved_lots):
            raise ValueError("sell reserve lots must match the order product")
        if _lot_quantity(self.reserved_lots) != self.remaining_quantity:
            raise ValueError("sell reserve must equal remaining quantity")
        lot_ids = [lot.lot_id for lot in self.reserved_lots]
        if len(lot_ids) != len(set(lot_ids)):
            raise ValueError("sell reserve lot ids must be unique")
        return self


type LimitOrder = Annotated[BuyOrder | SellOrder, Field(discriminator="side")]


@dataclass(frozen=True, slots=True)
class _QuotePlanLevel:
    """One deterministic target-to-existing reconciliation decision."""

    level: QuoteLevel
    action: QuoteLevelAction
    prior_order: LimitOrder | None = None


@dataclass(frozen=True, slots=True)
class _QuotePlan:
    """Complete staged mutation plan for one company-side ladder."""

    levels: tuple[_QuotePlanLevel, ...]
    cancelled_orders: tuple[LimitOrder, ...]


class TradeFill(StrictModel):
    """One maker-priced fill and its dispatched buyer-side lots."""

    trade_id: Identifier
    product: ProductId
    seller_id: CompanyId
    buyer_id: CompanyId
    quantity: PositiveQuantity
    unit_price: PositiveMoney
    total_value: PositiveMoney
    maker_order_id: Identifier
    taker_order_id: Identifier
    maker_remaining_quantity: Quantity
    taker_remaining_quantity: Quantity
    delivery_lots: tuple[InventoryLot, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_fill(self) -> Self:
        """Keep value and dispatched inventory exactly aligned."""
        if self.total_value != _cash_value(self.quantity, self.unit_price):
            raise ValueError("trade value must equal rounded quantity times unit price")
        if any(lot.product is not self.product for lot in self.delivery_lots):
            raise ValueError("delivery lots must match the traded product")
        if _lot_quantity(self.delivery_lots) != self.quantity:
            raise ValueError("delivery lots must equal the traded quantity")
        return self


@dataclass(frozen=True, slots=True)
class QuoteLadderExecution:
    """Atomic ladder result plus every immediate fill."""

    result: QuoteLadderResult
    fills: tuple[TradeFill, ...]


class MarketState(StrictModel):
    """Complete immutable state of one continuous product market."""

    product: ProductId
    is_open: bool = True
    orders: tuple[LimitOrder, ...] = ()
    volume: Quantity = ZERO
    traded_value: Money = ZERO
    last_trade_price: PositiveMoney | None = None

    @model_validator(mode="after")
    def validate_book(self) -> Self:
        """Reject duplicate, uncollateralized, closed, or crossed books."""
        order_ids = [order.order_id for order in self.orders]
        priorities = [order.priority_sequence for order in self.orders]
        if len(order_ids) != len(set(order_ids)):
            raise ValueError("market order ids must be unique")
        if len(priorities) != len(set(priorities)):
            raise ValueError("market priority sequences must be unique")
        if any(order.product is not self.product for order in self.orders):
            raise ValueError("every order must match the market product")
        ladders: dict[tuple[CompanyId, MarketSide], list[LimitOrder]] = {}
        for order in self.orders:
            ladders.setdefault((order.owner_id, order.side), []).append(order)
        if any(len(orders) > 3 for orders in ladders.values()):
            raise ValueError("a company quote ladder cannot exceed three levels")
        if any(
            len({order.limit_price for order in orders}) != len(orders)
            for orders in ladders.values()
        ):
            raise ValueError("a company quote ladder cannot repeat a price")
        reserved_lot_ids = [
            lot.lot_id
            for order in self.orders
            if isinstance(order, SellOrder)
            for lot in order.reserved_lots
        ]
        if len(reserved_lot_ids) != len(set(reserved_lot_ids)):
            raise ValueError("reserved lot ids must be unique across the market")
        if not self.is_open and self.orders:
            raise ValueError("a closed market cannot retain orders")
        if self.volume == ZERO:
            if self.traded_value != ZERO or self.last_trade_price is not None:
                raise ValueError("an untraded market cannot have trade statistics")
        elif self.traded_value <= ZERO or self.last_trade_price is None:
            raise ValueError("a traded market requires value and a last price")

        bids = [order.limit_price for order in self.orders if isinstance(order, BuyOrder)]
        asks = [order.limit_price for order in self.orders if isinstance(order, SellOrder)]
        if bids and asks and max(bids) >= min(asks):
            raise ValueError("a continuous order book cannot remain crossed")
        return self

    def view(self) -> OrderBookView:
        """Project complete anonymous depth without rebuilding an asset ledger."""
        return OrderBookView(
            product=self.product,
            bids=_price_levels(self.orders, MarketSide.BUY),
            asks=_price_levels(self.orders, MarketSide.SELL),
            last_trade_price=self.last_trade_price,
            daily_volume=self.volume,
        )

    def company_order_views(self, company_id: CompanyId) -> tuple[OpenOrderView, ...]:
        """Project one company's active orders with current queue depth."""
        return tuple(
            _open_order_view(order, self.orders)
            for order in self.orders
            if order.owner_id == company_id
        )


class ContinuousSpotMarket:
    """Match fully backed orders behind a small deterministic interface."""

    def __init__(self, state: MarketState, assets: AssetLedger) -> None:
        self._state = state
        self._assets = assets
        self._assets.track_lots(
            lot
            for order in state.orders
            if isinstance(order, SellOrder)
            for lot in order.reserved_lots
        )

    @classmethod
    def open(cls, product: ProductId, assets: AssetLedger) -> Self:
        """Create an empty open market for one product."""
        return cls(MarketState(product=product), assets)

    @property
    def state(self) -> MarketState:
        """Return the current immutable market state."""
        return self._state

    def set_quote_ladder(
        self,
        *,
        owner_id: CompanyId,
        ladder: QuoteLadder,
        placed_at: SimTime,
        order_identity_factory: OrderIdentityFactory,
        trade_id_factory: TradeIdFactory,
    ) -> QuoteLadderExecution:
        """Reconcile one target ladder atomically, preserving exact quotes."""
        self._require_open()
        staged_assets = self._assets.clone()
        staged = ContinuousSpotMarket(self._state, staged_assets)
        plan = staged._quote_plan(owner_id, ladder.side, ladder.levels)
        retired = (
            *(
                level.prior_order
                for level in plan.levels
                if level.action is QuoteLevelAction.REPLACE and level.prior_order is not None
            ),
            *plan.cancelled_orders,
        )
        for order in retired:
            staged._cancel(order_id=order.order_id, owner_id=owner_id)
        staged._require_ladder_collateral(owner_id, ladder.side, plan.levels)

        fills: list[TradeFill] = []
        results: list[QuoteLevelResult] = []
        new_order_count = 0
        for target in plan.levels:
            prior = target.prior_order
            if target.action is QuoteLevelAction.KEEP:
                if prior is None:
                    raise RuntimeError("kept quote has no prior order")
                results.append(
                    QuoteLevelResult(
                        level=target.level,
                        action=target.action,
                        order_id=prior.order_id,
                        priority_sequence=prior.priority_sequence,
                        remaining_quantity=prior.remaining_quantity,
                    )
                )
                continue

            new_order_count += 1
            identity = order_identity_factory(new_order_count)
            fill_offset = len(fills)
            fills.extend(
                staged._place(
                    order_id=identity.order_id,
                    owner_id=owner_id,
                    side=ladder.side,
                    quantity=target.level.quantity,
                    limit_price=target.level.limit_price,
                    placed_at=placed_at,
                    priority_sequence=identity.priority_sequence,
                    trade_id_factory=lambda offset, base=fill_offset: trade_id_factory(
                        base + offset
                    ),
                )
            )
            remaining = next(
                (
                    order.remaining_quantity
                    for order in staged.state.orders
                    if order.order_id == identity.order_id
                ),
                ZERO,
            )
            results.append(
                QuoteLevelResult(
                    level=target.level,
                    action=target.action,
                    order_id=identity.order_id,
                    replaced_order_id=(
                        prior.order_id
                        if target.action is QuoteLevelAction.REPLACE and prior is not None
                        else None
                    ),
                    priority_sequence=identity.priority_sequence,
                    remaining_quantity=remaining,
                )
            )

        result = QuoteLadderResult(
            levels=tuple(results),
            cancelled_order_ids=tuple(order.order_id for order in plan.cancelled_orders),
        )
        self._assets._commit_from(staged_assets)
        self._state = staged.state
        return QuoteLadderExecution(result=result, fills=tuple(fills))

    def _place(
        self,
        *,
        order_id: Identifier,
        owner_id: CompanyId,
        side: MarketSide,
        quantity: OrderQuantity,
        limit_price: PositiveMoney,
        placed_at: SimTime,
        priority_sequence: int,
        trade_id_factory: TradeIdFactory,
    ) -> tuple[TradeFill, ...]:
        """Reserve one exact order, match immediately, and rest any remainder."""
        self._require_open()
        self._require_new_identity(order_id, priority_sequence)
        self._reject_self_cross(owner_id, side, limit_price)
        order = self._reserve_order(
            order_id=order_id,
            owner_id=owner_id,
            side=side,
            quantity=quantity,
            limit_price=limit_price,
            placed_at=placed_at,
            priority_sequence=priority_sequence,
        )
        return self._match(order, trade_id_factory)

    def _cancel(self, *, order_id: Identifier, owner_id: CompanyId) -> LimitOrder:
        """Cancel one owned order and release its full remaining collateral."""
        self._require_open()
        order = self._owned_order(order_id, owner_id)
        self._release(order)
        self._state = self._state.model_copy(
            update={
                "orders": tuple(
                    candidate for candidate in self._state.orders if candidate.order_id != order_id
                )
            }
        )
        return order

    def _quote_plan(
        self,
        owner_id: CompanyId,
        side: MarketSide,
        levels: tuple[QuoteLevel, ...],
    ) -> _QuotePlan:
        """Pair target levels with existing quotes using one stable convention."""
        existing = sorted(
            (
                order
                for order in self._state.orders
                if order.owner_id == owner_id and order.side is side
            ),
            key=lambda order: (
                -order.limit_price if side is MarketSide.BUY else order.limit_price,
                *PriceTimeQueue.priority(order),
            ),
        )
        planned: list[_QuotePlanLevel | None] = [None] * len(levels)

        for index, level in enumerate(levels):
            prior = _pop_matching_order(
                existing,
                lambda order, target=level: (
                    order.limit_price == target.limit_price
                    and order.remaining_quantity == target.quantity
                ),
            )
            if prior is not None:
                planned[index] = _QuotePlanLevel(level, QuoteLevelAction.KEEP, prior)

        for index, level in enumerate(levels):
            if planned[index] is not None:
                continue
            prior = _pop_matching_order(
                existing,
                lambda order, target=level: order.limit_price == target.limit_price,
            )
            if prior is not None:
                planned[index] = _QuotePlanLevel(level, QuoteLevelAction.REPLACE, prior)

        unmatched_indices = [index for index, item in enumerate(planned) if item is None]
        paired_count = min(len(unmatched_indices), len(existing))
        for index, prior in zip(
            unmatched_indices[:paired_count],
            existing[:paired_count],
            strict=True,
        ):
            planned[index] = _QuotePlanLevel(
                levels[index],
                QuoteLevelAction.REPLACE,
                prior,
            )
        for index in unmatched_indices[paired_count:]:
            planned[index] = _QuotePlanLevel(levels[index], QuoteLevelAction.PLACE)
        return _QuotePlan(
            levels=tuple(item for item in planned if item is not None),
            cancelled_orders=tuple(existing[paired_count:]),
        )

    def _require_ladder_collateral(
        self,
        owner_id: CompanyId,
        side: MarketSide,
        levels: tuple[_QuotePlanLevel, ...],
    ) -> None:
        """Preflight aggregate collateral after all changed quotes are released."""
        changed = tuple(
            level.level for level in levels if level.action is not QuoteLevelAction.KEEP
        )
        if side is MarketSide.BUY:
            required = sum(
                (_cash_value(level.quantity, level.limit_price) for level in changed),
                start=ZERO,
            )
            available = self._assets.cash(owner_id)
            if required > available:
                raise MarketError(
                    f"insufficient cash for quote ladder: required {required}, "
                    f"available {available}"
                )
            return
        required = sum((level.quantity for level in changed), start=ZERO)
        available = self._assets.quantity(owner_id, self._state.product)
        if required > available:
            raise MarketError(
                f"insufficient inventory for quote ladder: requested {required}, "
                f"available {available}"
            )

    def close(self) -> tuple[LimitOrder, ...]:
        """Close the market and release every resting commitment."""
        self._require_open()
        released = self._state.orders
        for order in released:
            self._release(order)
        self._state = self._state.model_copy(update={"is_open": False, "orders": ()})
        return released

    def view(self) -> OrderBookView:
        """Return all anonymous public price levels."""
        return self._state.view()

    def _reserve_order(
        self,
        *,
        order_id: Identifier,
        owner_id: CompanyId,
        side: MarketSide,
        quantity: OrderQuantity,
        limit_price: PositiveMoney,
        placed_at: SimTime,
        priority_sequence: int,
    ) -> LimitOrder:
        if quantity <= ZERO:
            raise MarketError("order quantity must be positive")
        if limit_price <= ZERO:
            raise MarketError("order limit price must be positive")
        commitment = _cash_value(quantity, limit_price)
        if commitment <= ZERO:
            raise MarketError("order commitment rounds to zero at economic precision")
        common = {
            "order_id": order_id,
            "owner_id": owner_id,
            "product": self._state.product,
            "remaining_quantity": quantity,
            "limit_price": limit_price,
            "placed_at": placed_at,
            "priority_sequence": priority_sequence,
        }
        if side is MarketSide.BUY:
            self._assets.reserve_cash(owner_id, commitment)
            return BuyOrder(**common, reserved_cash=commitment)
        if side is MarketSide.SELL:
            lots = self._assets.reserve_inventory(owner_id, self._state.product, quantity)
            return SellOrder(**common, reserved_lots=lots)
        raise MarketError(f"unsupported market side: {side}")

    def _match(
        self,
        incoming: LimitOrder,
        trade_id_factory: TradeIdFactory,
    ) -> tuple[TradeFill, ...]:
        orders = list(self._state.orders)
        fills: list[TradeFill] = []
        volume = self._state.volume
        traded_value = self._state.traded_value
        last_trade_price = self._state.last_trade_price

        active: LimitOrder | None = incoming
        while (
            active is not None
            and (resting := PriceTimeQueue.best_crossing(active, orders)) is not None
        ):
            quantity = min(active.remaining_quantity, resting.remaining_quantity)
            price = resting.limit_price
            buyer = active if isinstance(active, BuyOrder) else resting
            seller = active if isinstance(active, SellOrder) else resting
            if not isinstance(buyer, BuyOrder) or not isinstance(seller, SellOrder):
                raise RuntimeError("crossing orders must have opposite sides")

            value = _cash_value(quantity, price)
            if value <= ZERO:
                raise MarketError("trade value rounds to zero at economic precision")
            updated_buyer, refund = _fill_buy_order(buyer, quantity, value)
            updated_seller, seller_lots, released_lots = _fill_sell_order(
                seller,
                quantity,
            )
            updated_resting = (
                updated_buyer if isinstance(resting, BuyOrder) else updated_seller
            )
            updated_incoming = (
                updated_buyer if isinstance(active, BuyOrder) else updated_seller
            )
            self._assets.credit_cash(seller.owner_id, value)
            self._assets.credit_cash(buyer.owner_id, refund)
            self._assets.restore_inventory(seller.owner_id, released_lots)
            delivery_lots = self._assets.relabel_for_buyer(buyer.owner_id, seller_lots)
            fill = TradeFill(
                trade_id=trade_id_factory(len(fills) + 1),
                product=self._state.product,
                seller_id=seller.owner_id,
                buyer_id=buyer.owner_id,
                quantity=quantity,
                unit_price=price,
                total_value=value,
                maker_order_id=resting.order_id,
                taker_order_id=active.order_id,
                maker_remaining_quantity=_remaining_quantity(updated_resting),
                taker_remaining_quantity=_remaining_quantity(updated_incoming),
                delivery_lots=delivery_lots,
            )
            fills.append(fill)
            volume += quantity
            traded_value += value
            last_trade_price = price

            _update_resting(orders, resting.order_id, updated_resting)
            active = updated_incoming

        if active is not None:
            orders.append(active)
        self._state = self._state.model_copy(
            update={
                "orders": tuple(sorted(orders, key=_book_storage_priority)),
                "volume": volume,
                "traded_value": traded_value,
                "last_trade_price": last_trade_price,
            }
        )
        return tuple(fills)

    def _release(self, order: LimitOrder) -> None:
        if isinstance(order, BuyOrder):
            self._assets.restore_cash(order.owner_id, order.reserved_cash)
        else:
            self._assets.restore_inventory(order.owner_id, order.reserved_lots)

    def _owned_order(self, order_id: Identifier, owner_id: CompanyId) -> LimitOrder:
        order = next(
            (candidate for candidate in self._state.orders if candidate.order_id == order_id),
            None,
        )
        if order is None:
            raise MarketError("order does not exist")
        if order.owner_id != owner_id:
            raise MarketError("a company cannot mutate another company's order")
        return order

    def _require_open(self) -> None:
        if not self._state.is_open:
            raise MarketError("market is closed")

    def _require_new_identity(self, order_id: Identifier, priority_sequence: int) -> None:
        if any(order.order_id == order_id for order in self._state.orders):
            raise MarketError("order id already exists")
        if any(order.priority_sequence == priority_sequence for order in self._state.orders):
            raise MarketError("priority sequence already exists")

    def _reject_self_cross(
        self,
        owner_id: CompanyId,
        side: MarketSide,
        limit_price: PositiveMoney,
    ) -> None:
        if any(
            order.owner_id == owner_id
            and order.side is not side
            and (
                limit_price >= order.limit_price
                if side is MarketSide.BUY
                else limit_price <= order.limit_price
            )
            for order in self._state.orders
        ):
            raise MarketError("self-crossing orders are not allowed")


def _pop_matching_order(
    orders: list[LimitOrder],
    predicate: Callable[[LimitOrder], bool],
) -> LimitOrder | None:
    """Remove and return the first deterministically ordered match."""
    for index, order in enumerate(orders):
        if predicate(order):
            return orders.pop(index)
    return None


def _take_fefo(
    lots: Iterable[InventoryLot],
    product: ProductId,
    quantity: Decimal,
) -> tuple[tuple[InventoryLot, ...], list[InventoryLot]]:
    remaining = quantity
    taken: list[InventoryLot] = []
    retained: list[InventoryLot] = []
    for lot in sorted(lots, key=_lot_priority):
        if lot.product is not product or remaining <= ZERO:
            retained.append(lot)
            continue
        amount = min(lot.quantity, remaining)
        taken.append(lot.model_copy(update={"quantity": amount}))
        remaining -= amount
        if (leftover := lot.quantity - amount) > ZERO:
            retained.append(lot.model_copy(update={"quantity": leftover}))
    if remaining != ZERO:
        raise RuntimeError("FEFO reservation was not exact")
    return tuple(taken), retained


def _fill_buy_order(
    order: BuyOrder,
    quantity: PositiveQuantity,
    trade_value: PositiveMoney,
) -> tuple[BuyOrder | None, Money]:
    remaining = order.remaining_quantity - quantity
    available_after_trade = order.reserved_cash - trade_value
    if available_after_trade < ZERO:
        raise MarketError("reserved buy collateral cannot fund the trade")
    if remaining == ZERO:
        return None, available_after_trade
    reserved = _cash_value(remaining, order.limit_price)
    if reserved <= ZERO or reserved > available_after_trade:
        return None, available_after_trade
    refund = available_after_trade - reserved
    return (
        order.model_copy(update={"remaining_quantity": remaining, "reserved_cash": reserved}),
        refund,
    )


def _fill_sell_order(
    order: SellOrder,
    quantity: PositiveQuantity,
) -> tuple[
    SellOrder | None,
    tuple[InventoryLot, ...],
    tuple[InventoryLot, ...],
]:
    sold, retained = _take_fefo(order.reserved_lots, order.product, quantity)
    remaining = order.remaining_quantity - quantity
    if remaining == ZERO:
        if retained:
            raise RuntimeError("filled sell order retained inventory")
        return None, sold, ()
    if _cash_value(remaining, order.limit_price) <= ZERO:
        return None, sold, tuple(retained)
    return (
        order.model_copy(
            update={"remaining_quantity": remaining, "reserved_lots": tuple(retained)}
        ),
        sold,
        (),
    )


def _update_resting(
    orders: list[LimitOrder],
    order_id: Identifier,
    updated: LimitOrder | None,
) -> None:
    index = next(index for index, order in enumerate(orders) if order.order_id == order_id)
    if updated is None:
        orders.pop(index)
    else:
        orders[index] = updated


def _price_levels(
    orders: tuple[LimitOrder, ...],
    side: MarketSide,
) -> tuple[PriceLevelView, ...]:
    orders_by_price: dict[Decimal, list[LimitOrder]] = {}
    for order in orders:
        if order.side is side:
            orders_by_price.setdefault(order.limit_price, []).append(order)
    return tuple(
        PriceLevelView(
            unit_price=price,
            quantity=sum(
                (order.remaining_quantity for order in orders_by_price[price]),
                start=ZERO,
            ),
            order_count=len(orders_by_price[price]),
        )
        for price in sorted(orders_by_price, reverse=side is MarketSide.BUY)
    )


def _open_order_view(
    order: LimitOrder,
    candidates: Iterable[LimitOrder],
) -> OpenOrderView:
    return OpenOrderView(
        order_id=order.order_id,
        owner_id=order.owner_id,
        side=order.side,
        product=order.product,
        remaining_quantity=order.remaining_quantity,
        limit_price=order.limit_price,
        placed_at=order.placed_at,
        priority_sequence=order.priority_sequence,
        queue_ahead_quantity=PriceTimeQueue.quantity_ahead(order, candidates),
    )


def _lot_quantity(lots: Iterable[InventoryLot]) -> Quantity:
    return sum((lot.quantity for lot in lots), start=ZERO)


def _remaining_quantity(order: LimitOrder | None) -> Quantity:
    """Return a live order remainder or canonical zero after retirement."""
    return order.remaining_quantity if order is not None else ZERO


def _cash_value(quantity: Decimal, unit_price: Decimal) -> Money:
    """Return one cash boundary value under the shared precision contract."""
    return EconomicPrecision.round(quantity * unit_price)


def _lot_priority(lot: InventoryLot) -> tuple[int, int, str, str]:
    return (
        lot.expires_end_of_day,
        lot.produced_day,
        lot.product.value,
        lot.lot_id,
    )


def _book_storage_priority(order: LimitOrder) -> tuple[str, int, int, str]:
    return (order.side.value, *PriceTimeQueue.priority(order))
