from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from company_bench.agent_models import CommandModelRequest, CommandModelResult
from company_bench.agents import (
    COMMAND_PROMPT_VERSION,
    BaselineCompanyAgent,
    CompanyAgent,
    FixedCommandAgent,
    LlmCompanyAgent,
    ReplayCompanyAgent,
)
from company_bench.codex_artifacts import (
    CodexArtifactIdentity,
    CodexArtifactStore,
    CodexArtifactView,
)
from company_bench.dairy_scenario import DAIRY_S9_V3_SCENARIO
from company_bench.market_timeline import MarketTimelineProjector
from company_bench.models import PolicyKind, PolicyMetadata, ProductId
from company_bench.repository import MemoryRunRepository, SQLiteRunRepository
from company_bench.run_models import (
    InvocationOutcome,
    PolicyInvocation,
    RunCheckpoint,
    RunJob,
    RunStatus,
    TokenUsage,
)
from company_bench.runtime import EpisodeExecution, EpisodeRuntime
from company_bench.runtime_models import (
    AgentTurn,
    CancelOrder,
    CompanyCommand,
    MarketSide,
    PlaceOrder,
    Produce,
    ReplaceOrder,
    SystemStepRecord,
    TurnRecord,
    Wait,
    WakeReason,
)
from company_bench.timeline import RunTimelineProjector
from company_bench.timeline_models import (
    ArtifactStatus,
    ArtifactUnavailableReason,
    MarketOrderCancelled,
    MarketOrderPlaced,
    MarketOrderReplaced,
)


def _complete(
    repository: MemoryRunRepository,
    execution: EpisodeExecution,
    *,
    mode: PolicyKind,
    source_run_id: str | None = None,
) -> None:
    result = execution.episode
    repository.complete_job(
        result,
        RunJob(
            run_id=result.run_id,
            mode=mode,
            status=RunStatus.COMPLETED,
            seed=result.seed,
            source_run_id=source_run_id,
            scenario_id=result.scenario.scenario_id,
            current_day=result.scenario.days,
            total_days=result.scenario.days,
            submitted_at=result.started_at,
            started_at=result.started_at,
            finished_at=result.finished_at,
        ),
    )


class _UnreadableArtifactStore(CodexArtifactStore):
    """Artifact store seam that simulates one optional read failure."""

    def __init__(self, root: Path, failure: OSError | UnicodeError) -> None:
        super().__init__(root)
        self._failure = failure

    def read(self, identity: CodexArtifactIdentity) -> CodexArtifactView | None:
        """Raise the configured filesystem or decoding failure."""
        raise self._failure


class _AuditedDustGateway:
    """Return one raw dust order with nonzero provider audit metadata."""

    async def generate_command(self, _: CommandModelRequest) -> CommandModelResult:
        """Return a schema-valid command that the economy must reject."""
        return CommandModelResult(
            command=PlaceOrder(
                side=MarketSide.SELL,
                product=ProductId.RAW_MILK,
                quantity=Decimal("8.9E-91"),
                limit_price=Decimal("1.40"),
            ),
            provider="scripted",
            model="dust-model",
            response_id="response_dust",
            request_id="request_dust",
            usage=TokenUsage(input_tokens=3, output_tokens=2, total_tokens=5),
            attempts=2,
            latency_ms=17,
        )

    async def close(self) -> None:
        """Release no resources."""


class _MarketTimelineAgent:
    """Create one partial fill, replacement, and cancellation for projection tests."""

    metadata = PolicyMetadata(name="market-timeline", kind=PolicyKind.BASELINE)

    async def act(self, turn: AgentTurn) -> CompanyCommand:
        """Return one deterministic command from the current virtual minute."""
        minute = turn.sim_time.minute_of_day
        open_minute = turn.observation.runtime.open_minute
        if turn.company_id == "processor_a" and WakeReason.DAY_OPEN in turn.wake_reasons:
            return PlaceOrder(
                side=MarketSide.BUY,
                product=ProductId.RAW_MILK,
                quantity=Decimal("40"),
                limit_price=Decimal("1.66"),
            )
        if turn.company_id != "farm_a":
            return Wait()
        if WakeReason.DAY_OPEN in turn.wake_reasons:
            return Produce(
                product=ProductId.RAW_MILK,
                quantity=Decimal("60"),
            )
        if minute == open_minute + 30:
            return PlaceOrder(
                side=MarketSide.SELL,
                product=ProductId.RAW_MILK,
                quantity=Decimal("50"),
                limit_price=Decimal("1.65"),
            )
        if minute == open_minute + 31 and turn.open_orders:
            return ReplaceOrder(
                order_id=turn.open_orders[0].order_id,
                quantity=Decimal("10"),
                limit_price=Decimal("1.64"),
            )
        if minute == open_minute + 32 and turn.open_orders:
            return CancelOrder(order_id=turn.open_orders[0].order_id)
        return Wait()


