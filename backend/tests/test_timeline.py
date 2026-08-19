from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from company_bench.agents.company import (
    DECISION_PROMPT_VERSION,
    BaselineCompanyAgent,
    CompanyAgent,
    LlmCompanyAgent,
    ReplayCompanyAgent,
)
from company_bench.agents.contracts import DecisionModelRequest, DecisionModelResult
from company_bench.domain.calendar import Weekday
from company_bench.domain.models import (
    PolicyKind,
    PolicyMetadata,
    PolicyProfileId,
    ProductId,
    TradeExecutedEvent,
)
from company_bench.domain.scenario import DAIRY_S9_SCENARIO
from company_bench.runs.models import (
    InvocationOutcome,
    PolicyInvocation,
    RunCheckpoint,
    RunJob,
    RunStatus,
    TokenUsage,
)
from company_bench.runtime.episode import EpisodeExecution, EpisodeRuntime
from company_bench.runtime.models import (
    ActionDecision,
    AgentTurn,
    CompanyDecision,
    MarketSide,
    Produce,
    QuoteLevel,
    SetQuoteLadder,
    SystemStepRecord,
    TurnRecord,
)
from company_bench.storage.memory import InMemoryRunRepository
from company_bench.storage.sqlite import SQLiteRunRepository
from company_bench.timeline.market import MarketProjectionError, MarketTimelineProjector
from company_bench.timeline.models import (
    DecisionDispositionSource,
    MarketOrderCancelled,
    MarketOrderPlaced,
    MarketOrderPreserved,
    MarketOrderReplaced,
)
from company_bench.timeline.projector import RunTimelineProjector
from tests.support.fakes import FixedDecisionAgent, company_decision


class _JobHidingRunRepository(InMemoryRunRepository):
    """Hide selected lifecycle records while retaining their persisted data."""

    def __init__(self) -> None:
        super().__init__()
        self._hidden_job_ids: set[str] = set()

    def hide_job(self, run_id: str) -> None:
        self._hidden_job_ids.add(run_id)

    def get_job(self, run_id: str) -> RunJob | None:
        if run_id in self._hidden_job_ids:
            return None
        return super().get_job(run_id)


def _complete(
    repository: InMemoryRunRepository,
    execution: EpisodeExecution,
    *,
    mode: PolicyKind,
    source_run_id: str | None = None,
) -> None:
    result = execution.episode
    profile_id = {
        PolicyKind.BASELINE: PolicyProfileId.BASELINE,
        PolicyKind.MODEL: PolicyProfileId.NEWAPI_MODEL,
        PolicyKind.REPLAY: PolicyProfileId.REPLAY,
    }[mode]
    repository.complete_job(
        result,
        RunJob(
            run_id=result.run_id,
            profile_id=profile_id,
            kind=mode,
            provider="newapi" if mode is PolicyKind.MODEL else None,
            model="test-model" if mode is PolicyKind.MODEL else None,
            wire_protocol=("chat_completions" if mode is PolicyKind.MODEL else None),
            adapter_version=("newapi-chat-completions-v1" if mode is PolicyKind.MODEL else None),
            prompt_version=(DECISION_PROMPT_VERSION if mode is PolicyKind.MODEL else None),
            config_fingerprint="test-config" if mode is PolicyKind.MODEL else None,
            status=RunStatus.COMPLETED,
            seed=result.seed,
            source_run_id=source_run_id,
            scenario_id=result.scenario.scenario_id,
            current_absolute_day=result.scenario.calendar.total_days,
            total_weeks=result.scenario.weeks,
            submitted_at=result.started_at,
            started_at=result.started_at,
            finished_at=result.finished_at,
            quality=result.quality,
        ),
    )


def _ladder(
    side: MarketSide,
    product: ProductId,
    *levels: tuple[str, str],
) -> SetQuoteLadder:
    """Build one concise target ladder from quantity-price text pairs."""
    return SetQuoteLadder(
        side=side,
        product=product,
        levels=tuple(
            QuoteLevel(quantity=Decimal(quantity), limit_price=Decimal(price))
            for quantity, price in levels
        ),
    )


class _AuditedUnauthorizedGateway:
    """Return one role-invalid ladder with nonzero provider audit metadata."""

    async def generate_decision(self, _: DecisionModelRequest) -> DecisionModelResult:
        """Return a schema-valid decision that the economy must reject."""
        return DecisionModelResult(
            decision=company_decision(
                _ladder(
                    MarketSide.BUY,
                    ProductId.RAW_MILK,
                    ("10", "1.40"),
                ),
                review_after_days=1,
            ),
            provider="scripted",
            model="rejection-model",
            response_id="response_rejected",
            request_id="request_rejected",
            usage=TokenUsage(input_tokens=3, output_tokens=2, total_tokens=5),
            attempts=2,
            latency_ms=17,
        )

    async def close(self) -> None:
        """Release no resources."""


