"""Deterministic V7 economy engine for weekly dairy-market episodes."""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from company_bench.domain.calendar import SimDay
from company_bench.domain.models import (
    MAX_SEED,
    ZERO,
    CompanyBankruptEvent,
    CompanyId,
    CompanyObservation,
    CompanySnapshot,
    CompanySpec,
    CompanyState,
    ConsumerSaleEvent,
    CostFunction,
    DeliveryCompletedEvent,
    DomainEvent,
    FarmOperation,
    Identifier,
    InventoryExpiredEvent,
    InventoryLot,
    InventoryPosition,
    MarketSummary,
    MilkProcessedEvent,
    MilkProducedEvent,
    Money,
    PositiveMoney,
    PositiveQuantity,
    ProcessorOperation,
    ProductId,
    PublicCompany,
    Quantity,
    RetailerOperation,
    ScenarioSpec,
    StrictModel,
    TradeExecutedEvent,
    WeeklyOperationState,
    WeekResult,
    WeekSnapshot,
    WorldState,
)
from company_bench.domain.precision import EconomicDecimal, EconomicPrecision
from company_bench.economy.demand import ConsumerDemandCurve
from company_bench.economy.market import (
    AssetLedger,
    BuyOrder,
    ContinuousSpotMarket,
    MarketError,
    MarketState,
    OrderIdentity,
    SellOrder,
    TradeFill,
)
from company_bench.economy.operations import OperatingEconomics
from company_bench.economy.valuation import EnterpriseValuation
from company_bench.runtime.models import (
    DecisionEnvelope,
    DecisionOutcome,
    DecisionStatus,
    DeliveryExpiryBucket,
    IncomingDeliveryView,
    InventoryExpiryBucket,
    MarketSide,
    OpenOrderView,
    OperationJobView,
    OrderBookView,
    Produce,
    QuoteLadderResult,
    QuoteLevelAction,
    RejectionCategory,
    ScheduledCompletion,
    SetQuoteLadder,
    SetRetailPrice,
    SystemEventKind,
    Transform,
)

__all__ = [
    "ConsumerSettlement",
    "EconomyEngine",
    "EconomyState",
    "OperationJob",
    "PendingDelivery",
    "ProductionJob",
    "RetailPriceState",
    "TransformationJob",
]


class ProductionJob(StrictModel):
    """One fully funded farm operation awaiting physical completion."""

    kind: Literal["production"] = "production"
    job_id: Identifier
    company_id: CompanyId
    started_on: SimDay
    completes_on: SimDay
    product: ProductId
    quantity: PositiveQuantity
    unit_cost: PositiveMoney
    cash_cost: PositiveMoney

    @model_validator(mode="after")
    def validate_job(self) -> Self:
        """Require exact average cost and a future completion this week."""
        _validate_job_time(self.started_on, self.completes_on)
        if self.unit_cost != EconomicPrecision.round(self.cash_cost / self.quantity):
            raise ValueError("production unit cost must equal its rounded batch average")
        return self

    @property
    def output_product(self) -> ProductId:
        """Return the guaranteed output product."""
        return self.product

    @property
    def output_quantity(self) -> Quantity:
        """Return the guaranteed output quantity."""
        return self.quantity


class TransformationJob(StrictModel):
    """One funded conversion whose inputs have already entered work in process."""

    kind: Literal["transformation"] = "transformation"
    job_id: Identifier
    company_id: CompanyId
    started_on: SimDay
    completes_on: SimDay
    input_product: ProductId
    output_product: ProductId
    input_quantity: PositiveQuantity
    output_quantity: PositiveQuantity
    processing_cost_per_input: PositiveMoney
    cash_cost: PositiveMoney

    @model_validator(mode="after")
    def validate_job(self) -> Self:
        """Require exact average cost, different products, and a future completion."""
        _validate_job_time(self.started_on, self.completes_on)
        if self.input_product is self.output_product:
            raise ValueError("transformation input and output products must differ")
        if self.processing_cost_per_input != EconomicPrecision.round(
            self.cash_cost / self.input_quantity
        ):
            raise ValueError("processing unit cost must equal its rounded batch average")
        return self


type OperationJob = Annotated[
    ProductionJob | TransformationJob,
    Field(discriminator="kind"),
]


