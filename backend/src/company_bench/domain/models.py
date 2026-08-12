from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from company_bench.domain.calendar import SimDay, TradingCalendar
from company_bench.domain.precision import (
    ECONOMIC_NORMALIZER,
    ECONOMIC_QUANTUM,
    EconomicDecimal,
    EconomicPrecision,
    economic_field,
)

ZERO = Decimal("0.0000")
ONE = Decimal("1.0000")
MAX_SEED = 2_147_483_647

type CompanyId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"),
]
type Identifier = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128),
]
type Quantity = Annotated[Decimal, economic_field(ge=ZERO), ECONOMIC_NORMALIZER]
type PositiveQuantity = Annotated[Decimal, economic_field(gt=ZERO), ECONOMIC_NORMALIZER]
type Money = Annotated[Decimal, economic_field(ge=ZERO), ECONOMIC_NORMALIZER]
type PositiveMoney = Annotated[Decimal, economic_field(gt=ZERO), ECONOMIC_NORMALIZER]
type Rate = Annotated[Decimal, economic_field(ge=ZERO), ECONOMIC_NORMALIZER]
type Persistence = Annotated[
    Decimal,
    economic_field(ge=ZERO, lt=ONE),
    ECONOMIC_NORMALIZER,
]
type OpenUnitInterval = Annotated[
    Decimal,
    economic_field(ge=ZERO, lt=ONE),
    ECONOMIC_NORMALIZER,
]
type UnitInterval = Annotated[
    Decimal,
    economic_field(ge=ZERO, le=ONE),
    ECONOMIC_NORMALIZER,
]
type BenchmarkScore = Annotated[
    Decimal,
    economic_field(ge=ZERO, le=Decimal("100")),
    ECONOMIC_NORMALIZER,
]
type ScoreVersion = Literal["s9-enterprise-v5"]


class InvalidOrderQuantity(ValueError):
    """An order quantity cannot be represented at the market's fixed precision."""


def _validate_operation_quantity(value: Decimal) -> Decimal:
    """Validate one exact positive operation quantity."""
    if value <= ZERO:
        raise ValueError("operation quantity must be positive and finite")
    return EconomicPrecision.normalize_exact(value)


type OrderQuantity = Annotated[
    Decimal,
    economic_field(gt=ZERO),
    ECONOMIC_NORMALIZER,
]
type OperationQuantity = Annotated[
    Decimal,
    economic_field(gt=ZERO),
    ECONOMIC_NORMALIZER,
]

_ORDER_QUANTITY_ADAPTER = TypeAdapter(OrderQuantity)


def require_order_quantity(value: Decimal) -> OrderQuantity:
    """Validate an exact positive quantity without rounding it."""
    try:
        return _ORDER_QUANTITY_ADAPTER.validate_python(value)
    except ValidationError as error:
        raise InvalidOrderQuantity(
            f"order quantity must be at least {ECONOMIC_QUANTUM} and an exact "
            f"multiple of {ECONOMIC_QUANTUM}"
        ) from error


class StrictModel(BaseModel):
    """Base for immutable, closed-schema domain values."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


def _require_finite_between(
    value: Decimal,
    minimum: Decimal,
    maximum: Decimal,
    label: str,
) -> None:
    """Reject non-finite values and values outside one closed interval."""
    if not value.is_finite() or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be finite and between {minimum} and {maximum}")


class ProductId(StrEnum):
    """Products traded in the Dairy Bench scenarios."""

    RAW_MILK = "raw_milk"
    BOTTLED_MILK = "bottled_milk"


class CompanyTier(StrEnum):
    """A company's position in the dairy value chain."""

    FARM = "farm"
    PROCESSOR = "processor"
    RETAILER = "retailer"


class CompanyStatus(StrEnum):
    """Irreversible operating status of one company."""

    ACTIVE = "active"
    BANKRUPT = "bankrupt"


class PolicyKind(StrEnum):
    """Controller implementation used by one company."""

    BASELINE = "baseline"
    MODEL = "model"
    REPLAY = "replay"


