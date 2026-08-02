from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

ZERO = Decimal("0")
QUANTITY_QUANTUM = Decimal("0.0001")
MAX_SEED = 2_147_483_647

type CompanyId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"),
]
type Identifier = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128),
]
type Quantity = Annotated[Decimal, Field(ge=ZERO)]
type PositiveQuantity = Annotated[Decimal, Field(gt=ZERO)]
type Money = Annotated[Decimal, Field(ge=ZERO)]
type PositiveMoney = Annotated[Decimal, Field(gt=ZERO)]
type Rate = Annotated[Decimal, Field(ge=ZERO)]
type UnitInterval = Annotated[
    Decimal,
    Field(ge=ZERO, le=Decimal("1")),
]


class InvalidOrderQuantity(ValueError):
    """An order quantity cannot be represented at the market's fixed precision."""


def _validate_order_quantity(value: Decimal) -> Decimal:
    """Validate the market tick from the Decimal coefficient without arithmetic."""
    if not value.is_finite() or value <= ZERO:
        raise ValueError("order quantity must be positive and finite")
    decimal_tuple = value.as_tuple()
    digits = decimal_tuple.digits
    excess_places = QUANTITY_QUANTUM.as_tuple().exponent - decimal_tuple.exponent
    if excess_places > 0 and (
        excess_places > len(digits) or any(digits[-excess_places:])
    ):
        raise ValueError(f"order quantity must be an exact multiple of {QUANTITY_QUANTUM}")
    return value


type OrderQuantity = Annotated[
    Decimal,
    Field(gt=ZERO),
    AfterValidator(_validate_order_quantity),
]

_ORDER_QUANTITY_ADAPTER = TypeAdapter(OrderQuantity)


def require_order_quantity(value: Decimal) -> OrderQuantity:
    """Validate an exact positive quantity without rounding it."""
    try:
        return _ORDER_QUANTITY_ADAPTER.validate_python(value)
    except ValidationError as error:
        raise InvalidOrderQuantity(
            f"order quantity must be at least {QUANTITY_QUANTUM} and an exact "
            f"multiple of {QUANTITY_QUANTUM}"
        ) from error


class StrictModel(BaseModel):
    """Base for immutable, closed-schema domain values."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class ProductId(StrEnum):
    """Products traded in the Dairy Bench scenarios."""

    RAW_MILK = "raw_milk"
    BOTTLED_MILK = "bottled_milk"


class CompanyTier(StrEnum):
    """A company's position in the dairy value chain."""

    FARM = "farm"
    PROCESSOR = "processor"
    RETAILER = "retailer"


class PolicyKind(StrEnum):
    """Controller implementation used by one company."""

    BASELINE = "baseline"
    CODEX = "codex"
    OPENAI = "openai"
    REPLAY = "replay"


class ProductSpec(StrictModel):
    """Immutable physical and accounting rules for a product."""

    product: ProductId
    name: str = Field(min_length=1, max_length=80)
    shelf_life_days: int = Field(ge=1)
    reference_value: PositiveMoney


class FarmOperation(StrictModel):
    """Production capability composed into a farm company."""

    kind: Literal["farm"] = "farm"
    output_product: ProductId = ProductId.RAW_MILK
    daily_capacity: PositiveQuantity
    unit_cost: PositiveMoney


class ProcessorOperation(StrictModel):
    """Conversion capability composed into a processor company."""

    kind: Literal["processor"] = "processor"
    input_product: ProductId = ProductId.RAW_MILK
    output_product: ProductId = ProductId.BOTTLED_MILK
    daily_input_capacity: PositiveQuantity
    yield_rate: PositiveQuantity
    processing_cost_per_input: Money


class RetailerOperation(StrictModel):
    """Consumer-market access composed into a retail company."""

    kind: Literal["retailer"] = "retailer"
    input_product: ProductId = ProductId.BOTTLED_MILK


