from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from company_bench.models import (
    MAX_SEED,
    QUANTITY_QUANTUM,
    ZERO,
    CompanyDecision,
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
    TradeExecutedEvent,
    WorldState,
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

    def __init__(self, state: WorldState, day: int) -> None:
        self._day = day
        self._lot_sequence = 0
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
        product = scenario.product(ProductId.RAW_MILK)
        for company in scenario.companies:
            operation = company.operation
            decision = decisions[company.company_id]
            if not (isinstance(operation, FarmOperation) and isinstance(decision, FarmDecision)):
                continue

            actual = min(
                decision.produce_quantity,
                operation.daily_capacity,
                ledger.affordable_quantity(
                    company.company_id,
                    operation.unit_cost,
                ),
            )
            cost = actual * operation.unit_cost
            ledger.charge(company.company_id, cost)
            lot_id = ledger.add_new_lot(
                company.company_id,
                ProductId.RAW_MILK,
                actual,
                product.shelf_life_days,
                "produce",
            )
            events.append(
                MilkProducedEvent(
                    day=day,
                    company_id=company.company_id,
                    requested_quantity=decision.produce_quantity,
                    actual_quantity=actual,
                    unit_cost=operation.unit_cost,
                    cash_cost=cost,
                    lot_id=lot_id,
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
        product = scenario.product(ProductId.BOTTLED_MILK)
        for company in scenario.companies:
            operation = company.operation
            decision = decisions[company.company_id]
            if not (
                isinstance(operation, ProcessorOperation)
                and isinstance(decision, ProcessorDecision)
            ):
                continue

            cash_limit = (
                ledger.affordable_quantity(
                    company.company_id,
                    operation.processing_cost_per_input,
                )
                if operation.processing_cost_per_input > ZERO
                else decision.process_quantity
            )
            actual_input = min(
                decision.process_quantity,
                operation.daily_input_capacity,
                ledger.quantity(company.company_id, operation.input_product),
                cash_limit,
            )
            cash_cost = actual_input * operation.processing_cost_per_input
            ledger.consume(
                company.company_id,
                operation.input_product,
                actual_input,
            )
            ledger.charge(company.company_id, cash_cost)
            output_quantity = actual_input * operation.yield_rate
            lot_id = ledger.add_new_lot(
                company.company_id,
                operation.output_product,
                output_quantity,
                product.shelf_life_days,
                "process",
            )
            events.append(
                MilkProcessedEvent(
                    day=day,
                    company_id=company.company_id,
                    requested_input=decision.process_quantity,
                    actual_input=actual_input,
                    output_quantity=output_quantity,
                    cash_cost=cash_cost,
                    output_lot_id=lot_id,
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
