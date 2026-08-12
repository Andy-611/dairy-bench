from decimal import Decimal

import pytest

from company_bench.domain.calendar import SimDay, Weekday
from company_bench.domain.models import (
    ZERO,
    CompanyBankruptEvent,
    CompanySnapshot,
    CompanyStatus,
    ConsumerSaleEvent,
    DomainEvent,
    RetailerOperation,
    ScenarioSpec,
    ScoreCard,
    WeekSnapshot,
    WorldState,
)
from company_bench.domain.scenario import DAIRY_S9_SCENARIO
from company_bench.economy.engine import EconomyEngine
from company_bench.economy.scoring import Evaluator


def _scenario(weeks: int = 1) -> ScenarioSpec:
    return DAIRY_S9_SCENARIO.model_copy(
        update={
            "weeks": weeks,
            "demand": DAIRY_S9_SCENARIO.demand.model_copy(
                update={"shock_min": 0, "shock_max": 0}
            ),
        }
    )


def _state(
    scenario: ScenarioSpec,
    week: int,
    cash: dict[str, Decimal] | None = None,
    bankrupt: frozenset[str] = frozenset(),
) -> WorldState:
    initial = EconomyEngine().initial_state(scenario, seed=42)
    if week == 0:
        return initial
    balances = cash or {}
    return initial.model_copy(
        update={
            "completed_weeks": week,
            "companies": tuple(
                company.model_copy(
                    update={
                        "cash": balances.get(
                            company.company_id,
                            scenario.company(company.company_id).initial_cash,
                        ),
                        "status": (
                            CompanyStatus.BANKRUPT
                            if company.company_id in bankrupt
                            else CompanyStatus.ACTIVE
                        ),
                        "bankrupt_on": (
                            SimDay.at(week=1)
                            if company.company_id in bankrupt
                            else None
                        ),
                    }
                )
                for company in initial.companies
            ),
        }
    )


def _snapshot(
    scenario: ScenarioSpec,
    week: int,
    cash: dict[str, Decimal] | None = None,
    *,
    potential_demand: Decimal = Decimal("40"),
    bankrupt: frozenset[str] = frozenset(),
) -> WeekSnapshot:
    balances = cash or {}
    companies = tuple(
        _company_snapshot(
            scenario,
            week,
            company.company_id,
            balances.get(company.company_id, company.initial_cash),
            (
                CompanyStatus.BANKRUPT
                if company.company_id in bankrupt
                else CompanyStatus.ACTIVE
            ),
        )
        for company in scenario.companies
    )
    retailer_count = sum(
        isinstance(company.operation, RetailerOperation) for company in scenario.companies
    )
    return WeekSnapshot(
        week=week,
        companies=companies,
        markets=(),
        consumer_demand=potential_demand * retailer_count,
        consumer_sales=ZERO,
        expired_quantity=ZERO,
    )


def _company_snapshot(
    scenario: ScenarioSpec,
    week: int,
    company_id: str,
    cash: Decimal,
    status: CompanyStatus,
) -> CompanySnapshot:
    company = scenario.company(company_id)
    return CompanySnapshot(
        week=week,
        company_id=company_id,
        tier=company.tier,
        status=status,
        cash=cash,
        raw_milk_quantity=ZERO,
        bottled_milk_quantity=ZERO,
        inventory_value=ZERO,
        net_worth=cash,
        surplus=cash - company.initial_cash,
        weekly_consumer_sales=ZERO,
        weekly_expired_quantity=ZERO,
    )


def _consumer_events(
    scenario: ScenarioSpec,
    *,
    potential_demand: Decimal = Decimal("40"),
) -> tuple[DomainEvent, ...]:
    return tuple(
        ConsumerSaleEvent(
            occurred_on=SimDay.at(week=week, weekday=Weekday.SUNDAY),
            company_id=company.company_id,
            potential_demand_quantity=potential_demand,
            demand_quantity=potential_demand,
            sold_quantity=ZERO,
            retail_price=None,
            revenue=ZERO,
        )
        for week in range(1, scenario.weeks + 1)
        for company in scenario.companies
        if isinstance(company.operation, RetailerOperation)
    )


