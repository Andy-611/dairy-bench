from decimal import Decimal

import pytest

from company_bench.dairy_scenario import DAIRY_S9_SCENARIO
from company_bench.engine import EconomyEngine
from company_bench.models import (
    ZERO,
    CompanySnapshot,
    ConsumerSaleEvent,
    DaySnapshot,
    DomainEvent,
    RetailerOperation,
    RunSummary,
    ScenarioSpec,
    ScoreCard,
    WorldState,
)
from company_bench.scoring import Evaluator


def _scenario(days: int = 1) -> ScenarioSpec:
    return DAIRY_S9_SCENARIO.model_copy(
        update={
            "days": days,
            "demand": DAIRY_S9_SCENARIO.demand.model_copy(
                update={"shock_min": 0, "shock_max": 0}
            ),
        }
    )


def _state(
    scenario: ScenarioSpec,
    day: int,
    cash: dict[str, Decimal] | None = None,
) -> WorldState:
    initial = EconomyEngine().initial_state(scenario, seed=42)
    if day == 0:
        return initial
    balances = cash or {}
    return initial.model_copy(
        update={
            "day": day,
            "companies": tuple(
                company.model_copy(
                    update={
                        "cash": balances.get(
                            company.company_id,
                            scenario.company(company.company_id).initial_cash,
                        )
                    }
                )
                for company in initial.companies
            ),
        }
    )


def _snapshot(
    scenario: ScenarioSpec,
    day: int,
    cash: dict[str, Decimal] | None = None,
    *,
    potential_demand: Decimal = Decimal("40"),
) -> DaySnapshot:
    balances = cash or {}
    companies = tuple(
        _company_snapshot(
            scenario,
            day,
            company.company_id,
            balances.get(company.company_id, company.initial_cash),
        )
        for company in scenario.companies
    )
    retailer_count = sum(
        isinstance(company.operation, RetailerOperation) for company in scenario.companies
    )
    return DaySnapshot(
        day=day,
        companies=companies,
        markets=(),
        consumer_demand=potential_demand * retailer_count,
        consumer_sales=ZERO,
        expired_quantity=ZERO,
    )


def _company_snapshot(
    scenario: ScenarioSpec,
    day: int,
    company_id: str,
    cash: Decimal,
) -> CompanySnapshot:
    company = scenario.company(company_id)
    return CompanySnapshot(
        day=day,
        company_id=company_id,
        tier=company.tier,
        cash=cash,
        raw_milk_quantity=ZERO,
        bottled_milk_quantity=ZERO,
        inventory_value=ZERO,
        net_worth=cash,
        surplus=cash - company.initial_cash,
        daily_consumer_sales=ZERO,
        daily_expired_quantity=ZERO,
    )


def _consumer_events(
    scenario: ScenarioSpec,
    *,
    potential_demand: Decimal = Decimal("40"),
) -> tuple[DomainEvent, ...]:
    return tuple(
        ConsumerSaleEvent(
            day=day,
            company_id=company.company_id,
            potential_demand_quantity=potential_demand,
            demand_quantity=potential_demand,
            sold_quantity=ZERO,
            retail_price=None,
            revenue=ZERO,
        )
        for day in range(1, scenario.days + 1)
        for company in scenario.companies
        if isinstance(company.operation, RetailerOperation)
    )


def _evaluate(
    scenario: ScenarioSpec,
    final_cash: dict[str, Decimal],
    snapshots: tuple[DaySnapshot, ...],
    events: tuple[DomainEvent, ...] | None = None,
) -> ScoreCard:
    return Evaluator().evaluate(
        scenario,
        _state(scenario, 0),
        _state(scenario, scenario.days, final_cash),
        snapshots,
        _consumer_events(scenario) if events is None else events,
    )


def test_score_card_exposes_only_the_official_s9_contract() -> None:
    assert set(ScoreCard.model_fields) == {
        "score_version",
        "final_score",
        "efficiency_raw",
        "efficiency_reference",
        "efficiency_score",
        "farm_gini",
        "processor_gini",
        "retailer_gini",
        "fairness_score",
        "bankrupt_company_count",
        "bankruptcy_rate",
        "companies",
    }

    assert set(RunSummary.model_fields) == {
        "run_id",
        "scenario_id",
        "seed",
        "started_at",
        "finished_at",
        "score_version",
        "final_score",
    }


