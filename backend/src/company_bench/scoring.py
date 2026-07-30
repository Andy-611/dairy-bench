from decimal import Decimal

from company_bench.models import (
    ZERO,
    CompanyScore,
    CompanyState,
    CompanyTier,
    ConstraintResult,
    ConsumerSaleEvent,
    DaySnapshot,
    DomainEvent,
    InventoryExpiredEvent,
    ScenarioSpec,
    ScoreCard,
    TierFairness,
    TradeExecutedEvent,
    WorldState,
)


class Evaluator:
    """Read-only evaluator shared by versioned Dairy Bench scenarios."""

    def evaluate(
        self,
        scenario: ScenarioSpec,
        initial_state: WorldState,
        final_state: WorldState,
        snapshots: tuple[DaySnapshot, ...],
        events: tuple[DomainEvent, ...],
    ) -> ScoreCard:
        """Calculate final metrics without changing economic state."""
        self._validate_episode(
            scenario,
            initial_state,
            final_state,
            snapshots,
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
        tier_results = self._tier_fairness(company_scores)
        gini = sum((tier.gini for tier in tier_results), start=ZERO) / Decimal(len(tier_results))
        constraints = (
            ConstraintResult(
                name="weighted_tier_gini",
                actual=gini,
                limit=scenario.scoring.max_gini,
                passed=gini <= scenario.scoring.max_gini,
            ),
            *(
                ConstraintResult(
                    name=f"{tier.tier.value}_growth_gap",
                    actual=tier.growth_gap,
                    limit=scenario.scoring.max_within_tier_growth_gap,
                    passed=(tier.growth_gap <= scenario.scoring.max_within_tier_growth_gap),
                )
                for tier in tier_results
            ),
        )
        total_demand = ZERO
        total_consumer_sales = ZERO
        expired_quantity = ZERO
        total_trade_quantity = ZERO
        consumer_revenue = ZERO
        for event in events:
            if isinstance(event, ConsumerSaleEvent):
                total_demand += event.potential_demand_quantity
                total_consumer_sales += event.sold_quantity
                consumer_revenue += event.revenue
            elif isinstance(event, InventoryExpiredEvent):
                expired_quantity += event.quantity
            elif isinstance(event, TradeExecutedEvent):
                total_trade_quantity += event.quantity

        return ScoreCard(
            efficiency=sum(
                (company.surplus for company in company_scores),
                start=ZERO,
            ),
            fairness=Decimal("1") - gini,
            gini=gini,
            eligible=all(constraint.passed for constraint in constraints),
            consumer_fill_rate=(
                min(
                    Decimal("1"),
                    total_consumer_sales / total_demand,
                )
                if total_demand > ZERO
                else ZERO
            ),
            expired_quantity=expired_quantity,
            total_trade_quantity=total_trade_quantity,
            consumer_revenue=consumer_revenue,
            companies=company_scores,
            tiers=tier_results,
            constraints=constraints,
        )

    def _company_score(
        self,
        scenario: ScenarioSpec,
        company_id: str,
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
            growth=final_value / initial_value,
        )

    def _tier_fairness(
        self,
        companies: tuple[CompanyScore, ...],
    ) -> tuple[TierFairness, ...]:
        """Compute Gini and growth spread inside each represented tier."""
        results: list[TierFairness] = []
        for tier in CompanyTier:
            growth = tuple(company.growth for company in companies if company.tier == tier)
            if not growth:
                continue
            gini = self._gini(growth)
            gap = max(growth) - min(growth)
            results.append(
                TierFairness(
                    tier=tier,
                    gini=gini,
                    growth_gap=gap,
                )
            )
        if not results:
            raise ValueError("fairness requires at least one company tier")
        return tuple(results)

    @staticmethod
    def _gini(values: tuple[Decimal, ...]) -> Decimal:
        """Calculate standard Gini for nonnegative capital growth."""
        total = sum(values, start=ZERO)
        if total == ZERO:
            return ZERO
        absolute_differences = sum(
            (abs(left - right) for left in values for right in values),
            start=ZERO,
        )
        return absolute_differences / (Decimal("2") * Decimal(len(values)) * total)

    @staticmethod
    def _validate_episode(
        scenario: ScenarioSpec,
        initial_state: WorldState,
        final_state: WorldState,
        snapshots: tuple[DaySnapshot, ...],
    ) -> None:
        """Reject mismatched or incomplete evaluation inputs."""
        if initial_state.scenario != scenario or final_state.scenario != scenario:
            raise ValueError("states do not belong to the supplied scenario")
        if initial_state.seed != final_state.seed:
            raise ValueError("initial and final seeds differ")
        if initial_state.day != 0:
            raise ValueError("evaluation must start from day zero")
        if final_state.day != scenario.days:
            raise ValueError("evaluation requires a complete episode")
        expected_days = tuple(range(1, scenario.days + 1))
        if tuple(snapshot.day for snapshot in snapshots) != expected_days:
            raise ValueError("snapshots must cover every day exactly once")

        final_companies = {company.company_id: company for company in final_state.companies}
        for snapshot in snapshots[-1].companies:
            company = final_companies[snapshot.company_id]
            if (
                snapshot.cash != company.cash
                or snapshot.inventory_value != scenario.inventory_value(company.inventory)
            ):
                raise ValueError("final snapshot does not match final state")