def _evaluate(
    scenario: ScenarioSpec,
    final_cash: dict[str, Decimal],
    snapshots: tuple[WeekSnapshot, ...],
    events: tuple[DomainEvent, ...] | None = None,
    bankrupt: frozenset[str] = frozenset(),
) -> ScoreCard:
    return Evaluator().evaluate(
        scenario,
        _state(scenario, 0),
        _state(scenario, scenario.weeks, final_cash, bankrupt),
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
        "global_gini",
        "fairness_score",
        "profit_participation_score",
        "bankrupt_company_count",
        "loss_making_company_count",
        "companies",
    }


def test_evaluator_combines_efficiency_global_fairness_and_profit_participation() -> None:
    scenario = _scenario()
    expected_reference = Decimal("273.375")
    per_company_gain = expected_reference / Decimal("18")
    final_cash = {
        company.company_id: company.initial_cash + per_company_gain
        for company in scenario.companies
    }

    score = _evaluate(scenario, final_cash, (_snapshot(scenario, 1, final_cash),))

    assert score.score_version == "s9-enterprise-v5"
    assert score.efficiency_raw == expected_reference / Decimal("2")
    assert score.efficiency_reference == expected_reference
    assert score.efficiency_score == Decimal("0.5")
    assert score.global_gini == ZERO
    assert score.fairness_score == Decimal("1")
    assert score.profit_participation_score == Decimal("1")
    assert score.bankrupt_company_count == 0
    assert score.loss_making_company_count == 0
    assert score.final_score == Decimal("50.0")


def test_global_gini_uses_all_nine_companies_final_assets() -> None:
    scenario = _scenario()
    surviving_id = scenario.companies[-1].company_id
    bankrupt = frozenset(
        company.company_id
        for company in scenario.companies
        if company.company_id != surviving_id
    )
    final_cash = {company_id: ZERO for company_id in bankrupt}
    final_cash[surviving_id] = Decimal("9000")
    bankruptcies = tuple(
        CompanyBankruptEvent(
            occurred_on=SimDay.at(week=1),
            company_id=company_id,
            total_assets=ZERO,
        )
        for company_id in bankrupt
    )

    score = _evaluate(
        scenario,
        final_cash,
        (_snapshot(scenario, 1, final_cash, bankrupt=bankrupt),),
        (*_consumer_events(scenario), *bankruptcies),
        bankrupt,
    )

    assert score.efficiency_raw == ZERO
    assert score.global_gini == Decimal("0.8889")
    assert score.fairness_score == ZERO
    assert score.profit_participation_score == Decimal("0.1111")
    assert score.bankrupt_company_count == 8
    assert score.loss_making_company_count == 8
    assert score.final_score == ZERO


def test_profit_participation_counts_only_negative_final_surplus() -> None:
    scenario = _scenario()
    final_cash = {
        company.company_id: Decimal("900" if index < 6 else "1400")
        for index, company in enumerate(scenario.companies)
    }

    score = _evaluate(scenario, final_cash, (_snapshot(scenario, 1, final_cash),))

    assert score.efficiency_score == Decimal("1")
    assert score.loss_making_company_count == 6
    assert score.profit_participation_score == Decimal("0.3333")
    assert score.final_score == Decimal("54.2467")


def test_bankruptcy_count_comes_from_irreversible_exit_events() -> None:
    scenario = _scenario(weeks=2)
    final_cash = {
        company.company_id: company.initial_cash
        for company in scenario.companies
    }
    bankrupt = frozenset({"processor_a"})
    week_one_cash = dict(final_cash)
    week_one_cash["processor_a"] = Decimal("0.9999")
    bankruptcy = CompanyBankruptEvent(
        occurred_on=SimDay.at(week=1),
        company_id="processor_a",
        total_assets=Decimal("0.9999"),
    )

    score = _evaluate(
        scenario,
        final_cash,
        (
            _snapshot(scenario, 1, week_one_cash, bankrupt=bankrupt),
            _snapshot(scenario, 2, final_cash, bankrupt=bankrupt),
        ),
        (*_consumer_events(scenario), bankruptcy),
        bankrupt,
    )

    assert score.bankrupt_company_count == 1
    assert score.loss_making_company_count == 0


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

    with pytest.raises(ValueError, match="every week and retailer exactly once"):
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