class _MarketTimelineAgent:
    """Create multi-flow ladder changes for projection tests."""

    metadata = PolicyMetadata(
        name="market-timeline",
        kind=PolicyKind.BASELINE,
        profile_id=PolicyProfileId.BASELINE,
    )

    async def act(self, turn: AgentTurn) -> CompanyDecision:
        """Return one deterministic decision from the current simulated day."""
        weekday = turn.sim_day.weekday
        if turn.company_id == "processor_a" and weekday is Weekday.MONDAY:
            return company_decision(
                _ladder(
                    MarketSide.BUY,
                    ProductId.RAW_MILK,
                    ("40", "1.66"),
                ),
                review_after_days=1,
            )
        if turn.company_id != "farm_a":
            return company_decision()
        if weekday is Weekday.MONDAY:
            return company_decision(
                Produce(
                    product=ProductId.RAW_MILK,
                    quantity=Decimal("60"),
                ),
                review_after_days=1,
            )
        if weekday is Weekday.TUESDAY:
            return company_decision(
                _ladder(
                    MarketSide.SELL,
                    ProductId.RAW_MILK,
                    ("50", "1.65"),
                    ("10", "1.80"),
                ),
                review_after_days=1,
            )
        if weekday is Weekday.WEDNESDAY and turn.open_orders:
            return company_decision(
                _ladder(
                    MarketSide.SELL,
                    ProductId.RAW_MILK,
                    ("10", "1.65"),
                    ("10", "1.75"),
                ),
                review_after_days=1,
            )
        if weekday is Weekday.THURSDAY and turn.open_orders:
            return company_decision(
                _ladder(MarketSide.SELL, ProductId.RAW_MILK),
                review_after_days=1,
            )
        return company_decision()


class _TwoBidMarketTimelineAgent(_MarketTimelineAgent):
    """Add a second same-price processor bid for priority-corruption tests."""

    async def act(self, turn: AgentTurn) -> CompanyDecision:
        if turn.company_id.startswith("processor_") and turn.sim_day.weekday is Weekday.MONDAY:
            return company_decision(
                _ladder(
                    MarketSide.BUY,
                    ProductId.RAW_MILK,
                    ("40", "1.66"),
                ),
                review_after_days=1,
            )
        return await super().act(turn)


class _ThreeFillMarketTimelineAgent:
    """Execute three target levels independently in one ladder command."""

    metadata = PolicyMetadata(
        name="three-fill-timeline",
        kind=PolicyKind.BASELINE,
        profile_id=PolicyProfileId.BASELINE,
    )

    async def act(self, turn: AgentTurn) -> CompanyDecision:
        if turn.company_id == "processor_a" and turn.sim_day.weekday is Weekday.MONDAY:
            return company_decision(
                _ladder(
                    MarketSide.BUY,
                    ProductId.RAW_MILK,
                    ("10", "1.70"),
                    ("10", "1.60"),
                    ("10", "1.50"),
                ),
                review_after_days=1,
            )
        if turn.company_id != "farm_a":
            return company_decision()
        if turn.sim_day.weekday is Weekday.MONDAY:
            return company_decision(
                Produce(product=ProductId.RAW_MILK, quantity=Decimal("30")),
                review_after_days=1,
            )
        if turn.sim_day.weekday is Weekday.TUESDAY:
            return company_decision(
                _ladder(
                    MarketSide.SELL,
                    ProductId.RAW_MILK,
                    ("10", "1.40"),
                    ("10", "1.45"),
                    ("10", "1.50"),
                ),
                review_after_days=1,
            )
        return company_decision()


