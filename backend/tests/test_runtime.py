from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import ClassVar

import pytest
from pydantic import ValidationError

from company_bench.agents.company import CompanyAgent, ReplayCompanyAgent
from company_bench.agents.contracts import ModelOutputError
from company_bench.domain.models import (
    ConsumerSaleEvent,
    DeliveryCompletedEvent,
    MilkProducedEvent,
    PolicyKind,
    PolicyMetadata,
    ProductId,
    ProtocolIssueKind,
    ScenarioSpec,
    TradeExecutedEvent,
)
from company_bench.domain.scenario import DAIRY_S9_SCENARIO
from company_bench.runs.models import RunCheckpoint, RunRecovery
from company_bench.runtime.episode import EpisodeExecution, EpisodeRuntime
from company_bench.runtime.models import (
    PROTOCOL_ERROR_PREFIX,
    AgentTurn,
    AttentionPlan,
    CompanyDecision,
    IdleDecision,
    MarketSide,
    Produce,
    QuoteAlert,
    QuoteLevel,
    SetQuoteLadder,
    SetRetailPrice,
    SimTime,
    SystemEventKind,
    SystemStepRecord,
    TurnRecord,
    WakeReason,
)
from company_bench.storage.store import InMemoryRunStore
from tests.support.fakes import company_decision

_OPEN = 9 * 60
_MARKET_CLOSE = 19 * 60
_DAY_CLOSE = 19 * 60 + 30
_QUANTITY = Decimal("10")
type _Decision = Callable[[AgentTurn], CompanyDecision]


@dataclass(slots=True)
class _ScriptedAgent:
    decide: _Decision
    delay_seconds: float = 0.0

    metadata: ClassVar[PolicyMetadata] = PolicyMetadata(
        name="runtime-test",
        kind=PolicyKind.BASELINE,
    )

    async def act(self, turn: AgentTurn) -> CompanyDecision:
        """Return one scripted decision after an optional real-time delay."""
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return self.decide(turn)


@dataclass(slots=True)
class _InterruptOnCommit:
    repository: InMemoryRunStore
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


