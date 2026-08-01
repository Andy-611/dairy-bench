from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import ClassVar

import pytest

from company_bench.agent_models import ModelOutputError
from company_bench.agents import CompanyAgent, ReplayCompanyAgent
from company_bench.dairy_scenario import DAIRY_S12_V3_SCENARIO
from company_bench.models import (
    ConsumerSaleEvent,
    DeliveryCompletedEvent,
    MilkProducedEvent,
    PolicyKind,
    PolicyMetadata,
    ProductId,
    ScenarioSpec,
    TradeExecutedEvent,
)
from company_bench.repository import MemoryRunRepository
from company_bench.run_models import RunCheckpoint
from company_bench.runtime import EpisodeExecution, EpisodeRuntime
from company_bench.runtime_models import (
    PROTOCOL_ERROR_PREFIX,
    AgentTurn,
    CompanyCommand,
    MarketSide,
    PlaceOrder,
    Produce,
    SetRetailPrice,
    SimTime,
    SystemEventKind,
    SystemStepRecord,
    TurnRecord,
    Wait,
    WakeReason,
)

_OPEN = 9 * 60
_MARKET_CLOSE = 19 * 60
_DAY_CLOSE = 19 * 60 + 30
_QUANTITY = Decimal("10")
type _Decision = Callable[[AgentTurn], CompanyCommand]


@dataclass(slots=True)
class _ScriptedAgent:
    decide: _Decision
    delay_seconds: float = 0.0

    metadata: ClassVar[PolicyMetadata] = PolicyMetadata(
        name="runtime-v3-test",
        version="3",
        kind=PolicyKind.BASELINE,
    )

    async def act(self, turn: AgentTurn) -> CompanyCommand:
        """Return one scripted command after an optional real-time delay."""
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return self.decide(turn)


@dataclass(slots=True)
class _InterruptOnCommit:
    repository: MemoryRunRepository
    kind: SystemEventKind
    interrupted: bool = False

    def save_progress(
        self,
        turns: tuple[TurnRecord, ...],
        system_steps: tuple[SystemStepRecord, ...],
        checkpoint: RunCheckpoint,
    ) -> None:
        """Stop only after a pending economic commitment is durable."""
        self.repository.save_progress(turns, system_steps, checkpoint)
        commitments = (
            checkpoint.economy.jobs
            if self.kind is SystemEventKind.OPERATION_COMPLETED
            else checkpoint.economy.deliveries
        )
        if commitments and not self.interrupted:
            self.interrupted = True
            raise RuntimeError("interrupted after durable commitment")


def _scenario(
    *,
    max_turns: int = 8,
    company_ids: tuple[str, ...] = ("farm_a", "processor_a", "retailer_a"),
    bottled_farm: bool = False,
) -> ScenarioSpec:
    companies = tuple(
        company.model_copy(
            update={
                "operation": company.operation.model_copy(
                    update={"output_product": ProductId.BOTTLED_MILK}
                )
            }
        )
        if bottled_farm and company.company_id == "farm_a"
        else company
        for company in (DAIRY_S12_V3_SCENARIO.company(item) for item in company_ids)
    )
    return DAIRY_S12_V3_SCENARIO.model_copy(
        update={
            "scenario_id": "test.runtime.s3.v3",
            "days": 1,
            "companies": companies,
            "runtime": DAIRY_S12_V3_SCENARIO.runtime.model_copy(
                update={"max_turns_per_company_day": max_turns}
            ),
        }
    )


def _agents(
    scenario: ScenarioSpec,
    decision: _Decision,
    delays: dict[str, float] | None = None,
) -> dict[str, CompanyAgent]:
    delays = delays or {}
    return {
        company.company_id: _ScriptedAgent(
            decision,
            delay_seconds=delays.get(company.company_id, 0.0),
        )
        for company in scenario.companies
    }


def _wait(_: AgentTurn) -> CompanyCommand:
    return Wait()