@pytest.mark.asyncio
async def test_timeline_projects_system_steps_turns_and_typed_state_changes() -> None:
    scenario = DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1})
    repository = InMemoryRunRepository()
    execution = await EpisodeRuntime(scenario).run(
        {company.company_id: BaselineCompanyAgent() for company in scenario.companies},
        42,
        run_id="timeline_baseline",
        store=repository,
    )
    _complete(repository, execution, mode=PolicyKind.BASELINE)

    page = RunTimelineProjector(repository).read_week(execution.episode.run_id, 1)

    assert page.context.model_call_count == 0
    assert page.context.current_usage == TokenUsage()
    diagnostics = page.context.diagnostics
    assert diagnostics.completed_weeks == 1
    assert diagnostics.benchmark_eligible
    assert diagnostics.protocol_invalid_turns == 0
    assert diagnostics.trade_count == sum(
        isinstance(record.event, TradeExecutedEvent) for record in execution.episode.events
    )
    assert diagnostics.consumer_demand == execution.episode.snapshots[0].consumer_demand
    assert diagnostics.consumer_sales == execution.episode.snapshots[0].consumer_sales
    steps = tuple(step for moment in page.days for step in moment.system_steps)
    assert {step.kind.value for step in steps}.issuperset(
        {"week_open", "day_started", "market_close", "consumer_sales", "week_close"}
    )
    assert tuple(day.sim_day.weekday for day in page.days) == tuple(Weekday)
    assert page.week_summaries[0].week == 1
    assert sum(len(moment.turns) for moment in page.days) == len(execution.turns)
    assert any(turn.state_changes for moment in page.days for turn in moment.turns)
    assert all(
        turn.observation.visible_event_count == len(turn.observation.visible_events)
        for moment in page.days
        for turn in moment.turns
    )
    assert all(
        turn.observation.cash >= 0 and turn.observation.reserved_cash >= 0
        for moment in page.days
        for turn in moment.turns
    )
    assert all(
        turn.disposition_source is DecisionDispositionSource.ECONOMIC_ENGINE
        for moment in page.days
        for turn in moment.turns
        if turn.outcome.accepted
    )
    close = next(
        step for moment in page.days for step in moment.system_steps if step.kind == "week_close"
    )
    assert close.state_version_after >= close.state_version_before

    projector = RunTimelineProjector(repository)
    turn_detail = projector.read_detail(
        execution.episode.run_id,
        execution.turns[0].turn.turn_id,
    )
    system_record = next(
        step
        for step in repository.list_system_steps(execution.episode.run_id)
        if step.entry_id == close.entry_id
    )
    system_detail = projector.read_detail(execution.episode.run_id, close.entry_id)

    assert turn_detail.turn == execution.turns[0]
    assert turn_detail.system_step is None
    assert system_detail.turn is None
    assert system_detail.system_step == system_record


@pytest.mark.asyncio
async def test_timeline_identifies_runtime_attention_rejection() -> None:
    scenario = DAIRY_S9_SCENARIO.model_copy(
        update={
            "weeks": 1,
            "runtime": DAIRY_S9_SCENARIO.runtime.model_copy(
                update={"max_turns_per_company_week": 1}
            ),
        }
    )
    agents = {
        company.company_id: FixedDecisionAgent(
            (company_decision(review_after_days=scenario.runtime.max_review_days + 1),)
            if company.company_id == "farm_a"
            else (company_decision(),)
        )
        for company in scenario.companies
    }
    repository = InMemoryRunRepository()
    execution = await EpisodeRuntime(scenario).run(
        agents,
        42,
        run_id="timeline_attention_rejection",
        store=repository,
    )
    _complete(repository, execution, mode=PolicyKind.BASELINE)

    page = RunTimelineProjector(repository).read_week(execution.episode.run_id, 1)
    farm_turn = next(
        turn for moment in page.days for turn in moment.turns if turn.company_id == "farm_a"
    )

    assert not farm_turn.outcome.accepted
    assert farm_turn.disposition_source is DecisionDispositionSource.RUNTIME_ATTENTION