type CompanyOperation = Annotated[
    FarmOperation | ProcessorOperation | RetailerOperation,
    Field(discriminator="kind"),
]


class CompanySpec(StrictModel):
    """A company's identity, starting capital, and operation."""

    company_id: CompanyId
    name: str = Field(min_length=1, max_length=80)
    initial_cash: PositiveMoney
    operation: CompanyOperation

    @property
    def tier(self) -> CompanyTier:
        """Return the value-chain tier implied by the operation."""
        return CompanyTier(self.operation.kind)


class DemandSpec(StrictModel):
    """Public deterministic demand curve and private shock range."""

    base_demand: Quantity
    reference_price: PositiveMoney
    price_sensitivity: Rate
    shock_min: int
    shock_max: int

    @model_validator(mode="after")
    def validate_shock_range(self) -> Self:
        """Require a nonempty ordered shock interval."""
        if self.shock_min > self.shock_max:
            raise ValueError("shock_min must not exceed shock_max")
        return self


class ScoringSpec(StrictModel):
    """Fairness gates used before efficiency ranking."""

    max_gini: UnitInterval
    max_within_tier_growth_gap: Rate


class RuntimeSpec(StrictModel):
    """Event-time and context budgets for one benchmark scenario."""

    open_minute: int = Field(default=9 * 60, ge=0, lt=24 * 60)
    close_minute: int = Field(default=19 * 60, ge=0, lt=24 * 60)
    day_close_minute: int = Field(default=19 * 60 + 30, ge=0, lt=24 * 60)
    operation_duration_minutes: int = Field(default=30, ge=1)
    delivery_duration_minutes: int = Field(default=30, ge=1)
    decision_interval_minutes: int = Field(default=30, ge=1)
    max_wait_minutes: int = Field(default=120, ge=1)
    max_turns_per_company_day: int = Field(default=10, ge=1)
    max_prompt_tokens: int = Field(default=16_384, ge=1_024)
    compaction_trigger_tokens: int = Field(default=12_288, ge=512)

    @model_validator(mode="after")
    def validate_schedule(self) -> Self:
        """Require ordered market boundaries and a usable context budget."""
        boundaries = (self.open_minute, self.close_minute, self.day_close_minute)
        if boundaries != tuple(sorted(set(boundaries))):
            raise ValueError("runtime boundaries must be strictly increasing")
        latest_completion = self.close_minute - 1 + max(
            self.operation_duration_minutes,
            self.delivery_duration_minutes,
        )
        if latest_completion >= self.day_close_minute:
            raise ValueError("day close must follow every possible completion")
        if self.compaction_trigger_tokens >= self.max_prompt_tokens:
            raise ValueError("compaction must start below the prompt-token limit")
        return self


class ScenarioSpec(StrictModel):
    """Complete immutable rules for one benchmark scenario."""

    scenario_id: Identifier
    version: int = Field(ge=1)
    days: int = Field(ge=1)
    products: tuple[ProductSpec, ...] = Field(min_length=1)
    companies: tuple[CompanySpec, ...] = Field(min_length=1)
    demand: DemandSpec
    scoring: ScoringSpec
    runtime: RuntimeSpec = RuntimeSpec()

    @model_validator(mode="after")
    def validate_unique_references(self) -> Self:
        """Reject duplicate identities and unknown operation products."""
        product_ids = [product.product for product in self.products]
        company_ids = [company.company_id for company in self.companies]
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("product identifiers must be unique")
        if len(company_ids) != len(set(company_ids)):
            raise ValueError("company identifiers must be unique")

        known_products = set(product_ids)
        for company in self.companies:
            operation = company.operation
            referenced = (
                (operation.output_product,)
                if isinstance(operation, FarmOperation)
                else (operation.input_product, operation.output_product)
                if isinstance(operation, ProcessorOperation)
                else (operation.input_product,)
            )
            if not set(referenced).issubset(known_products):
                raise ValueError(f"{company.company_id} references an unknown product")
        return self

    def product(self, product_id: ProductId) -> ProductSpec:
        """Return one product specification by identity."""
        return next(product for product in self.products if product.product == product_id)

    def company(self, company_id: str) -> CompanySpec:
        """Return one company specification by identity."""
        return next(company for company in self.companies if company.company_id == company_id)

    def inventory_value(
        self,
        inventory: tuple[InventoryLot, ...],
    ) -> Decimal:
        """Value inventory using immutable benchmark references."""
        return sum(
            (lot.quantity * self.product(lot.product).reference_value for lot in inventory),
            start=ZERO,
        )


