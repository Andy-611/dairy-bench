from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Self

from pydantic import Field, model_validator

from company_bench.models import (
    MAX_SEED,
    QUANTITY_QUANTUM,
    ZERO,
    CompanyDecision,
    CompanyId,
    CompanyObservation,
    CompanySnapshot,
    CompanySpec,
    CompanyState,
    ConsumerSaleEvent,
    DayResult,
    DaySnapshot,
    DecisionRejectedEvent,
    DomainEvent,
    FarmDecision,
    FarmOperation,
    InventoryExpiredEvent,
    InventoryLot,
    InventoryPosition,
    MarketSummary,
    MilkProcessedEvent,
    MilkProducedEvent,
    NoOpDecision,
    ProcessorDecision,
    ProcessorOperation,
    ProductId,
    PublicCompany,
    RecordedDecision,
    RetailerDecision,
    RetailerOperation,
    ScenarioSpec,
    StrictModel,
    TradeExecutedEvent,
    WorldState,
)
from company_bench.runtime_models import (
    CancelOrder,
    CommandEnvelope,
    CommandOutcome,
    CommandStatus,
    MarketSide,
    OpenOrderView,
    PlaceOrder,
    Produce,
    SetRetailPrice,
    Transform,
    Wait,
)


@dataclass(slots=True)
class _Account:
    """Mutable account used only inside one atomic engine step."""

    company_id: str
    cash: Decimal
    inventory: list[InventoryLot]

    def quantity(self, product: ProductId) -> Decimal:
        """Return available inventory for one product."""
        return sum(
            (lot.quantity for lot in self.inventory if lot.product == product),
            start=ZERO,
        )

    def remove_fefo(
        self,
        product: ProductId,
        requested: Decimal,
    ) -> tuple[InventoryLot, ...]:
        """Remove up to the requested quantity, earliest expiry first."""
        remaining = requested
        removed: list[InventoryLot] = []
        retained: list[InventoryLot] = []
        ordered = sorted(
            self.inventory,
            key=lambda lot: (
                lot.expires_end_of_day,
                lot.produced_day,
                lot.lot_id,
            ),
        )
        for lot in ordered:
            if lot.product != product or remaining <= ZERO:
                retained.append(lot)
                continue

            taken = min(lot.quantity, remaining)
            removed.append(lot.model_copy(update={"quantity": taken}))
            remaining -= taken
            leftover = lot.quantity - taken
            if leftover > ZERO:
                retained.append(lot.model_copy(update={"quantity": leftover}))

        self.inventory = retained
        return tuple(removed)


class _Ledger:
    """Centralizes every cash and inventory write within an engine step."""

    def __init__(
        self,
        state: WorldState,
        day: int,
        *,
        lot_sequence: int = 0,
    ) -> None:
        self._day = day
        self._lot_sequence = lot_sequence
        self._accounts = {
            company.company_id: _Account(
                company_id=company.company_id,
                cash=company.cash,
                inventory=list(company.inventory),
            )
            for company in state.companies
        }

    def account(self, company_id: str) -> _Account:
        """Return one internal account."""
        return self._accounts[company_id]

    def quantity(self, company_id: str, product: ProductId) -> Decimal:
        """Return one company's available product quantity."""
        return self.account(company_id).quantity(product)

    def affordable_quantity(
        self,
        company_id: str,
        unit_price: Decimal,
    ) -> Decimal:
        """Return the most a company can buy without negative cash."""
        return (self.account(company_id).cash / unit_price).quantize(
            QUANTITY_QUANTUM, rounding=ROUND_DOWN
        )

    def charge(self, company_id: str, amount: Decimal) -> None:
        """Deduct a known-affordable amount."""
        account = self.account(company_id)
        if amount < ZERO or amount > account.cash:
            raise ValueError("charge would violate nonnegative cash")
        account.cash -= amount

    def credit(self, company_id: str, amount: Decimal) -> None:
        """Credit nonnegative cash."""
        if amount < ZERO:
            raise ValueError("credit must be nonnegative")
        self.account(company_id).cash += amount

    def add_new_lot(
        self,
        company_id: str,
        product: ProductId,
        quantity: Decimal,
        shelf_life_days: int,
        source: str,
    ) -> str | None:
        """Create a fresh product lot and return its stable identity."""
        if quantity <= ZERO:
            return None
        lot_id = self._next_lot_id(company_id, source)
        self.account(company_id).inventory.append(
            InventoryLot(
                lot_id=lot_id,
                product=product,
                quantity=quantity,
                produced_day=self._day,
                expires_end_of_day=self._day + shelf_life_days - 1,
            )
        )
        return lot_id

    def transfer(
        self,
        seller_id: str,
        buyer_id: str,
        product: ProductId,
        quantity: Decimal,
        unit_price: Decimal,
    ) -> None:
        """Atomically transfer exact cash and FEFO inventory."""
        total_value = quantity * unit_price
        seller = self.account(seller_id)
        buyer = self.account(buyer_id)
        if seller.quantity(product) < quantity:
            raise ValueError("seller lacks inventory")
        if buyer.cash < total_value:
            raise ValueError("buyer lacks cash")

        moved_lots = seller.remove_fefo(product, quantity)
        self.charge(buyer_id, total_value)
        self.credit(seller_id, total_value)
        for moved in moved_lots:
            buyer.inventory.append(
                moved.model_copy(
                    update={
                        "lot_id": self._next_lot_id(
                            buyer_id,
                            f"trade_{product.value}",
                        )
                    }
                )
            )

    def consume(
        self,
        company_id: str,
        product: ProductId,
        quantity: Decimal,
    ) -> None:
        """Consume exact FEFO inventory after feasibility is checked."""
        if self.quantity(company_id, product) < quantity:
            raise ValueError("consumption exceeds inventory")
        self.account(company_id).remove_fefo(product, quantity)

    def expire(
        self,
    ) -> tuple[tuple[str, InventoryLot], ...]:
        """Remove all lots whose final usable day has completed."""
        expired: list[tuple[str, InventoryLot]] = []
        for company_id, account in self._accounts.items():
            retained: list[InventoryLot] = []
            for lot in account.inventory:
                if lot.expires_end_of_day <= self._day:
                    expired.append((company_id, lot))
                else:
                    retained.append(lot)
            account.inventory = retained
        return tuple(
            sorted(
                expired,
                key=lambda item: (
                    item[0],
                    item[1].expires_end_of_day,
                    item[1].lot_id,
                ),
            )
        )

    def states(
        self,
        scenario: ScenarioSpec,
    ) -> tuple[CompanyState, ...]:
        """Freeze internal accounts in scenario order."""
        return tuple(
            CompanyState(
                company_id=company.company_id,
                cash=self.account(company.company_id).cash,
                inventory=tuple(
                    sorted(
                        self.account(company.company_id).inventory,
                        key=lambda lot: (
                            lot.expires_end_of_day,
                            lot.product.value,
                            lot.lot_id,
                        ),
                    )
                ),
            )
            for company in scenario.companies
        )

    @property
    def lot_sequence(self) -> int:
        """Return the next checkpoint's deterministic lot counter."""
        return self._lot_sequence

    def _next_lot_id(self, company_id: str, source: str) -> str:
        """Allocate a stable lot identity within this deterministic day."""
        self._lot_sequence += 1
        return f"d{self._day}_{source}_{company_id}_{self._lot_sequence}"