@dataclass(slots=True)
class _InterruptOnAttention:
    repository: InMemoryRunStore
    interrupted: bool = False

    def save_progress(
        self,
        turns: tuple[TurnRecord, ...],
        system_steps: tuple[SystemStepRecord, ...],
        checkpoint: RunCheckpoint,
    ) -> None:
        """Stop after an armed attention plan and its fallback wake are durable."""
        self.repository.save_progress(turns, system_steps, checkpoint)
        if not self.interrupted and any(
            cursor.active_attention is not None for cursor in checkpoint.cursors
        ):
            self.interrupted = True
            raise RuntimeError("interrupted after durable attention plan")


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
        for company in (DAIRY_S9_SCENARIO.company(item) for item in company_ids)
    )
    return DAIRY_S9_SCENARIO.model_copy(
        update={
            "scenario_id": "test.runtime.current",
            "days": 1,
            "companies": companies,
            "runtime": DAIRY_S9_SCENARIO.runtime.model_copy(
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


def _idle(_: AgentTurn) -> CompanyDecision:
    return company_decision()


def _quote(
    side: MarketSide,
    product: ProductId,
    quantity: Decimal,
    limit_price: Decimal,
) -> SetQuoteLadder:
    """Build one focused ladder for runtime orchestration tests."""
    return SetQuoteLadder(
        side=side,
        product=product,
        levels=(QuoteLevel(quantity=quantity, limit_price=limit_price),),
    )


def _supply_decision(turn: AgentTurn) -> CompanyDecision:
    if turn.company_id == "farm_a":
        if WakeReason.DAY_OPEN in turn.wake_reasons:
            return company_decision(
                Produce(product=ProductId.RAW_MILK, quantity=_QUANTITY),
                review_after_minutes=30,
            )
        if WakeReason.OPERATION_COMPLETED in turn.wake_reasons:
            return company_decision(
                _quote(
                    MarketSide.SELL,
                    ProductId.RAW_MILK,
                    _QUANTITY,
                    Decimal("1.50"),
                ),
                review_after_minutes=30,
            )
    if turn.company_id == "processor_a" and WakeReason.DAY_OPEN in turn.wake_reasons:
        return company_decision(
            _quote(
                MarketSide.BUY,
                ProductId.RAW_MILK,
                _QUANTITY,
                Decimal("2.00"),
            ),
            review_after_minutes=30,
        )
    return company_decision()


def _supply_agents(scenario: ScenarioSpec) -> dict[str, CompanyAgent]:
    return _agents(scenario, _supply_decision)


def _timed_retail_agents(
    scenario: ScenarioSpec,
    trade_minute: int,
) -> dict[str, CompanyAgent]:
    trade_at = SimTime(absolute_minute=trade_minute)

    def idle_toward(turn: AgentTurn) -> CompanyDecision:
        return company_decision(
            review_after_minutes=min(
                trade_at.absolute_minute - turn.sim_time.absolute_minute,
                turn.observation.runtime.max_review_minutes,
            ),
        )

    def decide(turn: AgentTurn) -> CompanyDecision:
        if turn.company_id == "farm_a":
            if WakeReason.DAY_OPEN in turn.wake_reasons:
                return company_decision(
                    Produce(product=ProductId.BOTTLED_MILK, quantity=_QUANTITY),
                    review_after_minutes=30,
                )
            if turn.sim_time == trade_at:
                return company_decision(
                    _quote(
                        MarketSide.SELL,
                        ProductId.BOTTLED_MILK,
                        _QUANTITY,
                        Decimal("2.50"),
                    )
                )
            if turn.sim_time.absolute_minute < trade_at.absolute_minute:
                return idle_toward(turn)
        elif turn.company_id == "retailer_a":
            if WakeReason.DAY_OPEN in turn.wake_reasons:
                return company_decision(
                    SetRetailPrice(
                        product=ProductId.BOTTLED_MILK,
                        unit_price=Decimal("3.50"),
                    ),
                    review_after_minutes=1,
                )
            if turn.sim_time == trade_at:
                return company_decision(
                    _quote(
                        MarketSide.BUY,
                        ProductId.BOTTLED_MILK,
                        _QUANTITY,
                        Decimal("3.00"),
                    )
                )
            if turn.sim_time.absolute_minute < trade_at.absolute_minute:
                return idle_toward(turn)
        return company_decision()

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
    repository = InMemoryRunStore()

    await EpisodeRuntime(scenario).run(
        _agents(scenario, _idle),
        7,
        run_id="clock_order",
        store=repository,
    )

    steps = repository.list_system_steps("clock_order")
    assert tuple(
        (step.kind, step.occurred_at.minute_of_day)
        for step in steps
        if step.kind is not SystemEventKind.TURN_LIMIT_REACHED
    ) == (
        (SystemEventKind.DAY_OPEN, _OPEN),
        (SystemEventKind.MARKET_CLOSE, _MARKET_CLOSE),
        (SystemEventKind.CONSUMER_SALES, _MARKET_CLOSE),
        (SystemEventKind.DAY_CLOSE, _DAY_CLOSE),
    )
    limit_steps = tuple(step for step in steps if step.kind is SystemEventKind.TURN_LIMIT_REACHED)
    assert {step.company_id for step in limit_steps} == {
        company.company_id for company in scenario.companies
    }
    assert all(step.occurred_at.minute_of_day == _OPEN for step in limit_steps)


@pytest.mark.asyncio
async def test_turn_limit_journals_each_suppressed_wake_with_its_cause() -> None:
    scenario = _scenario(max_turns=1, company_ids=("farm_a",))
    repository = InMemoryRunStore()

    def produce_once(turn: AgentTurn) -> CompanyDecision:
        return company_decision(
            Produce(product=ProductId.RAW_MILK, quantity=_QUANTITY),
            review_after_minutes=30,
        )

    execution = await EpisodeRuntime(scenario).run(
        _agents(scenario, produce_once),
        8,
        run_id="turn_limit_audit",
        store=repository,
    )
    steps = repository.list_system_steps("turn_limit_audit")
    suppressed = tuple(step for step in steps if step.kind is SystemEventKind.AGENT_WAKE_SUPPRESSED)

    assert len(execution.turns) == 1
    assert len(suppressed) == 1
    assert suppressed[0].company_id == "farm_a"
    assert suppressed[0].occurred_at.minute_of_day == _OPEN + 30
    assert tuple(signal.reason for signal in suppressed[0].suppressed_wake_signals) == (
        WakeReason.OPERATION_COMPLETED,
    )
    assert suppressed[0].suppressed_wake_signals[0].reference_ids


@pytest.mark.asyncio
async def test_real_response_order_does_not_change_seed_hash_application_order() -> None:
    scenario = _scenario(max_turns=1)
    company_ids = tuple(company.company_id for company in scenario.companies)
    forward_delays = {company_id: offset * 0.003 for offset, company_id in enumerate(company_ids)}
    reverse_delays = dict(zip(company_ids, reversed(tuple(forward_delays.values())), strict=True))
    runtime = EpisodeRuntime(scenario)

    forward = await runtime.run(
        _agents(scenario, _idle, forward_delays),
        17,
        run_id="response_order",
    )
    reverse = await runtime.run(
        _agents(scenario, _idle, reverse_delays),
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

    first = await runtime.run(_agents(scenario, _idle), first_seed, run_id="seed_one")
    second = await runtime.run(_agents(scenario, _idle), second_seed, run_id="seed_two")

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
    trade_wakes = tuple(
        record
        for record in execution.turns
        if WakeReason.TRADE_EXECUTED in record.turn.wake_reasons
    )
    assert {record.turn.company_id for record in trade_wakes} == {
        "farm_a",
        "processor_a",
    }
    assert {record.turn.sim_time.minute_of_day for record in trade_wakes} == {_OPEN + 31}
    for trade_wake in trade_wakes:
        signal = next(
            signal
            for signal in trade_wake.turn.wake_signals
            if signal.reason is WakeReason.TRADE_EXECUTED
        )
        assert signal.source is None or (
            f".{trade_wake.turn.company_id}.t" in signal.source.entry_id
        )
    _assert_one_turn_per_company_minute(execution)


@pytest.mark.asyncio
async def test_resting_order_does_not_broadcast_and_action_review_is_bounded() -> None:
    scenario = _scenario(max_turns=4)

    def place_one_bid(turn: AgentTurn) -> CompanyDecision:
        if turn.company_id == "processor_a" and WakeReason.DAY_OPEN in turn.wake_reasons:
            return company_decision(
                _quote(
                    MarketSide.BUY,
                    ProductId.RAW_MILK,
                    _QUANTITY,
                    Decimal("2.00"),
                ),
                review_after_minutes=scenario.runtime.max_review_minutes,
            )
        return company_decision()

    execution = await EpisodeRuntime(scenario).run(
        _agents(scenario, place_one_bid),
        23,
        run_id="market_notifications",
    )
    at_next_minute = tuple(
        record for record in execution.turns if record.turn.sim_time.minute_of_day == _OPEN + 1
    )
    processor_review = next(
        record
        for record in execution.turns
        if record.turn.company_id == "processor_a"
        and WakeReason.REVIEW_DUE in record.turn.wake_reasons
    )

    assert at_next_minute == ()
    assert processor_review.turn.sim_time.minute_of_day == (
        _OPEN + scenario.runtime.max_review_minutes
    )
    assert tuple(order.product for order in processor_review.turn.open_orders) == (
        ProductId.RAW_MILK,
    )
    _assert_one_turn_per_company_minute(execution)


@pytest.mark.asyncio
async def test_price_alert_observes_the_committed_minute_and_wakes_once() -> None:
    scenario = _scenario(max_turns=3, company_ids=("farm_a", "processor_a"))
    alert = QuoteAlert(
        product=ProductId.RAW_MILK,
        quote="best_bid",
        operator="at_least",
        price=Decimal("1.50"),
    )

    def decide(turn: AgentTurn) -> CompanyDecision:
        if WakeReason.DAY_OPEN in turn.wake_reasons:
            if turn.company_id == "farm_a":
                return IdleDecision(attention=AttentionPlan(alerts=(alert,)))
            return company_decision(
                _quote(
                    MarketSide.BUY,
                    ProductId.RAW_MILK,
                    _QUANTITY,
                    Decimal("1.50"),
                ),
                review_after_minutes=30,
            )
        return company_decision()

    execution = await EpisodeRuntime(scenario).run(
        _agents(scenario, decide),
        24,
        run_id="price_alert",
    )
    source = next(
        record
        for record in execution.turns
        if record.turn.company_id == "farm_a" and record.turn.sim_time.minute_of_day == _OPEN
    )
    alerted = tuple(
        record for record in execution.turns if WakeReason.PRICE_ALERT in record.turn.wake_reasons
    )

    assert len(alerted) == 1
    assert alerted[0].turn.company_id == "farm_a"
    assert alerted[0].turn.sim_time.minute_of_day == _OPEN + 1
    signal = next(
        signal for signal in alerted[0].turn.wake_signals if signal.reason is WakeReason.PRICE_ALERT
    )
    assert signal.source is not None
    assert signal.source.entry_id == source.turn.turn_id
    assert len(signal.reference_ids) == 1
    assert "processor_a" not in signal.reference_ids[0]


@pytest.mark.asyncio
async def test_protocol_rejection_retries_after_exactly_one_virtual_minute() -> None:
    scenario = _scenario(max_turns=3)
    turns: list[TurnRecord] = []

    def broken(_: AgentTurn) -> CompanyDecision:
        raise ModelOutputError("invalid decision")

    async def remember(record: TurnRecord) -> None:
        turns.append(record)

    execution = await EpisodeRuntime(scenario).run(
        _agents(scenario, broken),
        29,
        run_id="protocol_rejection",
        on_turn_completed=remember,
    )

    assert not execution.episode.quality.benchmark_eligible
    assert execution.episode.quality.protocol.invalid_turn_count == 9
    assert execution.episode.quality.protocol.issues[0].kind is ProtocolIssueKind.INVALID_RESPONSE

    for company in scenario.companies:
        records = tuple(record for record in turns if record.turn.company_id == company.company_id)
        assert tuple(record.turn.sim_time.minute_of_day for record in records) == (
            _OPEN,
            _OPEN + 1,
            _OPEN + 2,
        )
        assert all(
            record.protocol_error == "ModelOutputError: invalid decision" for record in records
        )
        assert all(
            record.outcome.reason == f"{PROTOCOL_ERROR_PREFIX}{record.protocol_error}"
            for record in records
        )
        assert all(right.turn.previous_outcome == left.outcome for left, right in pairwise(records))
        assert all(
            WakeReason.DECISION_REJECTED in record.turn.wake_reasons for record in records[1:]
        )
    assert len({(record.turn.company_id, record.turn.sim_time) for record in turns}) == len(turns)


@pytest.mark.asyncio
async def test_invalid_attention_plan_is_rejected_without_changing_economy() -> None:
    scenario = _scenario(max_turns=2, company_ids=("farm_a",))

    def decide(turn: AgentTurn) -> CompanyDecision:
        if WakeReason.DAY_OPEN in turn.wake_reasons:
            return company_decision(
                review_after_minutes=turn.observation.runtime.max_review_minutes + 1
            )
        return company_decision()

    execution = await EpisodeRuntime(scenario).run(
        _agents(scenario, decide),
        30,
        run_id="invalid_attention",
    )
    first, correction = execution.turns

    assert not first.outcome.accepted
    assert first.outcome.reason == "attention review delay cannot exceed 120 minutes"
    assert first.outcome.resulting_state_version == first.turn.state_version
    assert correction.turn.sim_time == first.turn.sim_time.plus(1)
    assert correction.turn.wake_reasons == (WakeReason.DECISION_REJECTED,)
    assert correction.turn.previous_outcome == first.outcome


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
    repository = InMemoryRunStore()
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
    repository = InMemoryRunStore()
    store = _InterruptOnCommit(repository, completion_kind)

    with pytest.raises(RuntimeError, match="durable commitment"):
        await runtime.run(
            _supply_agents(scenario),
            37,
            run_id=run_id,
            store=store,
        )
    recovery = repository.load_recovery(run_id)
    assert recovery is not None
    checkpoint = recovery.checkpoint
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
        for record in recovery.turns
        for scheduled in record.outcome.scheduled_completions
    )

    resumed = await runtime.run(
        _supply_agents(scenario),
        37,
        run_id=run_id,
        store=repository,
        recovery=recovery,
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
async def test_checkpoint_restores_attention_plan_and_fallback_wake_exactly() -> None:
    scenario = _scenario(max_turns=3)
    run_id = "resume_attention"
    runtime = EpisodeRuntime(scenario)
    agents = _agents(scenario, _idle)
    expected = await runtime.run(agents, 39, run_id=run_id)
    repository = InMemoryRunStore()
    store = _InterruptOnAttention(repository)

    with pytest.raises(RuntimeError, match="durable attention plan"):
        await runtime.run(
            _agents(scenario, _idle),
            39,
            run_id=run_id,
            store=store,
        )
    recovery = repository.load_recovery(run_id)
    assert recovery is not None
    checkpoint = recovery.checkpoint
    assert all(cursor.active_attention is not None for cursor in checkpoint.cursors)
    review_wakes = tuple(
        event
        for event in checkpoint.scheduler.pending_events
        if event.kind is SystemEventKind.COMPANY_WAKE
        and WakeReason.REVIEW_DUE in event.wake_reasons
    )
    assert len(review_wakes) == len(scenario.companies)

    target_cursor = checkpoint.cursors[0]
    target_plan = target_cursor.active_attention
    assert target_plan is not None
    invalid_review = target_plan.armed_at.plus(scenario.runtime.max_review_minutes + 1)
    invalid_plan = target_plan.model_copy(update={"review_at": invalid_review})
    invalid_cursors = tuple(
        cursor.model_copy(update={"active_attention": invalid_plan})
        if cursor.company_id == target_cursor.company_id
        else cursor
        for cursor in checkpoint.cursors
    )
    invalid_events = tuple(
        event.model_copy(update={"at": invalid_review})
        if event.company_id == target_cursor.company_id
        and WakeReason.REVIEW_DUE in event.wake_reasons
        else event
        for event in checkpoint.scheduler.pending_events
    )
    invalid_checkpoint = checkpoint.model_copy(
        update={
            "cursors": invalid_cursors,
            "scheduler": checkpoint.scheduler.model_copy(update={"pending_events": invalid_events}),
        }
    )
    with pytest.raises(ValidationError, match="active attention must match"):
        RunRecovery(
            checkpoint=invalid_checkpoint,
            turns=recovery.turns,
            system_steps=recovery.system_steps,
        )

    resumed = await runtime.run(
        _agents(scenario, _idle),
        39,
        run_id=run_id,
        store=repository,
        recovery=recovery,
    )

    assert resumed.turns == expected.turns
    assert resumed.episode.events == expected.episode.events
    assert resumed.episode.snapshots == expected.episode.snapshots
    assert resumed.episode.score == expected.episode.score


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

    assert tuple(record.envelope.decision for record in replay.turns) == tuple(
        record.envelope.decision for record in source.turns
    )
    assert replay.episode.events == source.episode.events
    assert replay.episode.snapshots == source.episode.snapshots
    assert replay.episode.score == source.episode.score
    _assert_one_turn_per_company_minute(replay)
