from decimal import Decimal
from itertools import pairwise

from company_bench.domain.models import MarketRegime
from company_bench.domain.scenario import DAIRY_S9_SCENARIO
from company_bench.economy.consumer import RetailOffer, SharedConsumerMarket


def _market() -> SharedConsumerMarket:
    return SharedConsumerMarket(DAIRY_S9_SCENARIO.consumer_market)


def test_hidden_cycle_starts_normal_and_alternates_normal_with_extremes() -> None:
    market = _market()
    regimes = tuple(market.regime(42, week) for week in range(1, 53))
    runs: list[tuple[MarketRegime, int]] = []
    for regime in regimes:
        if runs and runs[-1][0] is regime:
            previous, duration = runs[-1]
            runs[-1] = previous, duration + 1
        else:
            runs.append((regime, 1))

    assert runs[0][0] is MarketRegime.NORMAL
    assert all(6 <= duration <= 10 for _, duration in runs[:-1])
    assert all(regime is MarketRegime.NORMAL for regime, _ in runs[::2])
    extremes = tuple(regime for regime, _ in runs[1::2])
    assert all(left is not right for left, right in pairwise(extremes))


def test_regimes_change_consumer_composition_not_only_total_scale() -> None:
    market = _market()
    normal = market.population(42, 1)
    slump = next(
        market.population(42, week)
        for week in range(1, 53)
        if market.regime(42, week) is MarketRegime.SLUMP
    )
    boom = next(
        market.population(42, week)
        for week in range(1, 53)
        if market.regime(42, week) is MarketRegime.BOOM
    )

    assert tuple(cohort.quantity for cohort in normal.cohorts) == (
        Decimal("40.0000"),
        Decimal("50.0000"),
        Decimal("30.0000"),
    )
    assert tuple(cohort.quantity for cohort in slump.cohorts) == (
        Decimal("32.0000"),
        Decimal("25.0000"),
        Decimal("9.0000"),
    )
    assert tuple(cohort.quantity for cohort in boom.cohorts) == (
        Decimal("44.0000"),
        Decimal("75.0000"),
        Decimal("60.0000"),
    )


def test_willingness_to_pay_combines_runwide_and_regime_hidden_shifts() -> None:
    market = _market()
    spec = DAIRY_S9_SCENARIO.consumer_market
    seed = 42
    purchasing_power = market.purchasing_power_shift(seed)

    for regime in MarketRegime:
        week = next(week for week in range(1, 53) if market.regime(seed, week) is regime)
        population = market.population(seed, week)
        expected_shift = purchasing_power + spec.regime_willingness_shift(regime)
        assert (
            tuple(
                realized.maximum_willingness_to_pay - base.maximum_willingness_to_pay
                for realized, base in zip(population.cohorts, spec.cohorts, strict=True)
            )
            == (expected_shift,) * 3
        )

    assert {market.purchasing_power_shift(candidate) for candidate in range(100)} == set(
        spec.purchasing_power_shifts
    )


def test_agents_receive_market_structure_but_not_hidden_demand_parameters() -> None:
    rules = DAIRY_S9_SCENARIO.consumer_market.public_rules()
    public = rules.model_dump()

    assert rules.willingness_to_pay_may_change_with_purchasing_power_and_regime
    assert "cohorts" not in public
    assert "purchasing_power_shifts" not in public
    assert "regime_willingness_shifts" not in public


def test_shared_market_is_cheapest_first_with_stockout_spillover() -> None:
    settlement = _market().settle(
        42,
        1,
        (
            RetailOffer(
                company_id="retailer_a",
                unit_price=Decimal("2.9000"),
                available_quantity=Decimal("10"),
            ),
            RetailOffer(
                company_id="retailer_b",
                unit_price=Decimal("3.5000"),
                available_quantity=Decimal("20"),
            ),
            RetailOffer(
                company_id="retailer_c",
                unit_price=Decimal("4.5000"),
                available_quantity=Decimal("100"),
            ),
        ),
    )

    assert {sale.company_id: sale.sold_quantity for sale in settlement.sales} == {
        "retailer_a": Decimal("10.0000"),
        "retailer_b": Decimal("20.0000"),
        "retailer_c": Decimal("30.0000"),
    }


def test_equal_price_allocation_is_balanced_and_input_order_independent() -> None:
    offers = tuple(
        RetailOffer(
            company_id=f"retailer_{suffix}",
            unit_price=Decimal("3.5000"),
            available_quantity=Decimal("100"),
        )
        for suffix in ("a", "b", "c")
    )
    forward = _market().settle(42, 1, offers)
    reverse = _market().settle(42, 1, tuple(reversed(offers)))
    forward_sales = {sale.company_id: sale.sold_quantity for sale in forward.sales}

    assert forward == reverse
    assert sum(forward_sales.values()) == Decimal("80.0000")
    assert max(forward_sales.values()) - min(forward_sales.values()) <= Decimal("0.0001")