class PolicyProfileId(StrEnum):
    """Stable selectable policy identity persisted with every run."""

    BASELINE = "baseline"
    NEWAPI_MODEL = "newapi-model"
    NEWAPI_CODEX = "newapi-codex"
    NEWAPI_CLAUDE_CODE = "newapi-claude-code"
    REPLAY = "replay"

    @property
    def kind(self) -> PolicyKind:
        """Return the provider-neutral controller kind for this profile."""
        if self is self.BASELINE:
            return PolicyKind.BASELINE
        if self is self.REPLAY:
            return PolicyKind.REPLAY
        return PolicyKind.MODEL


class ProductSpec(StrictModel):
    """Immutable physical and accounting rules for a product."""

    product: ProductId
    name: str = Field(min_length=1, max_length=80)
    shelf_life_weeks: int = Field(ge=1)
    reference_value: PositiveMoney


class WeeklyCapacity(StrictModel):
    """One realized weekly capacity and its persistent availability state."""

    availability: PositiveQuantity
    quantity: PositiveQuantity


class CapacityFunction(StrictModel):
    """Generate bounded, persistent weekly capacity from one private innovation."""

    normal_capacity: PositiveQuantity
    persistence: Persistence
    volatility: Rate
    minimum_factor: PositiveQuantity
    maximum_factor: PositiveQuantity

    @model_validator(mode="after")
    def validate_factor_range(self) -> Self:
        """Require the neutral operating state to lie inside an ordered range."""
        if self.minimum_factor > ONE or self.maximum_factor < ONE:
            raise ValueError("capacity factor range must contain 1")
        if self.minimum_factor > self.maximum_factor:
            raise ValueError("minimum capacity factor must not exceed maximum")
        minimum_capacity = EconomicPrecision.round(self.normal_capacity * self.minimum_factor)
        if minimum_capacity <= ZERO:
            raise ValueError("minimum realized capacity must survive quantity precision")
        return self

    def weekly_capacity(
        self,
        previous_availability: Decimal,
        innovation: Decimal,
    ) -> WeeklyCapacity:
        """Advance the AR(1) availability state and return its bounded capacity."""
        _require_finite_between(
            previous_availability,
            self.minimum_factor,
            self.maximum_factor,
            "previous availability",
        )
        _require_finite_between(innovation, -ONE, ONE, "capacity innovation")
        availability = min(
            self.maximum_factor,
            max(
                self.minimum_factor,
                ONE
                + self.persistence * (previous_availability - ONE)
                + self.volatility * innovation,
            ),
        )
        availability = EconomicPrecision.round(availability)
        return WeeklyCapacity(
            availability=availability,
            quantity=EconomicPrecision.round(self.normal_capacity * availability),
        )


class CostFunction(StrictModel):
    """Realize private base cost and price cumulative capacity use convexly."""

    normal_unit_cost: PositiveMoney
    weekly_volatility: OpenUnitInterval = ZERO
    curvature: PositiveQuantity

    @model_validator(mode="after")
    def validate_minimum_cost(self) -> Self:
        """Require the smallest supported batch to retain a positive cash cost."""
        minimum_unit_cost = self.normal_unit_cost * (ONE - self.weekly_volatility)
        if EconomicPrecision.round(minimum_unit_cost) <= ZERO:
            raise ValueError("minimum weekly unit cost must survive cost precision")
        return self

    def weekly_base_unit_cost(self, innovation: Decimal) -> PositiveMoney:
        """Return this week's positive private base cost."""
        _require_finite_between(innovation, -ONE, ONE, "cost innovation")
        return EconomicPrecision.round(
            self.normal_unit_cost * (ONE + self.weekly_volatility * innovation)
        )

    def incremental_cost(
        self,
        *,
        weekly_capacity: Decimal,
        used_capacity: Decimal,
        quantity: Decimal,
        weekly_base_unit_cost: Decimal,
    ) -> Money:
        """Charge the rounded cumulative-cost difference for one new batch."""
        if not weekly_capacity.is_finite() or weekly_capacity <= ZERO:
            raise ValueError("weekly capacity must be positive and finite")
        if not used_capacity.is_finite() or used_capacity < ZERO:
            raise ValueError("used capacity must be nonnegative and finite")
        _validate_operation_quantity(quantity)
        if not weekly_base_unit_cost.is_finite() or weekly_base_unit_cost <= ZERO:
            raise ValueError("weekly base unit cost must be positive and finite")
        if used_capacity + quantity > weekly_capacity:
            raise ValueError("production quantity exceeds remaining weekly capacity")
        return self._total_cost(
            used_capacity + quantity,
            weekly_capacity,
            weekly_base_unit_cost,
        ) - self._total_cost(
            used_capacity,
            weekly_capacity,
            weekly_base_unit_cost,
        )

    def _total_cost(
        self,
        quantity: Decimal,
        weekly_capacity: Decimal,
        weekly_base_unit_cost: Decimal,
    ) -> Money:
        total = weekly_base_unit_cost * quantity + (
            self.curvature * weekly_base_unit_cost * quantity**2 / (Decimal("2") * weekly_capacity)
        )
        return EconomicPrecision.round(total)