@pytest.mark.asyncio
async def test_market_timeline_projects_trade_tape_and_end_state_bid_ask_book() -> None:
    companies = tuple(
        DAIRY_S9_SCENARIO.company(company_id) for company_id in ("farm_a", "processor_a")
    )
    scenario = DAIRY_S9_SCENARIO.model_copy(
        update={
            "scenario_id": "timeline.market.current",
            "weeks": 1,
            "companies": companies,
        }
    )
    repository = InMemoryRunRepository()
    execution = await EpisodeRuntime(scenario).run(
        {company.company_id: _MarketTimelineAgent() for company in companies},
        23,
        run_id="timeline_market",
        store=repository,
    )
    _complete(repository, execution, mode=PolicyKind.BASELINE)

    page = RunTimelineProjector(repository).read_week(execution.episode.run_id, 1)
    frames = {day.sim_day.weekday: day.market for day in page.days}
    raw_at_open = next(
        book
        for book in frames[Weekday.MONDAY].closing_order_books
        if book.product is ProductId.RAW_MILK
    )
    raw_after_fill = next(
        book
        for book in frames[Weekday.TUESDAY].closing_order_books
        if book.product is ProductId.RAW_MILK
    )

    assert raw_at_open.best_bid == Decimal("1.66")
    assert raw_at_open.bids[0].size == Decimal("40")
    assert raw_at_open.bids[0].orders[0].owner_id == "processor_a"
    open_flow = frames[Weekday.MONDAY].order_flow
    assert len(open_flow) == 1
    assert isinstance(open_flow[0], MarketOrderPlaced)
    assert open_flow[0].incoming_order.owner_id == "processor_a"
    assert open_flow[0].matched_quantity == 0
    assert open_flow[0].remaining_quantity == Decimal("40")

    fill_frame = frames[Weekday.TUESDAY]
    assert len(fill_frame.order_flow) == 2
    assert len({flow.apply_sequence for flow in fill_frame.order_flow}) == 1
    fill_flow = fill_frame.order_flow[0]
    assert isinstance(fill_flow, MarketOrderPlaced)
    assert fill_flow.incoming_order.owner_id == "farm_a"
    assert fill_flow.incoming_order.remaining_quantity == Decimal("50")
    assert fill_flow.matched_quantity == Decimal("40")
    assert fill_flow.remaining_quantity == Decimal("10")
    assert len(fill_flow.matches) == 1
    assert fill_flow.matches[0].maker_order.owner_id == "processor_a"
    assert fill_flow.matches[0].quantity == Decimal("40")
    assert tuple(trade.quantity for trade in fill_frame.trades) == (Decimal("40"),)
    assert fill_frame.trades[0].maker_order_id == fill_flow.matches[0].maker_order.order_id
    assert fill_frame.trades[0].taker_order_id == fill_flow.incoming_order.order_id
    assert fill_frame.trades[0].arrives_on.weekday is Weekday.WEDNESDAY
    passive_flow = fill_frame.order_flow[1]
    assert isinstance(passive_flow, MarketOrderPlaced)
    assert passive_flow.incoming_order.limit_price == Decimal("1.80")
    assert passive_flow.matched_quantity == 0
    assert passive_flow.remaining_quantity == Decimal("10")
    assert raw_after_fill.last_trade_price == Decimal("1.66")
    assert raw_after_fill.best_bid is None
    assert raw_after_fill.best_ask == Decimal("1.65")
    assert raw_after_fill.asks[0].size == Decimal("10")
    assert tuple(level.unit_price for level in raw_after_fill.asks) == (
        Decimal("1.65"),
        Decimal("1.80"),
    )
    preserved_order_id = raw_after_fill.asks[0].orders[0].order_id
    replaced_order_id = raw_after_fill.asks[1].orders[0].order_id

    raw_after_replace = next(
        book
        for book in frames[Weekday.WEDNESDAY].closing_order_books
        if book.product is ProductId.RAW_MILK
    )
    replace_flow = frames[Weekday.WEDNESDAY].order_flow
    assert len(replace_flow) == 2
    assert len({flow.apply_sequence for flow in replace_flow}) == 1
    assert isinstance(replace_flow[0], MarketOrderPreserved)
    assert replace_flow[0].preserved_order.order_id == preserved_order_id
    assert isinstance(replace_flow[1], MarketOrderReplaced)
    assert replace_flow[1].replaced_order.order_id == replaced_order_id
    assert tuple(level.unit_price for level in raw_after_replace.asks) == (
        Decimal("1.65"),
        Decimal("1.75"),
    )
    assert raw_after_replace.asks[0].orders[0].order_id == preserved_order_id
    assert raw_after_replace.asks[1].orders[0].order_id != replaced_order_id

    raw_after_cancel = next(
        book
        for book in frames[Weekday.THURSDAY].closing_order_books
        if book.product is ProductId.RAW_MILK
    )
    cancel_flow = frames[Weekday.THURSDAY].order_flow
    assert len(cancel_flow) == 2
    assert len({flow.apply_sequence for flow in cancel_flow}) == 1
    assert all(isinstance(flow, MarketOrderCancelled) for flow in cancel_flow)
    assert {flow.cancelled_order.order_id for flow in cancel_flow} == {
        level.orders[0].order_id for level in raw_after_replace.asks
    }
    assert raw_after_cancel.bids == ()
    assert raw_after_cancel.asks == ()

    cancel_day = 4
    partial_projection = MarketTimelineProjector().project_week(
        scenario,
        1,
        repository.list_turns(execution.episode.run_id),
        repository.list_system_steps(execution.episode.run_id),
        (cancel_day,),
    )
    partial_raw_book = next(
        book
        for book in partial_projection[cancel_day].closing_order_books
        if book.product is ProductId.RAW_MILK
    )
    assert partial_raw_book.bids == ()
    assert partial_raw_book.asks == ()


