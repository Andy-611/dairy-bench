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
type ScoreVersion = Literal["s9-enterprise-v9"]


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
    weekly_operating_cost: PositiveMoney


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


class ConsumerGroup(StrEnum):
    """Hidden willingness-to-pay cohort identity."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class MarketRegime(StrEnum):
    """Latent weekly consumer-market regime."""

    SLUMP = "slump"
    NORMAL = "normal"
    BOOM = "boom"


class ConsumerCohortSpec(StrictModel):
    """One hidden consumer cohort in the shared retail market."""

    group: ConsumerGroup
    base_quantity: PositiveQuantity
    maximum_willingness_to_pay: PositiveMoney


class RegimeComposition(StrictModel):
    """Hidden cohort multipliers for one latent market regime."""

    regime: MarketRegime
    low: PositiveQuantity
    medium: PositiveQuantity
    high: PositiveQuantity

    def multiplier(self, group: ConsumerGroup) -> PositiveQuantity:
        """Return the multiplier for one cohort."""
        return {
            ConsumerGroup.LOW: self.low,
            ConsumerGroup.MEDIUM: self.medium,
            ConsumerGroup.HIGH: self.high,
        }[group]


class RegimeWillingnessShift(StrictModel):
    """Hidden willingness-to-pay shift for one market regime."""

    regime: MarketRegime
    shift: EconomicDecimal


class ConsumerMarketRules(StrictModel):
    """Public market structure supplied to Agents without hidden parameters."""

    consumer_group_count: Literal[3]
    groups_have_distinct_unknown_willingness_to_pay: Literal[True]
    purchase_priority: Literal["lowest_price_first"]
    stockout_spills_to_next_lowest_price: Literal[True]
    equal_price_allocation: Literal["deterministic_equal_share"]
    regimes: tuple[MarketRegime, MarketRegime, MarketRegime]
    minimum_regime_weeks: int = Field(ge=1)
    maximum_regime_weeks: int = Field(ge=1)
    normal_alternates_with_extreme_regimes: Literal[True]
    regimes_may_change_market_size_and_group_composition: Literal[True]
    willingness_to_pay_may_change_with_purchasing_power_and_regime: Literal[True]
    exact_demand_parameters_are_hidden: Literal[True]

    @model_validator(mode="after")
    def validate_duration(self) -> Self:
        """Require an ordered public regime-duration range."""
        if self.minimum_regime_weeks > self.maximum_regime_weeks:
            raise ValueError("minimum regime duration cannot exceed maximum")
        return self


class ConsumerMarketSpec(StrictModel):
    """Complete hidden parameters for the shared consumer market."""

    cohorts: tuple[ConsumerCohortSpec, ...] = Field(min_length=3, max_length=3)
    compositions: tuple[RegimeComposition, ...] = Field(min_length=3, max_length=3)
    purchasing_power_shifts: tuple[EconomicDecimal, ...] = Field(min_length=1)
    regime_willingness_shifts: tuple[RegimeWillingnessShift, ...] = Field(
        min_length=3,
        max_length=3,
    )
    minimum_regime_weeks: int = Field(default=6, ge=1)
    maximum_regime_weeks: int = Field(default=10, ge=1)
    initial_retail_price: PositiveMoney

    @model_validator(mode="after")
    def validate_market(self) -> Self:
        """Require exactly one ordered cohort and composition per public category."""
        groups = tuple(cohort.group for cohort in self.cohorts)
        if groups != tuple(ConsumerGroup):
            raise ValueError("consumer cohorts must follow low, medium, high order")
        willingness = tuple(cohort.maximum_willingness_to_pay for cohort in self.cohorts)
        if willingness != tuple(sorted(willingness)) or len(set(willingness)) != 3:
            raise ValueError("consumer willingness-to-pay values must be distinct and increasing")
        regimes = tuple(composition.regime for composition in self.compositions)
        if regimes != tuple(MarketRegime):
            raise ValueError("regime compositions must follow slump, normal, boom order")
        shifts = self.purchasing_power_shifts
        if shifts != tuple(sorted(set(shifts))) or ZERO not in shifts:
            raise ValueError("purchasing-power shifts must be unique, increasing, and include zero")
        shift_regimes = tuple(item.regime for item in self.regime_willingness_shifts)
        if shift_regimes != tuple(MarketRegime):
            raise ValueError("regime willingness shifts must follow slump, normal, boom order")
        minimum_shift = min(shifts) + min(item.shift for item in self.regime_willingness_shifts)
        if willingness[0] + minimum_shift <= ZERO:
            raise ValueError("hidden shifts must preserve positive willingness to pay")
        if self.minimum_regime_weeks > self.maximum_regime_weeks:
            raise ValueError("minimum regime duration cannot exceed maximum")
        return self

    def composition(self, regime: MarketRegime) -> RegimeComposition:
        """Return hidden composition parameters for one regime."""
        return next(item for item in self.compositions if item.regime is regime)

    def regime_willingness_shift(self, regime: MarketRegime) -> EconomicDecimal:
        """Return the hidden willingness-to-pay shift for one regime."""
        return next(item.shift for item in self.regime_willingness_shifts if item.regime is regime)

    def public_rules(self) -> ConsumerMarketRules:
        """Project only the market structure Agents are allowed to know."""
        return ConsumerMarketRules(
            consumer_group_count=3,
            groups_have_distinct_unknown_willingness_to_pay=True,
            purchase_priority="lowest_price_first",
            stockout_spills_to_next_lowest_price=True,
            equal_price_allocation="deterministic_equal_share",
            regimes=(
                MarketRegime.SLUMP,
                MarketRegime.NORMAL,
                MarketRegime.BOOM,
            ),
            minimum_regime_weeks=self.minimum_regime_weeks,
            maximum_regime_weeks=self.maximum_regime_weeks,
            normal_alternates_with_extreme_regimes=True,
            regimes_may_change_market_size_and_group_composition=True,
            willingness_to_pay_may_change_with_purchasing_power_and_regime=True,
            exact_demand_parameters_are_hidden=True,
        )


class ScoringSpec(StrictModel):
    """Immutable identity of the active benchmark scoring contract."""

    score_version: ScoreVersion = "s9-enterprise-v9"


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
    consumer_market: ConsumerMarketSpec
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
    unit_book_cost: Money = ZERO
    produced_week: int = Field(ge=1)
    expires_end_of_week: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_lifetime(self) -> Self:
        """Disallow a batch that expires before it is produced."""
        if self.expires_end_of_week < self.produced_week:
            raise ValueError("inventory cannot expire before production")
        return self

    @property
    def book_value(self) -> Money:
        """Return the lot's private acquisition or production cost."""
        return EconomicPrecision.round(self.quantity * self.unit_book_cost)


