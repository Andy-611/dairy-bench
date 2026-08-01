"""Behavioral tests for runtime commands and the deterministic scheduler."""

from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from company_bench.models import (
    CompanyObservation,
    InvalidOrderQuantity,
    require_order_quantity,
)
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
    WakeSignal,
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


@pytest.mark.parametrize("quantity", ("0.0001", "12.3456"))
def test_order_quantity_accepts_exact_four_decimal_precision(quantity: str) -> None:
    assert require_order_quantity(Decimal(quantity)) == Decimal(quantity)


@pytest.mark.parametrize("quantity", ("1E+24", "1E+999999"))
def test_order_quantity_accepts_large_tick_aligned_decimals(quantity: str) -> None:
    assert require_order_quantity(Decimal(quantity)) == Decimal(quantity)


@pytest.mark.parametrize("quantity", ("8.9E-91", "59.999999999999999"))
def test_order_quantity_rejects_dust_and_excess_precision(quantity: str) -> None:
    with pytest.raises(InvalidOrderQuantity, match=r"exact multiple of 0\.0001"):
        require_order_quantity(Decimal(quantity))


@pytest.mark.parametrize(
    "payload",
    (
        {
            "kind": "place_order",
            "side": "buy",
            "product": "raw_milk",
            "quantity": "59.98181",
            "limit_price": "1.50",
        },
        {
            "kind": "replace_order",
            "order_id": "order_1",
            "quantity": "0.00019",
            "limit_price": "1.50",
        },
    ),
)
def test_historical_unquantized_commands_remain_deserializable(
    payload: dict[str, str],
) -> None:
    """Raw audit commands stay readable; execution applies the new invariant."""
    command = TypeAdapter(CompanyCommand).validate_python(payload)

    assert command.quantity == Decimal(payload["quantity"])


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
        available_cash=first_observation.cash,
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
        SystemEventKind.OPERATION_COMPLETED,
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
    system_kinds = (
        SystemEventKind.DELIVERY_COMPLETED,
        SystemEventKind.OPERATION_COMPLETED,
        SystemEventKind.MARKET_CLOSE,
        SystemEventKind.CONSUMER_SALES,
        SystemEventKind.DAY_CLOSE,
    )
    system_events = tuple(
        scheduler.schedule_system(kind, at, event_id=kind.value)
        for kind in system_kinds
    )
    merged = scheduler.schedule_wake(
        "processor_a",
        at,
        WakeReason.EXTERNAL_EVENT,
        reference_ids=("message_1", "order_1"),
    )
    other = scheduler.schedule_wake("processor_b", at, WakeReason.DAY_OPEN)

    assert len(scheduler) == 7
    assert merged.sequence == first.sequence
    assert merged.wake_reasons == (
        WakeReason.ORDER_UPDATED,
        WakeReason.EXTERNAL_EVENT,
    )
    assert merged.wake_signals == (
        WakeSignal(
            reason=WakeReason.ORDER_UPDATED,
            reference_ids=("order_1",),
        ),
        WakeSignal(
            reason=WakeReason.EXTERNAL_EVENT,
            reference_ids=("message_1", "order_1"),
        ),
    )
    assert merged.reference_ids == ()
    bucket = scheduler.pop_bucket()
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
    scheduler = Scheduler(start_at=SimTime(absolute_minute=500))
    scheduler.schedule_system(
        SystemEventKind.DAY_OPEN,
        SimTime(absolute_minute=540),
        event_id="day_open",
    )
    scheduler.schedule_wake("farm_a", SimTime(absolute_minute=540), WakeReason.DAY_OPEN)
    for kind, minute in (
        (SystemEventKind.OPERATION_COMPLETED, 600),
        (SystemEventKind.DELIVERY_COMPLETED, 600),
        (SystemEventKind.MARKET_CLOSE, 1_110),
        (SystemEventKind.CONSUMER_SALES, 1_140),
        (SystemEventKind.DAY_CLOSE, 1_170),
    ):
        scheduler.schedule_system(
            kind,
            SimTime(absolute_minute=minute),
            event_id=kind.value,
        )

    checkpoint = scheduler.checkpoint()
    restored_checkpoint = SchedulerCheckpoint.model_validate_json(checkpoint.model_dump_json())
    restored = Scheduler.restore(restored_checkpoint)

    assert restored.now == scheduler.now
    assert restored.checkpoint() == checkpoint
    restored_buckets = []
    original_buckets = []
    while restored:
        restored_buckets.append(restored.pop_bucket())
    while scheduler:
        original_buckets.append(scheduler.pop_bucket())
    assert restored_buckets == original_buckets
    new_event = restored.schedule_system(
        SystemEventKind.DELIVERY_COMPLETED,
        SimTime(absolute_minute=1_200),
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
