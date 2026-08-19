from decimal import Decimal

import pytest

from company_bench.domain.calendar import SimDay
from company_bench.domain.models import (
    ZERO,
    CompanyBankruptEvent,
    CompanySnapshot,
    CompanyState,
    CompanyStatus,
    ConsumerSaleEvent,
    DomainEvent,
    MarketRegime,
    MarketSummary,
    RetailerOperation,
    ScenarioSpec,
    WeekSnapshot,
    WorldState,
)
from company_bench.domain.scenario import DAIRY_S9_SCENARIO
from company_bench.economy.consumer import SharedConsumerMarket
from company_bench.economy.engine import EconomyEngine
from company_bench.economy.oracle import ORACLE_VERSION, EnterpriseOracle, OracleResult
from company_bench.economy.scoring import Evaluator


class _FixedOracle:
    def __init__(self, value: Decimal = Decimal("100")) -> None:
        self._value = value
        self.horizons: list[int | None] = []

    def evaluate(
        self,
        _: ScenarioSpec,
        __: int,
        weeks: int | None = None,
    ) -> OracleResult:
        self.horizons.append(weeks)
        return OracleResult(
            oracle_version=ORACLE_VERSION,
            input_hash="test-oracle",
            solver="test",
            solver_status="optimal",
            enterprise_surplus_upper_bound=self._value,
        )


def _scenario() -> ScenarioSpec:
    return ScenarioSpec.model_validate_json(
        DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1}).model_dump_json()
    )


def _facts(
    surpluses: tuple[Decimal, ...],
    *,
    bankrupt_index: int | None = None,
) -> tuple[
    ScenarioSpec,
    WorldState,
    WorldState,
    tuple[WeekSnapshot, ...],
    tuple[DomainEvent, ...],
]:
    scenario = _scenario()
    engine = EconomyEngine()
    initial = engine.initial_state(scenario, 42)
    sunday = SimDay(absolute_day=7)
    final_companies = tuple(
        CompanyState(
            company_id=company.company_id,
            cash=company.initial_cash + surplus,
            status=(CompanyStatus.BANKRUPT if index == bankrupt_index else CompanyStatus.ACTIVE),
            bankrupt_on=sunday if index == bankrupt_index else None,
        )
        for index, (company, surplus) in enumerate(zip(scenario.companies, surpluses, strict=True))
    )
    final = WorldState(
        scenario=scenario,
        seed=42,
        completed_weeks=1,
        companies=final_companies,
        operation_states=initial.operation_states,
    )
    company_snapshots = tuple(
        CompanySnapshot(
            week=1,
            company_id=spec.company_id,
            tier=spec.tier,
            status=state.status,
            cash=state.cash,
            operating_cost_payable=ZERO,
            raw_milk_quantity=ZERO,
            bottled_milk_quantity=ZERO,
            inventory_value=ZERO,
            net_worth=state.cash,
            surplus=state.cash - spec.initial_cash,
            weekly_consumer_sales=ZERO,
            weekly_expired_quantity=ZERO,
        )
        for spec, state in zip(scenario.companies, final_companies, strict=True)
    )
    population = SharedConsumerMarket(scenario.consumer_market).population(42, 1)
    snapshot = WeekSnapshot(
        week=1,
        companies=company_snapshots,
        markets=tuple(
            MarketSummary(product=product.product, volume=ZERO) for product in scenario.products
        ),
        consumer_demand=population.quantity,
        consumer_sales=ZERO,
        expired_quantity=ZERO,
    )
    sales: tuple[DomainEvent, ...] = tuple(
        ConsumerSaleEvent(
            occurred_on=sunday,
            company_id=company.company_id,
            saleable_quantity=ZERO,
            saleable_book_value=ZERO,
            sold_quantity=ZERO,
            retail_price=Decimal("3.5000"),
            revenue=ZERO,
            cost_of_goods_sold=ZERO,
            gross_profit=ZERO,
            sold_out=False,
        )
        for company in scenario.companies
        if company.tier.value == "retailer"
    )
    bankruptcy: tuple[DomainEvent, ...] = (
        (
            CompanyBankruptEvent(
                occurred_on=sunday,
                company_id=final_companies[bankrupt_index].company_id,
                total_assets=final_companies[bankrupt_index].cash,
            ),
        )
        if bankrupt_index is not None
        else ()
    )
    return scenario, initial, final, (snapshot,), (*sales, *bankruptcy)


def _evaluate(
    surpluses: tuple[Decimal, ...],
    *,
    oracle: Decimal = Decimal("100"),
    bankrupt_index: int | None = None,
):
    facts = _facts(surpluses, bankrupt_index=bankrupt_index)
    return Evaluator(oracle=_FixedOracle(oracle)).evaluate(*facts)