class CompanyState(StrictModel):
    """The complete economic state of one company."""

    company_id: CompanyId
    cash: Money
    operating_cost_payable: Money = ZERO
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
        return self.model_copy(update={"status": CompanyStatus.BANKRUPT, "bankrupt_on": on})

    def _inventory_lots(self, product: ProductId) -> tuple[InventoryLot, ...]:
        """Return inventory lots for one product in stored order."""
        return tuple(lot for lot in self.inventory if lot.product is product)

    def inventory_quantity(self, product: ProductId) -> Quantity:
        """Return total on-hand quantity for one product."""
        return sum(
            (lot.quantity for lot in self._inventory_lots(product)),
            start=ZERO,
        )

    def inventory_book_value(self, product: ProductId) -> Money:
        """Return total private book value for one product."""
        return sum(
            (lot.book_value for lot in self._inventory_lots(product)),
            start=ZERO,
        )

    def inventory_positions(
        self,
        product: ProductId | None = None,
    ) -> tuple[InventoryBookPosition, ...]:
        """Group inventory quantity and book value by product and expiry week."""
        lots = self.inventory if product is None else self._inventory_lots(product)
        keys = sorted({(lot.product, lot.expires_end_of_week) for lot in lots})
        return tuple(
            InventoryBookPosition(
                product=position_product,
                expires_end_of_week=expiry_week,
                quantity=sum(
                    (
                        lot.quantity
                        for lot in lots
                        if lot.product is position_product
                        and lot.expires_end_of_week == expiry_week
                    ),
                    start=ZERO,
                ),
                book_value=sum(
                    (
                        lot.book_value
                        for lot in lots
                        if lot.product is position_product
                        and lot.expires_end_of_week == expiry_week
                    ),
                    start=ZERO,
                ),
            )
            for position_product, expiry_week in keys
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


class RetailPrice(StrictModel):
    """One retailer's latest committed consumer price."""

    company_id: CompanyId
    product: ProductId
    unit_price: PositiveMoney


class ProductFlowSummary(StrictModel):
    """Quantity and value summary for one product flow."""

    product: ProductId
    quantity: Quantity = ZERO
    value: Money = ZERO
    volume_weighted_unit_price: Money | None = None

    @model_validator(mode="after")
    def validate_average(self) -> Self:
        """Pair a positive flow with a volume-weighted unit price."""
        has_flow = self.quantity > ZERO
        if has_flow != (self.volume_weighted_unit_price is not None):
            raise ValueError("positive product flow and average price must be paired")
        if not has_flow and self.value != ZERO:
            raise ValueError("zero-quantity product flow cannot carry value")
        return self


class InventoryBookPosition(StrictModel):
    """Private inventory quantity and book value in one expiry bucket."""

    product: ProductId
    expires_end_of_week: int = Field(ge=1)
    quantity: Quantity
    book_value: Money


class CompanyWeeklyReportBase(StrictModel):
    """Common authoritative weekly facts for one company."""

    week: int = Field(ge=1)
    company_id: CompanyId
    tier: CompanyTier
    status: CompanyStatus
    opening_enterprise_value: Money
    closing_enterprise_value: Money
    weekly_surplus_change: EconomicDecimal
    cumulative_surplus: EconomicDecimal
    ending_cash: Money
    ending_operating_cost_payable: Money = ZERO
    purchases: tuple[ProductFlowSummary, ...] = ()
    wholesale_sales: tuple[ProductFlowSummary, ...] = ()
    operation_cost: Money = ZERO
    expired_inventory: tuple[ProductFlowSummary, ...] = ()
    ending_inventory: tuple[InventoryBookPosition, ...] = ()


class FarmWeeklyReport(CompanyWeeklyReportBase):
    """Private weekly operating report for a farm."""

    report_type: Literal["farm"] = "farm"
    tier: Literal[CompanyTier.FARM] = CompanyTier.FARM
    realized_capacity: PositiveQuantity
    weekly_base_unit_cost: PositiveMoney
    produced_quantity: Quantity
    production_cost: Money
    average_production_cost: Money | None = None
    capacity_utilization: UnitInterval
    raw_milk_sold: Quantity
    raw_milk_unsold: Quantity


class ProcessorWeeklyReport(CompanyWeeklyReportBase):
    """Private weekly operating report for a processor."""

    report_type: Literal["processor"] = "processor"
    tier: Literal[CompanyTier.PROCESSOR] = CompanyTier.PROCESSOR
    realized_capacity: PositiveQuantity
    weekly_base_unit_cost: PositiveMoney
    raw_milk_purchased: Quantity
    raw_milk_purchase_spend: Money
    raw_milk_purchase_vwap: Money | None = None
    raw_milk_processed: Quantity
    bottled_milk_output: Quantity
    realized_yield: Quantity
    processing_cost: Money
    capacity_utilization: UnitInterval
    bottled_milk_sold: Quantity
    bottled_milk_unsold: Quantity


class RetailerWeeklyReport(CompanyWeeklyReportBase):
    """Private weekly sales and inventory report for a retailer."""

    report_type: Literal["retailer"] = "retailer"
    tier: Literal[CompanyTier.RETAILER] = CompanyTier.RETAILER
    procured_quantity: Quantity
    procurement_spend: Money
    procurement_vwap: Money | None = None
    saleable_quantity: Quantity
    saleable_book_value: Money
    weighted_unit_cost: Money | None = None
    committed_retail_price: PositiveMoney
    sold_quantity: Quantity
    consumer_revenue: Money
    cost_of_goods_sold: Money
    gross_profit: EconomicDecimal
    operating_profit: EconomicDecimal
    ending_inventory_quantity: Quantity
    ending_inventory_book_value: Money
    sell_through_rate: UnitInterval
    sold_out: bool
    market_share: UnitInterval
    expiry_quantity: Quantity
    expiry_book_loss: Money


type CompanyWeeklyReport = Annotated[
    FarmWeeklyReport | ProcessorWeeklyReport | RetailerWeeklyReport,
    Field(discriminator="report_type"),
]


class PublicRetailerPerformance(StrictModel):
    """Publicly observable weekly outcome for one retailer."""

    company_id: CompanyId
    status: CompanyStatus
    posted_price: PositiveMoney | None = None
    sold_quantity: Quantity
    market_share: UnitInterval
    sold_out: bool


class PublicRetailMarketReport(StrictModel):
    """Public shared-retail-market history without hidden demand parameters."""

    week: int = Field(ge=1)
    active_retailer_count: int = Field(ge=0)
    total_sold_quantity: Quantity
    volume_weighted_average_price: Money | None = None
    retailers: tuple[PublicRetailerPerformance, ...]

    @model_validator(mode="after")
    def validate_market_totals(self) -> Self:
        """Keep public totals aligned with retailer-level facts."""
        if self.total_sold_quantity != sum(
            (retailer.sold_quantity for retailer in self.retailers),
            start=ZERO,
        ):
            raise ValueError("public retailer sales must sum to the market total")
        has_sales = self.total_sold_quantity > ZERO
        if has_sales != (self.volume_weighted_average_price is not None):
            raise ValueError("positive public sales and average price must be paired")
        return self


class WorldState(StrictModel):
    """Immutable state after the latest completed week."""

    scenario: ScenarioSpec
    seed: int
    completed_weeks: int = Field(ge=0)
    companies: tuple[CompanyState, ...]
    operation_states: tuple[WeeklyOperationState, ...]
    previous_markets: tuple[MarketSummary, ...] = ()
    retail_prices: tuple[RetailPrice, ...] = ()
    company_weekly_reports: tuple[CompanyWeeklyReport, ...] = ()
    public_retail_market_reports: tuple[PublicRetailMarketReport, ...] = ()
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
        report_keys = tuple(
            (report.week, report.company_id) for report in self.company_weekly_reports
        )
        if len(report_keys) != len(set(report_keys)):
            raise ValueError("company weekly reports must be unique")
        if any(
            report.week > self.completed_weeks or report.company_id not in configured
            for report in self.company_weekly_reports
        ):
            raise ValueError("company weekly report lies outside the world state")
        public_weeks = tuple(report.week for report in self.public_retail_market_reports)
        if public_weeks != tuple(sorted(set(public_weeks))):
            raise ValueError("public retail reports must use unique increasing weeks")
        if any(week > self.completed_weeks for week in public_weeks):
            raise ValueError("public retail report lies outside the world state")
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
    consumer_market_rules: ConsumerMarketRules
    scoring: ScoringSpec
    cash: Money
    operating_cost_payable: Money = ZERO
    inventory: tuple[InventoryPosition, ...]
    public_companies: tuple[PublicCompany, ...]
    previous_markets: tuple[MarketSummary, ...]
    weekly_reports: tuple[CompanyWeeklyReport, ...] = Field(default=(), max_length=8)
    public_retail_market_reports: tuple[PublicRetailMarketReport, ...] = Field(
        default=(),
        max_length=8,
    )
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
        report_weeks = tuple(report.week for report in self.weekly_reports)
        if any(report.company_id != self.company_id for report in self.weekly_reports):
            raise ValueError("private weekly reports must belong to the observed company")
        if report_weeks != tuple(sorted(set(report_weeks))):
            raise ValueError("private weekly reports must use unique increasing weeks")
        public_weeks = tuple(report.week for report in self.public_retail_market_reports)
        if public_weeks != tuple(sorted(set(public_weeks))):
            raise ValueError("public retail reports must use unique increasing weeks")
        if any(week >= self.sim_day.week for week in (*report_weeks, *public_weeks)):
            raise ValueError("weekly reports must precede the current simulation week")
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
    """One retailer's settlement in the shared consumer market."""

    event_type: Literal["consumer_sale"] = "consumer_sale"
    saleable_quantity: Quantity
    saleable_book_value: Money
    sold_quantity: Quantity
    retail_price: PositiveMoney | None = None
    revenue: Money
    cost_of_goods_sold: Money
    gross_profit: EconomicDecimal
    sold_out: bool

    @model_validator(mode="after")
    def validate_sale(self) -> Self:
        """Keep quantity, revenue, cost, and sold-out facts internally aligned."""
        if self.sold_quantity > self.saleable_quantity:
            raise ValueError("consumer sales cannot exceed saleable inventory")
        if self.retail_price is None:
            if self.sold_quantity != ZERO or self.revenue != ZERO:
                raise ValueError("an unpriced retailer cannot complete consumer sales")
        elif self.revenue != EconomicPrecision.round(self.sold_quantity * self.retail_price):
            raise ValueError("consumer revenue must equal price times sold quantity")
        if self.cost_of_goods_sold > self.saleable_book_value:
            raise ValueError("consumer cost of goods cannot exceed saleable book value")
        if self.gross_profit != self.revenue - self.cost_of_goods_sold:
            raise ValueError("consumer gross profit must equal revenue minus cost")
        if self.sold_out != (
            self.saleable_quantity > ZERO and self.sold_quantity == self.saleable_quantity
        ):
            raise ValueError("sold_out must match exhausted positive inventory")
        return self


class RetailOperatingCostChargedEvent(CompanyEvent):
    """One retailer's accrued weekly store cost and resulting payment."""

    event_type: Literal["retail_operating_cost_charged"] = "retail_operating_cost_charged"
    opening_payable: Money
    cost_accrued: PositiveMoney
    cash_paid: Money
    closing_payable: Money

    @model_validator(mode="after")
    def validate_settlement(self) -> Self:
        """Balance accrued cost, cash payment, and the closing payable."""
        if self.opening_payable + self.cost_accrued != (self.cash_paid + self.closing_payable):
            raise ValueError("retail operating-cost settlement must balance")
        return self


class InventoryExpiredEvent(CompanyEvent):
    """One expired lot removed at the end of a week."""

    event_type: Literal["inventory_expired"] = "inventory_expired"
    lot_id: Identifier
    product: ProductId
    quantity: PositiveQuantity
    reference_value_loss: PositiveMoney
    book_value_loss: Money = ZERO


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
    | RetailOperatingCostChargedEvent
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
    operating_cost_payable: Money
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
    final_operating_cost_payable: Money
    final_inventory_value: Money
    final_value: Money
    surplus: EconomicDecimal
    growth: Rate


class ScoreCard(StrictModel):
    """Official S9 enterprise benchmark score and auditable components."""

    score_version: ScoreVersion
    final_score: BenchmarkScore
    efficiency_raw: EconomicDecimal
    efficiency_oracle: Money
    efficiency_score: UnitInterval
    global_gini: UnitInterval
    fairness_score: UnitInterval
    non_loss_company_ratio: UnitInterval
    bankrupt_company_count: int = Field(ge=0)
    loss_making_company_count: int = Field(ge=0)
    loss_making_company_rate: UnitInterval
    companies: tuple[CompanyScore, ...]

    @model_validator(mode="after")
    def validate_company_counts(self) -> Self:
        """Keep aggregate bankruptcy and loss facts aligned with company detail."""
        company_ids = [company.company_id for company in self.companies]
        if len(company_ids) != len(set(company_ids)):
            raise ValueError("score companies must be unique")
        bankrupt = sum(company.status is CompanyStatus.BANKRUPT for company in self.companies)
        losses = sum(company.surplus < ZERO for company in self.companies)
        if self.bankrupt_company_count != bankrupt:
            raise ValueError("bankrupt_company_count must match company status")
        if self.loss_making_company_count != losses:
            raise ValueError("loss_making_company_count must match company surplus")
        expected_loss_rate = EconomicPrecision.round(Decimal(losses) / Decimal(len(self.companies)))
        if self.loss_making_company_rate != expected_loss_rate:
            raise ValueError("loss_making_company_rate must match company surplus")
        if self.non_loss_company_ratio != EconomicPrecision.round(ONE - expected_loss_rate):
            raise ValueError("non_loss_company_ratio must equal one minus loss rate")
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
