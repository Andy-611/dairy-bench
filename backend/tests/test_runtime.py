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

from company_bench.agents.company import (
    BaselineCompanyAgent,
    CompanyAgent,
    ReplayCompanyAgent,
)
from company_bench.agents.contracts import ModelOutputError
from company_bench.domain.calendar import SimDay, Weekday
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
    SystemEventKind,
    SystemStepRecord,
    TurnRecord,
    WakeReason,
)
from company_bench.storage.store import InMemoryRunStore
from tests.support.fakes import company_decision

QUANTITY = Decimal("10")
FIRST_DAY = SimDay(absolute_day=1)
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
        """Return one scripted decision after an optional real delay."""
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
        """Stop after a pending economic commitment is durable."""
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
        """Stop after attention and its review wake are durable."""
        self.repository.save_progress(turns, system_steps, checkpoint)
        if not self.interrupted and any(
            cursor.active_attention is not None for cursor in checkpoint.cursors
        ):
            self.interrupted = True
            raise RuntimeError("interrupted after durable attention plan")


def _scenario(
    *,
    weeks: int = 1,
    max_turns: int = 6,
    company_ids: tuple[str, ...] = ("farm_a", "processor_a", "retailer_a"),
    bottled_farm: bool = False,
) -> ScenarioSpec:
    """Build a validated, short scenario for orchestration tests."""
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
    candidate = DAIRY_S9_SCENARIO.model_copy(
        update={
            "scenario_id": "test.runtime.weekly",
            "weeks": weeks,
            "companies": companies,
            "runtime": DAIRY_S9_SCENARIO.runtime.model_copy(
                update={"max_turns_per_company_week": max_turns}
            ),
        }
    )
    return ScenarioSpec.model_validate_json(candidate.model_dump_json())


def _agents(
    scenario: ScenarioSpec,
    decision: _Decision,
    delays: dict[str, float] | None = None,
) -> dict[str, CompanyAgent]:
    """Create one isolated scripted Agent per company."""
    delays = delays or {}
    return {
        company.company_id: _ScriptedAgent(
            decision,
            delay_seconds=delays.get(company.company_id, 0.0),
        )
        for company in scenario.companies
    }


def _baseline_agents(scenario: ScenarioSpec) -> dict[str, CompanyAgent]:
    """Create independent deterministic baseline Agents."""
    return {company.company_id: BaselineCompanyAgent() for company in scenario.companies}


def _idle(_: AgentTurn) -> CompanyDecision:
    return company_decision()


def _quote(
    side: MarketSide,
    product: ProductId,
    quantity: Decimal = QUANTITY,
    limit_price: Decimal = Decimal("1.50"),
) -> SetQuoteLadder:
    """Build one focused quote ladder."""
    return SetQuoteLadder(
        side=side,
        product=product,
        levels=(QuoteLevel(quantity=quantity, limit_price=limit_price),),
    )


def _seed_order(
    scenario: ScenarioSpec,
    seed: int,
    on: SimDay = FIRST_DAY,
) -> tuple[str, ...]:
    """Reproduce the runtime's deterministic same-day application order."""
    return tuple(
        sorted(
            (company.company_id for company in scenario.companies),
            key=lambda company_id: (
                hashlib.sha256(f"{seed}|{on.absolute_day}|{company_id}".encode()).digest(),
                company_id,
            ),
        )
    )


def _apply_projection(
    execution: EpisodeExecution,
    on: SimDay = FIRST_DAY,
) -> tuple[tuple[str, int], ...]:
    """Return company and apply sequence for one simulated day."""
    return tuple(
        (record.turn.company_id, record.outcome.apply_sequence)
        for record in execution.turns
        if record.turn.sim_day == on
    )


def _assert_one_turn_per_company_day(execution: EpisodeExecution) -> None:
    keys = tuple(
        (record.turn.company_id, record.turn.sim_day.absolute_day)
        for record in execution.turns
    )
    assert len(keys) == len(set(keys))