@dataclass(slots=True)
class _Order:
    """Internal mutable remainder of a market order."""

    company_id: str
    quantity: Decimal
    unit_price: Decimal


class _CommandRejected(ValueError):
    """Internal control flow for a typed economic rejection."""


class CompanyQuantity(StrictModel):
    """One company's used daily operating capacity."""

    company_id: CompanyId
    quantity: Decimal = Field(ge=ZERO)


class RetailPriceState(StrictModel):
    """One retailer's current consumer price."""

    company_id: CompanyId
    product: ProductId
    unit_price: Decimal = Field(gt=ZERO)


class EconomyState(StrictModel):
    """Checkpointable state for one active event-driven business day."""

    base_state: WorldState
    day: int = Field(ge=1)
    state_version: int = Field(ge=0)
    companies: tuple[CompanyState, ...]
    orders: tuple[OpenOrderView, ...] = ()
    retail_prices: tuple[RetailPriceState, ...] = ()
    production_used: tuple[CompanyQuantity, ...] = ()
    transformation_used: tuple[CompanyQuantity, ...] = ()
    events: tuple[DomainEvent, ...] = ()
    raw_market: MarketSummary | None = None
    bottled_market: MarketSummary | None = None
    lot_sequence: int = Field(default=0, ge=0)
    order_sequence: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_active_day(self) -> Self:
        """Keep active or terminal checkpoint counters internally consistent."""
        active = self.day == self.base_state.day + 1
        terminal = (
            self.base_state.day == self.base_state.scenario.days and self.day == self.base_state.day
        )
        if not (active or terminal):
            raise ValueError("economy day must be active or terminal")
        expected = {company.company_id for company in self.base_state.scenario.companies}
        actual = [company.company_id for company in self.companies]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError("economy state must match the scenario company set")
        order_ids = [order.order_id for order in self.orders]
        if len(order_ids) != len(set(order_ids)):
            raise ValueError("open order ids must be unique")
        price_keys = [(price.company_id, price.product) for price in self.retail_prices]
        if len(price_keys) != len(set(price_keys)):
            raise ValueError("retail prices must be unique per company and product")
        return self

    @property
    def scenario(self) -> ScenarioSpec:
        """Return immutable scenario rules."""
        return self.base_state.scenario

    @property
    def seed(self) -> int:
        """Return the episode seed."""
        return self.base_state.seed


