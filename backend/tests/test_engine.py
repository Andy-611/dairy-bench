import asyncio
from collections.abc import Callable
from decimal import Decimal

import pytest
from pydantic import ValidationError

from company_bench.dairy_scenario import DAIRY_V1_SCENARIO
from company_bench.engine import EconomyEngine
from company_bench.models import (
    CompanyDecision,
    CompanyState,
    ConsumerSaleEvent,
    DecisionRejectedEvent,
    FarmDecision,
    InventoryExpiredEvent,
    MilkProcessedEvent,
    MilkProducedEvent,
    NoOpDecision,
    ProcessorDecision,
    ProductId,
    RecordedDecision,
    RetailerDecision,
    TradeExecutedEvent,
    WorldState,
)
from company_bench.policies import BaselinePolicy
from company_bench.scoring import Evaluator


def _record(
    engine: EconomyEngine,
    state: WorldState,
    choose: Callable[[str], CompanyDecision],
) -> tuple[RecordedDecision, ...]:
    """Bind custom decisions to the current morning observations."""
    return tuple(
        RecordedDecision(
            observation_id=observation.observation_id,
            day=observation.day,
            company_id=observation.company_id,
            decision=choose(observation.company_id),
        )
        for observation in engine.observe(state)
    )


def _baseline_records(
    engine: EconomyEngine,
    state: WorldState,
) -> tuple[RecordedDecision, ...]:
    """Run one BaselinePolicy concurrently for all observations."""
    observations = engine.observe(state)

    async def decide_all() -> tuple[CompanyDecision, ...]:
        policy = BaselinePolicy()
        return tuple(
            await asyncio.gather(
                *(policy.decide(observation) for observation in observations)
            )
        )

    decisions = asyncio.run(decide_all())
    return tuple(
        RecordedDecision(
            observation_id=observation.observation_id,
            day=observation.day,
            company_id=observation.company_id,
            decision=decision,
        )
        for observation, decision in zip(
            observations,
            decisions,
            strict=True,
        )
    )


def _no_op(_: str) -> CompanyDecision:
    """Return a valid no-op for any operation."""
    return NoOpDecision()


def test_scenario_is_strongly_typed_frozen_and_six_company() -> None:
    scenario = DAIRY_V1_SCENARIO

    assert scenario.days == 30
    assert len(scenario.companies) == 6
    assert {company.tier.value for company in scenario.companies} == {
        "farm",
        "processor",
        "retailer",
    }
    with pytest.raises(ValidationError):
        scenario.model_copy(update={"unexpected": "field"}).model_validate(
            {
                **scenario.model_dump(),
                "unexpected": "field",
            }
        )
    with pytest.raises(ValidationError):
        FarmDecision(
            produce_quantity=Decimal("-1"),
            raw_offer_quantity=Decimal("1"),
            minimum_raw_price=Decimal("1"),
        )


@pytest.mark.parametrize("seed", [-1, True, 2_147_483_648])
def test_engine_rejects_invalid_seed(seed: int) -> None:
    with pytest.raises(ValueError, match="seed must be"):
        EconomyEngine().initial_state(DAIRY_V1_SCENARIO, seed)


def test_baseline_settles_the_complete_chain_without_negative_balances() -> None:
    engine = EconomyEngine()
    state = engine.initial_state(DAIRY_V1_SCENARIO, seed=42)

    result = engine.step(state, _baseline_records(engine, state))

    assert result.state.day == 1
    assert result.snapshot.markets[0].volume == Decimal("100")
    assert result.snapshot.markets[1].volume == Decimal("80.0")
    assert all(company.cash >= 0 for company in result.state.companies)
    assert all(
        lot.quantity > 0
        for company in result.state.companies
        for lot in company.inventory
    )
    assert sum(
        isinstance(event, MilkProducedEvent) for event in result.events
    ) == 2
    assert sum(
        isinstance(event, MilkProcessedEvent) for event in result.events
    ) == 2
    assert sum(
        isinstance(event, TradeExecutedEvent) for event in result.events
    ) == 4
    assert sum(
        isinstance(event, ConsumerSaleEvent) for event in result.events
    ) == 2