class ProductiveOperation(StrictModel):
    """Shared operating economics for production and transformation roles."""

    capacity: CapacityFunction
    cost: CostFunction


class FarmOperation(ProductiveOperation):
    """Production capability composed into a farm company."""

    kind: Literal["farm"] = "farm"
    output_product: ProductId = ProductId.RAW_MILK


class ProcessorOperation(ProductiveOperation):
    """Conversion capability composed into a processor company."""

    kind: Literal["processor"] = "processor"
    input_product: ProductId = ProductId.RAW_MILK
    output_product: ProductId = ProductId.BOTTLED_MILK
    yield_rate: PositiveQuantity


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


class WeeklyOperationState(StrictModel):
    """One operator's private weekly economics and consumed capacity."""

    company_id: CompanyId
    availability: PositiveQuantity
    weekly_capacity: PositiveQuantity
    weekly_base_unit_cost: PositiveMoney
    used_capacity: Quantity = ZERO

    @model_validator(mode="after")
    def validate_usage(self) -> Self:
        """Keep consumed capacity inside this week's realized limit."""
        if self.used_capacity > self.weekly_capacity:
            raise ValueError("used capacity cannot exceed weekly capacity")
        return self

    @property
    def remaining_capacity(self) -> Quantity:
        """Return capacity that remains available this week."""
        return self.weekly_capacity - self.used_capacity

    def consume(self, quantity: Decimal) -> WeeklyOperationState:
        """Return the state after reserving capacity for one accepted batch."""
        quantity = _validate_operation_quantity(quantity)
        if quantity > self.remaining_capacity:
            raise ValueError("consumed capacity exceeds the weekly remainder")
        return self.model_copy(update={"used_capacity": self.used_capacity + quantity})


class DemandSpec(StrictModel):
    """Public deterministic demand curve and private shock range."""

    base_demand: Quantity
    reference_price: PositiveMoney
    price_sensitivity: PositiveQuantity
    shock_min: int
    shock_max: int

    @model_validator(mode="after")
    def validate_shock_range(self) -> Self:
        """Require a nonempty ordered shock interval."""
        if self.shock_min > self.shock_max:
            raise ValueError("shock_min must not exceed shock_max")
        return self


class ScoringSpec(StrictModel):
    """Immutable identity of the active benchmark scoring contract."""

    score_version: ScoreVersion = "s9-enterprise-v5"


class RuntimeSpec(StrictModel):
    """Day-granularity scheduling and memory budgets for one scenario."""

    operation_duration_days: int = Field(default=1, ge=1, le=1)
    delivery_duration_days: int = Field(default=1, ge=1, le=1)
    decision_interval_days: int = Field(default=1, ge=1, le=1)
    default_review_days: int = Field(default=1, ge=1)
    max_review_days: int = Field(default=2, ge=1)
    max_turns_per_company_week: int = Field(default=6, ge=1, le=6)
    compaction_trigger_tokens: int = Field(default=12_288, ge=512)

    @model_validator(mode="after")
    def validate_schedule(self) -> Self:
        """Keep default attention inside its explicit review ceiling."""
        if self.default_review_days > self.max_review_days:
            raise ValueError("default review days cannot exceed max review days")
        return self


