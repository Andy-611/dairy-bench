"""Behavioral tests for runtime decisions and the deterministic scheduler."""

from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from company_bench.domain.calendar import SimDay, Weekday
from company_bench.domain.models import (
    CompanyObservation,
    InvalidOrderQuantity,
    require_order_quantity,
)
from company_bench.runtime.models import (
    ActionDecision,
    AgentTurn,
    AttentionPlan,
    CompanyDecision,
    DecisionEnvelope,
    DecisionOutcome,
    DecisionStatus,
    EconomicCommand,
    IdleDecision,
    MarketSide,
    Produce,
    QuoteLevel,
    SetQuoteLadder,
    SetRetailPrice,
    SystemEventKind,
    Transform,
    TurnRecord,
    WakeReason,
    WakeSignal,
)
from company_bench.runtime.scheduler import Scheduler, SchedulerCheckpoint


def test_sim_day_is_absolute_and_immutable() -> None:
    at = SimDay.at(week=3, weekday=Weekday.WEDNESDAY)

    assert at.absolute_day == 17
    assert (at.week, at.weekday, at.day_of_week) == (3, Weekday.WEDNESDAY, 3)
    assert at.plus_days(2) == SimDay.at(week=3, weekday=Weekday.FRIDAY)
    with pytest.raises(ValidationError):
        SimDay(absolute_day=-1)


def test_company_decision_is_discriminated_and_identity_free() -> None:
    adapter = TypeAdapter(CompanyDecision)
    decision = adapter.validate_python(
        {
            "kind": "action",
            "action": {
                "kind": "set_quote_ladder",
                "side": "buy",
                "product": "raw_milk",
                "levels": [
                    {"quantity": "7.5", "limit_price": "4.20"},
                    {"quantity": "5", "limit_price": "4.00"},
                ],
            },
            "attention": {"review_after_days": 1},
        }
    )

    assert decision == ActionDecision(
        action=SetQuoteLadder(
            product="raw_milk",
            side=MarketSide.BUY,
            levels=(
                QuoteLevel(quantity=Decimal("7.5"), limit_price=Decimal("4.20")),
                QuoteLevel(quantity=Decimal("5"), limit_price=Decimal("4.00")),
            ),
        ),
        attention=AttentionPlan(review_after_days=1),
    )
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "kind": "action",
                "action": {
                    "kind": "produce",
                    "company_id": "farm_a",
                    "product": "raw_milk",
                    "quantity": "10",
                },
                "attention": {},
            }
        )


@pytest.mark.parametrize("quantity", ("0.0001", "12.3456"))
def test_order_quantity_accepts_exact_four_decimal_precision(quantity: str) -> None:
    assert require_order_quantity(Decimal(quantity)) == Decimal(quantity)


@pytest.mark.parametrize("quantity", ("1E+24", "1E+999999"))
def test_order_quantity_rejects_values_without_canonical_four_place_form(
    quantity: str,
) -> None:
    with pytest.raises(InvalidOrderQuantity):
        require_order_quantity(Decimal(quantity))


@pytest.mark.parametrize("quantity", ("8.9E-91", "59.999999999999999"))
def test_order_quantity_rejects_dust_and_excess_precision(quantity: str) -> None:
    with pytest.raises(InvalidOrderQuantity, match=r"exact multiple of 0\.0001"):
        require_order_quantity(Decimal(quantity))


@pytest.mark.parametrize("kind", ("place_order", "replace_order", "cancel_order"))
def test_removed_imperative_order_commands_are_rejected(kind: str) -> None:
    with pytest.raises(ValidationError, match="Input tag"):
        TypeAdapter(EconomicCommand).validate_python({"kind": kind})


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        (
            {"kind": "produce", "product": "raw_milk", "quantity": "10"},
            Produce,
        ),
        (
            {
                "kind": "transform",
                "input_product": "raw_milk",
                "output_product": "bottled_milk",
                "input_quantity": "8",
            },
            Transform,
        ),
        (
            {
                "kind": "set_quote_ladder",
                "side": "sell",
                "product": "bottled_milk",
                "levels": [
                    {"quantity": "3", "limit_price": "7"},
                    {"quantity": "2", "limit_price": "8"},
                ],
            },
            SetQuoteLadder,
        ),
        (
            {
                "kind": "set_retail_price",
                "product": "bottled_milk",
                "unit_price": "9",
            },
            SetRetailPrice,
        ),
    ],
)
def test_every_economic_command_has_a_discriminated_variant(
    payload: dict[str, str],
    expected_type: type,
) -> None:
    command = TypeAdapter(EconomicCommand).validate_python(payload)

    assert isinstance(command, expected_type)