@pytest.mark.asyncio
async def test_runtime_orders_seven_days_and_sunday_settlement() -> None:
    scenario = _scenario(max_turns=1)
    repository = InMemoryRunStore()

    await EpisodeRuntime(scenario).run(
        _agents(scenario, _idle),
        7,
        run_id="calendar_order",
        store=repository,
    )

    steps = tuple(
        step
        for step in repository.list_system_steps("calendar_order")
        if step.kind
        not in {SystemEventKind.TURN_LIMIT_REACHED, SystemEventKind.AGENT_WAKE_SUPPRESSED}
    )
    assert tuple((step.kind, step.occurred_on.weekday) for step in steps) == (
        (SystemEventKind.WEEK_OPEN, Weekday.MONDAY),
        (SystemEventKind.DAY_STARTED, Weekday.TUESDAY),
        (SystemEventKind.DAY_STARTED, Weekday.WEDNESDAY),
        (SystemEventKind.DAY_STARTED, Weekday.THURSDAY),
        (SystemEventKind.DAY_STARTED, Weekday.FRIDAY),
        (SystemEventKind.DAY_STARTED, Weekday.SATURDAY),
        (SystemEventKind.DAY_STARTED, Weekday.SUNDAY),
        (SystemEventKind.MARKET_CLOSE, Weekday.SUNDAY),
        (SystemEventKind.CONSUMER_SALES, Weekday.SUNDAY),
        (SystemEventKind.WEEK_CLOSE, Weekday.SUNDAY),
    )


@pytest.mark.asyncio
async def test_one_week_has_six_decision_days_and_no_sunday_agent_turn() -> None:
    scenario = _scenario()
    execution = await EpisodeRuntime(scenario).run(
        _agents(scenario, _idle),
        8,
        run_id="weekly_turn_budget",
    )

    assert len(execution.turns) == len(scenario.companies) * 6
    assert {record.turn.sim_day.weekday for record in execution.turns} == set(
        tuple(Weekday)[:-1]
    )
    assert all(record.turn.turn_limit_this_week == 6 for record in execution.turns)
    _assert_one_turn_per_company_day(execution)


@pytest.mark.asyncio
async def test_canonical_horizon_has_52_weekly_snapshots_and_2808_baseline_turns() -> None:
    scenario = DAIRY_S9_SCENARIO
    execution = await EpisodeRuntime(scenario).run(
        _baseline_agents(scenario),
        42,
        run_id="full_year_regression",
    )

    assert len(execution.episode.snapshots) == 52
    assert tuple(snapshot.week for snapshot in execution.episode.snapshots) == tuple(
        range(1, 53)
    )
    assert len(execution.turns) == 52 * 6 * len(scenario.companies)
    assert all(record.turn.sim_day.is_decision_day for record in execution.turns)


@pytest.mark.asyncio
async def test_real_response_order_does_not_change_seeded_application_order() -> None:
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
        for sequence, company_id in enumerate(_seed_order(scenario, 17), start=1)
    )

    assert _apply_projection(forward) == _apply_projection(reverse) == expected


@pytest.mark.asyncio
async def test_a_different_seed_can_change_same_day_application_order() -> None:
    scenario = _scenario(max_turns=1)
    first_seed = 1
    first_order = _seed_order(scenario, first_seed)
    second_seed = next(
        seed for seed in range(2, 100) if _seed_order(scenario, seed) != first_order
    )
    runtime = EpisodeRuntime(scenario)

    first = await runtime.run(_agents(scenario, _idle), first_seed, run_id="seed_one")
    second = await runtime.run(_agents(scenario, _idle), second_seed, run_id="seed_two")

    assert tuple(company_id for company_id, _ in _apply_projection(first)) == first_order
    assert _apply_projection(first) != _apply_projection(second)