class ScenarioSpec(StrictModel):
    """Complete immutable rules for one benchmark scenario."""

    scenario_id: Identifier
    version: int = Field(ge=1)
    weeks: int = Field(ge=1)
    products: tuple[ProductSpec, ...] = Field(min_length=1)
    companies: tuple[CompanySpec, ...] = Field(min_length=1)
    demand: DemandSpec
    scoring: ScoringSpec
    runtime: RuntimeSpec = RuntimeSpec()

    @property
    def calendar(self) -> TradingCalendar:
        """Return the canonical calendar for this scenario."""
        return TradingCalendar(weeks=self.weeks)

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

    @property
    def productive_companies(self) -> tuple[CompanySpec, ...]:
        """Return productive companies in canonical scenario order."""
        return tuple(
            company
            for company in self.companies
            if isinstance(company.operation, ProductiveOperation)
        )

    def inventory_value(
        self,
        inventory: tuple[InventoryLot, ...],
    ) -> Decimal:
        """Value inventory using immutable benchmark references."""
        return EconomicPrecision.round(
            sum(
                (lot.quantity * self.product(lot.product).reference_value for lot in inventory),
                start=ZERO,
            )
        )


class InventoryLot(StrictModel):
    """One traceable, expiring batch of inventory."""

    lot_id: Identifier
    product: ProductId
    quantity: PositiveQuantity
    produced_week: int = Field(ge=1)
    expires_end_of_week: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_lifetime(self) -> Self:
        """Disallow a batch that expires before it is produced."""
        if self.expires_end_of_week < self.produced_week:
            raise ValueError("inventory cannot expire before production")
        return self


class CompanyState(StrictModel):
    """The complete economic state of one company."""

    company_id: CompanyId
    cash: Money
    inventory: tuple[InventoryLot, ...] = ()
    status: CompanyStatus = CompanyStatus.ACTIVE
    bankrupt_on: SimDay | None = None

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        """Pair irreversible bankruptcy status with its declaration day."""
        bankrupt = self.status is CompanyStatus.BANKRUPT
        if bankrupt != (self.bankrupt_on is not None):
            raise ValueError("bankrupt status and declaration day must be paired")
        return self

    @property
    def is_active(self) -> bool:
        """Return whether this company may still operate."""
        return self.status is CompanyStatus.ACTIVE

    def declare_bankrupt(self, on: SimDay) -> CompanyState:
        """Enter the terminal bankrupt state exactly once."""
        if not self.is_active:
            return self
        return self.model_copy(
            update={"status": CompanyStatus.BANKRUPT, "bankrupt_on": on}
        )


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
    """Immutable state after the latest completed week."""

    scenario: ScenarioSpec
    seed: int
    completed_weeks: int = Field(ge=0)
    companies: tuple[CompanyState, ...]
    operation_states: tuple[WeeklyOperationState, ...]
    previous_markets: tuple[MarketSummary, ...] = ()
    next_lot_sequence: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_company_set(self) -> Self:
        """Require exactly one runtime state per configured company."""
        configured = {company.company_id for company in self.scenario.companies}
        actual = [company.company_id for company in self.companies]
        if len(actual) != len(set(actual)) or set(actual) != configured:
            raise ValueError("world state must match the scenario company set")
        if self.completed_weeks > self.scenario.weeks:
            raise ValueError("world state exceeds the scenario duration")
        expected_operators = tuple(
            company.company_id for company in self.scenario.productive_companies
        )
        if tuple(state.company_id for state in self.operation_states) != expected_operators:
            raise ValueError("world operation states must match productive companies")
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
    status: CompanyStatus


class CompanyObservation(StrictModel):
    """The bounded information supplied to one company policy."""

    observation_id: Identifier
    scenario_id: Identifier
    scenario_weeks: int = Field(ge=1)
    sim_day: SimDay
    company_id: CompanyId
    operation: CompanyOperation
    weekly_operation: WeeklyOperationState | None = None
    products: tuple[ProductSpec, ...]
    demand: DemandSpec
    scoring: ScoringSpec
    cash: Money
    inventory: tuple[InventoryPosition, ...]
    public_companies: tuple[PublicCompany, ...]
    previous_markets: tuple[MarketSummary, ...]
    runtime: RuntimeSpec = RuntimeSpec()
    retail_price: Money | None = None

    @model_validator(mode="after")
    def validate_weekly_operation(self) -> Self:
        """Expose weekly economics exactly to productive companies themselves."""
        productive = isinstance(self.operation, ProductiveOperation)
        if productive != (self.weekly_operation is not None):
            raise ValueError("weekly operation must match the company's productive role")
        if self.weekly_operation is not None and (
            self.weekly_operation.company_id != self.company_id
        ):
            raise ValueError("weekly operation must belong to the observed company")
        return self

    def quantity(self, product: ProductId) -> Decimal:
        """Return the observed total for a product."""
        return next(
            (position.quantity for position in self.inventory if position.product == product),
            ZERO,
        )