def test_company_decision_distinguishes_action_from_idle() -> None:
    action = ActionDecision(
        action=Produce(product="raw_milk", quantity="10"),
        attention=AttentionPlan(review_after_days=1),
    )
    idle = IdleDecision(attention=AttentionPlan(review_after_days=2))

    assert action.kind == "action"
    assert idle.kind == "idle"


def test_turn_record_requires_runtime_identity_consistency(
    first_observation: CompanyObservation,
) -> None:
    at = first_observation.sim_day
    turn = AgentTurn(
        turn_id="turn_1",
        company_id="farm_a",
        sim_day=at,
        state_version=0,
        turn_number_this_week=1,
        turn_limit_this_week=first_observation.runtime.max_turns_per_company_week,
        wake_reasons=(WakeReason.WEEK_OPEN,),
        observation=first_observation,
        available_cash=first_observation.cash,
        marked_surplus=Decimal(),
    )
    decision = ActionDecision(
        action=Produce(product="raw_milk", quantity=Decimal("10")),
        attention=AttentionPlan(review_after_days=1),
    )
    envelope = DecisionEnvelope(
        turn_id="turn_1",
        decision_id="decision_1",
        company_id="farm_a",
        issued_on=at,
        state_version=0,
        decision=decision,
    )
    outcome = DecisionOutcome(
        turn_id="turn_1",
        decision_id="decision_1",
        company_id="farm_a",
        occurred_on=at,
        status=DecisionStatus.ACCEPTED,
        accepted=True,
        resulting_state_version=1,
        apply_sequence=1,
        next_available_on=at.plus_days(1),
    )

    assert TurnRecord(
        run_id="run_1",
        turn=turn,
        envelope=envelope,
        outcome=outcome,
        observation_hash="observation_hash",
    ).outcome.accepted
    with pytest.raises(ValidationError, match="company_id must match"):
        TurnRecord(
            run_id="run_1",
            turn=turn,
            envelope=envelope,
            outcome=outcome.model_copy(update={"company_id": "farm_b"}),
            observation_hash="observation_hash",
        )
    with pytest.raises(ValidationError, match="state_version must match"):
        TurnRecord(
            run_id="run_1",
            turn=turn,
            envelope=envelope.model_copy(update={"state_version": 2}),
            outcome=outcome,
            observation_hash="observation_hash",
        )


def test_scheduler_clamps_past_events_and_pops_a_stable_bucket() -> None:
    scheduler = Scheduler(start_on=SimDay(absolute_day=3))
    first = scheduler.schedule_system(
        SystemEventKind.WEEK_OPEN,
        SimDay(absolute_day=1),
        event_id="first",
    )
    second = scheduler.schedule_system(
        SystemEventKind.OPERATION_COMPLETED,
        SimDay(absolute_day=3),
        event_id="second",
    )
    later = scheduler.schedule_system(
        SystemEventKind.WEEK_CLOSE,
        SimDay(absolute_day=5),
        event_id="later",
    )

    assert first.scheduled_for == scheduler.today
    assert scheduler.peek_day() == SimDay(absolute_day=3)
    assert scheduler.pop_day() == (first, second)
    assert scheduler.today == SimDay(absolute_day=3)
    assert scheduler.pop_day() == (later,)
    assert scheduler.today == SimDay(absolute_day=5)
    assert scheduler.pop_day() == ()


def test_scheduler_prioritizes_system_events_and_coalesces_company_wakes() -> None:
    scheduler = Scheduler()
    at = SimDay.at(week=1, weekday=Weekday.WEDNESDAY)
    first = scheduler.schedule_wake(
        "processor_a",
        at,
        WakeReason.PRICE_ALERT,
        reference_ids=("order_1",),
    )
    system_kinds = (
        SystemEventKind.DELIVERY_COMPLETED,
        SystemEventKind.OPERATION_COMPLETED,
        SystemEventKind.MARKET_CLOSE,
        SystemEventKind.CONSUMER_SALES,
        SystemEventKind.WEEK_CLOSE,
    )
    system_events = tuple(
        scheduler.schedule_system(kind, at, event_id=kind.value) for kind in system_kinds
    )
    merged = scheduler.schedule_wake(
        "processor_a",
        at,
        WakeReason.EXTERNAL_EVENT,
        reference_ids=("message_1", "order_1"),
    )
    other = scheduler.schedule_wake("processor_b", at, WakeReason.WEEK_OPEN)

    assert len(scheduler) == 7
    assert merged.sequence == first.sequence
    assert merged.wake_reasons == (
        WakeReason.PRICE_ALERT,
        WakeReason.EXTERNAL_EVENT,
    )
    assert merged.wake_signals == (
        WakeSignal(
            reason=WakeReason.PRICE_ALERT,
            reference_ids=("order_1",),
        ),
        WakeSignal(
            reason=WakeReason.EXTERNAL_EVENT,
            reference_ids=("message_1", "order_1"),
        ),
    )
    assert merged.reference_ids == ()
    bucket = scheduler.pop_day()
    assert [event.sequence for event in bucket] == [
        *(event.sequence for event in system_events),
        first.sequence,
        other.sequence,
    ]
    assert [event.kind for event in bucket] == [
        *system_kinds,
        SystemEventKind.COMPANY_WAKE,
        SystemEventKind.COMPANY_WAKE,
    ]
    assert bucket[-2] == merged