@pytest.mark.asyncio
async def test_operation_trade_and_delivery_events_reach_the_owning_agents() -> None:
    scenario = _scenario()
    execution = await EpisodeRuntime(scenario).run(
        _baseline_agents(scenario),
        13,
        run_id="completion_wakes",
    )

    assert any(isinstance(record.event, MilkProducedEvent) for record in execution.episode.events)
    assert any(isinstance(record.event, TradeExecutedEvent) for record in execution.episode.events)
    assert any(
        isinstance(record.event, DeliveryCompletedEvent) for record in execution.episode.events
    )
    assert any(
        isinstance(event, MilkProducedEvent)
        for record in execution.turns
        if record.turn.company_id.startswith("farm")
        for event in record.turn.visible_events
    )
    assert any(
        isinstance(event, DeliveryCompletedEvent)
        for record in execution.turns
        if record.turn.company_id.startswith("processor")
        for event in record.turn.visible_events
    )
    assert {
        record.turn.company_id
        for record in execution.turns
        if WakeReason.TRADE_EXECUTED in record.turn.wake_reasons
    } >= {"farm_a", "processor_a"}
    _assert_one_turn_per_company_day(execution)


@pytest.mark.asyncio
async def test_price_alert_uses_committed_book_and_wakes_once_next_day() -> None:
    scenario = _scenario(
        max_turns=3,
        company_ids=("farm_a", "processor_a"),
    )
    alert = QuoteAlert(
        product=ProductId.RAW_MILK,
        quote="best_bid",
        operator="at_least",
        price=Decimal("1.50"),
    )

    def decide(turn: AgentTurn) -> CompanyDecision:
        if WakeReason.WEEK_OPEN not in turn.wake_reasons:
            return company_decision()
        if turn.company_id == "farm_a":
            return IdleDecision(attention=AttentionPlan(alerts=(alert,)))
        return company_decision(
            _quote(MarketSide.BUY, ProductId.RAW_MILK),
        )

    execution = await EpisodeRuntime(scenario).run(
        _agents(scenario, decide),
        24,
        run_id="price_alert",
    )
    source = next(
        record
        for record in execution.turns
        if record.turn.company_id == "farm_a" and record.turn.sim_day.weekday is Weekday.MONDAY
    )
    alerted = tuple(
        record for record in execution.turns if WakeReason.PRICE_ALERT in record.turn.wake_reasons
    )

    assert len(alerted) == 1
    assert alerted[0].turn.company_id == "farm_a"
    assert alerted[0].turn.sim_day.weekday is Weekday.TUESDAY
    signal = next(
        signal for signal in alerted[0].turn.wake_signals if signal.reason is WakeReason.PRICE_ALERT
    )
    assert signal.source is not None and signal.source.entry_id == source.turn.turn_id
    assert len(signal.reference_ids) == 1


@pytest.mark.asyncio
async def test_protocol_rejection_retries_on_following_decision_days() -> None:
    scenario = _scenario(max_turns=3)
    observed: list[TurnRecord] = []

    def broken(_: AgentTurn) -> CompanyDecision:
        raise ModelOutputError("invalid decision")

    async def remember(record: TurnRecord) -> None:
        observed.append(record)

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
        records = tuple(
            record for record in observed if record.turn.company_id == company.company_id
        )
        assert tuple(record.turn.sim_day.weekday for record in records) == (
            Weekday.MONDAY,
            Weekday.TUESDAY,
            Weekday.WEDNESDAY,
        )
        assert all(
            record.outcome.reason == f"{PROTOCOL_ERROR_PREFIX}{record.protocol_error}"
            for record in records
        )
        assert all(right.turn.previous_outcome == left.outcome for left, right in pairwise(records))


@pytest.mark.asyncio
async def test_invalid_attention_is_rejected_without_changing_economy() -> None:
    scenario = _scenario(max_turns=2, company_ids=("farm_a",))

    def decide(turn: AgentTurn) -> CompanyDecision:
        return company_decision(
            review_after_days=(
                turn.observation.runtime.max_review_days + 1
                if WakeReason.WEEK_OPEN in turn.wake_reasons
                else None
            )
        )

    execution = await EpisodeRuntime(scenario).run(
        _agents(scenario, decide),
        30,
        run_id="invalid_attention",
    )
    first, correction = execution.turns

    assert not first.outcome.accepted
    assert first.outcome.reason == "attention review delay cannot exceed 2 days"
    assert first.outcome.resulting_state_version == first.turn.state_version
    assert correction.turn.sim_day.weekday is Weekday.TUESDAY
    assert correction.turn.wake_reasons == (WakeReason.DECISION_REJECTED,)
    assert correction.turn.previous_outcome == first.outcome