@pytest.mark.asyncio
async def test_market_timeline_replays_three_independently_filled_ladder_levels() -> None:
    companies = tuple(
        DAIRY_S9_SCENARIO.company(company_id) for company_id in ("farm_a", "processor_a")
    )
    scenario = DAIRY_S9_SCENARIO.model_copy(
        update={
            "scenario_id": "timeline.market.three-fill.current",
            "weeks": 1,
            "companies": companies,
        }
    )
    repository = InMemoryRunRepository()
    execution = await EpisodeRuntime(scenario).run(
        {company.company_id: _ThreeFillMarketTimelineAgent() for company in companies},
        31,
        run_id="timeline_three_fill",
        store=repository,
    )
    _complete(repository, execution, mode=PolicyKind.BASELINE)

    fill_day = 2
    ladder_record = next(
        record
        for record in execution.turns
        if record.turn.company_id == "farm_a" and record.turn.sim_day.absolute_day == fill_day
    )
    result = ladder_record.outcome.quote_ladder_result
    assert result is not None
    assert len(result.levels) == 3
    assert {level.action.value for level in result.levels} == {"place"}
    assert {level.remaining_quantity for level in result.levels} == {Decimal()}
    assert (
        len(
            tuple(
                event
                for event in ladder_record.outcome.events
                if isinstance(event, TradeExecutedEvent)
            )
        )
        == 3
    )

    page = RunTimelineProjector(repository).read_week(execution.episode.run_id, 1)
    fill_frame = next(day.market for day in page.days if day.sim_day.absolute_day == fill_day)
    assert len(fill_frame.order_flow) == 3
    assert all(isinstance(flow, MarketOrderPlaced) for flow in fill_frame.order_flow)
    assert {flow.apply_sequence for flow in fill_frame.order_flow} == {
        ladder_record.outcome.apply_sequence
    }
    assert tuple(flow.incoming_order.limit_price for flow in fill_frame.order_flow) == (
        Decimal("1.40"),
        Decimal("1.45"),
        Decimal("1.50"),
    )
    assert tuple(flow.matched_quantity for flow in fill_frame.order_flow) == (
        Decimal("10"),
        Decimal("10"),
        Decimal("10"),
    )
    assert all(len(flow.matches) == 1 for flow in fill_frame.order_flow)
    assert tuple(trade.unit_price for trade in fill_frame.trades) == (
        Decimal("1.70"),
        Decimal("1.60"),
        Decimal("1.50"),
    )
    assert len({trade.trade_id for trade in fill_frame.trades}) == 3
    raw_book = next(
        book for book in fill_frame.closing_order_books if book.product is ProductId.RAW_MILK
    )
    assert raw_book.bids == ()
    assert raw_book.asks == ()

    replayed = MarketTimelineProjector().project_week(
        scenario,
        1,
        repository.list_turns(execution.episode.run_id),
        repository.list_system_steps(execution.episode.run_id),
        (fill_day,),
    )
    assert replayed[fill_day] == fill_frame


@pytest.mark.asyncio
async def test_market_timeline_rejects_a_maker_that_skips_fifo_priority() -> None:
    companies = tuple(
        DAIRY_S9_SCENARIO.company(company_id)
        for company_id in ("farm_a", "processor_a", "processor_b")
    )
    scenario = DAIRY_S9_SCENARIO.model_copy(
        update={
            "scenario_id": "timeline.market.priority.current",
            "weeks": 1,
            "companies": companies,
        }
    )
    repository = InMemoryRunRepository()
    execution = await EpisodeRuntime(scenario).run(
        {company.company_id: _TwoBidMarketTimelineAgent() for company in companies},
        23,
        run_id="timeline_market_priority",
        store=repository,
    )
    system_steps = repository.list_system_steps(execution.episode.run_id)
    frames = MarketTimelineProjector().project_week(
        scenario,
        1,
        execution.turns,
        system_steps,
        (1, 2),
    )
    raw_at_open = next(
        book for book in frames[1].closing_order_books if book.product is ProductId.RAW_MILK
    )
    raw_after_fill = next(
        book for book in frames[2].closing_order_books if book.product is ProductId.RAW_MILK
    )
    assert tuple(order.queue_ahead_quantity for order in raw_at_open.bids[0].orders) == (
        Decimal("0"),
        Decimal("40"),
    )
    assert raw_after_fill.bids[0].orders[0].remaining_quantity == Decimal("30")
    assert raw_after_fill.bids[0].orders[0].queue_ahead_quantity == Decimal("0")

    trade_record = next(
        record
        for record in execution.turns
        if len(
            tuple(event for event in record.outcome.events if isinstance(event, TradeExecutedEvent))
        )
        == 2
    )
    first, second = (
        event for event in trade_record.outcome.events if isinstance(event, TradeExecutedEvent)
    )
    corrupted_first = first.model_copy(
        update={
            "maker_order_id": second.maker_order_id,
            "buyer_id": second.buyer_id,
        }
    )
    corrupted_record = trade_record.model_copy(
        update={
            "outcome": trade_record.outcome.model_copy(update={"events": (corrupted_first, second)})
        }
    )
    corrupted_turns = tuple(
        corrupted_record if record.turn.turn_id == trade_record.turn.turn_id else record
        for record in execution.turns
    )

    with pytest.raises(MarketProjectionError, match="price-time maker priority"):
        MarketTimelineProjector().project_week(
            scenario,
            1,
            corrupted_turns,
            system_steps,
            (2,),
        )


