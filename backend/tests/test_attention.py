from decimal import Decimal

import pytest
from pydantic import ValidationError

from company_bench.domain.models import CompanyObservation, ProductId, RuntimeSpec
from company_bench.runtime.attention import AgentAttention, ArmedWait, AttentionRejected
from company_bench.runtime.models import (
    AgentTurn,
    OrderBookView,
    PriceLevelView,
    QuoteAlert,
    SimTime,
    Wait,
    WakeReason,
)


def _alert(
    *,
    product: ProductId = ProductId.RAW_MILK,
    quote: str = "best_ask",
    operator: str = "at_most",
    price: str = "1.50",
) -> QuoteAlert:
    return QuoteAlert.model_validate(
        {
            "product": product,
            "quote": quote,
            "operator": operator,
            "price": price,
        }
    )


def _turn(
    observation: CompanyObservation,
    *,
    minute: int = 9 * 60,
    order_books: tuple[OrderBookView, ...] = (),
    turn_number: int = 1,
) -> AgentTurn:
    return AgentTurn(
        turn_id="run_1.farm_a.t1",
        company_id=observation.company_id,
        sim_time=SimTime(absolute_minute=minute),
        state_version=0,
        turn_number_today=turn_number,
        turn_limit_today=observation.runtime.max_turns_per_company_day,
        wake_reasons=(WakeReason.DAY_OPEN,),
        observation=observation,
        available_cash=observation.cash,
        marked_surplus=Decimal(),
        order_books=order_books,
    )


def _level(price: str) -> PriceLevelView:
    return PriceLevelView(
        unit_price=Decimal(price),
        quantity=Decimal("10"),
        order_count=1,
    )


def test_runtime_defaults_bound_attention_without_periodic_order_review() -> None:
    runtime = RuntimeSpec()

    assert runtime.max_wait_minutes == 120
    assert runtime.max_turns_per_company_day == 25
    assert "order_review_interval_minutes" not in RuntimeSpec.model_fields


def test_wait_accepts_at_most_three_typed_alerts() -> None:
    alerts = (
        _alert(price="1.10"),
        _alert(quote="best_bid", operator="at_least", price="1.40"),
        _alert(product=ProductId.BOTTLED_MILK, price="2.20"),
    )

    assert Wait(alerts=alerts).alerts == alerts
    with pytest.raises(ValidationError, match="at most 3 items"):
        Wait(alerts=(*alerts, _alert(price="1.20")))


def test_agent_turn_exposes_a_consistent_daily_budget(
    first_observation: CompanyObservation,
) -> None:
    turn = _turn(first_observation, turn_number=25)

    assert turn.turn_number_today == turn.turn_limit_today == 25
    with pytest.raises(ValidationError, match="cannot exceed"):
        _turn(first_observation, turn_number=26)
    with pytest.raises(ValidationError, match="must match the runtime turn limit"):
        AgentTurn(
            turn_id="run_1.farm_a.t1",
            company_id=first_observation.company_id,
            sim_time=SimTime.at(day=0, hour=9),
            state_version=0,
            turn_number_today=1,
            turn_limit_today=24,
            wake_reasons=(WakeReason.DAY_OPEN,),
            observation=first_observation,
            available_cash=first_observation.cash,
            marked_surplus=Decimal(),
        )


def test_arm_uses_the_bounded_default_review(
    first_observation: CompanyObservation,
) -> None:
    turn = _turn(first_observation)

    plan = AgentAttention().arm(Wait(), turn)

    assert plan.source_turn_id == turn.turn_id
    assert plan.armed_at == turn.sim_time
    assert plan.review_at == SimTime.at(day=0, hour=11)
    assert plan.alerts == ()


def test_default_review_does_not_cross_market_close(
    first_observation: CompanyObservation,
) -> None:
    turn = _turn(first_observation, minute=17 * 60)

    assert AgentAttention().arm(Wait(), turn).review_at is None


@pytest.mark.parametrize(
    ("until", "message"),
    [
        (SimTime.at(day=0, hour=9), "later than current time"),
        (SimTime.at(day=1, hour=9), "current business day"),
        (SimTime.at(day=0, hour=19), "inside business hours"),
        (SimTime.at(day=0, hour=11, minute=1), "cannot exceed 120 minutes"),
    ],
)
def test_explicit_review_must_obey_every_wait_constraint(
    first_observation: CompanyObservation,
    until: SimTime,
    message: str,
) -> None:
    turn = _turn(first_observation)

    with pytest.raises(AttentionRejected, match=message):
        AgentAttention().arm(Wait(until=until), turn)


def test_explicit_review_accepts_the_exact_maximum(
    first_observation: CompanyObservation,
) -> None:
    turn = _turn(first_observation)
    until = SimTime.at(day=0, hour=11)

    assert AgentAttention().arm(Wait(until=until), turn).review_at == until


def test_arm_rejects_hidden_duplicate_and_already_true_alerts(
    first_observation: CompanyObservation,
) -> None:
    visible = OrderBookView(product=ProductId.RAW_MILK, asks=(_level("1.40"),))
    turn = _turn(first_observation, order_books=(visible,))
    duplicate = _alert(price="1.30")

    with pytest.raises(AttentionRejected, match="not visible"):
        AgentAttention().arm(
            Wait(alerts=(_alert(product=ProductId.BOTTLED_MILK),)),
            turn,
        )
    with pytest.raises(AttentionRejected, match="must be unique"):
        AgentAttention().arm(Wait(alerts=(duplicate, duplicate)), turn)
    with pytest.raises(ValidationError, match="must be unique"):
        ArmedWait(
            source_turn_id=turn.turn_id,
            armed_at=turn.sim_time,
            alerts=(duplicate, duplicate),
        )
    with pytest.raises(AttentionRejected, match="must be false"):
        AgentAttention().arm(Wait(alerts=(_alert(price="1.40"),)), turn)


def test_missing_quote_is_false_and_matching_uses_or_semantics(
    first_observation: CompanyObservation,
) -> None:
    raw_book = OrderBookView(product=ProductId.RAW_MILK)
    bottled_book = OrderBookView(product=ProductId.BOTTLED_MILK, bids=(_level("2.00"),))
    turn = _turn(first_observation, order_books=(raw_book, bottled_book))
    raw_alert = _alert(price="1.30")
    bottled_alert = _alert(
        product=ProductId.BOTTLED_MILK,
        quote="best_bid",
        operator="at_least",
        price="2.20",
    )
    plan = AgentAttention().arm(Wait(alerts=(raw_alert, bottled_alert)), turn)

    assert AgentAttention().evaluate(plan, (raw_book, bottled_book)) is None

    match = AgentAttention().evaluate(
        plan,
        (
            OrderBookView(product=ProductId.RAW_MILK, asks=(_level("1.20"),)),
            OrderBookView(product=ProductId.BOTTLED_MILK, bids=(_level("2.30"),)),
        ),
    )

    assert match is not None
    assert match.source_turn_id == turn.turn_id
    assert match.matched_alerts == (raw_alert, bottled_alert)


def test_evaluate_treats_an_absent_product_as_not_matching(
    first_observation: CompanyObservation,
) -> None:
    book = OrderBookView(product=ProductId.RAW_MILK)
    plan = AgentAttention().arm(
        Wait(alerts=(_alert(price="1.30"),)),
        _turn(first_observation, order_books=(book,)),
    )

    assert AgentAttention().evaluate(plan, ()) is None