def _supply_command(turn: AgentTurn) -> CompanyCommand:
    if turn.company_id == "farm_a":
        if WakeReason.DAY_OPEN in turn.wake_reasons:
            return Produce(product=ProductId.RAW_MILK, quantity=_QUANTITY)
        if WakeReason.OPERATION_COMPLETED in turn.wake_reasons:
            return PlaceOrder(
                side=MarketSide.SELL,
                product=ProductId.RAW_MILK,
                quantity=_QUANTITY,
                limit_price=Decimal("1.50"),
            )
    if turn.company_id == "processor_a" and WakeReason.DAY_OPEN in turn.wake_reasons:
        return PlaceOrder(
            side=MarketSide.BUY,
            product=ProductId.RAW_MILK,
            quantity=_QUANTITY,
            limit_price=Decimal("2.00"),
        )
    return Wait()


def _supply_agents(scenario: ScenarioSpec) -> dict[str, CompanyAgent]:
    return _agents(scenario, _supply_command)


def _timed_retail_agents(
    scenario: ScenarioSpec,
    trade_minute: int,
) -> dict[str, CompanyAgent]:
    trade_at = SimTime(absolute_minute=trade_minute)

    def decide(turn: AgentTurn) -> CompanyCommand:
        if turn.company_id == "farm_a":
            if WakeReason.DAY_OPEN in turn.wake_reasons:
                return Produce(product=ProductId.BOTTLED_MILK, quantity=_QUANTITY)
            if WakeReason.OPERATION_COMPLETED in turn.wake_reasons:
                return Wait(until=trade_at)
            if turn.sim_time == trade_at:
                return PlaceOrder(
                    side=MarketSide.SELL,
                    product=ProductId.BOTTLED_MILK,
                    quantity=_QUANTITY,
                    limit_price=Decimal("2.50"),
                )
        elif turn.company_id == "retailer_a":
            if WakeReason.DAY_OPEN in turn.wake_reasons:
                return SetRetailPrice(
                    product=ProductId.BOTTLED_MILK,
                    unit_price=Decimal("3.50"),
                )
            if turn.sim_time.minute_of_day == _OPEN + 1:
                return Wait(until=trade_at)
            if turn.sim_time == trade_at:
                return PlaceOrder(
                    side=MarketSide.BUY,
                    product=ProductId.BOTTLED_MILK,
                    quantity=_QUANTITY,
                    limit_price=Decimal("3.00"),
                )
        return Wait()

    return _agents(scenario, decide)


def _seed_order(
    scenario: ScenarioSpec,
    seed: int,
    minute: int = _OPEN,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            (company.company_id for company in scenario.companies),
            key=lambda company_id: (
                hashlib.sha256(f"{seed}|{minute}|{company_id}".encode()).digest(),
                company_id,
            ),
        )
    )


def _apply_projection(
    execution: EpisodeExecution,
    minute: int = _OPEN,
) -> tuple[tuple[str, int], ...]:
    return tuple(
        (record.turn.company_id, record.outcome.apply_sequence)
        for record in execution.turns
        if record.turn.sim_time.minute_of_day == minute
    )


def _assert_one_turn_per_company_minute(execution: EpisodeExecution) -> None:
    keys = tuple(
        (record.turn.company_id, record.turn.sim_time.absolute_minute) for record in execution.turns
    )
    assert len(keys) == len(set(keys))


@pytest.mark.asyncio
async def test_runtime_orders_open_market_close_consumer_sales_and_day_close() -> None:
    scenario = _scenario(max_turns=1)
    repository = MemoryRunRepository()

    await EpisodeRuntime(scenario).run(
        _agents(scenario, _wait),
        7,
        run_id="clock_order",
        store=repository,
    )

    assert tuple(
        (step.kind, step.occurred_at.minute_of_day)
        for step in repository.list_system_steps("clock_order")
    ) == (
        (SystemEventKind.DAY_OPEN, _OPEN),
        (SystemEventKind.MARKET_CLOSE, _MARKET_CLOSE),
        (SystemEventKind.CONSUMER_SALES, _MARKET_CLOSE),
        (SystemEventKind.DAY_CLOSE, _DAY_CLOSE),
    )