class PendingDelivery(StrictModel):
    """One guaranteed trade transfer waiting to become buyer-usable inventory."""

    delivery_id: Identifier
    trade_id: Identifier
    buyer_id: CompanyId
    arrives_on: SimDay
    lots: tuple[InventoryLot, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_lots(self) -> Self:
        """Keep one delivery homogeneous and free of duplicate lot identities."""
        products = {lot.product for lot in self.lots}
        lot_ids = [lot.lot_id for lot in self.lots]
        if len(products) != 1:
            raise ValueError("a delivery must contain exactly one product")
        if len(lot_ids) != len(set(lot_ids)):
            raise ValueError("delivery lot ids must be unique")
        return self

    @property
    def product(self) -> ProductId:
        """Return the single delivered product."""
        return self.lots[0].product

    @property
    def quantity(self) -> Quantity:
        """Return the exact delivered quantity."""
        return sum((lot.quantity for lot in self.lots), start=ZERO)

    def view(self) -> IncomingDeliveryView:
        """Project exact arrival and expiry buckets for the buyer."""
        expiry_weeks = sorted({lot.expires_end_of_week for lot in self.lots})
        return IncomingDeliveryView(
            trade_id=self.trade_id,
            product=self.product,
            quantity=self.quantity,
            arrives_on=self.arrives_on,
            expiry_buckets=tuple(
                DeliveryExpiryBucket(
                    quantity=sum(
                        (
                            lot.quantity
                            for lot in self.lots
                            if lot.expires_end_of_week == expiry_week
                        ),
                        start=ZERO,
                    ),
                    expires_end_of_week=expiry_week,
                )
                for expiry_week in expiry_weeks
            ),
        )


class ConsumerSettlement(StrictModel):
    """The immutable Sunday consumer-market totals for one trading week."""

    week: int = Field(ge=1)
    potential_demand: Quantity
    demand: Quantity
    sold_quantity: Quantity


class RetailPriceState(StrictModel):
    """One retailer's active consumer price."""

    company_id: CompanyId
    product: ProductId
    unit_price: PositiveMoney


class EconomyState(StrictModel):
    """Complete checkpointable state of one active trading week."""

    base_state: WorldState
    week: int = Field(ge=1)
    state_version: int = Field(ge=0)
    companies: tuple[CompanyState, ...]
    markets: tuple[MarketState, ...]
    jobs: tuple[OperationJob, ...] = ()
    deliveries: tuple[PendingDelivery, ...] = ()
    retail_prices: tuple[RetailPriceState, ...] = ()
    operation_states: tuple[WeeklyOperationState, ...]
    consumer_settlement: ConsumerSettlement | None = None
    events: tuple[DomainEvent, ...] = ()
    next_lot_sequence: int = Field(default=1, ge=1)
    next_order_sequence: int = Field(default=1, ge=1)
    next_job_sequence: int = Field(default=1, ge=1)
    next_delivery_sequence: int = Field(default=1, ge=1)
    next_trade_sequence: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_active_week(self) -> Self:
        """Protect identities, ownership, and one-resource-per-company invariants."""
        active = self.week == self.base_state.completed_weeks + 1
        terminal = (
            self.base_state.completed_weeks == self.base_state.scenario.weeks
            and self.week == self.base_state.completed_weeks
        )
        if not (active or terminal):
            raise ValueError("economy week must be active or terminal")
        if active:
            expected_operations = OperatingEconomics(
                self.scenario,
                self.seed,
            ).open_week(self.week, self.base_state.operation_states)
            reset_operations = tuple(
                state.model_copy(update={"used_capacity": ZERO}) for state in self.operation_states
            )
            if reset_operations != expected_operations:
                raise ValueError("economy operation states do not match seed-derived conditions")

        company_ids = tuple(company.company_id for company in self.companies)
        expected_ids = tuple(company.company_id for company in self.scenario.companies)
        if company_ids != expected_ids:
            raise ValueError("economy companies must follow scenario order")

        products = tuple(market.product for market in self.markets)
        expected_products = tuple(product.product for product in self.scenario.products)
        if products != expected_products:
            raise ValueError("economy markets must follow scenario product order")

        _require_unique((job.job_id for job in self.jobs), "operation job ids")
        _require_unique((job.company_id for job in self.jobs), "active operation companies")
        _require_unique(
            (delivery.delivery_id for delivery in self.deliveries),
            "pending delivery ids",
        )
        _require_unique((delivery.trade_id for delivery in self.deliveries), "delivery trade ids")
        _require_unique(
            ((price.company_id, price.product) for price in self.retail_prices),
            "retail price keys",
        )
        expected_operators = tuple(
            company.company_id for company in self.scenario.productive_companies
        )
        if tuple(state.company_id for state in self.operation_states) != expected_operators:
            raise ValueError("economy operation states must match productive companies")
        _require_unique(
            (lot.lot_id for lot in _persisted_lots(self)),
            "global inventory lot ids",
        )

        known = set(company_ids)
        orders = tuple(order for market in self.markets for order in market.orders)
        _require_unique((order.order_id for order in orders), "global market order ids")
        _require_unique(
            (order.priority_sequence for order in orders),
            "global market priority sequences",
        )
        if any(order.owner_id not in known for order in orders):
            raise ValueError("market order belongs to an unknown company")
        if any(job.company_id not in known for job in self.jobs):
            raise ValueError("operation job belongs to an unknown company")
        if any(delivery.buyer_id not in known for delivery in self.deliveries):
            raise ValueError("pending delivery belongs to an unknown buyer")
        if any(price.company_id not in known for price in self.retail_prices):
            raise ValueError("retail price belongs to an unknown company")
        bankrupt = {
            company.company_id for company in self.companies if not company.is_active
        }
        if any(order.owner_id in bankrupt for order in orders):
            raise ValueError("bankrupt companies cannot retain market orders")
        if any(price.company_id in bankrupt for price in self.retail_prices):
            raise ValueError("bankrupt retailers cannot retain consumer prices")
        if len({market.is_open for market in self.markets}) != 1:
            raise ValueError("product markets must open and close together")
        if any(job.completes_on.week != self.week for job in self.jobs):
            raise ValueError("operation job must complete in the active week")
        if any(delivery.arrives_on.week != self.week for delivery in self.deliveries):
            raise ValueError("delivery must arrive in the active week")
        if any(event.occurred_on.week != self.week for event in self.events):
            raise ValueError("active economy events must belong to its week")
        if self.consumer_settlement is not None and self.consumer_settlement.week != self.week:
            raise ValueError("consumer settlement must belong to the active week")
        if self.consumer_settlement is not None and any(market.is_open for market in self.markets):
            raise ValueError("consumer settlement requires closed markets")
        return self

    @property
    def scenario(self) -> ScenarioSpec:
        """Return immutable scenario rules."""
        return self.base_state.scenario

    @property
    def seed(self) -> int:
        """Return the episode seed."""
        return self.base_state.seed

    def is_active(self, company_id: CompanyId) -> bool:
        """Return whether one known company may still operate."""
        return _company_state(self.companies, company_id).is_active

    def _with_market_session(self, snapshot: _SessionSnapshot) -> Self:
        """Commit common ledger and order-book fields from one transaction."""
        return self.model_copy(
            update={
                "state_version": self.state_version + 1,
                "companies": snapshot.companies,
                "markets": snapshot.markets,
                "next_lot_sequence": snapshot.next_lot_sequence,
            }
        )


class _DecisionRejected(ValueError):
    """Expected decision rejection that never commits transaction-local state."""


@dataclass(frozen=True, slots=True)
class _SessionSnapshot:
    companies: tuple[CompanyState, ...]
    markets: tuple[MarketState, ...]
    next_lot_sequence: int


@dataclass(slots=True)
class _MarketSession:
    """Share one transaction-local asset ledger across every product market."""

    assets: AssetLedger
    markets: dict[ProductId, ContinuousSpotMarket]
    product_order: tuple[ProductId, ...]

    @classmethod
    def from_economy(cls, economy: EconomyState) -> Self:
        """Restore available assets and every collateralized order atomically."""
        assets = AssetLedger.from_companies(
            economy.companies,
            next_lot_sequence=economy.next_lot_sequence,
        )
        assets.track_lots(lot for delivery in economy.deliveries for lot in delivery.lots)
        markets = {state.product: ContinuousSpotMarket(state, assets) for state in economy.markets}
        return cls(
            assets=assets,
            markets=markets,
            product_order=tuple(state.product for state in economy.markets),
        )

    def market(self, product: ProductId) -> ContinuousSpotMarket:
        """Return one configured product market."""
        try:
            return self.markets[product]
        except KeyError as error:
            raise MarketError(f"unknown market product: {product.value}") from error

    def freeze(self, *, next_lot_sequence: int | None = None) -> _SessionSnapshot:
        """Freeze a successful transaction back into checkpoint-safe values."""
        return _SessionSnapshot(
            companies=self.assets.freeze_states(),
            markets=tuple(self.markets[product].state for product in self.product_order),
            next_lot_sequence=max(
                self.assets.next_lot_sequence,
                next_lot_sequence or self.assets.next_lot_sequence,
            ),
        )


class BankruptcyManager:
    """Atomically delist every active company whose guaranteed assets fall below one."""

    threshold = Decimal("1.0000")

    def __init__(self, valuation: EnterpriseValuation) -> None:
        self._valuation = valuation

    def settle(self, economy: EconomyState, on: SimDay) -> EconomyState:
        """Declare new bankruptcies and release their reversible commitments."""
        candidates = tuple(
            (company.company_id, total_assets)
            for company in economy.companies
            if company.is_active
            and (
                total_assets := self._valuation.active_value(
                    economy,
                    company.company_id,
                )
            )
            < self.threshold
        )
        if not candidates:
            return economy

        session = _MarketSession.from_economy(economy)
        cancelled_by_company: dict[CompanyId, tuple[Identifier, ...]] = {}
        for company_id, _ in candidates:
            cancelled_by_company[company_id] = tuple(
                order.order_id
                for market in session.markets.values()
                if market.state.is_open
                for order in market.cancel_company_orders(company_id)
            )
        snapshot = session.freeze()
        bankrupt_ids = {company_id for company_id, _ in candidates}
        prices_removed = {
            company_id
            for company_id in bankrupt_ids
            if any(price.company_id == company_id for price in economy.retail_prices)
        }
        assets = dict(candidates)
        events = tuple(
            CompanyBankruptEvent(
                occurred_on=on,
                company_id=company_id,
                total_assets=assets[company_id],
                cancelled_order_ids=cancelled_by_company[company_id],
                retail_price_removed=company_id in prices_removed,
            )
            for company_id, _ in candidates
        )
        return economy.model_copy(
            update={
                "companies": tuple(
                    company.declare_bankrupt(on)
                    if company.company_id in bankrupt_ids
                    else company
                    for company in snapshot.companies
                ),
                "markets": snapshot.markets,
                "retail_prices": tuple(
                    price
                    for price in economy.retail_prices
                    if price.company_id not in bankrupt_ids
                ),
                "next_lot_sequence": snapshot.next_lot_sequence,
                "events": (*economy.events, *events),
            }
        )


@dataclass(frozen=True, slots=True)
class _DecisionEffect:
    """One successfully applied decision and its immediate audit output."""

    economy: EconomyState
    quote_ladder_result: QuoteLadderResult | None = None
    job_id: Identifier | None = None
    completions: tuple[ScheduledCompletion, ...] = ()


@dataclass(frozen=True, slots=True)
class _TradeEffects:
    """Domain and scheduling records derived from one matching operation."""

    events: tuple[TradeExecutedEvent, ...]
    deliveries: tuple[PendingDelivery, ...]
    completions: tuple[ScheduledCompletion, ...]
    next_delivery_sequence: int
    next_trade_sequence: int


class EconomyEngine:
    """Own every V7 cash, inventory, market, operation, delivery, and exit transition."""

    def __init__(self, valuation: EnterpriseValuation | None = None) -> None:
        self._valuation = valuation or EnterpriseValuation()
        self._bankruptcies = BankruptcyManager(self._valuation)

    def initial_state(self, scenario: ScenarioSpec, seed: int) -> WorldState:
        """Create an empty-inventory V7 world."""
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= MAX_SEED:
            raise ValueError(f"seed must be an integer from 0 to {MAX_SEED}")
        economics = OperatingEconomics(scenario, seed)
        return WorldState(
            scenario=scenario,
            seed=seed,
            completed_weeks=0,
            companies=tuple(
                CompanyState(company_id=company.company_id, cash=company.initial_cash)
                for company in scenario.companies
            ),
            operation_states=economics.initial_states(),
        )

    def open_week(self, state: WorldState, *, state_version: int = 0) -> EconomyState:
        """Open fresh product books and weekly operating resources."""
        if state.completed_weeks >= state.scenario.weeks:
            raise ValueError("the episode is already complete")
        assets = AssetLedger.from_world(
            state,
            next_lot_sequence=state.next_lot_sequence,
        )
        week = state.completed_weeks + 1
        economy = EconomyState(
            base_state=state,
            week=week,
            state_version=state_version,
            companies=state.companies,
            markets=tuple(
                ContinuousSpotMarket.open(product.product, assets).state
                for product in state.scenario.products
            ),
            operation_states=OperatingEconomics(
                state.scenario,
                state.seed,
            ).open_week(week, state.operation_states),
            next_lot_sequence=state.next_lot_sequence,
        )
        return self._bankruptcies.settle(economy, SimDay.at(week=week))

    def observe(self, state: WorldState) -> tuple[CompanyObservation, ...]:
        """Project next-week Monday facts without opening mutable runtime state."""
        if state.completed_weeks >= state.scenario.weeks:
            return ()
        operation_states = OperatingEconomics(
            state.scenario,
            state.seed,
        ).open_week(state.completed_weeks + 1, state.operation_states)
        sim_day = SimDay.at(week=state.completed_weeks + 1)
        return tuple(
            self._observation(
                state=state,
                sim_day=sim_day,
                company_id=company.company_id,
                company_states=state.companies,
                weekly_operation=_optional_operation_state(
                    operation_states,
                    company.company_id,
                ),
                retail_price=None,
            )
            for company in state.companies
            if company.is_active
        )

    def observe_active(
        self,
        economy: EconomyState,
        company_id: CompanyId,
        sim_day: SimDay,
    ) -> CompanyObservation:
        """Project only currently available assets for one active company."""
        if sim_day.week != economy.week:
            raise ValueError("observation day must belong to the current active week")
        if not sim_day.is_decision_day:
            raise ValueError("company observations require a Monday-Saturday decision day")
        company_state = _company_state(economy.companies, company_id)
        if not company_state.is_active:
            raise ValueError("bankrupt companies cannot receive observations")
        price = next(
            (item.unit_price for item in economy.retail_prices if item.company_id == company_id),
            None,
        )
        return self._observation(
            state=economy.base_state,
            sim_day=sim_day,
            company_id=company_id,
            company_states=economy.companies,
            weekly_operation=_optional_operation_state(
                economy.operation_states,
                company_id,
            ),
            retail_price=price,
        )

    @staticmethod
    def company_orders(
        economy: EconomyState,
        company_id: CompanyId,
    ) -> tuple[OpenOrderView, ...]:
        """Return owned active orders with current same-price queue depth."""
        return tuple(
            order for market in economy.markets for order in market.company_order_views(company_id)
        )

    @staticmethod
    def order_books(
        economy: EconomyState,
        company_id: CompanyId,
    ) -> tuple[OrderBookView, ...]:
        """Return complete anonymous books relevant to one company's role."""
        company = economy.scenario.company(company_id)
        relevant = set(_relevant_products(company))
        return tuple(market.view() for market in economy.markets if market.product in relevant)

    @staticmethod
    def reserved_cash(economy: EconomyState, company_id: CompanyId) -> Money:
        """Return full cash collateral held by the company's open bids."""
        return sum(
            (
                order.reserved_cash
                for market in economy.markets
                for order in market.orders
                if isinstance(order, BuyOrder) and order.owner_id == company_id
            ),
            start=ZERO,
        )

    def marked_surplus(
        self,
        economy: EconomyState,
        company_id: CompanyId,
    ) -> EconomicDecimal:
        """Mark guaranteed owned assets against the episode's initial value."""
        return (
            self._valuation.active_value(economy, company_id)
            - economy.scenario.company(company_id).initial_cash
        )

    @staticmethod
    def inventory_expiry(
        economy: EconomyState,
        company_id: CompanyId,
    ) -> tuple[InventoryExpiryBucket, ...]:
        """Aggregate available and ask-reserved spot inventory by expiry."""
        available = _expiry_quantities(_company_state(economy.companies, company_id).inventory)
        reserved = _expiry_quantities(_reserved_lots(economy, company_id))
        product_order = {
            product.product: index for index, product in enumerate(economy.scenario.products)
        }
        keys = sorted(
            available.keys() | reserved.keys(),
            key=lambda key: (product_order[key[0]], key[1]),
        )
        return tuple(
            InventoryExpiryBucket(
                product=product,
                expires_end_of_week=expiry_week,
                available_quantity=available.get((product, expiry_week), ZERO),
                reserved_quantity=reserved.get((product, expiry_week), ZERO),
            )
            for product, expiry_week in keys
        )

    @staticmethod
    def pending_delivery_views(
        economy: EconomyState,
        company_id: CompanyId,
    ) -> tuple[IncomingDeliveryView, ...]:
        """Return guaranteed inbound inventory and its exact arrival time."""
        return tuple(
            delivery.view() for delivery in economy.deliveries if delivery.buyer_id == company_id
        )

    @staticmethod
    def operation_view(
        economy: EconomyState,
        company_id: CompanyId,
    ) -> OperationJobView | None:
        """Return the company's sole active physical operation."""
        job = next((job for job in economy.jobs if job.company_id == company_id), None)
        if job is None:
            return None
        if isinstance(job, ProductionJob):
            product = job.product
            quantity = job.quantity
        else:
            product = job.output_product
            quantity = job.output_quantity
        return OperationJobView(
            job_id=job.job_id,
            kind=job.kind,
            completes_on=job.completes_on,
            output_product=product,
            output_quantity=quantity,
        )

    @staticmethod
    def remaining_operation_capacity(
        economy: EconomyState,
        company_id: CompanyId,
    ) -> Quantity | None:
        """Return this week's unused production input capacity for an operator."""
        state = _optional_operation_state(economy.operation_states, company_id)
        return None if state is None else state.remaining_capacity

    def apply_batch(
        self,
        economy: EconomyState,
        envelopes: tuple[DecisionEnvelope, ...],
        *,
        first_apply_sequence: int,
        apply_sequences: tuple[int, ...] | None = None,
    ) -> tuple[EconomyState, tuple[DecisionOutcome, ...]]:
        """Apply one deterministic same-day decision batch serially."""
        if first_apply_sequence < 1:
            raise ValueError("first_apply_sequence must be positive")
        companies = [envelope.company_id for envelope in envelopes]
        if len(companies) != len(set(companies)):
            raise ValueError("a company may submit at most one decision per batch")
        if any(envelope.state_version != economy.state_version for envelope in envelopes):
            raise ValueError("every batch decision must target the shared state version")
        issued_days = {envelope.issued_on.absolute_day for envelope in envelopes}
        if len(issued_days) > 1:
            raise ValueError("one decision batch must share a simulation day")
        sequences = (
            tuple(range(first_apply_sequence, first_apply_sequence + len(envelopes)))
            if apply_sequences is None
            else apply_sequences
        )
        if len(sequences) != len(envelopes):
            raise ValueError("apply_sequences must align with submitted decisions")
        if tuple(sorted(set(sequences))) != sequences:
            raise ValueError("apply_sequences must be unique and increasing")
        if sequences and sequences[0] < first_apply_sequence:
            raise ValueError("apply_sequences cannot precede first_apply_sequence")

        current = economy
        outcomes: list[DecisionOutcome] = []
        for envelope, sequence in zip(envelopes, sequences, strict=True):
            current, outcome = self._apply_decision(current, envelope, sequence)
            outcomes.append(outcome)
        return current, tuple(outcomes)

    def complete_operation(
        self,
        economy: EconomyState,
        job_id: Identifier,
        on: SimDay,
    ) -> EconomyState:
        """Complete one scheduled physical operation exactly once."""
        job = next((candidate for candidate in economy.jobs if candidate.job_id == job_id), None)
        if job is None:
            raise ValueError("operation job does not exist")
        if on != job.completes_on:
            raise ValueError("operation must complete on its scheduled day")
        self._validate_system_day(economy, on)

        session = _MarketSession.from_economy(economy)
        next_lot_sequence = economy.next_lot_sequence + 1
        if isinstance(job, ProductionJob):
            product = job.product
            quantity = job.quantity
            source = "produce"
        else:
            product = job.output_product
            quantity = job.output_quantity
            source = "transform"
        lot = InventoryLot(
            lot_id=(
                f"w{economy.week}.d{on.day_of_week}.{source}."
                f"{job.company_id}.l{economy.next_lot_sequence}"
            ),
            product=product,
            quantity=quantity,
            produced_week=economy.week,
            expires_end_of_week=(
                economy.week + economy.scenario.product(product).shelf_life_weeks - 1
            ),
        )
        session.assets.restore_inventory(job.company_id, (lot,))
        event: DomainEvent = (
            MilkProducedEvent(
                occurred_on=on,
                company_id=job.company_id,
                requested_quantity=job.quantity,
                actual_quantity=job.quantity,
                unit_cost=job.unit_cost,
                cash_cost=job.cash_cost,
                lot_id=lot.lot_id,
            )
            if isinstance(job, ProductionJob)
            else MilkProcessedEvent(
                occurred_on=on,
                company_id=job.company_id,
                requested_input=job.input_quantity,
                actual_input=job.input_quantity,
                output_quantity=job.output_quantity,
                cash_cost=job.cash_cost,
                output_lot_id=lot.lot_id,
            )
        )
        updated = economy._with_market_session(
            session.freeze(next_lot_sequence=next_lot_sequence)
        ).model_copy(
            update={
                "jobs": tuple(
                    candidate for candidate in economy.jobs if candidate.job_id != job_id
                ),
                "events": (*economy.events, event),
            }
        )
        return self._bankruptcies.settle(updated, on)

    def complete_delivery(
        self,
        economy: EconomyState,
        delivery_id: Identifier,
        on: SimDay,
    ) -> EconomyState:
        """Move one guaranteed delivery into the buyer's available inventory."""
        delivery = next(
            (candidate for candidate in economy.deliveries if candidate.delivery_id == delivery_id),
            None,
        )
        if delivery is None:
            raise ValueError("pending delivery does not exist")
        if on != delivery.arrives_on:
            raise ValueError("delivery must complete on its scheduled day")
        self._validate_system_day(economy, on)
        session = _MarketSession.from_economy(economy)
        session.assets.restore_inventory(delivery.buyer_id, delivery.lots)
        event = DeliveryCompletedEvent(
            occurred_on=on,
            company_id=delivery.buyer_id,
            trade_id=delivery.trade_id,
            product=delivery.product,
            quantity=delivery.quantity,
        )
        updated = economy._with_market_session(session.freeze()).model_copy(
            update={
                "deliveries": tuple(
                    candidate
                    for candidate in economy.deliveries
                    if candidate.delivery_id != delivery_id
                ),
                "events": (*economy.events, event),
            }
        )
        return self._bankruptcies.settle(updated, on)

    def close_markets(self, economy: EconomyState, on: SimDay) -> EconomyState:
        """Expire every weekly order and release its remaining collateral."""
        self._require_settlement_day(economy, on)
        if not all(market.is_open for market in economy.markets):
            raise ValueError("markets are already closed")
        session = _MarketSession.from_economy(economy)
        for market in session.markets.values():
            market.close()
        return self._bankruptcies.settle(
            economy._with_market_session(session.freeze()),
            on,
        )

    def settle_consumer_sales(self, economy: EconomyState, on: SimDay) -> EconomyState:
        """Execute the sole Sunday consumer purchase event."""
        self._require_settlement_day(economy, on)
        if economy.consumer_settlement is not None:
            raise ValueError("consumer sales are already settled")
        if any(market.is_open for market in economy.markets):
            raise ValueError("markets must close before consumer sales")

        session = _MarketSession.from_economy(economy)
        demand_curve = ConsumerDemandCurve(spec=economy.scenario.demand)
        events: list[ConsumerSaleEvent] = []
        potential_total = ZERO
        demand_total = ZERO
        sold_total = ZERO
        prices = {
            (price.company_id, price.product): price.unit_price for price in economy.retail_prices
        }
        for company in economy.scenario.companies:
            operation = company.operation
            if not isinstance(operation, RetailerOperation):
                continue
            potential = demand_curve.potential(economy.seed, economy.week, company.company_id)
            price = prices.get((company.company_id, operation.input_product))
            if price is None:
                demand = potential
                sold = ZERO
                revenue = ZERO
            else:
                demand = demand_curve.quantity(potential, price)
                candidate_sale = min(
                    demand,
                    session.assets.quantity(company.company_id, operation.input_product),
                )
                revenue = EconomicPrecision.round(candidate_sale * price)
                sold = candidate_sale if revenue > ZERO else ZERO
                if sold > ZERO:
                    session.assets.reserve_inventory(
                        company.company_id,
                        operation.input_product,
                        sold,
                    )
                    session.assets.credit_cash(company.company_id, revenue)
            events.append(
                ConsumerSaleEvent(
                    occurred_on=on,
                    company_id=company.company_id,
                    potential_demand_quantity=potential,
                    demand_quantity=demand,
                    sold_quantity=sold,
                    retail_price=price,
                    revenue=revenue,
                )
            )
            potential_total += potential
            demand_total += demand
            sold_total += sold

        updated = economy._with_market_session(session.freeze()).model_copy(
            update={
                "consumer_settlement": ConsumerSettlement(
                    week=economy.week,
                    potential_demand=potential_total,
                    demand=demand_total,
                    sold_quantity=sold_total,
                ),
                "events": (*economy.events, *events),
            }
        )
        return self._bankruptcies.settle(updated, on)

    def close_week(self, economy: EconomyState, on: SimDay) -> WeekResult:
        """Assert a drained runtime, expire inventory, and freeze the weekly result."""
        self._require_settlement_day(economy, on)
        if any(market.is_open or market.orders for market in economy.markets):
            raise ValueError("week close requires closed empty markets")
        if economy.jobs:
            raise ValueError("week close requires every operation to complete")
        if economy.deliveries:
            raise ValueError("week close requires every delivery to complete")
        if economy.consumer_settlement is None:
            raise ValueError("consumer sales must settle before day close")

        companies, expiry_events = self._expire_inventory(economy, on)
        settled = self._bankruptcies.settle(
            economy.model_copy(
                update={
                    "companies": companies,
                    "events": (*economy.events, *expiry_events),
                }
            ),
            on,
        )
        events = settled.events
        markets = tuple(_market_summary(market) for market in economy.markets)
        state = WorldState(
            scenario=economy.scenario,
            seed=economy.seed,
            completed_weeks=economy.week,
            companies=settled.companies,
            operation_states=economy.operation_states,
            previous_markets=markets,
            next_lot_sequence=economy.next_lot_sequence,
        )
        settlement = economy.consumer_settlement
        return WeekResult(
            state=state,
            events=events,
            snapshot=self._snapshot(
                state,
                events,
                markets,
                settlement.potential_demand,
                settlement.sold_quantity,
                sum((event.quantity for event in expiry_events), start=ZERO),
            ),
        )

    def _apply_decision(
        self,
        economy: EconomyState,
        envelope: DecisionEnvelope,
        apply_sequence: int,
    ) -> tuple[EconomyState, DecisionOutcome]:
        """Apply one decision transaction or return an unchanged rejection."""
        try:
            self._validate_decision_time(economy, envelope)
            effect = self._dispatch(economy, envelope, apply_sequence)
        except (_DecisionRejected, MarketError) as error:
            return economy, self._outcome(
                economy,
                envelope,
                apply_sequence,
                accepted=False,
                reason=str(error),
            )
        except DecimalException as error:
            return economy, self._outcome(
                economy,
                envelope,
                apply_sequence,
                accepted=False,
                reason=f"numeric value exceeds the supported range: {type(error).__name__}",
            )
        settled = self._bankruptcies.settle(effect.economy, envelope.issued_on)
        return settled, self._outcome(
            settled,
            envelope,
            apply_sequence,
            accepted=True,
            quote_ladder_result=effect.quote_ladder_result,
            job_id=effect.job_id,
            events=settled.events[len(economy.events) :],
            completions=effect.completions,
        )

    def _dispatch(
        self,
        economy: EconomyState,
        envelope: DecisionEnvelope,
        apply_sequence: int,
    ) -> _DecisionEffect:
        action = envelope.action
        if action is None:
            return _DecisionEffect(economy=economy)
        if isinstance(action, Produce):
            return self._produce(economy, envelope, action)
        if isinstance(action, Transform):
            return self._transform(economy, envelope, action)
        if isinstance(action, SetQuoteLadder):
            return self._set_quote_ladder(economy, envelope, action)
        if isinstance(action, SetRetailPrice):
            return self._set_retail_price(economy, envelope, action)
        raise TypeError(f"unsupported action: {type(action).__name__}")

    def _produce(
        self,
        economy: EconomyState,
        envelope: DecisionEnvelope,
        action: Produce,
    ) -> _DecisionEffect:
        company = economy.scenario.company(envelope.company_id)
        operation = company.operation
        if not isinstance(operation, FarmOperation):
            raise _DecisionRejected("produce is only available to farms")
        if action.product is not operation.output_product:
            raise _DecisionRejected("farm cannot produce the requested product")
        self._require_idle(economy, company.company_id)
        operation_state, cost = _price_operation(
            economy,
            company.company_id,
            action.quantity,
            operation.cost,
            "production",
        )
        session = _MarketSession.from_economy(economy)
        if cost > session.assets.cash(company.company_id):
            raise _DecisionRejected(
                f"insufficient cash: required {cost}, "
                f"available {session.assets.cash(company.company_id)}"
            )
        session.assets.debit_cash(company.company_id, cost)
        sequence = economy.next_job_sequence
        job = ProductionJob(
            job_id=(
                f"w{economy.week}.d{envelope.issued_on.day_of_week}."
                f"job{sequence}.{company.company_id}"
            ),
            company_id=company.company_id,
            started_on=envelope.issued_on,
            completes_on=envelope.issued_on.plus_days(
                economy.scenario.runtime.operation_duration_days
            ),
            product=action.product,
            quantity=action.quantity,
            unit_cost=EconomicPrecision.round(cost / action.quantity),
            cash_cost=cost,
        )
        updated = economy._with_market_session(session.freeze()).model_copy(
            update={
                "jobs": (*economy.jobs, job),
                "operation_states": _replace_operation_state(
                    economy.operation_states,
                    operation_state,
                ),
                "next_job_sequence": sequence + 1,
            }
        )
        completion = _operation_completion(job)
        return _DecisionEffect(
            economy=updated,
            job_id=job.job_id,
            completions=(completion,),
        )

    def _transform(
        self,
        economy: EconomyState,
        envelope: DecisionEnvelope,
        action: Transform,
    ) -> _DecisionEffect:
        company = economy.scenario.company(envelope.company_id)
        operation = company.operation
        if not isinstance(operation, ProcessorOperation):
            raise _DecisionRejected("transform is only available to processors")
        if (
            action.input_product is not operation.input_product
            or action.output_product is not operation.output_product
        ):
            raise _DecisionRejected("processor cannot perform the requested transformation")
        self._require_idle(economy, company.company_id)
        operation_state, cost = _price_operation(
            economy,
            company.company_id,
            action.input_quantity,
            operation.cost,
            "transformation",
        )
        output_quantity = EconomicPrecision.floor_quantity(
            action.input_quantity * operation.yield_rate
        )
        if output_quantity <= ZERO:
            raise _DecisionRejected("transformation output rounds to zero")
        session = _MarketSession.from_economy(economy)
        available_input = session.assets.quantity(company.company_id, action.input_product)
        if action.input_quantity > available_input:
            raise _DecisionRejected(
                f"insufficient inventory: requested {action.input_quantity}, "
                f"available {available_input}"
            )
        if cost > session.assets.cash(company.company_id):
            raise _DecisionRejected(
                f"insufficient cash: required {cost}, "
                f"available {session.assets.cash(company.company_id)}"
            )
        session.assets.reserve_inventory(
            company.company_id,
            action.input_product,
            action.input_quantity,
        )
        session.assets.debit_cash(company.company_id, cost)
        sequence = economy.next_job_sequence
        job = TransformationJob(
            job_id=(
                f"w{economy.week}.d{envelope.issued_on.day_of_week}."
                f"job{sequence}.{company.company_id}"
            ),
            company_id=company.company_id,
            started_on=envelope.issued_on,
            completes_on=envelope.issued_on.plus_days(
                economy.scenario.runtime.operation_duration_days
            ),
            input_product=action.input_product,
            output_product=action.output_product,
            input_quantity=action.input_quantity,
            output_quantity=output_quantity,
            processing_cost_per_input=EconomicPrecision.round(cost / action.input_quantity),
            cash_cost=cost,
        )
        updated = economy._with_market_session(session.freeze()).model_copy(
            update={
                "jobs": (*economy.jobs, job),
                "operation_states": _replace_operation_state(
                    economy.operation_states,
                    operation_state,
                ),
                "next_job_sequence": sequence + 1,
            }
        )
        return _DecisionEffect(
            economy=updated,
            job_id=job.job_id,
            completions=(_operation_completion(job),),
        )

    def _set_quote_ladder(
        self,
        economy: EconomyState,
        envelope: DecisionEnvelope,
        action: SetQuoteLadder,
    ) -> _DecisionEffect:
        company = economy.scenario.company(envelope.company_id)
        if not _order_is_authorized(company, action.side, action.product):
            raise _DecisionRejected("order side or product is not authorized for this company")
        session = _MarketSession.from_economy(economy)
        sequence = economy.next_order_sequence
        execution = session.market(action.product).set_quote_ladder(
            owner_id=company.company_id,
            ladder=action,
            placed_on=envelope.issued_on,
            order_identity_factory=lambda offset: OrderIdentity(
                order_id=(
                    f"w{economy.week}.d{envelope.issued_on.day_of_week}."
                    f"o{sequence + offset - 1}.{company.company_id}"
                ),
                priority_sequence=sequence + offset - 1,
            ),
            trade_id_factory=self._trade_id_factory(economy),
        )
        created = sum(
            level.action is not QuoteLevelAction.KEEP for level in execution.result.levels
        )
        return self._commit_quote_ladder(
            economy,
            session,
            envelope.issued_on,
            execution.result,
            execution.fills,
            sequence + created,
        )

    @staticmethod
    def _set_retail_price(
        economy: EconomyState,
        envelope: DecisionEnvelope,
        action: SetRetailPrice,
    ) -> _DecisionEffect:
        company = economy.scenario.company(envelope.company_id)
        operation = company.operation
        if not isinstance(operation, RetailerOperation):
            raise _DecisionRejected("set_retail_price is only available to retailers")
        if action.product is not operation.input_product:
            raise _DecisionRejected("retailer cannot price the requested product")
        prices = tuple(
            price
            for price in economy.retail_prices
            if (price.company_id, price.product) != (company.company_id, action.product)
        )
        price = RetailPriceState(
            company_id=company.company_id,
            product=action.product,
            unit_price=action.unit_price,
        )
        return _DecisionEffect(
            economy=economy.model_copy(
                update={
                    "state_version": economy.state_version + 1,
                    "retail_prices": (*prices, price),
                }
            )
        )

    def _commit_quote_ladder(
        self,
        economy: EconomyState,
        session: _MarketSession,
        on: SimDay,
        result: QuoteLadderResult,
        fills: tuple[TradeFill, ...],
        next_order_sequence: int,
    ) -> _DecisionEffect:
        changed = bool(result.cancelled_order_ids) or any(
            level.action is not QuoteLevelAction.KEEP for level in result.levels
        )
        if not changed:
            return _DecisionEffect(economy=economy, quote_ladder_result=result)
        trade_effects = self._trade_effects(economy, fills, on)
        updated = economy._with_market_session(session.freeze()).model_copy(
            update={
                "deliveries": (*economy.deliveries, *trade_effects.deliveries),
                "events": (*economy.events, *trade_effects.events),
                "next_order_sequence": next_order_sequence,
                "next_delivery_sequence": trade_effects.next_delivery_sequence,
                "next_trade_sequence": trade_effects.next_trade_sequence,
            }
        )
        return _DecisionEffect(
            economy=updated,
            quote_ladder_result=result,
            completions=trade_effects.completions,
        )

    def _trade_effects(
        self,
        economy: EconomyState,
        fills: tuple[TradeFill, ...],
        on: SimDay,
    ) -> _TradeEffects:
        events: list[TradeExecutedEvent] = []
        deliveries: list[PendingDelivery] = []
        completions: list[ScheduledCompletion] = []
        for offset, fill in enumerate(fills):
            delivery_sequence = economy.next_delivery_sequence + offset
            delivery_id = f"w{economy.week}.delivery{delivery_sequence}"
            delivery = PendingDelivery(
                delivery_id=delivery_id,
                trade_id=fill.trade_id,
                buyer_id=fill.buyer_id,
                arrives_on=on.plus_days(economy.scenario.runtime.delivery_duration_days),
                lots=fill.delivery_lots,
            )
            deliveries.append(delivery)
            events.append(
                TradeExecutedEvent(
                    occurred_on=on,
                    trade_id=fill.trade_id,
                    maker_order_id=fill.maker_order_id,
                    taker_order_id=fill.taker_order_id,
                    product=fill.product,
                    seller_id=fill.seller_id,
                    buyer_id=fill.buyer_id,
                    quantity=fill.quantity,
                    unit_price=fill.unit_price,
                    total_value=fill.total_value,
                    maker_remaining_quantity=fill.maker_remaining_quantity,
                    taker_remaining_quantity=fill.taker_remaining_quantity,
                )
            )
            completions.append(
                ScheduledCompletion(
                    event_id=f"{delivery_id}.complete",
                    kind=SystemEventKind.DELIVERY_COMPLETED,
                    scheduled_for=delivery.arrives_on,
                    reference_id=delivery.delivery_id,
                    company_id=delivery.buyer_id,
                )
            )
        return _TradeEffects(
            events=tuple(events),
            deliveries=tuple(deliveries),
            completions=tuple(completions),
            next_delivery_sequence=economy.next_delivery_sequence + len(fills),
            next_trade_sequence=economy.next_trade_sequence + len(fills),
        )

    @staticmethod
    def _trade_id_factory(
        economy: EconomyState,
    ) -> Callable[[int], Identifier]:
        """Return a deterministic per-decision trade identity factory."""
        return lambda offset: f"w{economy.week}.trade{economy.next_trade_sequence + offset - 1}"

    def _outcome(
        self,
        economy: EconomyState,
        envelope: DecisionEnvelope,
        apply_sequence: int,
        *,
        accepted: bool,
        reason: str | None = None,
        quote_ladder_result: QuoteLadderResult | None = None,
        job_id: Identifier | None = None,
        events: tuple[DomainEvent, ...] = (),
        completions: tuple[ScheduledCompletion, ...] = (),
    ) -> DecisionOutcome:
        next_available = envelope.issued_on.plus_days(
            economy.scenario.runtime.decision_interval_days
        )
        return DecisionOutcome(
            turn_id=envelope.turn_id,
            decision_id=envelope.decision_id,
            company_id=envelope.company_id,
            occurred_on=envelope.issued_on,
            status=DecisionStatus.ACCEPTED if accepted else DecisionStatus.REJECTED,
            accepted=accepted,
            rejection_category=None if accepted else RejectionCategory.ECONOMIC,
            reason=reason,
            resulting_state_version=economy.state_version,
            apply_sequence=apply_sequence,
            quote_ladder_result=quote_ladder_result,
            job_id=job_id,
            events=events,
            scheduled_completions=completions,
            next_available_on=next_available,
        )

    @staticmethod
    def _validate_decision_time(economy: EconomyState, envelope: DecisionEnvelope) -> None:
        if not economy.is_active(envelope.company_id):
            raise _DecisionRejected("company is bankrupt and delisted")
        day = envelope.issued_on
        if day.week != economy.week:
            raise _DecisionRejected("decision targets a different trading week")
        if not economy.scenario.calendar.contains(day) or not day.is_decision_day:
            raise _DecisionRejected("decision must occur on Monday-Saturday")

    @staticmethod
    def _validate_system_day(economy: EconomyState, day: SimDay) -> None:
        if day.week != economy.week or not economy.scenario.calendar.contains(day):
            raise ValueError("system event targets a different trading week")

    def _require_settlement_day(
        self,
        economy: EconomyState,
        day: SimDay,
    ) -> None:
        self._validate_system_day(economy, day)
        if not day.is_settlement_day:
            raise ValueError("weekly settlement must occur on Sunday")

    @staticmethod
    def _require_idle(economy: EconomyState, company_id: CompanyId) -> None:
        if any(job.company_id == company_id for job in economy.jobs):
            raise _DecisionRejected("company operation resource is busy")

    def _observation(
        self,
        *,
        state: WorldState,
        sim_day: SimDay,
        company_id: CompanyId,
        company_states: tuple[CompanyState, ...],
        weekly_operation: WeeklyOperationState | None,
        retail_price: Money | None,
    ) -> CompanyObservation:
        scenario = state.scenario
        company = scenario.company(company_id)
        company_state = _company_state(company_states, company_id)
        status_by_company = {
            candidate.company_id: candidate.status for candidate in company_states
        }
        return CompanyObservation(
            observation_id=(
                f"{scenario.scenario_id}|d{sim_day.absolute_day}|{company_id}"
            ),
            scenario_id=scenario.scenario_id,
            scenario_weeks=scenario.weeks,
            sim_day=sim_day,
            company_id=company_id,
            operation=company.operation,
            weekly_operation=weekly_operation,
            products=scenario.products,
            demand=scenario.demand,
            scoring=scenario.scoring,
            cash=company_state.cash,
            inventory=tuple(
                InventoryPosition(
                    product=product.product,
                    quantity=_inventory_quantity(company_state, product.product),
                )
                for product in scenario.products
            ),
            public_companies=tuple(
                PublicCompany(
                    company_id=public.company_id,
                    name=public.name,
                    tier=public.tier,
                    status=status_by_company[public.company_id],
                )
                for public in scenario.companies
            ),
            previous_markets=state.previous_markets,
            runtime=scenario.runtime,
            retail_price=retail_price,
        )

    def _expire_inventory(
        self,
        economy: EconomyState,
        on: SimDay,
    ) -> tuple[tuple[CompanyState, ...], tuple[InventoryExpiredEvent, ...]]:
        companies: list[CompanyState] = []
        events: list[InventoryExpiredEvent] = []
        for company in economy.companies:
            retained: list[InventoryLot] = []
            for lot in company.inventory:
                if lot.expires_end_of_week <= economy.week:
                    events.append(
                        InventoryExpiredEvent(
                            occurred_on=on,
                            company_id=company.company_id,
                            lot_id=lot.lot_id,
                            product=lot.product,
                            quantity=lot.quantity,
                            reference_value_loss=EconomicPrecision.round(
                                lot.quantity * economy.scenario.product(lot.product).reference_value
                            ),
                        )
                    )
                else:
                    retained.append(lot)
            companies.append(company.model_copy(update={"inventory": tuple(retained)}))
        return tuple(companies), tuple(events)

    @staticmethod
    def _snapshot(
        state: WorldState,
        events: tuple[DomainEvent, ...],
        markets: tuple[MarketSummary, ...],
        consumer_demand: Quantity,
        consumer_sales: Quantity,
        expired_quantity: Quantity,
    ) -> WeekSnapshot:
        weekly_sales = {company.company_id: ZERO for company in state.scenario.companies}
        weekly_expired = dict(weekly_sales)
        for event in events:
            if isinstance(event, ConsumerSaleEvent):
                weekly_sales[event.company_id] += event.sold_quantity
            elif isinstance(event, InventoryExpiredEvent):
                weekly_expired[event.company_id] += event.quantity
        states = {company.company_id: company for company in state.companies}
        companies: list[CompanySnapshot] = []
        for company in state.scenario.companies:
            current = states[company.company_id]
            inventory_value = state.scenario.inventory_value(current.inventory)
            net_worth = current.cash + inventory_value
            companies.append(
                CompanySnapshot(
                    week=state.completed_weeks,
                    company_id=company.company_id,
                    tier=company.tier,
                    status=current.status,
                    cash=current.cash,
                    raw_milk_quantity=_inventory_quantity(current, ProductId.RAW_MILK),
                    bottled_milk_quantity=_inventory_quantity(
                        current,
                        ProductId.BOTTLED_MILK,
                    ),
                    inventory_value=inventory_value,
                    net_worth=net_worth,
                    surplus=net_worth - company.initial_cash,
                    weekly_consumer_sales=weekly_sales[company.company_id],
                    weekly_expired_quantity=weekly_expired[company.company_id],
                )
            )
        return WeekSnapshot(
            week=state.completed_weeks,
            companies=tuple(companies),
            markets=markets,
            consumer_demand=consumer_demand,
            consumer_sales=consumer_sales,
            expired_quantity=expired_quantity,
        )


def _validate_job_time(started_on: SimDay, completes_on: SimDay) -> None:
    if completes_on.absolute_day <= started_on.absolute_day:
        raise ValueError("operation completion must follow its start")
    if completes_on.week != started_on.week:
        raise ValueError("operation must complete within its trading week")


def _reserved_lots(
    economy: EconomyState,
    company_id: CompanyId,
) -> tuple[InventoryLot, ...]:
    """Return every lot collateralizing the company's active asks."""
    return tuple(
        lot
        for market in economy.markets
        for order in market.orders
        if isinstance(order, SellOrder) and order.owner_id == company_id
        for lot in order.reserved_lots
    )


def _expiry_quantities(
    lots: Iterable[InventoryLot],
) -> dict[tuple[ProductId, int], Quantity]:
    """Aggregate lots without exposing their private identities."""
    quantities: dict[tuple[ProductId, int], Quantity] = {}
    for lot in lots:
        key = (lot.product, lot.expires_end_of_week)
        quantities[key] = quantities.get(key, ZERO) + lot.quantity
    return quantities


def _persisted_lots(economy: EconomyState) -> Iterable[InventoryLot]:
    """Yield every on-hand, reserved, and in-transit inventory lot."""
    for company in economy.companies:
        yield from company.inventory
    for market in economy.markets:
        for order in market.orders:
            if isinstance(order, SellOrder):
                yield from order.reserved_lots
    for delivery in economy.deliveries:
        yield from delivery.lots


def _require_unique(values: Iterable[Hashable], label: str) -> None:
    items = tuple(values)
    if len(items) != len(set(items)):
        raise ValueError(f"{label} must be unique")


def _company_state(
    companies: tuple[CompanyState, ...],
    company_id: CompanyId,
) -> CompanyState:
    try:
        return next(company for company in companies if company.company_id == company_id)
    except StopIteration as error:
        raise ValueError(f"unknown company: {company_id}") from error


def _inventory_quantity(state: CompanyState, product: ProductId) -> Quantity:
    return sum(
        (lot.quantity for lot in state.inventory if lot.product is product),
        start=ZERO,
    )


def _optional_operation_state(
    states: tuple[WeeklyOperationState, ...],
    company_id: CompanyId,
) -> WeeklyOperationState | None:
    return next((state for state in states if state.company_id == company_id), None)


def _operation_state(
    states: tuple[WeeklyOperationState, ...],
    company_id: CompanyId,
) -> WeeklyOperationState:
    state = _optional_operation_state(states, company_id)
    if state is None:
        raise ValueError(f"company has no productive operation: {company_id}")
    return state


def _replace_operation_state(
    states: tuple[WeeklyOperationState, ...],
    replacement: WeeklyOperationState,
) -> tuple[WeeklyOperationState, ...]:
    return tuple(
        replacement if state.company_id == replacement.company_id else state for state in states
    )


def _price_operation(
    economy: EconomyState,
    company_id: CompanyId,
    quantity: Decimal,
    cost_function: CostFunction,
    label: str,
) -> tuple[WeeklyOperationState, Money]:
    state = _operation_state(economy.operation_states, company_id)
    if quantity > state.remaining_capacity:
        raise _DecisionRejected(
            f"insufficient {label} capacity: requested {quantity}, "
            f"available {state.remaining_capacity}"
        )
    try:
        cost = cost_function.incremental_cost(
            weekly_capacity=state.weekly_capacity,
            used_capacity=state.used_capacity,
            quantity=quantity,
            weekly_base_unit_cost=state.weekly_base_unit_cost,
        )
        if cost <= ZERO:
            raise ValueError(f"{label} cost rounds to zero")
        return state.consume(quantity), cost
    except ValueError as error:
        raise _DecisionRejected(str(error)) from error


def _operation_completion(job: OperationJob) -> ScheduledCompletion:
    return ScheduledCompletion(
        event_id=f"{job.job_id}.complete",
        kind=SystemEventKind.OPERATION_COMPLETED,
        scheduled_for=job.completes_on,
        reference_id=job.job_id,
        company_id=job.company_id,
    )


def _order_is_authorized(
    company: CompanySpec,
    side: MarketSide,
    product: ProductId,
) -> bool:
    operation = company.operation
    return (
        (
            isinstance(operation, FarmOperation)
            and side is MarketSide.SELL
            and product is operation.output_product
        )
        or (
            isinstance(operation, ProcessorOperation)
            and (
                (side is MarketSide.BUY and product is operation.input_product)
                or (side is MarketSide.SELL and product is operation.output_product)
            )
        )
        or (
            isinstance(operation, RetailerOperation)
            and side is MarketSide.BUY
            and product is operation.input_product
        )
    )


def _relevant_products(company: CompanySpec) -> tuple[ProductId, ...]:
    operation = company.operation
    if isinstance(operation, FarmOperation):
        return (operation.output_product,)
    if isinstance(operation, ProcessorOperation):
        return (operation.input_product, operation.output_product)
    if isinstance(operation, RetailerOperation):
        return (operation.input_product,)
    raise TypeError(f"unsupported company operation: {type(operation).__name__}")


def _market_summary(market: MarketState) -> MarketSummary:
    return MarketSummary(
        product=market.product,
        volume=market.volume,
        average_price=(
            EconomicPrecision.round(market.traded_value / market.volume)
            if market.volume > ZERO
            else None
        ),
    )
