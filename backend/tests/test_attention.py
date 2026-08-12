from decimal import Decimal

import pytest
from pydantic import ValidationError

from company_bench.domain.calendar import SimDay, Weekday
from company_bench.domain.models import CompanyObservation, ProductId, RuntimeSpec
from company_bench.runtime.attention import AgentAttention, ArmedAttention, AttentionRejected
from company_bench.runtime.models import (
    AgentTurn,
    AttentionPlan,
    OrderBookView,
    PriceLevelView,
    QuoteAlert,
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
    weekday: Weekday = Weekday.MONDAY,
    order_books: tuple[OrderBookView, ...] = (),
    turn_number: int = 1,
) -> AgentTurn:
    sim_day = SimDay.at(week=1, weekday=weekday)
    observed_today = observation.model_copy(update={"sim_day": sim_day})
    return AgentTurn(
        turn_id="run_1.farm_a.t1",
        company_id=observation.company_id,
        sim_day=sim_day,
        state_version=0,
        turn_number_this_week=turn_number,
        turn_limit_this_week=observation.runtime.max_turns_per_company_week,
        wake_reasons=(WakeReason.WEEK_OPEN,),
        observation=observed_today,
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

    assert runtime.default_review_days == 1
    assert runtime.max_review_days == 2
    assert runtime.max_turns_per_company_week == 6
    assert "order_review_interval_minutes" not in RuntimeSpec.model_fields


def test_attention_accepts_at_most_three_typed_alerts() -> None:
    alerts = (
        _alert(price="1.10"),
        _alert(quote="best_bid", operator="at_least", price="1.40"),
        _alert(product=ProductId.BOTTLED_MILK, price="2.20"),
    )

    assert AttentionPlan(alerts=alerts).alerts == alerts
    with pytest.raises(ValidationError, match="at most 3 items"):
        AttentionPlan(alerts=(*alerts, _alert(price="1.20")))


def test_agent_turn_exposes_a_consistent_weekly_budget(
    first_observation: CompanyObservation,
) -> None:
    turn = _turn(first_observation, turn_number=6)

    assert turn.turn_number_this_week == turn.turn_limit_this_week == 6
    with pytest.raises(ValidationError, match="cannot exceed"):
        _turn(first_observation, turn_number=7)
    with pytest.raises(ValidationError, match="must match the runtime turn limit"):
        AgentTurn(
            turn_id="run_1.farm_a.t1",
            company_id=first_observation.company_id,
            sim_day=first_observation.sim_day,
            state_version=0,
            turn_number_this_week=1,
            turn_limit_this_week=5,
            wake_reasons=(WakeReason.WEEK_OPEN,),
            observation=first_observation,
            available_cash=first_observation.cash,
            marked_surplus=Decimal(),
        )


def test_arm_uses_the_bounded_default_review(
    first_observation: CompanyObservation,
) -> None:
    turn = _turn(first_observation)

    plan = AgentAttention().arm(AttentionPlan(), turn)

    assert plan.source_turn_id == turn.turn_id
    assert plan.armed_on == turn.sim_day
    assert plan.review_on == SimDay.at(week=1, weekday=Weekday.TUESDAY)
    assert plan.alerts == ()


def test_default_review_does_not_cross_sunday_settlement(
    first_observation: CompanyObservation,
) -> None:
    turn = _turn(first_observation, weekday=Weekday.SATURDAY)

    assert AgentAttention().arm(AttentionPlan(), turn).review_on is None


def test_explicit_review_must_obey_attention_bound(
    first_observation: CompanyObservation,
) -> None:
    turn = _turn(first_observation)

    with pytest.raises(AttentionRejected, match="cannot exceed 2 days"):
        AgentAttention().arm(AttentionPlan(review_after_days=3), turn)


def test_explicit_review_must_remain_on_a_decision_day_in_the_same_week(
    first_observation: CompanyObservation,
) -> None:
    turn = _turn(first_observation, weekday=Weekday.FRIDAY)

    with pytest.raises(AttentionRejected, match="Monday-Saturday"):
        AgentAttention().arm(AttentionPlan(review_after_days=2), turn)


def test_explicit_review_accepts_the_exact_maximum(
    first_observation: CompanyObservation,
) -> None:
    turn = _turn(first_observation)

    assert AgentAttention().arm(AttentionPlan(review_after_days=2), turn).review_on == (
        SimDay.at(week=1, weekday=Weekday.WEDNESDAY)
    )


def test_arm_rejects_hidden_duplicate_and_already_true_alerts(
    first_observation: CompanyObservation,
) -> None:
    visible = OrderBookView(product=ProductId.RAW_MILK, asks=(_level("1.40"),))
    turn = _turn(first_observation, order_books=(visible,))
    duplicate = _alert(price="1.30")

    with pytest.raises(AttentionRejected, match="not visible"):
        AgentAttention().arm(
            AttentionPlan(alerts=(_alert(product=ProductId.BOTTLED_MILK),)),
            turn,
        )
    with pytest.raises(AttentionRejected, match="must be unique"):
        AgentAttention().arm(AttentionPlan(alerts=(duplicate, duplicate)), turn)
    with pytest.raises(ValidationError, match="must be unique"):
        ArmedAttention(
            source_turn_id=turn.turn_id,
            armed_on=turn.sim_day,
            alerts=(duplicate, duplicate),
        )
    with pytest.raises(AttentionRejected, match="must be false"):
        AgentAttention().arm(AttentionPlan(alerts=(_alert(price="1.40"),)), turn)


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
    plan = AgentAttention().arm(AttentionPlan(alerts=(raw_alert, bottled_alert)), turn)

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
        AttentionPlan(alerts=(_alert(price="1.30"),)),
        _turn(first_observation, order_books=(book,)),
    )

    assert AgentAttention().evaluate(plan, ()) is None