@pytest.mark.asyncio
async def test_saturday_trade_delivery_precedes_sunday_consumer_settlement() -> None:
    scenario = _scenario(
        company_ids=("farm_a", "retailer_a"),
        bottled_farm=True,
    )
    repository = InMemoryRunStore()

    def decide(turn: AgentTurn) -> CompanyDecision:
        weekday = turn.sim_day.weekday
        if turn.company_id == "farm_a":
            if weekday is Weekday.MONDAY:
                return company_decision(
                    Produce(product=ProductId.BOTTLED_MILK, quantity=QUANTITY)
                )
            if weekday is Weekday.SATURDAY:
                return company_decision(
                    _quote(
                        MarketSide.SELL,
                        ProductId.BOTTLED_MILK,
                        limit_price=Decimal("2.50"),
                    )
                )
        elif turn.company_id == "retailer_a":
            if weekday is Weekday.MONDAY:
                return company_decision(
                    SetRetailPrice(
                        product=ProductId.BOTTLED_MILK,
                        unit_price=Decimal("3.50"),
                    )
                )
            if weekday is Weekday.SATURDAY:
                return company_decision(
                    _quote(
                        MarketSide.BUY,
                        ProductId.BOTTLED_MILK,
                        limit_price=Decimal("3.00"),
                    )
                )
        return company_decision()

    execution = await EpisodeRuntime(scenario).run(
        _agents(scenario, decide),
        31,
        run_id="sunday_delivery",
        store=repository,
    )
    sunday_kinds = tuple(
        step.kind
        for step in repository.list_system_steps("sunday_delivery")
        if step.occurred_on.weekday is Weekday.SUNDAY
    )
    sale = next(
        record.event
        for record in execution.episode.events
        if isinstance(record.event, ConsumerSaleEvent)
        and record.event.company_id == "retailer_a"
    )

    assert sunday_kinds == (
        SystemEventKind.DAY_STARTED,
        SystemEventKind.DELIVERY_COMPLETED,
        SystemEventKind.MARKET_CLOSE,
        SystemEventKind.CONSUMER_SALES,
        SystemEventKind.WEEK_CLOSE,
    )
    assert sale.sold_quantity > 0


@pytest.mark.asyncio
async def test_turn_limit_audits_a_suppressed_completion_wake() -> None:
    scenario = _scenario(max_turns=1, company_ids=("farm_a",))
    repository = InMemoryRunStore()

    def produce(_: AgentTurn) -> CompanyDecision:
        return company_decision(
            Produce(product=ProductId.RAW_MILK, quantity=QUANTITY),
        )

    execution = await EpisodeRuntime(scenario).run(
        _agents(scenario, produce),
        8,
        run_id="turn_limit_audit",
        store=repository,
    )
    suppressed = tuple(
        step
        for step in repository.list_system_steps("turn_limit_audit")
        if step.kind is SystemEventKind.AGENT_WAKE_SUPPRESSED
    )

    assert len(execution.turns) == 1
    assert len(suppressed) == 1
    assert suppressed[0].company_id == "farm_a"
    assert suppressed[0].occurred_on.weekday is Weekday.WEDNESDAY
    assert tuple(signal.reason for signal in suppressed[0].suppressed_wake_signals) == (
        WakeReason.OPERATION_COMPLETED,
    )