@pytest.mark.asyncio
async def test_real_response_order_does_not_change_seed_hash_application_order() -> None:
    scenario = _scenario(max_turns=1)
    company_ids = tuple(company.company_id for company in scenario.companies)
    forward_delays = {company_id: offset * 0.003 for offset, company_id in enumerate(company_ids)}
    reverse_delays = dict(zip(company_ids, reversed(tuple(forward_delays.values())), strict=True))
    runtime = EpisodeRuntime(scenario)

    forward = await runtime.run(
        _agents(scenario, _wait, forward_delays),
        17,
        run_id="response_order",
    )
    reverse = await runtime.run(
        _agents(scenario, _wait, reverse_delays),
        17,
        run_id="response_order",
    )

    expected = tuple(
        (company_id, sequence)
        for sequence, company_id in enumerate(
            _seed_order(scenario, 17),
            start=1,
        )
    )
    assert _apply_projection(forward) == _apply_projection(reverse) == expected


@pytest.mark.asyncio
async def test_a_different_seed_can_change_same_minute_application_order() -> None:
    scenario = _scenario(max_turns=1)
    first_seed = 1
    first_order = _seed_order(scenario, first_seed)
    second_seed = next(
        seed for seed in range(first_seed + 1, 100) if _seed_order(scenario, seed) != first_order
    )
    runtime = EpisodeRuntime(scenario)

    first = await runtime.run(_agents(scenario, _wait), first_seed, run_id="seed_one")
    second = await runtime.run(_agents(scenario, _wait), second_seed, run_id="seed_two")

    assert tuple(company_id for company_id, _ in _apply_projection(first)) == first_order
    assert _apply_projection(first) != _apply_projection(second)


@pytest.mark.asyncio
async def test_operation_and_delivery_completions_wake_the_owning_companies() -> None:
    scenario = _scenario()
    execution = await EpisodeRuntime(scenario).run(
        _supply_agents(scenario),
        13,
        run_id="completion_wakes",
    )

    production_turn = next(
        record
        for record in execution.turns
        if record.turn.company_id == "farm_a" and record.turn.sim_time.minute_of_day == _OPEN + 30
    )
    delivery_turn = next(
        record
        for record in execution.turns
        if record.turn.company_id == "processor_a"
        and record.turn.sim_time.minute_of_day == _OPEN + 60
    )

    assert WakeReason.OPERATION_COMPLETED in production_turn.turn.wake_reasons
    assert any(
        isinstance(event, MilkProducedEvent) for event in production_turn.turn.visible_events
    )
    assert WakeReason.DELIVERY_COMPLETED in delivery_turn.turn.wake_reasons
    assert any(
        isinstance(event, DeliveryCompletedEvent) for event in delivery_turn.turn.visible_events
    )
    assert any(isinstance(record.event, TradeExecutedEvent) for record in execution.episode.events)
    _assert_one_turn_per_company_minute(execution)