class EconomyEngine:
    """Deterministic owner of all Dairy Bench economic transitions."""

    def initial_state(
        self,
        scenario: ScenarioSpec,
        seed: int,
    ) -> WorldState:
        """Create an empty-inventory world from immutable scenario rules."""
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= MAX_SEED:
            raise ValueError(f"seed must be an integer from 0 to {MAX_SEED}")
        return WorldState(
            scenario=scenario,
            seed=seed,
            day=0,
            companies=tuple(
                CompanyState(
                    company_id=company.company_id,
                    cash=company.initial_cash,
                )
                for company in scenario.companies
            ),
        )

    def open_day(
        self,
        state: WorldState,
        *,
        state_version: int = 0,
    ) -> EconomyState:
        """Open the next event-driven day without changing economic value."""
        if state.day >= state.scenario.days:
            raise ValueError("the episode is already complete")
        return EconomyState(
            base_state=state,
            day=state.day + 1,
            state_version=state_version,
            companies=state.companies,
        )

    def observe_active(
        self,
        economy: EconomyState,
        company_id: CompanyId,
    ) -> CompanyObservation:
        """Project one private-safe observation from current intraday facts."""
        current = economy.base_state.model_copy(update={"companies": economy.companies})
        observation = next(
            observation
            for observation in self.observe(current)
            if observation.company_id == company_id
        )
        retail_price = next(
            (price.unit_price for price in economy.retail_prices if price.company_id == company_id),
            None,
        )
        return observation.model_copy(update={"retail_price": retail_price})

    @staticmethod
    def company_orders(
        economy: EconomyState,
        company_id: CompanyId,
    ) -> tuple[OpenOrderView, ...]:
        """Return only the active commitments owned by one company."""
        return tuple(order for order in economy.orders if order.owner_id == company_id)

    def apply_batch(
        self,
        economy: EconomyState,
        envelopes: tuple[CommandEnvelope, ...],
        *,
        first_apply_sequence: int,
        apply_sequences: tuple[int, ...] | None = None,
    ) -> tuple[EconomyState, tuple[CommandOutcome, ...]]:
        """Apply same-snapshot commands in caller-supplied deterministic order."""
        if first_apply_sequence < 1:
            raise ValueError("first_apply_sequence must be positive")
        company_ids = [envelope.company_id for envelope in envelopes]
        if len(company_ids) != len(set(company_ids)):
            raise ValueError("a company may submit at most one command per batch")
        if any(envelope.state_version != economy.state_version for envelope in envelopes):
            raise ValueError("every batch command must target the shared base state version")
        sequences = apply_sequences or tuple(
            range(first_apply_sequence, first_apply_sequence + len(envelopes))
        )
        if len(sequences) != len(envelopes) or any(
            sequence < first_apply_sequence for sequence in sequences
        ):
            raise ValueError("apply_sequences must align with the submitted envelopes")
        if tuple(sorted(set(sequences))) != sequences:
            raise ValueError("apply_sequences must be unique and increasing")

        current = economy
        outcomes: list[CommandOutcome] = []
        for envelope, apply_sequence in zip(envelopes, sequences, strict=True):
            current, outcome = self._apply_command(
                current,
                envelope,
                apply_sequence,
            )
            outcomes.append(outcome)
        return current, tuple(outcomes)

    def clear_active_market(
        self,
        economy: EconomyState,
        product: ProductId,
    ) -> EconomyState:
        """Clear one standing-order market at its configured system event."""
        market_field = "raw_market" if product is ProductId.RAW_MILK else "bottled_market"
        if getattr(economy, market_field) is not None:
            raise ValueError(f"{product.value} market already cleared")

        ledger = self._active_ledger(economy)
        orders = tuple(order for order in economy.orders if order.product is product)
        sellers = [
            _Order(
                company_id=order.owner_id,
                quantity=order.remaining_quantity,
                unit_price=order.limit_price,
            )
            for order in orders
            if order.side is MarketSide.SELL
        ]
        buyers = [
            _Order(
                company_id=order.owner_id,
                quantity=order.remaining_quantity,
                unit_price=order.limit_price,
            )
            for order in orders
            if order.side is MarketSide.BUY
        ]
        events = list(economy.events)
        market = self._clear_market(
            economy.day,
            product,
            ledger,
            sellers,
            buyers,
            events,
        )
        return economy.model_copy(
            update={
                "state_version": economy.state_version + 1,
                "companies": ledger.states(economy.scenario),
                "orders": tuple(order for order in economy.orders if order.product is not product),
                "events": tuple(events),
                "lot_sequence": ledger.lot_sequence,
                market_field: market,
            }
        )

    def close_day(self, economy: EconomyState) -> DayResult:
        """Settle consumer sales and expiry, then freeze a completed day."""
        if economy.raw_market is None or economy.bottled_market is None:
            raise ValueError("both markets must clear before day close")

        ledger = self._active_ledger(economy)
        events = list(economy.events)
        decisions = self._retail_decisions(economy)
        consumer_demand, consumer_sales = self._sell_to_consumers(
            economy.scenario,
            economy.seed,
            economy.day,
            ledger,
            decisions,
            events,
        )
        expired_quantity = self._expire(
            economy.scenario,
            economy.day,
            ledger,
            events,
        )
        markets = (economy.raw_market, economy.bottled_market)
        state = WorldState(
            scenario=economy.scenario,
            seed=economy.seed,
            day=economy.day,
            companies=ledger.states(economy.scenario),
            previous_markets=markets,
        )
        return DayResult(
            state=state,
            events=tuple(events),
            snapshot=self._snapshot(
                state,
                events,
                markets,
                consumer_demand,
                consumer_sales,
                expired_quantity,
            ),
        )

    def observe(
        self,
        state: WorldState,
    ) -> tuple[CompanyObservation, ...]:
        """Produce private-safe morning observations for every company."""
        if state.day >= state.scenario.days:
            return ()
        scenario = state.scenario
        next_day = state.day + 1
        states = {company.company_id: company for company in state.companies}
        public_companies = tuple(
            PublicCompany(
                company_id=company.company_id,
                name=company.name,
                tier=company.tier,
            )
            for company in scenario.companies
        )
        return tuple(
            CompanyObservation(
                observation_id=self._observation_id(
                    state,
                    next_day,
                    company.company_id,
                ),
                scenario_id=scenario.scenario_id,
                scenario_days=scenario.days,
                day=next_day,
                company_id=company.company_id,
                operation=company.operation,
                products=scenario.products,
                demand=scenario.demand,
                scoring=scenario.scoring,
                cash=states[company.company_id].cash,
                inventory=tuple(
                    InventoryPosition(
                        product=product.product,
                        quantity=self._inventory_quantity(
                            states[company.company_id],
                            product.product,
                        ),
                    )
                    for product in scenario.products
                ),
                public_companies=public_companies,
                previous_markets=state.previous_markets,
                runtime=scenario.runtime,
            )
            for company in scenario.companies
        )

    def step(
        self,
        state: WorldState,
        decisions: tuple[RecordedDecision, ...],
    ) -> DayResult:
        """Settle one complete day and return a new immutable state."""
        scenario = state.scenario
        if state.day >= scenario.days:
            raise ValueError("the episode is already complete")

        day = state.day + 1
        ledger = _Ledger(state, day)
        events: list[DomainEvent] = []
        accepted = self._accepted_decisions(state, decisions, events)

        self._produce(scenario, day, ledger, accepted, events)
        raw_market = self._clear_raw_market(
            scenario,
            day,
            ledger,
            accepted,
            events,
        )
        self._process(scenario, day, ledger, accepted, events)
        bottled_market = self._clear_bottled_market(
            scenario,
            day,
            ledger,
            accepted,
            events,
        )
        consumer_demand, consumer_sales = self._sell_to_consumers(
            scenario,
            state.seed,
            day,
            ledger,
            accepted,
            events,
        )
        expired_quantity = self._expire(
            scenario,
            day,
            ledger,
            events,
        )
        markets = (raw_market, bottled_market)
        next_state = WorldState(
            scenario=scenario,
            seed=state.seed,
            day=day,
            companies=ledger.states(scenario),
            previous_markets=markets,
        )
        snapshot = self._snapshot(
            next_state,
            events,
            markets,
            consumer_demand,
            consumer_sales,
            expired_quantity,
        )
        return DayResult(
            state=next_state,
            events=tuple(events),
            snapshot=snapshot,
        )

    def _apply_command(
        self,
        economy: EconomyState,
        envelope: CommandEnvelope,
        apply_sequence: int,
    ) -> tuple[EconomyState, CommandOutcome]:
        """Authorize and apply one command without exposing mutable internals."""
        command = envelope.command
        try:
            self._validate_command_time(economy, envelope)
            if isinstance(command, Produce):
                updated, order_id = self._apply_produce(economy, envelope)
            elif isinstance(command, Transform):
                updated, order_id = self._apply_transform(economy, envelope)
            elif isinstance(command, PlaceOrder):
                updated, order_id = self._apply_order(economy, envelope)
            elif isinstance(command, CancelOrder):
                updated, order_id = self._apply_cancel(economy, envelope)
            elif isinstance(command, SetRetailPrice):
                updated, order_id = self._apply_retail_price(economy, envelope)
            elif isinstance(command, Wait):
                updated, order_id = self._apply_wait(economy, envelope)
            else:
                raise TypeError(f"unsupported command: {type(command).__name__}")
        except _CommandRejected as error:
            return economy, self._command_outcome(
                economy,
                envelope,
                apply_sequence,
                accepted=False,
                reason=str(error),
            )

        new_events = updated.events[len(economy.events) :]
        return updated, self._command_outcome(
            updated,
            envelope,
            apply_sequence,
            accepted=True,
            order_id=order_id,
            events=new_events,
        )

    def _apply_produce(
        self,
        economy: EconomyState,
        envelope: CommandEnvelope,
    ) -> tuple[EconomyState, None]:
        command = envelope.command
        company = economy.scenario.company(envelope.company_id)
        operation = company.operation
        if not isinstance(command, Produce) or not isinstance(operation, FarmOperation):
            raise _CommandRejected("produce is only available to farms")
        if command.product is not operation.output_product:
            raise _CommandRejected("farm cannot produce the requested product")

        used = self._used(economy.production_used, company.company_id)
        # Calculate the remaining daily capacity.
        remaining_capacity = max(ZERO, operation.daily_capacity - used)

        ledger = self._active_ledger(economy)
        # Settle cash, inventory, and the production event atomically.
        event = self._settle_production(
            economy.scenario,
            economy.day,
            ledger,
            company.company_id,
            operation,
            command.quantity,
            remaining_capacity,
        )
        if event.actual_quantity <= ZERO:
            raise _CommandRejected("production has no available capacity or cash")

        # 返回新的 EconomyState
        return (
            economy.model_copy(
                update={
                    "state_version": economy.state_version + 1,
                    "companies": ledger.states(economy.scenario),
                    "production_used": self._set_used(
                        economy.production_used,
                        company.company_id,
                        used + event.actual_quantity,
                    ),
                    "events": (*economy.events, event),
                    "lot_sequence": ledger.lot_sequence,
                }
            ),
            None,
        )

    def _apply_transform(
        self,
        economy: EconomyState,
        envelope: CommandEnvelope,
    ) -> tuple[EconomyState, None]:
        command = envelope.command
        company = economy.scenario.company(envelope.company_id)
        operation = company.operation
        if not isinstance(command, Transform) or not isinstance(
            operation,
            ProcessorOperation,
        ):
            raise _CommandRejected("transform is only available to processors")
        if (
            command.input_product is not operation.input_product
            or command.output_product is not operation.output_product
        ):
            raise _CommandRejected("processor cannot perform the requested transformation")

        used = self._used(economy.transformation_used, company.company_id)
        remaining_capacity = max(ZERO, operation.daily_input_capacity - used)
        ledger = self._active_ledger(economy)
        event = self._settle_transformation(
            economy.scenario,
            economy.day,
            ledger,
            company.company_id,
            operation,
            command.input_quantity,
            remaining_capacity,
        )
        if event.actual_input <= ZERO:
            raise _CommandRejected("transformation has no available inventory, capacity, or cash")

        return (
            economy.model_copy(
                update={
                    "state_version": economy.state_version + 1,
                    "companies": ledger.states(economy.scenario),
                    "transformation_used": self._set_used(
                        economy.transformation_used,
                        company.company_id,
                        used + event.actual_input,
                    ),
                    "events": (*economy.events, event),
                    "lot_sequence": ledger.lot_sequence,
                }
            ),
            None,
        )

    def _apply_order(
        self,
        economy: EconomyState,
        envelope: CommandEnvelope,
    ) -> tuple[EconomyState, str]:
        command = envelope.command
        if not isinstance(command, PlaceOrder):
            raise TypeError("order handler requires PlaceOrder")
        company = economy.scenario.company(envelope.company_id)
        if not self._order_is_authorized(company, command):
            raise _CommandRejected("order side or product is not authorized for this company")
        if (command.product is ProductId.RAW_MILK and economy.raw_market is not None) or (
            command.product is ProductId.BOTTLED_MILK and economy.bottled_market is not None
        ):
            raise _CommandRejected("the requested market has already cleared")

        sequence = economy.order_sequence + 1
        order_id = f"d{economy.day}.o{sequence}.{company.company_id}"
        order = OpenOrderView(
            order_id=order_id,
            owner_id=company.company_id,
            side=command.side,
            product=command.product,
            remaining_quantity=command.quantity,
            limit_price=command.limit_price,
            placed_at=envelope.issued_at,
        )
        return (
            economy.model_copy(
                update={
                    "state_version": economy.state_version + 1,
                    "orders": (*economy.orders, order),
                    "order_sequence": sequence,
                }
            ),
            order_id,
        )

    @staticmethod
    def _apply_cancel(
        economy: EconomyState,
        envelope: CommandEnvelope,
    ) -> tuple[EconomyState, str]:
        command = envelope.command
        if not isinstance(command, CancelOrder):
            raise TypeError("cancel handler requires CancelOrder")
        order = next(
            (candidate for candidate in economy.orders if candidate.order_id == command.order_id),
            None,
        )
        if order is None:
            raise _CommandRejected("order does not exist or has already cleared")
        if order.owner_id != envelope.company_id:
            raise _CommandRejected("a company cannot cancel another company's order")
        return (
            economy.model_copy(
                update={
                    "state_version": economy.state_version + 1,
                    "orders": tuple(
                        candidate
                        for candidate in economy.orders
                        if candidate.order_id != command.order_id
                    ),
                }
            ),
            order.order_id,
        )

    @staticmethod
    def _apply_retail_price(
        economy: EconomyState,
        envelope: CommandEnvelope,
    ) -> tuple[EconomyState, None]:
        command = envelope.command
        company = economy.scenario.company(envelope.company_id)
        operation = company.operation
        if not isinstance(command, SetRetailPrice) or not isinstance(
            operation,
            RetailerOperation,
        ):
            raise _CommandRejected("set_retail_price is only available to retailers")
        if command.product is not operation.input_product:
            raise _CommandRejected("retailer cannot price the requested product")
        retained = tuple(
            price
            for price in economy.retail_prices
            if (price.company_id, price.product) != (company.company_id, command.product)
        )
        price = RetailPriceState(
            company_id=company.company_id,
            product=command.product,
            unit_price=command.unit_price,
        )
        return (
            economy.model_copy(
                update={
                    "state_version": economy.state_version + 1,
                    "retail_prices": (*retained, price),
                }
            ),
            None,
        )

    @staticmethod
    def _apply_wait(
        economy: EconomyState,
        envelope: CommandEnvelope,
    ) -> tuple[EconomyState, None]:
        command = envelope.command
        if not isinstance(command, Wait):
            raise TypeError("wait handler requires Wait")
        if command.until is not None:
            if command.until.absolute_minute <= envelope.issued_at.absolute_minute:
                raise _CommandRejected("wait deadline must be later than the current time")
            runtime = economy.scenario.runtime
            if command.until.day >= economy.scenario.days:
                raise _CommandRejected("wait deadline exceeds the scenario")
            if not runtime.open_minute <= command.until.minute_of_day < runtime.close_minute:
                raise _CommandRejected("wait deadline must be inside business hours")
        return economy, None

    def _command_outcome(
        self,
        economy: EconomyState,
        envelope: CommandEnvelope,
        apply_sequence: int,
        *,
        accepted: bool,
        reason: str | None = None,
        order_id: str | None = None,
        events: tuple[DomainEvent, ...] = (),
    ) -> CommandOutcome:
        command = envelope.command
        next_available = (
            command.until
            if accepted and isinstance(command, Wait)
            else envelope.issued_at.plus(economy.scenario.runtime.command_duration_minutes)
        )
        return CommandOutcome(
            turn_id=envelope.turn_id,
            command_id=envelope.command_id,
            company_id=envelope.company_id,
            occurred_at=envelope.issued_at,
            status=CommandStatus.ACCEPTED if accepted else CommandStatus.REJECTED,
            accepted=accepted,
            reason=reason,
            resulting_state_version=economy.state_version,
            apply_sequence=apply_sequence,
            order_id=order_id,
            events=events,
            next_available_at=next_available,
        )

    @staticmethod
    def _validate_command_time(
        economy: EconomyState,
        envelope: CommandEnvelope,
    ) -> None:
        runtime = economy.scenario.runtime
        if envelope.issued_at.day != economy.day - 1:
            raise _CommandRejected("command targets a different business day")
        if not runtime.open_minute <= envelope.issued_at.minute_of_day < runtime.close_minute:
            raise _CommandRejected("command is outside business hours")

    @staticmethod
    def _order_is_authorized(
        company: CompanySpec,
        command: PlaceOrder,
    ) -> bool:
        operation = company.operation
        return (
            (
                isinstance(operation, FarmOperation)
                and command.side is MarketSide.SELL
                and command.product is operation.output_product
            )
            or (
                isinstance(operation, ProcessorOperation)
                and (
                    (command.side is MarketSide.BUY and command.product is operation.input_product)
                    or (
                        command.side is MarketSide.SELL
                        and command.product is operation.output_product
                    )
                )
            )
            or (
                isinstance(operation, RetailerOperation)
                and command.side is MarketSide.BUY
                and command.product is operation.input_product
            )
        )

    @staticmethod
    def _used(
        usage: tuple[CompanyQuantity, ...],
        company_id: CompanyId,
    ) -> Decimal:
        return next(
            (entry.quantity for entry in usage if entry.company_id == company_id),
            ZERO,
        )

    @staticmethod
    def _set_used(
        usage: tuple[CompanyQuantity, ...],
        company_id: CompanyId,
        quantity: Decimal,
    ) -> tuple[CompanyQuantity, ...]:
        retained = tuple(entry for entry in usage if entry.company_id != company_id)
        return (*retained, CompanyQuantity(company_id=company_id, quantity=quantity))

    @staticmethod
    def _settle_production(
        scenario: ScenarioSpec,
        day: int,
        ledger: _Ledger,
        company_id: CompanyId,
        operation: FarmOperation,
        requested: Decimal,
        capacity: Decimal,
    ) -> MilkProducedEvent:
        """Apply the shared V1/V2 farm production formula."""
        actual = min(
            requested,
            capacity,
            ledger.affordable_quantity(company_id, operation.unit_cost),
        )
        cost = actual * operation.unit_cost
        ledger.charge(company_id, cost)
        lot_id = ledger.add_new_lot(
            company_id,
            operation.output_product,
            actual,
            scenario.product(operation.output_product).shelf_life_days,
            "produce",
        )
        return MilkProducedEvent(
            day=day,
            company_id=company_id,
            requested_quantity=requested,
            actual_quantity=actual,
            unit_cost=operation.unit_cost,
            cash_cost=cost,
            lot_id=lot_id,
        )

    @staticmethod
    def _settle_transformation(
        scenario: ScenarioSpec,
        day: int,
        ledger: _Ledger,
        company_id: CompanyId,
        operation: ProcessorOperation,
        requested: Decimal,
        capacity: Decimal,
    ) -> MilkProcessedEvent:
        """Apply the shared V1/V2 processing formula."""
        cash_limit = (
            ledger.affordable_quantity(
                company_id,
                operation.processing_cost_per_input,
            )
            if operation.processing_cost_per_input > ZERO
            else requested
        )
        actual = min(
            requested,
            capacity,
            ledger.quantity(company_id, operation.input_product),
            cash_limit,
        )
        cost = actual * operation.processing_cost_per_input
        ledger.consume(company_id, operation.input_product, actual)
        ledger.charge(company_id, cost)
        output = actual * operation.yield_rate
        lot_id = ledger.add_new_lot(
            company_id,
            operation.output_product,
            output,
            scenario.product(operation.output_product).shelf_life_days,
            "process",
        )
        return MilkProcessedEvent(
            day=day,
            company_id=company_id,
            requested_input=requested,
            actual_input=actual,
            output_quantity=output,
            cash_cost=cost,
            output_lot_id=lot_id,
        )

    @staticmethod
    def _active_ledger(economy: EconomyState) -> _Ledger:
        current = economy.base_state.model_copy(update={"companies": economy.companies})
        return _Ledger(
            current,
            economy.day,
            lot_sequence=economy.lot_sequence,
        )

    @staticmethod
    def _retail_decisions(
        economy: EconomyState,
    ) -> dict[str, CompanyDecision]:
        prices = {
            (price.company_id, price.product): price.unit_price for price in economy.retail_prices
        }
        decisions: dict[str, CompanyDecision] = {}
        for company in economy.scenario.companies:
            operation = company.operation
            if not isinstance(operation, RetailerOperation):
                decisions[company.company_id] = NoOpDecision(reason="not_a_retailer")
                continue
            price = prices.get((company.company_id, operation.input_product))
            decisions[company.company_id] = (
                RetailerDecision(
                    bottled_bid_quantity=ZERO,
                    maximum_bottled_price=economy.scenario.demand.reference_price,
                    retail_price=price,
                )
                if price is not None
                else NoOpDecision(reason="retail_price_not_set")
            )
        return decisions

    def _accepted_decisions(
        self,
        state: WorldState,
        recorded: tuple[RecordedDecision, ...],
        events: list[DomainEvent],
    ) -> dict[str, CompanyDecision]:
        """Validate identity, day, observation, uniqueness, and authority."""
        day = state.day + 1
        grouped: dict[str, list[RecordedDecision]] = {}
        for item in recorded:
            grouped.setdefault(item.company_id, []).append(item)

        configured = {company.company_id: company for company in state.scenario.companies}
        accepted: dict[str, CompanyDecision] = {}
        for company in state.scenario.companies:
            entries = grouped.get(company.company_id, [])
            reason = self._rejection_reason(state, company, entries)
            if reason is not None:
                events.append(
                    DecisionRejectedEvent(
                        day=day,
                        company_id=company.company_id,
                        reason=reason,
                    )
                )
                accepted[company.company_id] = NoOpDecision(reason=reason)
            else:
                accepted[company.company_id] = entries[0].decision

        for unknown_id in sorted(set(grouped) - set(configured)):
            events.append(
                DecisionRejectedEvent(
                    day=day,
                    company_id=unknown_id,
                    reason="company is not part of this scenario",
                )
            )
        return accepted

    def _rejection_reason(
        self,
        state: WorldState,
        company: CompanySpec,
        entries: list[RecordedDecision],
    ) -> str | None:
        """Return why a decision cannot be authorized, if applicable."""
        if not entries:
            return "decision is missing"
        if len(entries) > 1:
            return "multiple decisions were submitted"

        recorded = entries[0]
        day = state.day + 1
        if recorded.day != day:
            return f"decision day must be {day}"
        expected_observation = self._observation_id(
            state,
            day,
            company.company_id,
        )
        if recorded.observation_id != expected_observation:
            return "decision does not match the morning observation"
        if isinstance(recorded.decision, NoOpDecision):
            return None

        allowed = (
            (
                isinstance(company.operation, FarmOperation)
                and isinstance(recorded.decision, FarmDecision)
            )
            or (
                isinstance(company.operation, ProcessorOperation)
                and isinstance(recorded.decision, ProcessorDecision)
            )
            or (
                isinstance(company.operation, RetailerOperation)
                and isinstance(recorded.decision, RetailerDecision)
            )
        )
        return None if allowed else "decision type is not authorized"

    def _produce(
        self,
        scenario: ScenarioSpec,
        day: int,
        ledger: _Ledger,
        decisions: dict[str, CompanyDecision],
        events: list[DomainEvent],
    ) -> None:
        """Settle farm production in stable company order."""
        for company in scenario.companies:
            operation = company.operation
            decision = decisions[company.company_id]
            if not (isinstance(operation, FarmOperation) and isinstance(decision, FarmDecision)):
                continue

            events.append(
                self._settle_production(
                    scenario,
                    day,
                    ledger,
                    company.company_id,
                    operation,
                    decision.produce_quantity,
                    operation.daily_capacity,
                )
            )

    def _clear_raw_market(
        self,
        scenario: ScenarioSpec,
        day: int,
        ledger: _Ledger,
        decisions: dict[str, CompanyDecision],
        events: list[DomainEvent],
    ) -> MarketSummary:
        """Match farms to processors on the raw-milk spot market."""
        sellers = [
            _Order(
                company_id=company.company_id,
                quantity=decision.raw_offer_quantity,
                unit_price=decision.minimum_raw_price,
            )
            for company in scenario.companies
            if isinstance(company.operation, FarmOperation)
            and isinstance(
                decision := decisions[company.company_id],
                FarmDecision,
            )
        ]
        buyers = [
            _Order(
                company_id=company.company_id,
                quantity=decision.raw_bid_quantity,
                unit_price=decision.maximum_raw_price,
            )
            for company in scenario.companies
            if isinstance(company.operation, ProcessorOperation)
            and isinstance(
                decision := decisions[company.company_id],
                ProcessorDecision,
            )
        ]
        return self._clear_market(
            day,
            ProductId.RAW_MILK,
            ledger,
            sellers,
            buyers,
            events,
        )

    def _process(
        self,
        scenario: ScenarioSpec,
        day: int,
        ledger: _Ledger,
        decisions: dict[str, CompanyDecision],
        events: list[DomainEvent],
    ) -> None:
        """Convert raw milk using cash, inventory, and capacity limits."""
        for company in scenario.companies:
            operation = company.operation
            decision = decisions[company.company_id]
            if not (
                isinstance(operation, ProcessorOperation)
                and isinstance(decision, ProcessorDecision)
            ):
                continue

            events.append(
                self._settle_transformation(
                    scenario,
                    day,
                    ledger,
                    company.company_id,
                    operation,
                    decision.process_quantity,
                    operation.daily_input_capacity,
                )
            )

    def _clear_bottled_market(
        self,
        scenario: ScenarioSpec,
        day: int,
        ledger: _Ledger,
        decisions: dict[str, CompanyDecision],
        events: list[DomainEvent],
    ) -> MarketSummary:
        """Match processors to retailers on the bottled-milk market."""
        sellers = [
            _Order(
                company_id=company.company_id,
                quantity=decision.bottled_offer_quantity,
                unit_price=decision.minimum_bottled_price,
            )
            for company in scenario.companies
            if isinstance(company.operation, ProcessorOperation)
            and isinstance(
                decision := decisions[company.company_id],
                ProcessorDecision,
            )
        ]
        buyers = [
            _Order(
                company_id=company.company_id,
                quantity=decision.bottled_bid_quantity,
                unit_price=decision.maximum_bottled_price,
            )
            for company in scenario.companies
            if isinstance(company.operation, RetailerOperation)
            and isinstance(
                decision := decisions[company.company_id],
                RetailerDecision,
            )
        ]
        return self._clear_market(
            day,
            ProductId.BOTTLED_MILK,
            ledger,
            sellers,
            buyers,
            events,
        )

    def _clear_market(
        self,
        day: int,
        product: ProductId,
        ledger: _Ledger,
        sellers: list[_Order],
        buyers: list[_Order],
        events: list[DomainEvent],
    ) -> MarketSummary:
        """Run deterministic price-time-free spot matching."""
        seller_priority = self._rotating_priority(sellers, day)
        buyer_priority = self._rotating_priority(buyers, day)
        sellers.sort(
            key=lambda order: (
                order.unit_price,
                seller_priority[order.company_id],
            )
        )
        buyers.sort(
            key=lambda order: (
                -order.unit_price,
                buyer_priority[order.company_id],
            )
        )
        seller_index = 0
        buyer_index = 0
        volume = ZERO
        traded_value = ZERO

        while seller_index < len(sellers) and buyer_index < len(buyers):
            seller = sellers[seller_index]
            buyer = buyers[buyer_index]
            if buyer.unit_price < seller.unit_price:
                break

            available = min(
                seller.quantity,
                ledger.quantity(seller.company_id, product),
            )
            affordable = ledger.affordable_quantity(
                buyer.company_id,
                seller.unit_price,
            )
            quantity = min(available, buyer.quantity, affordable)
            if quantity > ZERO:
                value = quantity * seller.unit_price
                ledger.transfer(
                    seller.company_id,
                    buyer.company_id,
                    product,
                    quantity,
                    seller.unit_price,
                )
                seller.quantity -= quantity
                buyer.quantity -= quantity
                volume += quantity
                traded_value += value
                events.append(
                    TradeExecutedEvent(
                        day=day,
                        product=product,
                        seller_id=seller.company_id,
                        buyer_id=buyer.company_id,
                        quantity=quantity,
                        unit_price=seller.unit_price,
                        total_value=value,
                    )
                )

            if seller.quantity <= ZERO or available <= ZERO:
                seller_index += 1
            if buyer.quantity <= ZERO or affordable <= ZERO:
                buyer_index += 1

        return MarketSummary(
            product=product,
            volume=volume,
            average_price=traded_value / volume if volume > ZERO else None,
        )

    @staticmethod
    def _rotating_priority(
        orders: list[_Order],
        day: int,
    ) -> dict[str, int]:
        """Rotate equal-price priority so one ID is not always first."""
        company_ids = sorted(order.company_id for order in orders)
        if not company_ids:
            return {}
        offset = (day - 1) % len(company_ids)
        rotated = company_ids[offset:] + company_ids[:offset]
        return {company_id: rank for rank, company_id in enumerate(rotated)}

    def _sell_to_consumers(
        self,
        scenario: ScenarioSpec,
        seed: int,
        day: int,
        ledger: _Ledger,
        decisions: dict[str, CompanyDecision],
        events: list[DomainEvent],
    ) -> tuple[Decimal, Decimal]:
        """Settle independent local consumer demand for each retailer."""
        total_demand = ZERO
        total_sales = ZERO
        for company in scenario.companies:
            operation = company.operation
            decision = decisions[company.company_id]
            if not isinstance(operation, RetailerOperation):
                continue

            shock = self._named_demand_shock(
                seed,
                day,
                company.company_id,
                scenario.demand.shock_min,
                scenario.demand.shock_max,
            )
            potential_demand = max(
                ZERO,
                (scenario.demand.base_demand + Decimal(shock)).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                ),
            )
            if isinstance(decision, RetailerDecision):
                price_adjustment = scenario.demand.price_sensitivity * (
                    decision.retail_price - scenario.demand.reference_price
                )
                demand = max(
                    ZERO,
                    (potential_demand - price_adjustment).quantize(
                        Decimal("1"),
                        rounding=ROUND_HALF_UP,
                    ),
                )
                sold = min(
                    demand,
                    ledger.quantity(
                        company.company_id,
                        operation.input_product,
                    ),
                )
                ledger.consume(
                    company.company_id,
                    operation.input_product,
                    sold,
                )
                retail_price = decision.retail_price
                revenue = sold * retail_price
                ledger.credit(company.company_id, revenue)
            else:
                demand = potential_demand
                sold = ZERO
                retail_price = None
                revenue = ZERO

            total_demand += potential_demand
            total_sales += sold
            events.append(
                ConsumerSaleEvent(
                    day=day,
                    company_id=company.company_id,
                    potential_demand_quantity=potential_demand,
                    demand_quantity=demand,
                    sold_quantity=sold,
                    retail_price=retail_price,
                    revenue=revenue,
                )
            )
        return total_demand, total_sales

    def _expire(
        self,
        scenario: ScenarioSpec,
        day: int,
        ledger: _Ledger,
        events: list[DomainEvent],
    ) -> Decimal:
        """Remove expired inventory and expose its fixed-value loss."""
        total = ZERO
        for company_id, lot in ledger.expire():
            total += lot.quantity
            events.append(
                InventoryExpiredEvent(
                    day=day,
                    company_id=company_id,
                    lot_id=lot.lot_id,
                    product=lot.product,
                    quantity=lot.quantity,
                    reference_value_loss=(
                        lot.quantity * scenario.product(lot.product).reference_value
                    ),
                )
            )
        return total

    def _snapshot(
        self,
        state: WorldState,
        events: list[DomainEvent],
        markets: tuple[MarketSummary, ...],
        consumer_demand: Decimal,
        consumer_sales: Decimal,
        expired_quantity: Decimal,
    ) -> DaySnapshot:
        """Create an accounting projection without changing state."""
        daily_sales = {company.company_id: ZERO for company in state.scenario.companies}
        daily_expired = dict(daily_sales)
        for event in events:
            if isinstance(event, ConsumerSaleEvent):
                daily_sales[event.company_id] += event.sold_quantity
            elif isinstance(event, InventoryExpiredEvent):
                daily_expired[event.company_id] += event.quantity

        states = {company.company_id: company for company in state.companies}
        snapshots: list[CompanySnapshot] = []
        for company in state.scenario.companies:
            company_state = states[company.company_id]
            raw_quantity = self._inventory_quantity(
                company_state,
                ProductId.RAW_MILK,
            )
            bottled_quantity = self._inventory_quantity(
                company_state,
                ProductId.BOTTLED_MILK,
            )
            inventory_value = state.scenario.inventory_value(company_state.inventory)
            net_worth = company_state.cash + inventory_value
            snapshots.append(
                CompanySnapshot(
                    day=state.day,
                    company_id=company.company_id,
                    tier=company.tier,
                    cash=company_state.cash,
                    raw_milk_quantity=raw_quantity,
                    bottled_milk_quantity=bottled_quantity,
                    inventory_value=inventory_value,
                    net_worth=net_worth,
                    surplus=net_worth - company.initial_cash,
                    daily_consumer_sales=daily_sales[company.company_id],
                    daily_expired_quantity=daily_expired[company.company_id],
                )
            )
        return DaySnapshot(
            day=state.day,
            companies=tuple(snapshots),
            markets=markets,
            consumer_demand=consumer_demand,
            consumer_sales=consumer_sales,
            expired_quantity=expired_quantity,
        )

    @staticmethod
    def _inventory_quantity(
        state: CompanyState,
        product: ProductId,
    ) -> Decimal:
        """Aggregate one product without exposing private lots."""
        return sum(
            (lot.quantity for lot in state.inventory if lot.product == product),
            start=ZERO,
        )

    @staticmethod
    def _observation_id(
        state: WorldState,
        day: int,
        company_id: str,
    ) -> str:
        """Create a reproducible binding without exposing the hidden seed."""
        return f"{state.scenario.scenario_id}|{day}|{company_id}"

    @staticmethod
    def _named_demand_shock(
        seed: int,
        day: int,
        retailer_id: str,
        minimum: int,
        maximum: int,
    ) -> int:
        """Derive a stable named random value independent of call order."""
        stream = f"{seed}|consumer_demand|{day}|{retailer_id}".encode()
        value = int.from_bytes(hashlib.sha256(stream).digest()[:8], "big")
        return minimum + value % (maximum - minimum + 1)
