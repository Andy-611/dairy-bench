"""Behavioral tests for runtime commands and the deterministic scheduler."""

from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from company_bench.models import CompanyObservation
from company_bench.runtime_models import (
    AgentTurn,
    CancelOrder,
    CommandEnvelope,
    CommandOutcome,
    CommandStatus,
    CompanyCommand,
    MarketSide,
    PlaceOrder,
    Produce,
    SetRetailPrice,
    SimTime,
    SystemEventKind,
    Transform,
    TurnRecord,
    Wait,
    WakeReason,
)
from company_bench.scheduler import Scheduler, SchedulerCheckpoint


def test_sim_time_is_absolute_and_immutable() -> None:
    at = SimTime.at(day=2, hour=9, minute=15)

    assert at.absolute_minute == 3_435
    assert (at.day, at.hour, at.minute, at.minute_of_day) == (2, 9, 15, 555)
    assert at.plus(30) == SimTime(absolute_minute=3_465)
    with pytest.raises(ValidationError):
        SimTime(absolute_minute=-1)


def test_company_command_is_discriminated_and_identity_free() -> None:
    adapter = TypeAdapter(CompanyCommand)
    command = adapter.validate_python(
        {
            "kind": "place_order",
            "side": "buy",
            "product": "raw_milk",
            "quantity": "12.5",
            "limit_price": "4.20",
        }
    )

    assert command == PlaceOrder(
        side=MarketSide.BUY,
        product="raw_milk",
        quantity=Decimal("12.5"),
        limit_price=Decimal("4.20"),
    )
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "kind": "produce",
                "company_id": "farm_a",
                "product": "raw_milk",
                "quantity": "10",
            }
        )


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
                "kind": "place_order",
                "side": "sell",
                "product": "bottled_milk",
                "quantity": "5",
                "limit_price": "7",
            },
            PlaceOrder,
        ),
        ({"kind": "cancel_order", "order_id": "order_1"}, CancelOrder),
        (
            {
                "kind": "set_retail_price",
                "product": "bottled_milk",
                "unit_price": "9",
            },
            SetRetailPrice,
        ),
        ({"kind": "wait"}, Wait),
    ],
)
def test_every_company_command_has_a_discriminated_variant(
    payload: dict[str, str],
    expected_type: type,
) -> None:
    command = TypeAdapter(CompanyCommand).validate_python(payload)

    assert isinstance(command, expected_type)