def test_checkpoint_json_round_trip_preserves_order_and_next_sequence() -> None:
    scheduler = Scheduler(start_on=SimDay(absolute_day=1))
    scheduler.schedule_system(
        SystemEventKind.WEEK_OPEN,
        SimDay.at(week=1),
        event_id="week_open",
    )
    scheduler.schedule_wake("farm_a", SimDay.at(week=1), WakeReason.WEEK_OPEN)
    for kind, absolute_day in (
        (SystemEventKind.OPERATION_COMPLETED, 2),
        (SystemEventKind.DELIVERY_COMPLETED, 2),
        (SystemEventKind.MARKET_CLOSE, 7),
        (SystemEventKind.CONSUMER_SALES, 7),
        (SystemEventKind.WEEK_CLOSE, 7),
    ):
        scheduler.schedule_system(
            kind,
            SimDay(absolute_day=absolute_day),
            event_id=kind.value,
        )

    checkpoint = scheduler.checkpoint()
    restored_checkpoint = SchedulerCheckpoint.model_validate_json(checkpoint.model_dump_json())
    restored = Scheduler.restore(restored_checkpoint)

    assert restored.today == scheduler.today
    assert restored.checkpoint() == checkpoint
    restored_buckets = []
    original_buckets = []
    while restored:
        restored_buckets.append(restored.pop_day())
    while scheduler:
        original_buckets.append(scheduler.pop_day())
    assert restored_buckets == original_buckets
    new_event = restored.schedule_system(
        SystemEventKind.DELIVERY_COMPLETED,
        SimDay(absolute_day=8),
    )
    assert new_event.sequence == checkpoint.next_sequence


def test_scheduler_cancels_superseded_company_wake_timers() -> None:
    scheduler = Scheduler()
    scheduler.schedule_wake(
        "farm_a",
        SimDay(absolute_day=2),
        WakeReason.REVIEW_DUE,
    )
    scheduler.schedule_wake(
        "farm_a",
        SimDay(absolute_day=3),
        WakeReason.DECISION_REJECTED,
    )
    retained = scheduler.schedule_wake(
        "farm_b",
        SimDay(absolute_day=2),
        WakeReason.DECISION_REJECTED,
    )

    scheduler.cancel_company_wakes("farm_a")
    scheduler.cancel_company_wakes("farm_a")

    assert scheduler.checkpoint().pending_events == (retained,)
    assert scheduler.pop_day() == (retained,)


def test_scheduler_cancels_one_reason_without_losing_a_coalesced_wake() -> None:
    scheduler = Scheduler()
    at = SimDay(absolute_day=2)
    scheduler.schedule_wake("farm_a", at, WakeReason.REVIEW_DUE)
    retained = scheduler.schedule_wake(
        "farm_a",
        at,
        WakeReason.TRADE_EXECUTED,
        reference_ids=("trade_1",),
    )

    scheduler.cancel_wake("farm_a", at, WakeReason.REVIEW_DUE)
    scheduler.cancel_wake("farm_a", at, WakeReason.REVIEW_DUE)

    pending = scheduler.checkpoint().pending_events
    assert len(pending) == 1
    assert pending[0].event_id == retained.event_id
    assert pending[0].wake_reasons == (WakeReason.TRADE_EXECUTED,)
    assert scheduler.pop_day() == pending


def test_attention_can_target_time_but_cannot_spoof_runtime_fields() -> None:
    decision = IdleDecision(attention=AttentionPlan(review_after_days=2))
    envelope = DecisionEnvelope(
        turn_id="turn_idle_1",
        decision_id="idle_1",
        company_id="retailer_a",
        issued_on=SimDay(absolute_day=5),
        state_version=4,
        decision=decision,
    )

    assert envelope.decision.attention.review_after_days == 2
    with pytest.raises(ValidationError):
        AttentionPlan.model_validate(
            {
                "until": {"absolute_day": 7},
                "company_id": "retailer_b",
            }
        )
