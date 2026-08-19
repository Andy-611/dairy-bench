"""Enterprise-efficiency, fairness, and non-loss scoring for Dairy Bench V9."""

from decimal import Decimal

from company_bench.domain.models import (
    ZERO,
    CompanyBankruptEvent,
    CompanyId,
    CompanyScore,
    CompanyState,
    CompanyStatus,
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
from company_bench.economy.consumer import RetailOffer, SharedConsumerMarket
from company_bench.economy.oracle import EnterpriseOracle
from company_bench.economy.valuation import EnterpriseValuation

_ONE = Decimal("1")
_HUNDRED = Decimal("100")
_ORACLE_TOLERANCE = Decimal("0.0001")


class Evaluator:
    """Calculate the complete S9 enterprise score behind one pure interface."""

    def __init__(
        self,
        valuation: EnterpriseValuation | None = None,
        oracle: EnterpriseOracle | None = None,
    ) -> None:
        self._valuation = valuation or EnterpriseValuation()
        self._oracle = oracle or EnterpriseOracle()

    def evaluate(
        self,
        scenario: ScenarioSpec,
        initial_state: WorldState,
        final_state: WorldState,
        snapshots: tuple[WeekSnapshot, ...],
        events: tuple[DomainEvent, ...],
    ) -> ScoreCard:
        """Validate and score one settled prefix of a deterministic episode."""
        self._validate_episode(scenario, initial_state, final_state, snapshots)
        self._validate_consumer_sales(
            scenario,
            initial_state.seed,
            snapshots,
            events,
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
        oracle = self._oracle.evaluate(
            scenario,
            initial_state.seed,
            final_state.completed_weeks,
        )
        efficiency_oracle = oracle.enterprise_surplus_upper_bound
        if efficiency_raw > efficiency_oracle + _ORACLE_TOLERANCE:
            raise ValueError(
                "realized enterprise surplus exceeds the Oracle reference; "
                "score integrity cannot be guaranteed"
            )
        efficiency = (
            self._unit_interval(efficiency_raw / efficiency_oracle)
            if efficiency_oracle > ZERO
            else ZERO
        )
        company_count = len(company_scores)
        global_gini = self._gini(tuple(company.final_value for company in company_scores))
        maximum_gini = Decimal(company_count - 1) / Decimal(company_count)
        fairness = (
            self._unit_interval(_ONE - global_gini / maximum_gini) if maximum_gini > ZERO else _ONE
        )
        loss_count = sum(company.surplus < ZERO for company in company_scores)
        loss_rate = Decimal(loss_count) / Decimal(company_count)
        non_loss_ratio = _ONE - loss_rate
        bankrupt_count = self._bankrupt_company_count(events, final_state)
        final_score = EconomicPrecision.round(
            _HUNDRED * efficiency * (fairness * non_loss_ratio).sqrt()
        )

        return ScoreCard(
            score_version=scenario.scoring.score_version,
            final_score=final_score,
            efficiency_raw=efficiency_raw,
            efficiency_oracle=efficiency_oracle,
            efficiency_score=EconomicPrecision.round(efficiency),
            global_gini=EconomicPrecision.round(global_gini),
            fairness_score=EconomicPrecision.round(fairness),
            non_loss_company_ratio=EconomicPrecision.round(non_loss_ratio),
            bankrupt_company_count=bankrupt_count,
            loss_making_company_count=loss_count,
            loss_making_company_rate=EconomicPrecision.round(loss_rate),
            companies=company_scores,
        )

    def _company_score(
        self,
        scenario: ScenarioSpec,
        company_id: CompanyId,
        tier: CompanyTier,
        initial: CompanyState,
        final: CompanyState,
    ) -> CompanyScore:
        """Value one company's cash and inventory at fixed references."""
        final_inventory = scenario.inventory_value(final.inventory)
        initial_value = self._valuation.settled_value(scenario, initial)
        final_value = self._valuation.settled_value(scenario, final)
        if initial_value <= ZERO:
            raise ValueError("company initial value must be positive")
        return CompanyScore(
            company_id=company_id,
            tier=tier,
            status=final.status,
            initial_value=initial_value,
            final_cash=final.cash,
            final_operating_cost_payable=final.operating_cost_payable,
            final_inventory_value=final_inventory,
            final_value=final_value,
            surplus=final_value - initial_value,
            growth=EconomicPrecision.round(final_value / initial_value),
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
    def _bankrupt_company_count(
        events: tuple[DomainEvent, ...],
        final_state: WorldState,
    ) -> int:
        """Count unique authoritative exits and validate terminal company status."""
        bankruptcies = tuple(event for event in events if isinstance(event, CompanyBankruptEvent))
        event_ids = [event.company_id for event in bankruptcies]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("each company may declare bankruptcy only once")
        if any(event.total_assets >= _ONE for event in bankruptcies):
            raise ValueError("bankruptcy requires total assets below one")
        final_ids = {
            company.company_id
            for company in final_state.companies
            if company.status is CompanyStatus.BANKRUPT
        }
        if set(event_ids) != final_ids:
            raise ValueError("bankruptcy events must match final company status")
        return len(bankruptcies)

    @staticmethod
    def _validate_consumer_sales(
        scenario: ScenarioSpec,
        seed: int,
        snapshots: tuple[WeekSnapshot, ...],
        events: tuple[DomainEvent, ...],
    ) -> None:
        """Recompute every settled shared-market allocation from authoritative facts."""
        retailer_ids = tuple(
            company.company_id
            for company in scenario.companies
            if isinstance(company.operation, RetailerOperation)
        )
        expected_keys = {
            (week, company_id)
            for week in range(1, len(snapshots) + 1)
            for company_id in retailer_ids
        }
        sales = tuple(event for event in events if isinstance(event, ConsumerSaleEvent))
        indexed = {(event.occurred_on.week, event.company_id): event for event in sales}
        if len(indexed) != len(sales) or set(indexed) != expected_keys:
            raise ValueError("consumer sale events must cover each week and retailer once")

        market = SharedConsumerMarket(scenario.consumer_market)
        snapshots_by_week = {snapshot.week: snapshot for snapshot in snapshots}
        for week in range(1, len(snapshots) + 1):
            weekly_sales = tuple(indexed[(week, company_id)] for company_id in retailer_ids)
            expected = market.settle(
                seed,
                week,
                tuple(
                    RetailOffer(
                        company_id=sale.company_id,
                        unit_price=sale.retail_price,
                        available_quantity=sale.saleable_quantity,
                    )
                    for sale in weekly_sales
                ),
            )
            expected_by_company = {sale.company_id: sale.sold_quantity for sale in expected.sales}
            if any(
                sale.sold_quantity != expected_by_company[sale.company_id] for sale in weekly_sales
            ):
                raise ValueError("consumer sale allocation does not match shared-market rules")
            snapshot = snapshots_by_week[week]
            if snapshot.consumer_demand != expected.population.quantity:
                raise ValueError("snapshot consumer demand does not match hidden population")
            if snapshot.consumer_sales != sum(
                (sale.sold_quantity for sale in weekly_sales),
                start=ZERO,
            ):
                raise ValueError("snapshot consumer sales do not match consumer events")

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
        """Reject mismatched or internally inconsistent settled-prefix facts."""
        if initial_state.scenario != scenario or final_state.scenario != scenario:
            raise ValueError("states do not belong to the supplied scenario")
        if initial_state.seed != final_state.seed:
            raise ValueError("initial and final seeds differ")
        if initial_state.completed_weeks != 0:
            raise ValueError("evaluation must start before week one")
        if not 1 <= final_state.completed_weeks <= scenario.weeks:
            raise ValueError("evaluation requires at least one completed week")
        expected_weeks = tuple(range(1, final_state.completed_weeks + 1))
        if tuple(snapshot.week for snapshot in snapshots) != expected_weeks:
            raise ValueError("snapshots must cover every week exactly once")

        expected_company_ids = tuple(company.company_id for company in scenario.companies)
        initial_companies = {company.company_id: company for company in initial_state.companies}
        if any(not company.is_active for company in initial_state.companies):
            raise ValueError("evaluation must begin with active companies")
        raw_reference = scenario.product(ProductId.RAW_MILK).reference_value
        bottled_reference = scenario.product(ProductId.BOTTLED_MILK).reference_value
        bankrupt_ids: set[CompanyId] = set()
        for snapshot in snapshots:
            if tuple(company.company_id for company in snapshot.companies) != (
                expected_company_ids
            ):
                raise ValueError("every snapshot must contain each scenario company once")
            for company in snapshot.companies:
                if company.week != snapshot.week:
                    raise ValueError("company snapshot week does not match its parent")
                if company.tier is not scenario.company(company.company_id).tier:
                    raise ValueError("company snapshot tier does not match the scenario")
                if company.company_id in bankrupt_ids and company.status is CompanyStatus.ACTIVE:
                    raise ValueError("bankrupt companies cannot become active again")
                if company.status is CompanyStatus.BANKRUPT:
                    bankrupt_ids.add(company.company_id)
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
            if (
                company.cash != final.cash
                or company.status is not final.status
                or company.inventory_value != scenario.inventory_value(final.inventory)
            ):
                raise ValueError("final snapshot does not match final state")