def test_score_card_uses_oracle_fairness_and_non_loss_contract() -> None:
    score = _evaluate((Decimal("10"),) * 9)

    assert score.score_version == "s9-enterprise-v9"
    assert score.efficiency_raw == Decimal("90.0000")
    assert score.efficiency_oracle == Decimal("100.0000")
    assert score.efficiency_score == Decimal("0.9000")
    assert score.global_gini == ZERO
    assert score.fairness_score == Decimal("1.0000")
    assert score.non_loss_company_ratio == Decimal("1.0000")
    assert score.loss_making_company_count == 0
    assert score.loss_making_company_rate == ZERO
    assert score.bankrupt_company_count == 0
    assert score.final_score == Decimal("90.0000")


def test_evaluator_matches_the_oracle_to_the_settled_prefix() -> None:
    oracle = _FixedOracle()
    scenario, initial, final, snapshots, events = _facts((Decimal("0"),) * 9)
    scenario = ScenarioSpec.model_validate_json(
        scenario.model_copy(update={"weeks": 2}).model_dump_json()
    )
    initial = initial.model_copy(update={"scenario": scenario})
    final = final.model_copy(update={"scenario": scenario})

    Evaluator(oracle=oracle).evaluate(
        scenario,
        initial,
        final,
        snapshots,
        events,
    )

    assert oracle.horizons == [1]


def test_zero_surplus_is_non_loss_while_only_negative_surplus_enters_l() -> None:
    score = _evaluate(
        (
            Decimal("10"),
            Decimal("0"),
            Decimal("0"),
            Decimal("-1"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
        )
    )

    assert score.loss_making_company_count == 1
    assert score.loss_making_company_rate == Decimal("0.1111")
    assert score.non_loss_company_ratio == Decimal("0.8889")


def test_bankruptcy_is_a_count_and_requires_strictly_sub_one_assets() -> None:
    surpluses = (Decimal("-999.5"), *(Decimal("0") for _ in range(8)))
    score = _evaluate(tuple(surpluses), oracle=Decimal("100"), bankrupt_index=0)

    assert score.bankrupt_company_count == 1
    assert score.loss_making_company_count == 1


def test_evaluator_fails_closed_when_realized_surplus_exceeds_oracle() -> None:
    with pytest.raises(ValueError, match="exceeds the Oracle"):
        _evaluate((Decimal("20"),) * 9, oracle=Decimal("100"))


def test_evaluator_recomputes_shared_consumer_allocation() -> None:
    scenario, initial, final, snapshots, events = _facts((Decimal("0"),) * 9)
    first = next(event for event in events if isinstance(event, ConsumerSaleEvent))
    tampered = first.model_copy(
        update={
            "saleable_quantity": Decimal("1"),
        }
    )
    changed = tuple(tampered if event is first else event for event in events)

    with pytest.raises(ValueError, match="shared-market rules"):
        Evaluator(oracle=_FixedOracle()).evaluate(
            scenario,
            initial,
            final,
            snapshots,
            changed,
        )


def test_evaluator_rejects_an_incomplete_consumer_sale_grid() -> None:
    scenario, initial, final, snapshots, events = _facts((Decimal("0"),) * 9)

    with pytest.raises(ValueError, match="each week and retailer once"):
        Evaluator(oracle=_FixedOracle()).evaluate(
            scenario,
            initial,
            final,
            snapshots,
            events[:-1],
        )


def test_oracle_uses_realized_wtp_and_retail_operating_costs() -> None:
    scenario = _scenario()
    controlled_market = scenario.consumer_market.model_copy(
        update={"purchasing_power_shifts": (ZERO,)}
    )
    scenario = ScenarioSpec.model_validate_json(
        scenario.model_copy(update={"consumer_market": controlled_market}).model_dump_json()
    )
    higher_wtp = controlled_market.model_copy(
        update={
            "regime_willingness_shifts": tuple(
                shift.model_copy(update={"shift": Decimal("0.2")})
                if shift.regime is MarketRegime.NORMAL
                else shift
                for shift in controlled_market.regime_willingness_shifts
            )
        }
    )
    expensive_retailers = tuple(
        company.model_copy(
            update={
                "operation": company.operation.model_copy(
                    update={"weekly_operating_cost": Decimal("10")}
                )
            }
        )
        if isinstance(company.operation, RetailerOperation)
        else company
        for company in scenario.companies
    )
    oracle = EnterpriseOracle()
    base = oracle.evaluate(scenario, 42).enterprise_surplus_upper_bound
    with_higher_wtp = oracle.evaluate(
        ScenarioSpec.model_validate_json(
            scenario.model_copy(update={"consumer_market": higher_wtp}).model_dump_json()
        ),
        42,
    ).enterprise_surplus_upper_bound
    with_higher_cost = oracle.evaluate(
        ScenarioSpec.model_validate_json(
            scenario.model_copy(update={"companies": expensive_retailers}).model_dump_json()
        ),
        42,
    ).enterprise_surplus_upper_bound

    assert with_higher_wtp > base
    assert base - with_higher_cost == pytest.approx(Decimal("15"), abs=Decimal("0.01"))