def test_market_caps_purchase_by_cash() -> None:
    engine = EconomyEngine()
    state = engine.initial_state(DAIRY_V1_SCENARIO, seed=7)

    def decide(company_id: str) -> CompanyDecision:
        if company_id == "farm_a":
            return FarmDecision(
                produce_quantity=Decimal("50"),
                raw_offer_quantity=Decimal("50"),
                minimum_raw_price=Decimal("100"),
            )
        if company_id == "processor_a":
            return ProcessorDecision(
                raw_bid_quantity=Decimal("50"),
                maximum_raw_price=Decimal("100"),
                process_quantity=Decimal("0"),
                bottled_offer_quantity=Decimal("0"),
                minimum_bottled_price=Decimal("1"),
            )
        return NoOpDecision()

    result = engine.step(state, _record(engine, state, decide))
    raw_trades = [
        event
        for event in result.events
        if isinstance(event, TradeExecutedEvent)
        and event.product == ProductId.RAW_MILK
    ]
    processor = next(
        company
        for company in result.state.companies
        if company.company_id == "processor_a"
    )

    assert len(raw_trades) == 1
    assert raw_trades[0].quantity == Decimal("10")
    assert processor.cash == Decimal("0")
    assert sum(
        (
            lot.quantity
            for lot in processor.inventory
            if lot.product == ProductId.RAW_MILK
        ),
        start=Decimal("0"),
    ) == Decimal("10")


def test_fractional_affordability_is_rounded_down() -> None:
    engine = EconomyEngine()
    initial = engine.initial_state(DAIRY_V1_SCENARIO, seed=7)
    state = initial.model_copy(
        update={
            "companies": tuple(
                CompanyState(
                    company_id=company.company_id,
                    cash=(
                        Decimal("20")
                        if company.company_id == "processor_a"
                        else company.cash
                    ),
                    inventory=company.inventory,
                )
                for company in initial.companies
            )
        }
    )

    def decide(company_id: str) -> CompanyDecision:
        if company_id == "farm_a":
            return FarmDecision(
                produce_quantity=Decimal("50"),
                raw_offer_quantity=Decimal("50"),
                minimum_raw_price=Decimal("1.4"),
            )
        if company_id == "processor_a":
            return ProcessorDecision(
                raw_bid_quantity=Decimal("50"),
                maximum_raw_price=Decimal("1.4"),
                process_quantity=Decimal("0"),
                bottled_offer_quantity=Decimal("0"),
                minimum_bottled_price=Decimal("1"),
            )
        return NoOpDecision()

    result = engine.step(state, _record(engine, state, decide))
    trade = next(
        event
        for event in result.events
        if isinstance(event, TradeExecutedEvent)
    )
    processor = next(
        company
        for company in result.state.companies
        if company.company_id == "processor_a"
    )

    assert trade.quantity == Decimal("14.2857")
    assert processor.cash == Decimal("0.00002")


def test_fefo_moves_oldest_lot_then_expiration_removes_it() -> None:
    engine = EconomyEngine()
    state = engine.initial_state(DAIRY_V1_SCENARIO, seed=9)

    def day_one(company_id: str) -> CompanyDecision:
        if company_id == "farm_a":
            return FarmDecision(
                produce_quantity=Decimal("10"),
                raw_offer_quantity=Decimal("0"),
                minimum_raw_price=Decimal("1"),
            )
        return NoOpDecision()

    first = engine.step(state, _record(engine, state, day_one))

    def day_two(company_id: str) -> CompanyDecision:
        if company_id == "farm_a":
            return FarmDecision(
                produce_quantity=Decimal("10"),
                raw_offer_quantity=Decimal("10"),
                minimum_raw_price=Decimal("1"),
            )
        if company_id == "processor_a":
            return ProcessorDecision(
                raw_bid_quantity=Decimal("10"),
                maximum_raw_price=Decimal("1"),
                process_quantity=Decimal("0"),
                bottled_offer_quantity=Decimal("0"),
                minimum_bottled_price=Decimal("1"),
            )
        return NoOpDecision()

    second = engine.step(
        first.state,
        _record(engine, first.state, day_two),
    )
    farm = next(
        company
        for company in second.state.companies
        if company.company_id == "farm_a"
    )
    expired = [
        event
        for event in second.events
        if isinstance(event, InventoryExpiredEvent)
    ]

    assert len(farm.inventory) == 1
    assert farm.inventory[0].produced_day == 2
    assert farm.inventory[0].quantity == Decimal("10")
    assert len(expired) == 1
    assert expired[0].company_id == "processor_a"
    assert expired[0].quantity == Decimal("10")


def test_wrong_decision_type_is_rejected_without_side_effects() -> None:
    engine = EconomyEngine()
    state = engine.initial_state(DAIRY_V1_SCENARIO, seed=4)

    def decide(company_id: str) -> CompanyDecision:
        if company_id == "farm_a":
            return RetailerDecision(
                bottled_bid_quantity=Decimal("10"),
                maximum_bottled_price=Decimal("3"),
                retail_price=Decimal("4"),
            )
        return NoOpDecision()

    result = engine.step(state, _record(engine, state, decide))

    assert any(
        isinstance(event, DecisionRejectedEvent)
        and event.company_id == "farm_a"
        for event in result.events
    )
    farm = next(
        company
        for company in result.state.companies
        if company.company_id == "farm_a"
    )
    assert farm.cash == Decimal("1000")
    assert farm.inventory == ()