class InventoryLot(StrictModel):
    """One traceable, expiring batch of inventory."""

    lot_id: Identifier
    product: ProductId
    quantity: PositiveQuantity
    produced_day: int = Field(ge=1)
    expires_end_of_day: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_lifetime(self) -> Self:
        """Disallow a batch that expires before it is produced."""
        if self.expires_end_of_day < self.produced_day:
            raise ValueError("inventory cannot expire before production")
        return self


class CompanyState(StrictModel):
    """The complete economic state of one company."""

    company_id: CompanyId
    cash: Money
    inventory: tuple[InventoryLot, ...] = ()


class MarketSummary(StrictModel):
    """Public aggregate from the latest completed market."""

    product: ProductId
    volume: Quantity
    average_price: Money | None = None

    @model_validator(mode="after")
    def validate_average_price(self) -> Self:
        """Keep zero-volume markets free of a fictitious price."""
        if self.volume == ZERO and self.average_price is not None:
            raise ValueError("a zero-volume market has no average price")
        if self.volume > ZERO and self.average_price is None:
            raise ValueError("a positive-volume market needs an average price")
        return self


class WorldState(StrictModel):
    """Immutable state after the latest completed day."""

    scenario: ScenarioSpec
    seed: int
    day: int = Field(ge=0)
    companies: tuple[CompanyState, ...]
    previous_markets: tuple[MarketSummary, ...] = ()
    next_lot_sequence: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_company_set(self) -> Self:
        """Require exactly one runtime state per configured company."""
        configured = {company.company_id for company in self.scenario.companies}
        actual = [company.company_id for company in self.companies]
        if len(actual) != len(set(actual)) or set(actual) != configured:
            raise ValueError("world state must match the scenario company set")
        if self.day > self.scenario.days:
            raise ValueError("world state exceeds the scenario duration")
        return self


class InventoryPosition(StrictModel):
    """Aggregated inventory safe to expose in an observation."""

    product: ProductId
    quantity: Quantity


class PublicCompany(StrictModel):
    """Public identity of another economic actor."""

    company_id: CompanyId
    name: str
    tier: CompanyTier


class CompanyObservation(StrictModel):
    """The bounded information supplied to one company policy."""

    observation_id: Identifier
    scenario_id: Identifier
    scenario_days: int = Field(ge=1)
    day: int = Field(ge=1)
    company_id: CompanyId
    operation: CompanyOperation
    products: tuple[ProductSpec, ...]
    demand: DemandSpec
    scoring: ScoringSpec
    cash: Money
    inventory: tuple[InventoryPosition, ...]
    public_companies: tuple[PublicCompany, ...]
    previous_markets: tuple[MarketSummary, ...]
    runtime: RuntimeSpec = RuntimeSpec()
    retail_price: Money | None = None

    def quantity(self, product: ProductId) -> Decimal:
        """Return the observed total for a product."""
        return next(
            (position.quantity for position in self.inventory if position.product == product),
            ZERO,
        )


class NoOpDecision(StrictModel):
    """A valid request to perform no economic action."""

    kind: Literal["no_op"] = "no_op"
    reason: str = Field(default="no_operation", min_length=1, max_length=200)


class FarmDecision(StrictModel):
    """A farm's daily production and raw-milk offer."""

    kind: Literal["farm"] = "farm"
    produce_quantity: Quantity
    raw_offer_quantity: Quantity
    minimum_raw_price: PositiveMoney