def test_evaluator_combines_continuous_efficiency_fairness_and_survival() -> None:
    scenario = _scenario()
    expected_reference = Decimal("273.375")
    per_company_gain = expected_reference / Decimal("18")
    final_cash = {
        company.company_id: company.initial_cash + per_company_gain
        for company in scenario.companies
    }

    score = _evaluate(scenario, final_cash, (_snapshot(scenario, 1, final_cash),))

    assert score.score_version == "s9-enterprise-v2"
    assert score.efficiency_raw == expected_reference / Decimal("2")
    assert score.efficiency_reference == expected_reference
    assert score.efficiency_score == Decimal("0.5")
    assert score.farm_gini == score.processor_gini == score.retailer_gini == ZERO
    assert score.fairness_score == Decimal("1")
    assert score.bankrupt_company_count == 0
    assert score.bankruptcy_rate == ZERO
    assert score.final_score == Decimal("50.0")


def test_three_company_tier_gini_uses_two_thirds_maximum() -> None:
    scenario = _scenario()
    final_cash = {company.company_id: company.initial_cash for company in scenario.companies}
    final_cash.update(
        {
            "farm_a": ZERO,
            "farm_b": ZERO,
            "farm_c": Decimal("3000"),
        }
    )

    score = _evaluate(scenario, final_cash, (_snapshot(scenario, 1, final_cash),))

    assert score.efficiency_raw == ZERO
    assert score.farm_gini == Decimal("0.6667")
    assert score.processor_gini == score.retailer_gini == ZERO
    assert score.fairness_score == Decimal("0.6667")
    assert score.bankrupt_company_count == 2
    assert score.bankruptcy_rate == Decimal("0.2222")
    assert score.final_score == ZERO


def test_bankruptcy_counts_any_zero_net_worth_day_even_after_recovery() -> None:
    scenario = _scenario(days=2)
    expected_reference = Decimal("546.750")
    per_company_gain = expected_reference / Decimal("18")
    final_cash = {
        company.company_id: company.initial_cash + per_company_gain
        for company in scenario.companies
    }
    day_one_cash = {company.company_id: company.initial_cash for company in scenario.companies}
    day_one_cash["farm_a"] = ZERO

    score = _evaluate(
        scenario,
        final_cash,
        (
            _snapshot(scenario, 1, day_one_cash),
            _snapshot(scenario, 2, final_cash),
        ),
    )

    assert score.efficiency_reference == expected_reference
    assert score.efficiency_score == Decimal("0.5")
    assert score.fairness_score == Decimal("1")
    assert score.bankrupt_company_count == 1
    assert score.bankruptcy_rate == Decimal("0.1111")
    expected = Decimal("50") * (Decimal("8") / Decimal("9")).sqrt()
    assert score.final_score == expected.quantize(Decimal("0.0001"))


def test_evaluator_rejects_an_incomplete_company_snapshot() -> None:
    scenario = _scenario()
    final_cash = {company.company_id: company.initial_cash for company in scenario.companies}
    snapshot = _snapshot(scenario, 1, final_cash)
    incomplete = snapshot.model_copy(update={"companies": snapshot.companies[:-1]})

    with pytest.raises(ValueError, match="each scenario company exactly once"):
        _evaluate(scenario, final_cash, (incomplete,))


def test_evaluator_rejects_an_incomplete_consumer_sale_grid() -> None:
    scenario = _scenario()
    final_cash = {company.company_id: company.initial_cash for company in scenario.companies}
    events = _consumer_events(scenario)

    with pytest.raises(ValueError, match="every day and retailer exactly once"):
        _evaluate(
            scenario,
            final_cash,
            (_snapshot(scenario, 1, final_cash),),
            events[:-1],
        )


def test_evaluator_rejects_consumer_potential_that_does_not_match_seed() -> None:
    scenario = _scenario()
    final_cash = {company.company_id: company.initial_cash for company in scenario.companies}
    events = _consumer_events(scenario)
    first = events[0]
    assert isinstance(first, ConsumerSaleEvent)
    tampered = first.model_copy(
        update={
            "potential_demand_quantity": Decimal("41"),
            "demand_quantity": Decimal("41"),
        }
    )

    with pytest.raises(ValueError, match="does not match the episode seed"):
        _evaluate(
            scenario,
            final_cash,
            (_snapshot(scenario, 1, final_cash),),
            (tampered, *events[1:]),
        )