@pytest.mark.asyncio
async def test_replay_timeline_resolves_source_turn_and_all_physical_calls() -> None:
    scenario = DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1})
    repository = _JobHidingRunRepository()
    runtime = EpisodeRuntime(scenario)
    source = await runtime.run(
        {company.company_id: BaselineCompanyAgent() for company in scenario.companies},
        9,
        run_id="trace_source",
        store=repository,
    )
    _complete(repository, source, mode=PolicyKind.MODEL)
    source_turn = source.turns[0]
    started_at = datetime.now(UTC)
    for index, applied in enumerate((False, True), start=1):
        repository.record_invocation(
            PolicyInvocation(
                invocation_id=f"inv_{index}",
                run_id=source.episode.run_id,
                company_id=source_turn.turn.company_id,
                week=source_turn.turn.sim_day.week,
                observation=source_turn.turn.observation,
                profile_id=PolicyProfileId.NEWAPI_MODEL,
                provider="newapi",
                model="test-model",
                wire_protocol="chat_completions",
                adapter_version="newapi-chat-completions-v1",
                config_fingerprint="test-config",
                prompt_version="current",
                prompt_hash=f"hash_{index}",
                started_at=started_at,
                finished_at=started_at,
                outcome=InvocationOutcome.SUCCESS,
                domain_turn_id=source_turn.turn.turn_id,
                absolute_day=source_turn.turn.sim_day.absolute_day,
                state_version=source_turn.turn.state_version,
                apply_sequence=(source_turn.outcome.apply_sequence if applied else None),
                decision=source_turn.envelope.decision,
                decision_outcome=source_turn.outcome if applied else None,
                usage=TokenUsage(input_tokens=index, total_tokens=index),
            )
        )

    replay = await runtime.run(
        {
            company.company_id: ReplayCompanyAgent(
                company.company_id,
                source.turns,
            )
            for company in scenario.companies
        },
        9,
        run_id="trace_replay",
        store=repository,
        replay_source=source.episode,
    )
    _complete(
        repository,
        replay,
        mode=PolicyKind.REPLAY,
        source_run_id=source.episode.run_id,
    )

    projector = RunTimelineProjector(repository)
    page = projector.read_week(replay.episode.run_id, 1)
    replay_turn_id = next(
        record.turn.turn_id
        for record in replay.turns
        if record.replay_origin is not None
        and record.replay_origin.source_turn_id == source_turn.turn.turn_id
    )
    detail = projector.read_detail(replay.episode.run_id, replay_turn_id)

    assert page.context.model_call_count == 0
    assert page.context.source_model_call_count == 2
    assert page.context.current_usage.total_tokens == 0
    assert page.context.source_usage.total_tokens == 3
    assert detail.item.replay_origin is not None
    assert detail.item.replay_origin.source_turn_id == source_turn.turn.turn_id
    assert len(detail.traces) == 2
    assert {trace.preview.profile_id for trace in detail.traces} == {PolicyProfileId.NEWAPI_MODEL}
    assert sum(trace.preview.applied_to_committed_turn for trace in detail.traces) == 1

    repository.hide_job(source.episode.run_id)
    with pytest.raises(ValueError, match=r"replay source .* missing its RunJob"):
        projector.read_week(replay.episode.run_id, 1)


@pytest.mark.asyncio
async def test_timeline_rejects_completed_result_without_run_job() -> None:
    scenario = DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1})
    repository = _JobHidingRunRepository()
    execution = await EpisodeRuntime(scenario).run(
        {company.company_id: BaselineCompanyAgent() for company in scenario.companies},
        10,
        run_id="orphan_timeline_result",
        store=repository,
    )
    _complete(repository, execution, mode=PolicyKind.BASELINE)
    repository.hide_job(execution.episode.run_id)

    with pytest.raises(ValueError, match="data is missing its RunJob"):
        RunTimelineProjector(repository).read_week(execution.episode.run_id, 1)


@pytest.mark.asyncio
async def test_timeline_rejects_checkpoint_without_run_job() -> None:
    scenario = DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1})
    repository = InMemoryRunRepository()
    execution = await EpisodeRuntime(scenario).run(
        {company.company_id: BaselineCompanyAgent() for company in scenario.companies},
        11,
        run_id="orphan_timeline_checkpoint",
        store=repository,
    )

    with pytest.raises(ValueError, match="data is missing its RunJob"):
        RunTimelineProjector(repository).read_week(execution.episode.run_id, 1)