class ProcessorDecision(StrictModel):
    """A processor's daily procurement, conversion, and sale plan."""

    kind: Literal["processor"] = "processor"
    raw_bid_quantity: Quantity
    maximum_raw_price: PositiveMoney
    process_quantity: Quantity
    bottled_offer_quantity: Quantity
    minimum_bottled_price: PositiveMoney


class RetailerDecision(StrictModel):
    """A retailer's daily procurement and consumer price."""

    kind: Literal["retailer"] = "retailer"
    bottled_bid_quantity: Quantity
    maximum_bottled_price: PositiveMoney
    retail_price: PositiveMoney


type CompanyDecision = Annotated[
    NoOpDecision | FarmDecision | ProcessorDecision | RetailerDecision,
    Field(discriminator="kind"),
]


class RecordedDecision(StrictModel):
    """One policy decision bound to its company and observation."""

    observation_id: Identifier
    day: int = Field(ge=1)
    company_id: CompanyId
    decision: CompanyDecision


class DayEvent(StrictModel):
    """Shared fields for every fact emitted during a simulated day."""

    day: int = Field(ge=1)


class CompanyEvent(DayEvent):
    """Shared fields for an event owned by one company."""

    company_id: CompanyId


class CompanyIssueEvent(CompanyEvent):
    """Shared fields for a rejected or failed company decision."""

    reason: str = Field(min_length=1, max_length=300)


class MilkProducedEvent(CompanyEvent):
    """Actual farm production after capacity and cash limits."""

    event_type: Literal["milk_produced"] = "milk_produced"
    requested_quantity: Quantity
    actual_quantity: Quantity
    unit_cost: Money
    cash_cost: Money
    lot_id: Identifier | None = None


class TradeExecutedEvent(DayEvent):
    """One actual spot-market transfer of money and inventory."""

    event_type: Literal["trade_executed"] = "trade_executed"
    trade_id: Identifier
    maker_order_id: Identifier
    taker_order_id: Identifier
    product: ProductId
    seller_id: CompanyId
    buyer_id: CompanyId
    quantity: PositiveQuantity
    unit_price: PositiveMoney
    total_value: PositiveMoney


class DeliveryCompletedEvent(CompanyEvent):
    """One guaranteed trade delivery becoming usable inventory."""

    event_type: Literal["delivery_completed"] = "delivery_completed"
    trade_id: Identifier
    product: ProductId
    quantity: PositiveQuantity


class MilkProcessedEvent(CompanyEvent):
    """Actual raw-milk conversion after resource limits."""

    event_type: Literal["milk_processed"] = "milk_processed"
    requested_input: Quantity
    actual_input: Quantity
    output_quantity: Quantity
    cash_cost: Money
    output_lot_id: Identifier | None = None


class ConsumerSaleEvent(CompanyEvent):
    """A retailer's completed sale to its local consumers."""

    event_type: Literal["consumer_sale"] = "consumer_sale"
    potential_demand_quantity: Quantity
    demand_quantity: Quantity
    sold_quantity: Quantity
    retail_price: PositiveMoney | None = None
    revenue: Money


class InventoryExpiredEvent(CompanyEvent):
    """One expired lot removed at the end of a day."""

    event_type: Literal["inventory_expired"] = "inventory_expired"
    lot_id: Identifier
    product: ProductId
    quantity: PositiveQuantity
    reference_value_loss: PositiveMoney


class DecisionRejectedEvent(CompanyIssueEvent):
    """A malformed, missing, duplicate, or unauthorized decision."""

    event_type: Literal["decision_rejected"] = "decision_rejected"


class PolicyFailedEvent(CompanyIssueEvent):
    """A policy failure converted by the runner into a no-op."""

    event_type: Literal["policy_failed"] = "policy_failed"