class WeekEvent(StrictModel):
    """Shared fields for every fact emitted during a simulated week."""

    occurred_on: SimDay


class CompanyEvent(WeekEvent):
    """Shared fields for an event owned by one company."""

    company_id: CompanyId


class MilkProducedEvent(CompanyEvent):
    """Actual farm production after capacity and cash limits."""

    event_type: Literal["milk_produced"] = "milk_produced"
    requested_quantity: Quantity
    actual_quantity: Quantity
    unit_cost: Money
    cash_cost: Money
    lot_id: Identifier | None = None


class TradeExecutedEvent(WeekEvent):
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
    maker_remaining_quantity: Quantity
    taker_remaining_quantity: Quantity


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
    """One expired lot removed at the end of a week."""

    event_type: Literal["inventory_expired"] = "inventory_expired"
    lot_id: Identifier
    product: ProductId
    quantity: PositiveQuantity
    reference_value_loss: PositiveMoney


class CompanyBankruptEvent(CompanyEvent):
    """One irreversible exchange exit triggered by sub-threshold assets."""

    event_type: Literal["company_bankrupt"] = "company_bankrupt"
    total_assets: Money
    cancelled_order_ids: tuple[Identifier, ...] = ()
    retail_price_removed: bool = False


type DomainEvent = Annotated[
    MilkProducedEvent
    | TradeExecutedEvent
    | DeliveryCompletedEvent
    | MilkProcessedEvent
    | ConsumerSaleEvent
    | InventoryExpiredEvent
    | CompanyBankruptEvent,
    Field(discriminator="event_type"),
]


class EventRecord(StrictModel):
    """A domain event with an episode-wide stable sequence."""

    sequence: int = Field(ge=1)
    event: DomainEvent


class CompanySnapshot(StrictModel):
    """One company's auditable end-of-week accounting view."""

    week: int = Field(ge=1)
    company_id: CompanyId
    tier: CompanyTier
    status: CompanyStatus
    cash: Money
    raw_milk_quantity: Quantity
    bottled_milk_quantity: Quantity
    inventory_value: Money
    net_worth: Money
    surplus: EconomicDecimal
    weekly_consumer_sales: Quantity
    weekly_expired_quantity: Quantity


class WeekSnapshot(StrictModel):
    """System-wide facts captured after a week is settled."""

    week: int = Field(ge=1)
    companies: tuple[CompanySnapshot, ...]
    markets: tuple[MarketSummary, ...]
    consumer_demand: Quantity
    consumer_sales: Quantity
    expired_quantity: Quantity


class WeekResult(StrictModel):
    """The only public state transition returned by the engine."""

    state: WorldState
    events: tuple[DomainEvent, ...]
    snapshot: WeekSnapshot


class CompanyScore(StrictModel):
    """Final value creation for one company."""

    company_id: CompanyId
    tier: CompanyTier
    status: CompanyStatus
    initial_value: PositiveMoney
    final_cash: Money
    final_inventory_value: Money
    final_value: Money
    surplus: EconomicDecimal
    growth: Rate


class ScoreCard(StrictModel):
    """Official S9 enterprise benchmark score and auditable components."""

    score_version: ScoreVersion
    final_score: BenchmarkScore
    efficiency_raw: EconomicDecimal
    efficiency_reference: Money
    efficiency_score: UnitInterval
    global_gini: UnitInterval
    fairness_score: UnitInterval
    profit_participation_score: UnitInterval
    bankrupt_company_count: int = Field(ge=0)
    loss_making_company_count: int = Field(ge=0)
    companies: tuple[CompanyScore, ...]

    @model_validator(mode="after")
    def validate_company_counts(self) -> Self:
        """Keep aggregate bankruptcy and loss facts aligned with company detail."""
        company_ids = [company.company_id for company in self.companies]
        if len(company_ids) != len(set(company_ids)):
            raise ValueError("score companies must be unique")
        bankrupt = sum(
            company.status is CompanyStatus.BANKRUPT for company in self.companies
        )
        losses = sum(company.surplus < ZERO for company in self.companies)
        if self.bankrupt_company_count != bankrupt:
            raise ValueError("bankrupt_company_count must match company status")
        if self.loss_making_company_count != losses:
            raise ValueError("loss_making_company_count must match company surplus")
        return self