@pytest.mark.asyncio
async def test_sqlite_rolls_back_system_step_when_checkpoint_validation_fails(
    tmp_path: Path,
) -> None:
    scenario = DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1})
    memory = InMemoryRunRepository()
    await EpisodeRuntime(scenario).run(
        {company.company_id: BaselineCompanyAgent() for company in scenario.companies},
        12,
        run_id="atomic_source",
        store=memory,
    )
    recovery = memory.load_recovery("atomic_source")
    assert recovery is not None
    checkpoint = recovery.checkpoint

    with SQLiteRunRepository(tmp_path / "atomic.sqlite3") as repository:
        with pytest.raises(ValueError, match="follow completed company turns"):
            repository.save_progress((), (recovery.system_steps[0],), checkpoint)
        assert repository.list_system_steps(checkpoint.run_id) == ()
        assert repository.load_recovery(checkpoint.run_id) is None


@pytest.mark.asyncio
async def test_timeline_does_not_invent_unpersisted_system_steps() -> None:
    scenario = DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1})
    durable = InMemoryRunRepository()
    execution = await EpisodeRuntime(scenario).run(
        {company.company_id: BaselineCompanyAgent() for company in scenario.companies},
        4,
        run_id="persisted_timeline",
        store=durable,
    )
    recovery = durable.load_recovery(execution.episode.run_id)
    assert recovery is not None

    journal_only = InMemoryRunRepository()
    turns = tuple(
        record.model_copy(update={"journal_sequence": sequence})
        for sequence, record in enumerate(execution.turns, start=1)
    )
    final_step = recovery.system_steps[-1].model_copy(update={"journal_sequence": len(turns) + 1})
    journal_only.save_progress(turns, (final_step,), recovery.checkpoint)
    _complete(journal_only, execution, mode=PolicyKind.BASELINE)

    page = RunTimelineProjector(journal_only).read_week(execution.episode.run_id, 1)
    steps = tuple(step for moment in page.days for step in moment.system_steps)
    assert len(steps) == 1
    assert steps[0].kind == "week_close"


class _StopAfterFirstProgress:
    def __init__(self, repository: InMemoryRunRepository) -> None:
        self.repository = repository

    def save_progress(
        self,
        turns: tuple[TurnRecord, ...],
        system_steps: tuple[SystemStepRecord, ...],
        checkpoint: RunCheckpoint,
    ) -> None:
        self.repository.save_progress(turns, system_steps, checkpoint)
        if turns or system_steps:
            raise RuntimeError("stop after first progress")


@pytest.mark.asyncio
async def test_failed_run_timeline_preserves_rejected_ladder_and_provider_audit() -> None:
    scenario = DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1})
    repository = InMemoryRunRepository()
    run_id = "failed_ladder_history"
    agents: dict[str, CompanyAgent] = {
        company.company_id: FixedDecisionAgent((company_decision(),))
        for company in scenario.companies
    }
    agents["farm_a"] = LlmCompanyAgent(
        run_id=run_id,
        company_id="farm_a",
        gateway=_AuditedUnauthorizedGateway(),
        audit_sink=repository,
        metadata=PolicyMetadata(
            name="audited-unauthorized-ladder",
            kind=PolicyKind.MODEL,
            profile_id=PolicyProfileId.NEWAPI_MODEL,
            provider="scripted",
            model="rejection-model",
            wire_protocol="scripted-tools",
            adapter_version="scripted-v1",
            prompt_version=DECISION_PROMPT_VERSION,
            config_fingerprint="scripted-config",
        ),
    )

    with pytest.raises(RuntimeError, match="first progress"):
        await EpisodeRuntime(scenario).run(
            agents,
            17,
            run_id=run_id,
            store=_StopAfterFirstProgress(repository),
        )
    recovery = repository.load_recovery(run_id)
    assert recovery is not None
    checkpoint = recovery.checkpoint
    repository.save_job(
        RunJob(
            run_id=run_id,
            profile_id=PolicyProfileId.NEWAPI_MODEL,
            kind=PolicyKind.MODEL,
            provider="scripted",
            model="rejection-model",
            wire_protocol="scripted-tools",
            adapter_version="scripted-v1",
            prompt_version=DECISION_PROMPT_VERSION,
            config_fingerprint="scripted-config",
            status=RunStatus.FAILED,
            seed=17,
            scenario_id=scenario.scenario_id,
            total_weeks=scenario.weeks,
            submitted_at=checkpoint.episode_started_at,
            started_at=checkpoint.episode_started_at,
            finished_at=datetime.now(UTC),
            error_message="synthetic failure after durable progress",
        )
    )

    page = RunTimelineProjector(repository).read_week(run_id, 1)
    turn = next(
        turn for moment in page.days for turn in moment.turns if turn.company_id == "farm_a"
    )
    invocation = repository.list_invocations(run_id)[0]

    assert isinstance(turn.decision, ActionDecision)
    assert turn.decision == company_decision(
        _ladder(
            MarketSide.BUY,
            ProductId.RAW_MILK,
            ("10", "1.40"),
        ),
        review_after_days=1,
    )
    assert not turn.outcome.accepted
    assert turn.disposition_source is DecisionDispositionSource.ECONOMIC_ENGINE
    assert "not authorized" in turn.outcome.reason
    assert turn.protocol_error is None
    assert invocation.decision == turn.decision
    assert invocation.decision_outcome == turn.outcome
    assert invocation.response_id == "response_rejected"
    assert invocation.request_id == "request_rejected"
    assert invocation.usage.total_tokens == 5
    assert invocation.attempts == 2
    assert invocation.latency_ms == 17
    assert page.context.checkpoint_on == checkpoint.scheduler.today
    assert page.context.checkpoint_state_version == checkpoint.economy.state_version