type DomainEvent = Annotated[
    MilkProducedEvent
    | TradeExecutedEvent
    | DeliveryCompletedEvent
    | MilkProcessedEvent
    | ConsumerSaleEvent
    | InventoryExpiredEvent
    | DecisionRejectedEvent
    | PolicyFailedEvent,
    Field(discriminator="event_type"),
]


class EventRecord(StrictModel):
    """A domain event with an episode-wide stable sequence."""

    sequence: int = Field(ge=1)
    event: DomainEvent


class CompanySnapshot(StrictModel):
    """One company's auditable end-of-day accounting view."""

    day: int = Field(ge=1)
    company_id: CompanyId
    tier: CompanyTier
    cash: Money
    raw_milk_quantity: Quantity
    bottled_milk_quantity: Quantity
    inventory_value: Money
    net_worth: Money
    surplus: Decimal
    daily_consumer_sales: Quantity
    daily_expired_quantity: Quantity


class DaySnapshot(StrictModel):
    """System-wide facts captured after a day is settled."""

    day: int = Field(ge=1)
    companies: tuple[CompanySnapshot, ...]
    markets: tuple[MarketSummary, ...]
    consumer_demand: Quantity
    consumer_sales: Quantity
    expired_quantity: Quantity


class DayResult(StrictModel):
    """The only public state transition returned by the engine."""

    state: WorldState
    events: tuple[DomainEvent, ...]
    snapshot: DaySnapshot


class CompanyScore(StrictModel):
    """Final value creation for one company."""

    company_id: CompanyId
    tier: CompanyTier
    initial_value: PositiveMoney
    final_cash: Money
    final_inventory_value: Money
    final_value: Money
    surplus: Decimal
    growth: Rate


class TierFairness(StrictModel):
    """Within-tier inequality and threshold status."""

    tier: CompanyTier
    gini: UnitInterval
    growth_gap: Rate


class ConstraintResult(StrictModel):
    """One explicit fairness gate and its observed value."""

    name: Identifier
    actual: Rate
    limit: Rate
    passed: bool


class ScoreCard(StrictModel):
    """Efficiency ranking result plus fairness and diagnostics."""

    efficiency: Decimal
    fairness: UnitInterval
    gini: UnitInterval
    eligible: bool
    consumer_fill_rate: UnitInterval
    expired_quantity: Quantity
    total_trade_quantity: Quantity
    consumer_revenue: Money
    companies: tuple[CompanyScore, ...]
    tiers: tuple[TierFairness, ...]
    constraints: tuple[ConstraintResult, ...]


class PolicyMetadata(StrictModel):
    """Versioned configuration shared by equivalent company policies."""

    name: Identifier
    version: Identifier = "1"
    kind: PolicyKind = PolicyKind.BASELINE
    provider: Identifier | None = None
    model: Identifier | None = None
    prompt_version: Identifier | None = None
    config_fingerprint: Identifier | None = None
    source_run_id: Identifier | None = None


class PolicyDescriptor(PolicyMetadata):
    """Reproducibility metadata for one company's policy."""

    company_id: CompanyId


class EpisodeResult(StrictModel):
    """Complete immutable aggregate for one benchmark run."""

    run_id: Identifier
    scenario: ScenarioSpec
    seed: int
    started_at: datetime
    finished_at: datetime
    policies: tuple[PolicyDescriptor, ...]
    decisions: tuple[RecordedDecision, ...]
    events: tuple[EventRecord, ...]
    snapshots: tuple[DaySnapshot, ...]
    score: ScoreCard

    @model_validator(mode="after")
    def validate_time_order(self) -> Self:
        """Require a run to finish no earlier than it started."""
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        return self


class RunSummary(StrictModel):
    """Compact projection used when listing persisted runs."""

    run_id: Identifier
    scenario_id: Identifier
    seed: int
    started_at: datetime
    finished_at: datetime
    efficiency: Decimal
    fairness: UnitInterval
    eligible: bool