class ProtocolIssueKind(StrEnum):
    """Stable categories for model-decision protocol violations."""

    CONTEXT_TOO_LARGE = "context_too_large"
    INVALID_ARGUMENTS = "invalid_arguments"
    INVALID_RESPONSE = "invalid_response"
    MISSING_TOOL_CALL = "missing_tool_call"
    MULTIPLE_TOOL_CALLS = "multiple_tool_calls"
    UNAUTHORIZED_DECISION_TOOL = "unauthorized_decision_tool"


class ProtocolIssueCount(StrictModel):
    """Count one protocol-violation category across an episode."""

    kind: ProtocolIssueKind
    count: int = Field(ge=1)


class ProtocolReport(StrictModel):
    """Episode-wide protocol quality independent of economic execution."""

    total_turn_count: int = Field(ge=0)
    invalid_turn_count: int = Field(ge=0)
    issues: tuple[ProtocolIssueCount, ...] = ()

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        """Keep issue categories unique and equal to the invalid-turn total."""
        kinds = tuple(issue.kind for issue in self.issues)
        if len(kinds) != len(set(kinds)):
            raise ValueError("protocol issue categories must be unique")
        if sum(issue.count for issue in self.issues) != self.invalid_turn_count:
            raise ValueError("protocol issue counts must equal invalid_turn_count")
        if self.invalid_turn_count > self.total_turn_count:
            raise ValueError("invalid_turn_count cannot exceed total_turn_count")
        return self

    @classmethod
    def from_issues(
        cls,
        total_turn_count: int,
        issues: tuple[ProtocolIssueKind, ...],
    ) -> ProtocolReport:
        """Aggregate ordered issue categories without leaking provider messages."""
        counts = Counter(issues)
        return cls(
            total_turn_count=total_turn_count,
            invalid_turn_count=len(issues),
            issues=tuple(
                ProtocolIssueCount(kind=kind, count=count) for kind, count in counts.items()
            ),
        )


class EpisodeQuality(StrictModel):
    """Eligibility metadata kept separate from the diagnostic economic score."""

    benchmark_eligible: bool
    protocol: ProtocolReport

    @model_validator(mode="after")
    def validate_eligibility(self) -> Self:
        """Only a protocol-clean episode may enter benchmark comparisons."""
        if self.benchmark_eligible != (self.protocol.invalid_turn_count == 0):
            raise ValueError("benchmark eligibility must match protocol validity")
        return self


class PolicyMetadata(StrictModel):
    """Versioned configuration shared by equivalent company policies."""

    name: Identifier
    kind: PolicyKind = PolicyKind.BASELINE
    profile_id: PolicyProfileId
    provider: Identifier | None = None
    model: Identifier | None = None
    wire_protocol: Identifier | None = None
    adapter_version: Identifier | None = None
    prompt_version: Identifier | None = None
    config_fingerprint: Identifier | None = None
    source_run_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_profile_identity(self) -> Self:
        """Keep persisted profile identity and adapter evidence inseparable."""
        if self.profile_id.kind is not self.kind:
            raise ValueError("profile_id and kind must describe the same policy")
        adapter_fields = (
            self.provider,
            self.model,
            self.wire_protocol,
            self.adapter_version,
            self.prompt_version,
            self.config_fingerprint,
        )
        if self.kind is PolicyKind.MODEL and any(value is None for value in adapter_fields):
            raise ValueError(
                "model policies require provider, model, wire protocol, adapter version, "
                "prompt version, and config fingerprint"
            )
        if self.kind is not PolicyKind.MODEL and any(value is not None for value in adapter_fields):
            raise ValueError("non-model policies cannot carry model adapter metadata")
        return self


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
    events: tuple[EventRecord, ...]
    snapshots: tuple[WeekSnapshot, ...]
    score: ScoreCard
    quality: EpisodeQuality

    @model_validator(mode="after")
    def validate_time_order(self) -> Self:
        """Require a run to finish no earlier than it started."""
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        return self