@pytest.mark.asyncio
async def test_running_replay_timeline_accepts_a_valid_source_prefix() -> None:
    scenario = DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1})
    repository = InMemoryRunRepository()
    runtime = EpisodeRuntime(scenario)
    source = await runtime.run(
        {company.company_id: BaselineCompanyAgent() for company in scenario.companies},
        7,
        run_id="prefix_source",
        store=repository,
    )
    _complete(repository, source, mode=PolicyKind.BASELINE)

    with pytest.raises(RuntimeError, match="first progress"):
        await runtime.run(
            {
                company.company_id: ReplayCompanyAgent(
                    company.company_id,
                    source.turns,
                )
                for company in scenario.companies
            },
            7,
            run_id="prefix_replay",
            store=_StopAfterFirstProgress(repository),
            replay_source=source.episode,
        )
    recovery = repository.load_recovery("prefix_replay")
    assert recovery is not None
    checkpoint = recovery.checkpoint
    repository.save_job(
        RunJob(
            run_id="prefix_replay",
            profile_id=PolicyProfileId.REPLAY,
            kind=PolicyKind.REPLAY,
            status=RunStatus.RUNNING,
            seed=7,
            source_run_id=source.episode.run_id,
            scenario_id=scenario.scenario_id,
            total_weeks=1,
            submitted_at=checkpoint.episode_started_at,
            started_at=checkpoint.episode_started_at,
        )
    )

    page = RunTimelineProjector(repository).read_week("prefix_replay", 1)
    assert page.context.replay is True
    assert page.context.checkpoint_on == checkpoint.scheduler.today
    assert page.context.checkpoint_state_version == checkpoint.economy.state_version
    assert sum(len(moment.turns) for moment in page.days) == len(recovery.turns)
    assert all(turn.replay_origin is not None for moment in page.days for turn in moment.turns)


@pytest.mark.asyncio
async def test_replay_of_replay_resolves_the_ultimate_trace_run() -> None:
    scenario = DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1})
    repository = InMemoryRunRepository()
    runtime = EpisodeRuntime(scenario)
    source = await runtime.run(
        {company.company_id: BaselineCompanyAgent() for company in scenario.companies},
        8,
        run_id="ultimate_source",
        store=repository,
    )
    _complete(repository, source, mode=PolicyKind.MODEL)

    first = await runtime.run(
        {
            company.company_id: ReplayCompanyAgent(
                company.company_id,
                source.turns,
            )
            for company in scenario.companies
        },
        8,
        run_id="first_replay",
        store=repository,
        replay_source=source.episode,
    )
    _complete(
        repository,
        first,
        mode=PolicyKind.REPLAY,
        source_run_id=source.episode.run_id,
    )
    second = await runtime.run(
        {
            company.company_id: ReplayCompanyAgent(
                company.company_id,
                first.turns,
            )
            for company in scenario.companies
        },
        8,
        run_id="second_replay",
        store=repository,
        replay_source=first.episode,
    )
    _complete(
        repository,
        second,
        mode=PolicyKind.REPLAY,
        source_run_id=first.episode.run_id,
    )

    projector = RunTimelineProjector(repository)
    page = projector.read_week(second.episode.run_id, 1)
    first_turn = next(turn for moment in page.days for turn in moment.turns)
    assert page.context.source_run_id == first.episode.run_id
    assert page.context.trace_run_id == source.episode.run_id
    assert first_turn.replay_origin is not None
    assert first_turn.replay_origin.source_run_id == source.episode.run_id