@pytest.mark.asyncio
async def test_market_change_wakes_next_minute_and_resting_order_gets_reviewed() -> None:
    scenario = _scenario(max_turns=4)

    def place_one_bid(turn: AgentTurn) -> CompanyCommand:
        if turn.company_id == "processor_a" and WakeReason.DAY_OPEN in turn.wake_reasons:
            return PlaceOrder(
                side=MarketSide.BUY,
                product=ProductId.RAW_MILK,
                quantity=_QUANTITY,
                limit_price=Decimal("2.00"),
            )
        return Wait()

    execution = await EpisodeRuntime(scenario).run(
        _agents(scenario, place_one_bid),
        23,
        run_id="market_notifications",
    )
    changed = {
        record.turn.company_id
        for record in execution.turns
        if record.turn.sim_time.minute_of_day == _OPEN + 1
        and WakeReason.MARKET_CHANGED in record.turn.wake_reasons
    }
    notifications = tuple(
        record
        for record in execution.turns
        if record.turn.sim_time.minute_of_day == _OPEN + 1
        and WakeReason.MARKET_CHANGED in record.turn.wake_reasons
    )
    placed = next(
        record
        for record in execution.turns
        if record.turn.company_id == "processor_a" and record.turn.sim_time.minute_of_day == _OPEN
    )
    review = next(
        record
        for record in execution.turns
        if record.turn.company_id == "processor_a"
        and WakeReason.ORDER_UPDATED in record.turn.wake_reasons
    )
    review_signal = next(
        signal for signal in review.turn.wake_signals if signal.reason is WakeReason.ORDER_UPDATED
    )

    assert changed == {"farm_a", "processor_a"}
    for notification in notifications:
        market_signal = next(
            signal
            for signal in notification.turn.wake_signals
            if signal.reason is WakeReason.MARKET_CHANGED
        )
        assert market_signal.source is None
        assert all(".t" not in reference for reference in market_signal.reference_ids)
        continuation = next(
            (
                signal
                for signal in notification.turn.wake_signals
                if signal.reason is WakeReason.CONTINUE
            ),
            None,
        )
        if continuation is not None:
            assert continuation.source is not None
            assert f".{notification.turn.company_id}.t" in continuation.source.entry_id
    assert review.turn.sim_time.minute_of_day == (
        _OPEN + 1 + scenario.runtime.order_review_interval_minutes
    )
    assert review.turn.wake_reasons == (WakeReason.ORDER_UPDATED,)
    assert tuple(order.order_id for order in review.turn.open_orders) == (placed.outcome.order_id,)
    assert review_signal.reference_ids == (placed.outcome.order_id,)
    _assert_one_turn_per_company_minute(execution)


@pytest.mark.asyncio
async def test_protocol_rejection_retries_after_exactly_one_virtual_minute() -> None:
    scenario = _scenario(max_turns=3)

    def broken(_: AgentTurn) -> CompanyCommand:
        raise ModelOutputError("invalid command")

    execution = await EpisodeRuntime(scenario).run(
        _agents(scenario, broken),
        29,
        run_id="protocol_rejection",
    )

    for company in scenario.companies:
        records = tuple(
            record for record in execution.turns if record.turn.company_id == company.company_id
        )
        assert tuple(record.turn.sim_time.minute_of_day for record in records) == (
            _OPEN,
            _OPEN + 1,
            _OPEN + 2,
        )
        assert all(
            record.protocol_error == "ModelOutputError: invalid command" for record in records
        )
        assert all(
            record.outcome.reason == f"{PROTOCOL_ERROR_PREFIX}{record.protocol_error}"
            for record in records
        )
        assert all(right.turn.previous_outcome == left.outcome for left, right in pairwise(records))
        assert all(WakeReason.CONTINUE in record.turn.wake_reasons for record in records[1:])
    _assert_one_turn_per_company_minute(execution)


@pytest.mark.parametrize(
    ("trade_minute", "arrival_minute", "participates_in_consumption"),
    (
        (18 * 60 + 30, 19 * 60, True),
        (18 * 60 + 31, 19 * 60 + 1, False),
        (18 * 60 + 59, 19 * 60 + 29, False),
    ),
)
@pytest.mark.asyncio
async def test_delivery_boundary_controls_same_day_consumer_inventory(
    trade_minute: int,
    arrival_minute: int,
    participates_in_consumption: bool,
) -> None:
    scenario = _scenario(
        company_ids=("farm_a", "retailer_a"),
        bottled_farm=True,
    )
    run_id = f"delivery_{arrival_minute}"
    repository = MemoryRunRepository()
    execution = await EpisodeRuntime(scenario).run(
        _timed_retail_agents(scenario, trade_minute),
        31,
        run_id=run_id,
        store=repository,
    )
    delivery_steps = tuple(
        step
        for step in repository.list_system_steps(run_id)
        if step.kind is SystemEventKind.DELIVERY_COMPLETED
    )
    close_minute_kinds = tuple(
        step.kind
        for step in repository.list_system_steps(run_id)
        if step.occurred_at.minute_of_day == _MARKET_CLOSE
    )
    consumer_sale = next(
        record.event
        for record in execution.episode.events
        if isinstance(record.event, ConsumerSaleEvent) and record.event.company_id == "retailer_a"
    )

    assert len(delivery_steps) == 1
    assert delivery_steps[0].occurred_at.minute_of_day == arrival_minute
    assert (consumer_sale.sold_quantity > 0) is participates_in_consumption
    assert close_minute_kinds == (
        (
            SystemEventKind.DELIVERY_COMPLETED,
            SystemEventKind.MARKET_CLOSE,
            SystemEventKind.CONSUMER_SALES,
        )
        if arrival_minute == _MARKET_CLOSE
        else (
            SystemEventKind.MARKET_CLOSE,
            SystemEventKind.CONSUMER_SALES,
        )
    )