@pytest.mark.parametrize(
    "completion_kind",
    (SystemEventKind.OPERATION_COMPLETED, SystemEventKind.DELIVERY_COMPLETED),
)
@pytest.mark.asyncio
async def test_checkpoint_resumes_each_commitment_exactly_once(
    completion_kind: SystemEventKind,
) -> None:
    scenario = _scenario()
    run_id = f"resume_{completion_kind.value}"
    runtime = EpisodeRuntime(scenario)
    expected = await runtime.run(
        _baseline_agents(scenario),
        37,
        run_id=run_id,
    )
    repository = InMemoryRunStore()
    store = _InterruptOnCommit(repository, completion_kind)

    with pytest.raises(RuntimeError, match="durable commitment"):
        await runtime.run(
            _baseline_agents(scenario),
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
    assert commitments
    commitment_ids = tuple(
        commitment.job_id
        if completion_kind is SystemEventKind.OPERATION_COMPLETED
        else commitment.delivery_id
        for commitment in commitments
    )
    completion_events = tuple(
        event for event in checkpoint.scheduler.pending_events if event.kind is completion_kind
    )
    assert tuple(event.reference_ids[0] for event in completion_events) == commitment_ids
    assert tuple(event.scheduled_for for event in completion_events) == tuple(
        commitment.completes_on
        if completion_kind is SystemEventKind.OPERATION_COMPLETED
        else commitment.arrives_on
        for commitment in commitments
    )

    resumed = await runtime.run(
        _baseline_agents(scenario),
        37,
        run_id=run_id,
        store=repository,
        recovery=recovery,
    )
    assert resumed.turns == expected.turns
    assert resumed.episode.events == expected.episode.events
    assert resumed.episode.snapshots == expected.episode.snapshots
    assert resumed.episode.score == expected.episode.score
    completed_steps = tuple(
        step
        for step in repository.list_system_steps(run_id)
        if step.kind is completion_kind
    )
    assert all(
        sum(commitment_id in step.reference_ids for step in completed_steps) == 1
        for commitment_id in commitment_ids
    )


@pytest.mark.asyncio
async def test_checkpoint_restores_attention_and_review_wake_exactly() -> None:
    scenario = _scenario(max_turns=3)
    run_id = "resume_attention"
    runtime = EpisodeRuntime(scenario)
    expected = await runtime.run(_agents(scenario, _idle), 39, run_id=run_id)
    repository = InMemoryRunStore()

    with pytest.raises(RuntimeError, match="durable attention plan"):
        await runtime.run(
            _agents(scenario, _idle),
            39,
            run_id=run_id,
            store=_InterruptOnAttention(repository),
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
    invalid_review = target_plan.armed_on.plus_days(3)
    invalid_plan = target_plan.model_copy(update={"review_on": invalid_review})
    invalid_cursors = tuple(
        cursor.model_copy(update={"active_attention": invalid_plan})
        if cursor.company_id == target_cursor.company_id
        else cursor
        for cursor in checkpoint.cursors
    )
    invalid_events = tuple(
        event.model_copy(update={"scheduled_for": invalid_review})
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
async def test_checkpoint_accepts_a_scoring_only_contract_upgrade() -> None:
    scenario = _scenario(max_turns=3)
    legacy_scenario = scenario.model_copy(
        update={
            "scoring": scenario.scoring.model_copy(
                update={"score_version": "s9-enterprise-v3"}
            )
        }
    )
    run_id = "resume_scoring_upgrade"
    repository = InMemoryRunStore()

    with pytest.raises(RuntimeError, match="durable attention plan"):
        await EpisodeRuntime(legacy_scenario).run(
            _agents(legacy_scenario, _idle),
            40,
            run_id=run_id,
            store=_InterruptOnAttention(repository),
        )
    recovery = repository.load_recovery(run_id)
    assert recovery is not None
    assert recovery.checkpoint.economy.scenario.scoring.score_version == "s9-enterprise-v3"

    resumed = await EpisodeRuntime(scenario).run(
        _agents(scenario, _idle),
        40,
        run_id=run_id,
        store=repository,
        recovery=recovery,
    )

    assert resumed.episode.scenario == scenario
    assert resumed.episode.score.score_version == "s9-enterprise-v4"


@pytest.mark.asyncio
async def test_turn_journal_replay_is_economically_deterministic() -> None:
    scenario = _scenario()
    runtime = EpisodeRuntime(scenario)
    source = await runtime.run(
        _baseline_agents(scenario),
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
    _assert_one_turn_per_company_day(replay)