def test_same_seed_and_decisions_are_exactly_reproducible() -> None:
    engine = EconomyEngine()
    left = engine.initial_state(DAIRY_V1_SCENARIO, seed=42)
    right = engine.initial_state(DAIRY_V1_SCENARIO, seed=42)

    left_result = engine.step(left, _baseline_records(engine, left))
    right_result = engine.step(right, _baseline_records(engine, right))

    assert left_result == right_result


def test_observation_hides_seed_and_exposes_public_rules() -> None:
    engine = EconomyEngine()
    state = engine.initial_state(DAIRY_V1_SCENARIO, seed=314159)

    observation = engine.observe(state)[0]

    assert observation.observation_id == (
        "flow.dairy.base.s6.v1|1|farm_a"
    )
    assert observation.products == DAIRY_V1_SCENARIO.products
    assert observation.demand == DAIRY_V1_SCENARIO.demand
    assert observation.scoring == DAIRY_V1_SCENARIO.scoring


def test_equal_price_priority_rotates_between_companies() -> None:
    engine = EconomyEngine()
    state = engine.initial_state(DAIRY_V1_SCENARIO, seed=1)

    def decide(company_id: str) -> CompanyDecision:
        if company_id == "farm_a":
            return FarmDecision(
                produce_quantity=Decimal("10"),
                raw_offer_quantity=Decimal("10"),
                minimum_raw_price=Decimal("1"),
            )
        if company_id.startswith("processor_"):
            return ProcessorDecision(
                raw_bid_quantity=Decimal("10"),
                maximum_raw_price=Decimal("1"),
                process_quantity=Decimal("0"),
                bottled_offer_quantity=Decimal("0"),
                minimum_bottled_price=Decimal("1"),
            )
        return NoOpDecision()

    first = engine.step(state, _record(engine, state, decide))
    second = engine.step(
        first.state,
        _record(engine, first.state, decide),
    )
    buyers = [
        next(
            event.buyer_id
            for event in result.events
            if isinstance(event, TradeExecutedEvent)
            and event.product == ProductId.RAW_MILK
        )
        for result in (first, second)
    ]

    assert buyers == ["processor_a", "processor_b"]


def test_thirty_day_baseline_can_be_scored_read_only() -> None:
    engine = EconomyEngine()
    evaluator = Evaluator()
    initial = engine.initial_state(DAIRY_V1_SCENARIO, seed=42)
    state = initial
    snapshots = []
    events = []

    for _ in range(DAIRY_V1_SCENARIO.days):
        result = engine.step(state, _baseline_records(engine, state))
        state = result.state
        snapshots.append(result.snapshot)
        events.extend(result.events)

    state_before_scoring = state.model_dump_json()
    score = evaluator.evaluate(
        DAIRY_V1_SCENARIO,
        initial,
        state,
        tuple(snapshots),
        tuple(events),
    )

    assert state.day == 30
    assert state.model_dump_json() == state_before_scoring
    assert score.efficiency == sum(
        (company.surplus for company in score.companies),
        start=Decimal("0"),
    )
    assert score.fairness == Decimal("1") - score.gini
    assert Decimal("0") <= score.consumer_fill_rate <= Decimal("1")
    assert not hasattr(score, "final_score")


def test_no_op_retailers_have_zero_fill_rate() -> None:
    engine = EconomyEngine()
    evaluator = Evaluator()
    initial = engine.initial_state(DAIRY_V1_SCENARIO, seed=42)
    state = initial
    snapshots = []
    events = []

    for _ in range(DAIRY_V1_SCENARIO.days):
        result = engine.step(state, _record(engine, state, _no_op))
        state = result.state
        snapshots.append(result.snapshot)
        events.extend(result.events)

    score = evaluator.evaluate(
        DAIRY_V1_SCENARIO,
        initial,
        state,
        tuple(snapshots),
        tuple(events),
    )

    assert score.consumer_fill_rate == 0
    assert sum(
        isinstance(event, ConsumerSaleEvent)
        for event in events
    ) == 2 * DAIRY_V1_SCENARIO.days


def test_evaluator_rejects_an_incomplete_episode() -> None:
    engine = EconomyEngine()
    initial = engine.initial_state(DAIRY_V1_SCENARIO, seed=42)

    with pytest.raises(ValueError, match="complete episode"):
        Evaluator().evaluate(
            DAIRY_V1_SCENARIO,
            initial,
            initial,
            (),
            (),
        )