@pytest.mark.parametrize(
    "completion_kind",
    (SystemEventKind.OPERATION_COMPLETED, SystemEventKind.DELIVERY_COMPLETED),
)
@pytest.mark.asyncio
async def test_atomic_completion_checkpoint_resumes_each_commitment_exactly_once(
    completion_kind: SystemEventKind,
) -> None:
    scenario = _scenario()
    run_id = f"resume_{completion_kind.value}"
    runtime = EpisodeRuntime(scenario)
    expected = await runtime.run(
        _supply_agents(scenario),
        37,
        run_id=run_id,
    )
    repository = MemoryRunRepository()
    store = _InterruptOnCommit(repository, completion_kind)

    with pytest.raises(RuntimeError, match="durable commitment"):
        await runtime.run(
            _supply_agents(scenario),
            37,
            run_id=run_id,
            store=store,
        )
    checkpoint = repository.get_checkpoint(run_id)
    assert checkpoint is not None
    commitments = (
        checkpoint.economy.jobs
        if completion_kind is SystemEventKind.OPERATION_COMPLETED
        else checkpoint.economy.deliveries
    )
    assert len(commitments) == 1
    commitment = commitments[0]
    commitment_id = (
        commitment.job_id
        if completion_kind is SystemEventKind.OPERATION_COMPLETED
        else commitment.delivery_id
    )
    completes_at = (
        commitment.completes_at
        if completion_kind is SystemEventKind.OPERATION_COMPLETED
        else commitment.arrives_at
    )
    company_id = (
        commitment.company_id
        if completion_kind is SystemEventKind.OPERATION_COMPLETED
        else commitment.buyer_id
    )
    completion_events = tuple(
        event for event in checkpoint.scheduler.pending_events if event.kind is completion_kind
    )

    assert len(completion_events) == 1
    assert completion_events[0].reference_ids == (commitment_id,)
    assert completion_events[0].company_id == company_id
    assert completion_events[0].at == completes_at
    assert any(
        scheduled.reference_id == commitment_id
        for record in checkpoint.turns
        for scheduled in record.outcome.scheduled_completions
    )

    resumed = await runtime.run(
        _supply_agents(scenario),
        37,
        run_id=run_id,
        store=repository,
        checkpoint=checkpoint,
    )
    completion_type = (
        MilkProducedEvent
        if completion_kind is SystemEventKind.OPERATION_COMPLETED
        else DeliveryCompletedEvent
    )

    assert resumed.turns == expected.turns
    assert resumed.episode.events == expected.episode.events
    assert resumed.episode.snapshots == expected.episode.snapshots
    assert resumed.episode.score == expected.episode.score
    assert sum(step.kind is completion_kind for step in repository.list_system_steps(run_id)) == 1
    assert sum(isinstance(record.event, completion_type) for record in resumed.episode.events) == 1


@pytest.mark.asyncio
async def test_turn_journal_replay_is_economically_deterministic() -> None:
    scenario = _scenario()
    runtime = EpisodeRuntime(scenario)
    source = await runtime.run(
        _supply_agents(scenario),
        41,
        run_id="replay_source",
    )
    replay = await runtime.run(
        {
            company.company_id: ReplayCompanyAgent(company.company_id, source.turns)
            for company in scenario.companies
        },
        41,
        run_id="replay_result",
        replay_source=source.episode,
    )

    assert tuple(record.envelope.command for record in replay.turns) == tuple(
        record.envelope.command for record in source.turns
    )
    assert replay.episode.events == source.episode.events
    assert replay.episode.snapshots == source.episode.snapshots
    assert replay.episode.score == source.episode.score
    _assert_one_turn_per_company_minute(replay)