def test_turn_record_requires_runtime_identity_consistency(
    first_observation: CompanyObservation,
) -> None:
    at = SimTime(absolute_minute=540)
    turn = AgentTurn(
        turn_id="turn_1",
        company_id="farm_a",
        sim_time=at,
        state_version=0,
        wake_reasons=(WakeReason.DAY_OPEN,),
        observation=first_observation.model_copy(update={"company_id": "farm_a"}),
    )
    envelope = CommandEnvelope(
        turn_id="turn_1",
        command_id="command_1",
        company_id="farm_a",
        issued_at=at,
        state_version=0,
        command=Produce(product="raw_milk", quantity=Decimal("10")),
    )
    outcome = CommandOutcome(
        turn_id="turn_1",
        command_id="command_1",
        company_id="farm_a",
        occurred_at=at,
        status=CommandStatus.ACCEPTED,
        accepted=True,
        resulting_state_version=1,
        apply_sequence=1,
        next_available_at=at.plus(30),
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
    scheduler = Scheduler(start_at=SimTime(absolute_minute=100))
    first = scheduler.schedule_system(
        SystemEventKind.DAY_OPEN,
        SimTime(absolute_minute=50),
        event_id="first",
    )
    second = scheduler.schedule_system(
        SystemEventKind.MARKET_CLEAR,
        SimTime(absolute_minute=100),
        event_id="second",
    )
    later = scheduler.schedule_system(
        SystemEventKind.DAY_CLOSE,
        SimTime(absolute_minute=200),
        event_id="later",
    )

    assert first.at == scheduler.now
    assert scheduler.peek_time() == SimTime(absolute_minute=100)
    assert scheduler.pop_bucket() == (first, second)
    assert scheduler.now == SimTime(absolute_minute=100)
    assert scheduler.pop_bucket() == (later,)
    assert scheduler.now == SimTime(absolute_minute=200)
    assert scheduler.pop_bucket() == ()


def test_scheduler_prioritizes_system_events_and_coalesces_company_wakes() -> None:
    scheduler = Scheduler()
    at = SimTime(absolute_minute=540)
    first = scheduler.schedule_wake(
        "processor_a",
        at,
        WakeReason.ORDER_UPDATED,
        reference_ids=("order_1",),
    )
    scheduler.schedule_system(
        SystemEventKind.MARKET_CLEAR,
        at,
        event_id="market_clear",
    )
    merged = scheduler.schedule_wake(
        "processor_a",
        at,
        WakeReason.EXTERNAL_EVENT,
        reference_ids=("message_1", "order_1"),
    )
    other = scheduler.schedule_wake("processor_b", at, WakeReason.DAY_OPEN)

    assert len(scheduler) == 3
    assert merged.sequence == first.sequence
    assert merged.wake_reasons == (
        WakeReason.ORDER_UPDATED,
        WakeReason.EXTERNAL_EVENT,
    )
    assert merged.reference_ids == ("order_1", "message_1")
    bucket = scheduler.pop_bucket()
    assert [event.sequence for event in bucket] == [
        first.sequence + 1,
        first.sequence,
        other.sequence,
    ]
    assert bucket[1] == merged


def test_checkpoint_json_round_trip_preserves_order_and_next_sequence() -> None:
    scheduler = Scheduler(start_at=SimTime(absolute_minute=500))
    scheduler.schedule_system(
        SystemEventKind.DAY_OPEN,
        SimTime(absolute_minute=540),
        event_id="day_open",
    )
    scheduler.schedule_wake("farm_a", SimTime(absolute_minute=540), WakeReason.DAY_OPEN)
    scheduler.schedule_system(
        SystemEventKind.DAY_CLOSE,
        SimTime(absolute_minute=1_140),
        event_id="day_close",
    )

    checkpoint = scheduler.checkpoint()
    restored_checkpoint = SchedulerCheckpoint.model_validate_json(checkpoint.model_dump_json())
    restored = Scheduler.restore(restored_checkpoint)

    assert restored.now == scheduler.now
    assert restored.checkpoint() == checkpoint
    assert restored.pop_bucket() == scheduler.pop_bucket()
    new_event = restored.schedule_system(
        SystemEventKind.MARKET_CLEAR,
        SimTime(absolute_minute=600),
    )
    assert new_event.sequence == checkpoint.next_sequence


def test_scheduler_cancels_superseded_company_wake_timers() -> None:
    scheduler = Scheduler()
    scheduler.schedule_wake(
        "farm_a",
        SimTime(absolute_minute=600),
        WakeReason.WAIT_EXPIRED,
    )
    scheduler.schedule_wake(
        "farm_a",
        SimTime(absolute_minute=630),
        WakeReason.CONTINUE,
    )
    retained = scheduler.schedule_wake(
        "farm_b",
        SimTime(absolute_minute=620),
        WakeReason.CONTINUE,
    )

    scheduler.cancel_company_wakes("farm_a")
    scheduler.cancel_company_wakes("farm_a")

    assert scheduler.checkpoint().pending_events == (retained,)
    assert scheduler.pop_bucket() == (retained,)


def test_wait_can_target_time_but_cannot_spoof_runtime_fields() -> None:
    command = Wait(until=SimTime(absolute_minute=1_140))
    envelope = CommandEnvelope(
        turn_id="turn_wait_1",
        command_id="wait_1",
        company_id="retailer_a",
        issued_at=SimTime(absolute_minute=900),
        state_version=4,
        command=command,
    )

    assert envelope.command.until == SimTime(absolute_minute=1_140)
    with pytest.raises(ValidationError):
        Wait.model_validate(
            {
                "kind": "wait",
                "until": {"absolute_minute": 1_140},
                "company_id": "retailer_b",
            }
        )
