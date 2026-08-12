from decimal import Decimal

from company_bench.domain.models import (
    ZERO,
    CompanyId,
    CompanyScore,
    CompanyState,
    CompanyTier,
    ConsumerSaleEvent,
    DomainEvent,
    ProductId,
    RetailerOperation,
    ScenarioSpec,
    ScoreCard,
    WeekSnapshot,
    WorldState,
)
from company_bench.domain.precision import EconomicPrecision
from company_bench.economy.demand import ConsumerDemandCurve

_ONE = Decimal("1")
_HUNDRED = Decimal("100")
_BANKRUPTCY_NET_WORTH_THRESHOLD = Decimal("1.0000")
_S9_TIER_GINI_MAX = Decimal("2") / Decimal("3")


class Evaluator:
    """Calculate the complete S9 benchmark score behind one pure interface."""

    def evaluate(
        self,
        scenario: ScenarioSpec,
        initial_state: WorldState,
        final_state: WorldState,
        snapshots: tuple[WeekSnapshot, ...],
        events: tuple[DomainEvent, ...],
    ) -> ScoreCard:
        """Validate and score one complete deterministic episode."""
        self._validate_episode(scenario, initial_state, final_state, snapshots)
        demand_curve = ConsumerDemandCurve(spec=scenario.demand)
        self._validate_consumer_sales(
            scenario,
            initial_state.seed,
            snapshots,
            events,
            demand_curve,
        )
        initial = {company.company_id: company for company in initial_state.companies}
        final = {company.company_id: company for company in final_state.companies}
        company_scores = tuple(
            self._company_score(
                scenario,
                company.company_id,
                company.tier,
                initial[company.company_id],
                final[company.company_id],
            )
            for company in scenario.companies
        )

        efficiency_raw = sum(
            (company.surplus for company in company_scores),
            start=ZERO,
        )
        efficiency_reference_raw = self._efficiency_reference(
            scenario,
            initial_state.seed,
            demand_curve,
        )
        efficiency_score_raw = (
            self._unit_interval(efficiency_raw / efficiency_reference_raw)
            if efficiency_reference_raw > ZERO
            else ZERO
        )
        farm_gini_raw = self._tier_gini(company_scores, CompanyTier.FARM)
        processor_gini_raw = self._tier_gini(company_scores, CompanyTier.PROCESSOR)
        retailer_gini_raw = self._tier_gini(company_scores, CompanyTier.RETAILER)
        mean_gini = (
            farm_gini_raw + processor_gini_raw + retailer_gini_raw
        ) / Decimal("3")
        fairness_score_raw = self._unit_interval(
            _ONE - mean_gini / _S9_TIER_GINI_MAX
        )
        bankrupt_company_ids = {
            company.company_id
            for snapshot in snapshots
            for company in snapshot.companies
            if company.net_worth <= _BANKRUPTCY_NET_WORTH_THRESHOLD
        }
        bankrupt_company_count = len(bankrupt_company_ids)
        bankruptcy_rate_raw = Decimal(bankrupt_company_count) / Decimal(len(company_scores))
        survival_score = _ONE - bankruptcy_rate_raw
        final_score = EconomicPrecision.round(
            _HUNDRED
            * efficiency_score_raw
            * (fairness_score_raw * survival_score).sqrt()
        )

        return ScoreCard(
            score_version=scenario.scoring.score_version,
            final_score=final_score,
            efficiency_raw=efficiency_raw,
            efficiency_reference=EconomicPrecision.round(efficiency_reference_raw),
            efficiency_score=EconomicPrecision.round(efficiency_score_raw),
            farm_gini=EconomicPrecision.round(farm_gini_raw),
            processor_gini=EconomicPrecision.round(processor_gini_raw),
            retailer_gini=EconomicPrecision.round(retailer_gini_raw),
            fairness_score=EconomicPrecision.round(fairness_score_raw),
            bankrupt_company_count=bankrupt_company_count,
            bankruptcy_rate=EconomicPrecision.round(bankruptcy_rate_raw),
            companies=company_scores,
        )

    @staticmethod
    def _company_score(
        scenario: ScenarioSpec,
        company_id: CompanyId,
        tier: CompanyTier,
        initial: CompanyState,
        final: CompanyState,
    ) -> CompanyScore:
        """Value one company's cash and inventory at fixed references."""
        initial_inventory = scenario.inventory_value(initial.inventory)
        final_inventory = scenario.inventory_value(final.inventory)
        initial_value = initial.cash + initial_inventory
        final_value = final.cash + final_inventory
        if initial_value <= ZERO:
            raise ValueError("company initial value must be positive")
        return CompanyScore(
            company_id=company_id,
            tier=tier,
            initial_value=initial_value,
            final_cash=final.cash,
            final_inventory_value=final_inventory,
            final_value=final_value,
            surplus=final_value - initial_value,
            growth=EconomicPrecision.round(final_value / initial_value),
        )

    @classmethod
    def _tier_gini(
        cls,
        companies: tuple[CompanyScore, ...],
        tier: CompanyTier,
    ) -> Decimal:
        """Calculate one tier's raw Gini from final capital growth."""
        return cls._gini(
            tuple(
                company.final_value / company.initial_value
                for company in companies
                if company.tier is tier
            )
        )

    @staticmethod
    def _gini(values: tuple[Decimal, ...]) -> Decimal:
        """Calculate standard population Gini for nonnegative values."""
        total = sum(values, start=ZERO)
        if total == ZERO:
            return ZERO
        absolute_differences = sum(
            (abs(left - right) for left in values for right in values),
            start=ZERO,
        )
        return absolute_differences / (Decimal("2") * Decimal(len(values)) * total)

    @staticmethod
    def _validate_consumer_sales(
        scenario: ScenarioSpec,
        seed: int,
        snapshots: tuple[WeekSnapshot, ...],
        events: tuple[DomainEvent, ...],
        demand_curve: ConsumerDemandCurve,
    ) -> None:
        """Validate the unique complete consumer-sale grid and snapshot totals."""
        retailer_ids = tuple(
            company.company_id
            for company in scenario.companies
            if isinstance(company.operation, RetailerOperation)
        )
        expected_keys = tuple(
            (week, company_id)
            for week in range(1, scenario.weeks + 1)
            for company_id in retailer_ids
        )
        sales = tuple(event for event in events if isinstance(event, ConsumerSaleEvent))
        indexed_sales = {(event.occurred_on.week, event.company_id): event for event in sales}
        if len(indexed_sales) != len(sales):
            raise ValueError("consumer sale events must be unique per week and retailer")
        if set(indexed_sales) != set(expected_keys):
            raise ValueError("consumer sale events must cover every week and retailer exactly once")

        for sale in sales:
            expected_potential = demand_curve.potential(
                seed,
                sale.occurred_on.week,
                sale.company_id,
            )
            if sale.potential_demand_quantity != expected_potential:
                raise ValueError("consumer sale potential does not match the episode seed")
            expected_demand = demand_curve.quantity(
                sale.potential_demand_quantity,
                sale.retail_price,
            )
            if sale.demand_quantity != expected_demand:
                raise ValueError("consumer sale demand does not match the continuous curve")
            if sale.sold_quantity > sale.demand_quantity:
                raise ValueError("consumer sales cannot exceed demand")
            expected_revenue = (
                EconomicPrecision.round(sale.sold_quantity * sale.retail_price)
                if sale.retail_price is not None
                else ZERO
            )
            if sale.revenue != expected_revenue:
                raise ValueError("consumer sale revenue does not match price times quantity")
            if sale.retail_price is None and sale.sold_quantity != ZERO:
                raise ValueError("a retailer without a price cannot complete consumer sales")

        snapshots_by_week = {snapshot.week: snapshot for snapshot in snapshots}
        for week in range(1, scenario.weeks + 1):
            weekly_sales = tuple(
                indexed_sales[(week, company_id)] for company_id in retailer_ids
            )
            snapshot = snapshots_by_week[week]
            if snapshot.consumer_demand != sum(
                (sale.potential_demand_quantity for sale in weekly_sales),
                start=ZERO,
            ):
                raise ValueError("snapshot consumer demand does not match consumer events")
            if snapshot.consumer_sales != sum(
                (sale.sold_quantity for sale in weekly_sales),
                start=ZERO,
            ):
                raise ValueError("snapshot consumer sales do not match consumer events")

    @staticmethod
    def _efficiency_reference(
        scenario: ScenarioSpec,
        seed: int,
        demand_curve: ConsumerDemandCurve,
    ) -> Decimal:
        """Derive this seed's reference directly from every retailer-week market."""
        total = ZERO
        for week in range(1, scenario.weeks + 1):
            for company in scenario.companies:
                operation = company.operation
                if not isinstance(operation, RetailerOperation):
                    continue
                potential = demand_curve.potential(seed, week, company.company_id)
                unit_cost = scenario.product(operation.input_product).reference_value
                total += demand_curve.max_net_value(potential, unit_cost)
        return total

    @staticmethod
    def _unit_interval(value: Decimal) -> Decimal:
        """Clamp one Decimal to the closed unit interval."""
        return min(_ONE, max(ZERO, value))

    @staticmethod
    def _validate_episode(
        scenario: ScenarioSpec,
        initial_state: WorldState,
        final_state: WorldState,
        snapshots: tuple[WeekSnapshot, ...],
    ) -> None:
        """Reject mismatched, incomplete, or internally inconsistent episode facts."""
        if initial_state.scenario != scenario or final_state.scenario != scenario:
            raise ValueError("states do not belong to the supplied scenario")
        if initial_state.seed != final_state.seed:
            raise ValueError("initial and final seeds differ")
        if initial_state.completed_weeks != 0:
            raise ValueError("evaluation must start before week one")
        if final_state.completed_weeks != scenario.weeks:
            raise ValueError("evaluation requires a complete episode")
        expected_weeks = tuple(range(1, scenario.weeks + 1))
        if tuple(snapshot.week for snapshot in snapshots) != expected_weeks:
            raise ValueError("snapshots must cover every week exactly once")

        expected_company_ids = tuple(company.company_id for company in scenario.companies)
        initial_companies = {company.company_id: company for company in initial_state.companies}
        raw_reference = scenario.product(ProductId.RAW_MILK).reference_value
        bottled_reference = scenario.product(ProductId.BOTTLED_MILK).reference_value
        for snapshot in snapshots:
            if tuple(company.company_id for company in snapshot.companies) != expected_company_ids:
                raise ValueError("every snapshot must contain each scenario company exactly once")
            for company in snapshot.companies:
                if company.week != snapshot.week:
                    raise ValueError("company snapshot week does not match its parent snapshot")
                if company.tier is not scenario.company(company.company_id).tier:
                    raise ValueError("company snapshot tier does not match the scenario")
                expected_inventory_value = EconomicPrecision.round(
                    company.raw_milk_quantity * raw_reference
                    + company.bottled_milk_quantity * bottled_reference
                )
                if company.inventory_value != expected_inventory_value:
                    raise ValueError("company snapshot inventory value is inconsistent")
                if company.net_worth != company.cash + company.inventory_value:
                    raise ValueError("company snapshot net worth is inconsistent")
                initial = initial_companies[company.company_id]
                initial_value = initial.cash + scenario.inventory_value(initial.inventory)
                if company.surplus != company.net_worth - initial_value:
                    raise ValueError("company snapshot surplus is inconsistent")

        final_companies = {company.company_id: company for company in final_state.companies}
        for company in snapshots[-1].companies:
            final = final_companies[company.company_id]
            if company.cash != final.cash or company.inventory_value != scenario.inventory_value(
                final.inventory
            ):
                raise ValueError("final snapshot does not match final state")