@pytest.mark.asyncio
async def test_timeline_projects_system_steps_turns_and_typed_state_changes() -> None:
    scenario = DAIRY_S9_V3_SCENARIO.model_copy(update={"days": 1})
    repository = MemoryRunRepository()
    execution = await EpisodeRuntime(scenario).run(
        {company.company_id: BaselineCompanyAgent() for company in scenario.companies},
        42,
        run_id="timeline_baseline",
        store=repository,
    )
    _complete(repository, execution, mode=PolicyKind.BASELINE)

    page = RunTimelineProjector(repository).read_day(execution.episode.run_id, 1)

    assert page.context.model_call_count == 0
    assert page.context.current_usage == TokenUsage()
    steps = tuple(step for moment in page.moments for step in moment.system_steps)
    assert {step.kind.value for step in steps}.issuperset(
        {"day_open", "market_close", "consumer_sales", "day_close"}
    )
    assert sum(len(moment.turns) for moment in page.moments) == len(execution.turns)
    assert any(turn.state_changes for moment in page.moments for turn in moment.turns)
    assert all(
        turn.observation.visible_event_count == len(turn.observation.visible_events)
        for moment in page.moments
        for turn in moment.turns
    )
    assert all(
        turn.observation.cash >= 0 and turn.observation.reserved_cash >= 0
        for moment in page.moments
        for turn in moment.turns
    )
    close = next(
        step for moment in page.moments for step in moment.system_steps if step.kind == "day_close"
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
async def test_market_timeline_projects_trade_tape_and_end_state_bid_ask_book() -> None:
    companies = tuple(
        DAIRY_S9_V3_SCENARIO.company(company_id)
        for company_id in ("farm_a", "processor_a")
    )
    scenario = DAIRY_S9_V3_SCENARIO.model_copy(
        update={
            "scenario_id": "timeline.market.s2.v3",
            "days": 1,
            "companies": companies,
        }
    )
    repository = MemoryRunRepository()
    execution = await EpisodeRuntime(scenario).run(
        {company.company_id: _MarketTimelineAgent() for company in companies},
        23,
        run_id="timeline_market",
        store=repository,
    )
    _complete(repository, execution, mode=PolicyKind.BASELINE)

    page = RunTimelineProjector(repository).read_day(execution.episode.run_id, 1)
    frames = {moment.sim_time.minute_of_day: moment.market for moment in page.moments}
    raw_at_open = next(
        book
        for book in frames[scenario.runtime.open_minute].closing_order_books
        if book.product is ProductId.RAW_MILK
    )
    raw_after_fill = next(
        book
        for book in frames[scenario.runtime.open_minute + 30].closing_order_books
        if book.product is ProductId.RAW_MILK
    )

    assert raw_at_open.best_bid == Decimal("1.66")
    assert raw_at_open.bids[0].size == Decimal("40")
    assert raw_at_open.bids[0].orders[0].owner_id == "processor_a"
    open_flow = frames[scenario.runtime.open_minute].order_flow
    assert len(open_flow) == 1
    assert isinstance(open_flow[0], MarketOrderPlaced)
    assert open_flow[0].incoming_order.owner_id == "processor_a"
    assert open_flow[0].matched_quantity == 0
    assert open_flow[0].remaining_quantity == Decimal("40")

    fill_frame = frames[scenario.runtime.open_minute + 30]
    assert len(fill_frame.order_flow) == 1
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
    assert fill_frame.trades[0].arrives_at.minute_of_day == scenario.runtime.open_minute + 60
    assert raw_after_fill.last_trade_price == Decimal("1.66")
    assert raw_after_fill.best_bid is None
    assert raw_after_fill.best_ask == Decimal("1.65")
    assert raw_after_fill.asks[0].size == Decimal("10")
    original_order_id = raw_after_fill.asks[0].orders[0].order_id

    raw_after_replace = next(
        book
        for book in frames[scenario.runtime.open_minute + 31].closing_order_books
        if book.product is ProductId.RAW_MILK
    )
    replace_flow = frames[scenario.runtime.open_minute + 31].order_flow
    assert len(replace_flow) == 1
    assert isinstance(replace_flow[0], MarketOrderReplaced)
    assert replace_flow[0].replaced_order.order_id == original_order_id
    assert raw_after_replace.best_ask == Decimal("1.64")
    assert raw_after_replace.asks[0].orders[0].order_id != original_order_id

    raw_after_cancel = next(
        book
        for book in frames[scenario.runtime.open_minute + 32].closing_order_books
        if book.product is ProductId.RAW_MILK
    )
    cancel_flow = frames[scenario.runtime.open_minute + 32].order_flow
    assert len(cancel_flow) == 1
    assert isinstance(cancel_flow[0], MarketOrderCancelled)
    assert cancel_flow[0].cancelled_order.order_id == raw_after_replace.asks[0].orders[0].order_id
    assert raw_after_cancel.bids == ()
    assert raw_after_cancel.asks == ()

    cancel_minute = scenario.runtime.open_minute + 32
    partial_projection = MarketTimelineProjector().project_day(
        scenario,
        1,
        repository.list_turns(execution.episode.run_id),
        repository.list_system_steps(execution.episode.run_id),
        (cancel_minute,),
    )
    partial_raw_book = next(
        book
        for book in partial_projection[cancel_minute].closing_order_books
        if book.product is ProductId.RAW_MILK
    )
    assert partial_raw_book.bids == ()
    assert partial_raw_book.asks == ()


@pytest.mark.asyncio
async def test_replay_timeline_resolves_source_turn_and_all_physical_calls() -> None:
    scenario = DAIRY_S9_V3_SCENARIO.model_copy(update={"days": 1})
    repository = MemoryRunRepository()
    runtime = EpisodeRuntime(scenario)
    source = await runtime.run(
        {company.company_id: BaselineCompanyAgent() for company in scenario.companies},
        9,
        run_id="trace_source",
        store=repository,
    )
    _complete(repository, source, mode=PolicyKind.CODEX)
    source_turn = source.turns[0]
    started_at = datetime.now(UTC)
    for index, applied in enumerate((False, True), start=1):
        repository.record_invocation(
            PolicyInvocation(
                invocation_id=f"inv_{index}",
                run_id=source.episode.run_id,
                company_id=source_turn.turn.company_id,
                day=source_turn.turn.observation.day,
                observation=source_turn.turn.observation,
                provider="codex",
                model="test-model",
                prompt_version="v2",
                prompt_hash=f"hash_{index}",
                started_at=started_at,
                finished_at=started_at,
                outcome=InvocationOutcome.SUCCESS,
                domain_turn_id=source_turn.turn.turn_id,
                sim_minute=source_turn.turn.sim_time.absolute_minute,
                state_version=source_turn.turn.state_version,
                apply_sequence=(source_turn.outcome.apply_sequence if applied else None),
                command=source_turn.envelope.command,
                command_outcome=source_turn.outcome if applied else None,
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
    page = projector.read_day(replay.episode.run_id, 1)
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
    assert sum(trace.preview.applied_to_committed_turn for trace in detail.traces) == 1


@pytest.mark.asyncio
async def test_trace_detail_degrades_when_optional_artifact_is_missing_or_unreadable(
    tmp_path: Path,
) -> None:
    scenario = DAIRY_S9_V3_SCENARIO.model_copy(update={"days": 1})
    repository = MemoryRunRepository()
    execution = await EpisodeRuntime(scenario).run(
        {company.company_id: BaselineCompanyAgent() for company in scenario.companies},
        13,
        run_id="artifact_degradation",
        store=repository,
    )
    _complete(repository, execution, mode=PolicyKind.CODEX)
    record = execution.turns[0]
    now = datetime.now(UTC)
    repository.record_invocation(
        PolicyInvocation(
            invocation_id="artifact_invocation",
            run_id=execution.episode.run_id,
            company_id=record.turn.company_id,
            day=record.turn.observation.day,
            observation=record.turn.observation,
            provider="codex",
            model="test-model",
            prompt_version="v2",
            prompt_hash="artifact_hash",
            started_at=now,
            finished_at=now,
            outcome=InvocationOutcome.SUCCESS,
            domain_turn_id=record.turn.turn_id,
            sim_minute=record.turn.sim_time.absolute_minute,
            state_version=record.turn.state_version,
            apply_sequence=record.outcome.apply_sequence,
            command=record.envelope.command,
            command_outcome=record.outcome,
            request_id="thread-id",
            provider_turn_id="provider-turn-id",
        )
    )

    missing_detail = RunTimelineProjector(
        repository,
        CodexArtifactStore(tmp_path / "missing"),
    ).read_detail(execution.episode.run_id, record.turn.turn_id)
    missing_trace = missing_detail.traces[0]

    assert missing_trace.artifact_status is ArtifactStatus.UNAVAILABLE
    assert missing_trace.artifact_unavailable_reason is ArtifactUnavailableReason.NOT_FOUND
    assert missing_trace.reasoning_markdown is None
    assert missing_trace.final_output is None

    for failure in (OSError("disk unavailable"), UnicodeError("invalid UTF-8")):
        unreadable_detail = RunTimelineProjector(
            repository,
            _UnreadableArtifactStore(tmp_path / "unreadable", failure),
        ).read_detail(execution.episode.run_id, record.turn.turn_id)
        unreadable_trace = unreadable_detail.traces[0]

        assert unreadable_trace.artifact_status is ArtifactStatus.UNAVAILABLE
        assert unreadable_trace.artifact_unavailable_reason is ArtifactUnavailableReason.READ_ERROR
        assert unreadable_trace.reasoning_markdown is None
        assert unreadable_trace.final_output is None


@pytest.mark.asyncio
async def test_sqlite_rolls_back_system_step_when_checkpoint_validation_fails(
    tmp_path: Path,
) -> None:
    scenario = DAIRY_S9_V3_SCENARIO.model_copy(update={"days": 1})
    memory = MemoryRunRepository()
    await EpisodeRuntime(scenario).run(
        {company.company_id: BaselineCompanyAgent() for company in scenario.companies},
        12,
        run_id="atomic_source",
        store=memory,
    )
    checkpoint = memory.get_checkpoint("atomic_source")
    assert checkpoint is not None

    with SQLiteRunRepository(tmp_path / "atomic.sqlite3") as repository:
        with pytest.raises(ValueError, match="checkpoint turns"):
            repository.save_progress((), (checkpoint.system_steps[0],), checkpoint)
        assert repository.list_system_steps(checkpoint.run_id) == ()
        assert repository.get_checkpoint(checkpoint.run_id) is None


@pytest.mark.asyncio
async def test_timeline_does_not_invent_unpersisted_system_steps() -> None:
    scenario = DAIRY_S9_V3_SCENARIO.model_copy(update={"days": 1})
    durable = MemoryRunRepository()
    execution = await EpisodeRuntime(scenario).run(
        {company.company_id: BaselineCompanyAgent() for company in scenario.companies},
        4,
        run_id="persisted_timeline",
        store=durable,
    )
    checkpoint = durable.get_checkpoint(execution.episode.run_id)
    assert checkpoint is not None

    journal_only = MemoryRunRepository()
    for record in execution.turns:
        journal_only.record_turn(record)
    journal_only.record_system_step(checkpoint.system_steps[-1])
    _complete(journal_only, execution, mode=PolicyKind.BASELINE)

    page = RunTimelineProjector(journal_only).read_day(execution.episode.run_id, 1)
    steps = tuple(step for moment in page.moments for step in moment.system_steps)
    assert len(steps) == 1
    assert steps[0].kind == "day_close"


class _StopAfterFirstProgress:
    def __init__(self, repository: MemoryRunRepository) -> None:
        self.repository = repository

    def save_progress(
        self,
        turns: tuple[TurnRecord, ...],
        system_steps: tuple[SystemStepRecord, ...],
        checkpoint: RunCheckpoint,
    ) -> None:
        self.repository.save_progress(turns, system_steps, checkpoint)
        raise RuntimeError("stop after first progress")


@pytest.mark.asyncio
async def test_failed_run_timeline_preserves_raw_dust_order_and_provider_audit() -> None:
    scenario = DAIRY_S9_V3_SCENARIO.model_copy(update={"days": 1})
    repository = MemoryRunRepository()
    run_id = "failed_dust_history"
    agents: dict[str, CompanyAgent] = {
        company.company_id: FixedCommandAgent((Wait(),))
        for company in scenario.companies
    }
    agents["farm_a"] = LlmCompanyAgent(
        run_id=run_id,
        company_id="farm_a",
        gateway=_AuditedDustGateway(),
        audit_sink=repository,
        metadata=PolicyMetadata(
            name="audited-dust",
            version="3",
            kind=PolicyKind.OPENAI,
            provider="scripted",
            model="dust-model",
            prompt_version=COMMAND_PROMPT_VERSION,
        ),
    )

    with pytest.raises(RuntimeError, match="first progress"):
        await EpisodeRuntime(scenario).run(
            agents,
            17,
            run_id=run_id,
            store=_StopAfterFirstProgress(repository),
        )
    checkpoint = repository.get_checkpoint(run_id)
    assert checkpoint is not None
    repository.save_job(
        RunJob(
            run_id=run_id,
            mode=PolicyKind.OPENAI,
            status=RunStatus.FAILED,
            seed=17,
            scenario_id=scenario.scenario_id,
            total_days=scenario.days,
            submitted_at=checkpoint.episode_started_at,
            started_at=checkpoint.episode_started_at,
            finished_at=datetime.now(UTC),
            error_message="synthetic failure after durable progress",
        )
    )

    page = RunTimelineProjector(repository).read_day(run_id, 1)
    turn = next(
        turn
        for moment in page.moments
        for turn in moment.turns
        if turn.company_id == "farm_a"
    )
    invocation = repository.list_invocations(run_id)[0]

    assert isinstance(turn.command, PlaceOrder)
    assert turn.command.quantity == Decimal("8.9E-91")
    assert not turn.outcome.accepted
    assert "exact multiple of 0.0001" in turn.outcome.reason
    assert turn.protocol_error is None
    assert invocation.command == turn.command
    assert invocation.command_outcome == turn.outcome
    assert invocation.response_id == "response_dust"
    assert invocation.request_id == "request_dust"
    assert invocation.usage.total_tokens == 5
    assert invocation.attempts == 2
    assert invocation.latency_ms == 17
    assert page.context.checkpoint_at == checkpoint.scheduler.now
    assert page.context.checkpoint_state_version == checkpoint.economy.state_version


@pytest.mark.asyncio
async def test_running_replay_timeline_accepts_a_valid_source_prefix() -> None:
    scenario = DAIRY_S9_V3_SCENARIO.model_copy(update={"days": 1})
    repository = MemoryRunRepository()
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
    checkpoint = repository.get_checkpoint("prefix_replay")
    assert checkpoint is not None
    repository.save_job(
        RunJob(
            run_id="prefix_replay",
            mode=PolicyKind.REPLAY,
            status=RunStatus.RUNNING,
            seed=7,
            source_run_id=source.episode.run_id,
            scenario_id=scenario.scenario_id,
            total_days=1,
            submitted_at=checkpoint.episode_started_at,
            started_at=checkpoint.episode_started_at,
        )
    )

    page = RunTimelineProjector(repository).read_day("prefix_replay", 1)
    assert page.context.replay is True
    assert page.context.checkpoint_at == checkpoint.scheduler.now
    assert page.context.checkpoint_state_version == checkpoint.economy.state_version
    assert sum(len(moment.turns) for moment in page.moments) == len(checkpoint.turns)
    assert all(turn.replay_origin is not None for moment in page.moments for turn in moment.turns)


@pytest.mark.asyncio
async def test_replay_of_replay_resolves_the_ultimate_trace_run() -> None:
    scenario = DAIRY_S9_V3_SCENARIO.model_copy(update={"days": 1})
    repository = MemoryRunRepository()
    runtime = EpisodeRuntime(scenario)
    source = await runtime.run(
        {company.company_id: BaselineCompanyAgent() for company in scenario.companies},
        8,
        run_id="ultimate_source",
        store=repository,
    )
    _complete(repository, source, mode=PolicyKind.CODEX)

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
    page = projector.read_day(second.episode.run_id, 1)
    first_turn = next(turn for moment in page.moments for turn in moment.turns)
    assert page.context.source_run_id == first.episode.run_id
    assert page.context.trace_run_id == source.episode.run_id
    assert first_turn.replay_origin is not None
    assert first_turn.replay_origin.source_run_id == source.episode.run_id
