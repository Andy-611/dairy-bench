from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import pytest

from company_bench.agent_models import ModelOutputError, PolicyInfrastructureError
from company_bench.agents import (
    BaselineCompanyAgent,
    CompanyAgent,
    ReplayCompanyAgent,
    ReplayDriftError,
)
from company_bench.dairy_scenario import DAIRY_S12_V2_SCENARIO
from company_bench.models import (
    CompanyEvent,
    FarmOperation,
    PolicyKind,
    PolicyMetadata,
    ScenarioSpec,
    TradeExecutedEvent,
)
from company_bench.repository import MemoryRunRepository, SQLiteRunRepository
from company_bench.run_models import RunCheckpoint, RunJob, RunStatus
from company_bench.runtime import EpisodeExecution, EpisodeRuntime
from company_bench.runtime_models import (
    AgentTurn,
    CompanyCommand,
    Produce,
    SimTime,
    SystemEventKind,
    SystemStepRecord,
    TurnRecord,
    Wait,
    WakeReason,
)


def _scenario(days: int = 1) -> ScenarioSpec:
    return DAIRY_S12_V2_SCENARIO.model_copy(update={"days": days})


def _baseline_agents(scenario: ScenarioSpec) -> dict[str, CompanyAgent]:
    return {company.company_id: BaselineCompanyAgent() for company in scenario.companies}


@pytest.mark.asyncio
async def test_v2_runs_multiple_atomic_turns_per_company_day() -> None:
    scenario = _scenario()
    execution = await EpisodeRuntime(scenario).run(
        _baseline_agents(scenario),
        42,
        run_id="run_v2",
    )

    assert execution.episode.scenario.scenario_id == "flow.dairy.base.s12.v2"
    assert len(execution.turns) > len(scenario.companies)
    assert {record.envelope.command.kind for record in execution.turns} >= {
        "produce",
        "transform",
        "place_order",
        "set_retail_price",
        "wait",
    }
    assert all(
        left.turn.sim_time.absolute_minute <= right.turn.sim_time.absolute_minute
        for left, right in zip(execution.turns, execution.turns[1:], strict=False)
    )

    versions_by_minute: dict[int, set[int]] = {}
    for record in execution.turns:
        versions_by_minute.setdefault(
            record.turn.sim_time.absolute_minute,
            set(),
        ).add(record.turn.state_version)
    assert all(len(versions) == 1 for versions in versions_by_minute.values())


@pytest.mark.asyncio
async def test_thirty_day_baseline_is_exactly_reproducible() -> None:
    scenario = _scenario(days=30)
    runtime = EpisodeRuntime(scenario)

    first = await runtime.run(
        _baseline_agents(scenario),
        42,
        run_id="deterministic_first",
    )
    second = await runtime.run(
        _baseline_agents(scenario),
        42,
        run_id="deterministic_second",
    )

    def turn_projection(execution: EpisodeExecution) -> tuple[tuple[object, ...], ...]:
        return tuple(
            (
                record.turn.company_id,
                record.turn.sim_time,
                record.turn.state_version,
                record.turn.wake_reasons,
                record.observation_hash,
                record.envelope.command,
                record.outcome.status,
                record.outcome.reason,
            )
            for record in execution.turns
        )

    assert turn_projection(first) == turn_projection(second)
    assert first.episode.events == second.episode.events
    assert first.episode.snapshots == second.episode.snapshots
    assert first.episode.score == second.episode.score


class _AlwaysActAgent:
    metadata = BaselineCompanyAgent.metadata

    async def act(self, turn: AgentTurn) -> CompanyCommand:
        return Produce(product="raw_milk", quantity=1)


@pytest.mark.asyncio
async def test_runtime_enforces_the_daily_turn_cap_for_every_company() -> None:
    scenario = _scenario()
    execution = await EpisodeRuntime(scenario).run(
        {company.company_id: _AlwaysActAgent() for company in scenario.companies},
        42,
        run_id="daily_turn_cap",
    )

    counts = {
        company.company_id: sum(
            record.turn.company_id == company.company_id for record in execution.turns
        )
        for company in scenario.companies
    }
    assert set(counts.values()) == {scenario.runtime.max_turns_per_company_day}


class _ContinueFarmAgent:
    metadata = BaselineCompanyAgent.metadata

    async def act(self, turn: AgentTurn) -> CompanyCommand:
        if isinstance(turn.observation.operation, FarmOperation):
            return Produce(product="raw_milk", quantity=1)
        return Wait()


@pytest.mark.asyncio
async def test_system_generated_wakes_merge_with_existing_same_minute_wakes() -> None:
    scenario = _scenario()
    execution = await EpisodeRuntime(scenario).run(
        {company.company_id: _ContinueFarmAgent() for company in scenario.companies},
        42,
        run_id="merged_wakes",
    )
    turn_keys = {
        (record.turn.sim_time.absolute_minute, record.turn.company_id) for record in execution.turns
    }

    assert len(turn_keys) == len(execution.turns)
    farm_clear_turns = tuple(
        record
        for record in execution.turns
        if record.turn.company_id.startswith("farm_")
        and record.turn.sim_time.minute_of_day == scenario.runtime.raw_market_clear_minute
    )
    assert len(farm_clear_turns) == sum(
        company.company_id.startswith("farm_") for company in scenario.companies
    )
    assert all(
        set(record.turn.wake_reasons) == {WakeReason.CONTINUE, WakeReason.MARKET_CLEARED}
        for record in farm_clear_turns
    )


class _BusyAtMarketClearAgent:
    metadata = BaselineCompanyAgent.metadata

    async def act(self, turn: AgentTurn) -> CompanyCommand:
        if turn.company_id != "farm_a":
            return Wait()
        minute = turn.sim_time.minute_of_day
        if minute == turn.observation.runtime.open_minute:
            return Wait(until=turn.sim_time.plus(110))
        if minute == turn.observation.runtime.raw_market_clear_minute - 10:
            return Produce(product="raw_milk", quantity=1)
        return Wait()


def _busy_agents(scenario: ScenarioSpec) -> dict[str, CompanyAgent]:
    return {company.company_id: _BusyAtMarketClearAgent() for company in scenario.companies}


@pytest.mark.asyncio
async def test_external_wake_waits_for_the_company_command_cooldown() -> None:
    scenario = _scenario()
    execution = await EpisodeRuntime(scenario).run(
        _busy_agents(scenario),
        12,
        run_id="busy_company",
    )
    farm_turns = tuple(record for record in execution.turns if record.turn.company_id == "farm_a")

    assert [record.turn.sim_time.minute_of_day for record in farm_turns] == [
        scenario.runtime.open_minute,
        scenario.runtime.raw_market_clear_minute - 10,
        scenario.runtime.raw_market_clear_minute - 10 + scenario.runtime.command_duration_minutes,
    ]
    assert set(farm_turns[-1].turn.wake_reasons) == {
        WakeReason.CONTINUE,
        WakeReason.MARKET_CLEARED,
    }


@dataclass(slots=True)
class _DelayedAgent:
    delegate: BaselineCompanyAgent
    delay: float

    @property
    def metadata(self) -> PolicyMetadata:
        return self.delegate.metadata

    async def act(self, turn: AgentTurn) -> CompanyCommand:
        await asyncio.sleep(self.delay)
        return await self.delegate.act(turn)


@pytest.mark.asyncio
async def test_model_completion_order_cannot_change_economic_result() -> None:
    scenario = _scenario()
    company_ids = [company.company_id for company in scenario.companies]

    def agents(reverse: bool) -> Mapping[str, CompanyAgent]:
        delays = range(len(company_ids))
        if reverse:
            delays = reversed(tuple(delays))
        return {
            company_id: _DelayedAgent(
                BaselineCompanyAgent(),
                delay * 0.0001,
            )
            for company_id, delay in zip(company_ids, delays, strict=True)
        }

    runtime = EpisodeRuntime(scenario)
    first = await runtime.run(agents(False), 17, run_id="same_run")
    second = await runtime.run(agents(True), 17, run_id="same_run")

    assert first.turns == second.turns
    assert first.episode.events == second.episode.events
    assert first.episode.snapshots == second.episode.snapshots
    assert first.episode.score == second.episode.score


@pytest.mark.asyncio
async def test_replay_uses_turn_journal_without_a_model_gateway() -> None:
    scenario = _scenario()
    runtime = EpisodeRuntime(scenario)
    source = await runtime.run(
        _baseline_agents(scenario),
        9,
        run_id="source",
    )
    replay_agents = {
        company.company_id: ReplayCompanyAgent(
            company.company_id,
            source.turns,
        )
        for company in scenario.companies
    }

    replay = await runtime.run(
        replay_agents,
        9,
        run_id="replay",
        replay_source=source.episode,
    )

    assert replay.episode.events == source.episode.events
    assert replay.episode.snapshots == source.episode.snapshots
    assert replay.episode.score == source.episode.score
    assert all(policy.source_run_id == source.episode.run_id for policy in replay.episode.policies)
    assert tuple(record.envelope.command for record in replay.turns) == tuple(
        record.envelope.command for record in source.turns
    )


@pytest.mark.asyncio
async def test_replay_rejects_a_journal_from_another_source_run() -> None:
    scenario = _scenario()
    runtime = EpisodeRuntime(scenario)
    expected_source = await runtime.run(
        _baseline_agents(scenario),
        9,
        run_id="expected_source",
    )
    wrong_source = await runtime.run(
        _baseline_agents(scenario),
        9,
        run_id="wrong_source",
    )

    with pytest.raises(ValueError, match="source run_id"):
        await runtime.run(
            {
                company.company_id: ReplayCompanyAgent(
                    company.company_id,
                    wrong_source.turns,
                )
                for company in scenario.companies
            },
            9,
            run_id="lineage_replay",
            replay_source=expected_source.episode,
        )


class _BrokenAgent:
    metadata = BaselineCompanyAgent.metadata

    async def act(self, turn: AgentTurn) -> CompanyCommand:
        raise ModelOutputError("invalid model command")


@pytest.mark.asyncio
async def test_protocol_rejection_continues_after_normal_command_duration() -> None:
    scenario = _scenario()
    execution = await EpisodeRuntime(scenario).run(
        {company.company_id: _BrokenAgent() for company in scenario.companies},
        9,
        run_id="protocol_continuation",
    )

    for company in scenario.companies:
        records = tuple(
            record for record in execution.turns if record.turn.company_id == company.company_id
        )
        assert len(records) == scenario.runtime.max_turns_per_company_day
        assert records[0].turn.wake_reasons == (WakeReason.DAY_OPEN,)
        assert all(WakeReason.CONTINUE in record.turn.wake_reasons for record in records[1:])
        assert all(
            current.turn.previous_outcome == previous.outcome
            for previous, current in pairwise(records)
        )
        assert all(record.protocol_error is not None for record in records)
        assert all(not record.outcome.accepted for record in records)
        assert all(
            right.turn.sim_time.absolute_minute - left.turn.sim_time.absolute_minute
            == scenario.runtime.command_duration_minutes
            for left, right in pairwise(records)
        )


@pytest.mark.asyncio
async def test_replay_preserves_protocol_rejections() -> None:
    scenario = _scenario()
    runtime = EpisodeRuntime(scenario)
    source = await runtime.run(
        {company.company_id: _BrokenAgent() for company in scenario.companies},
        9,
        run_id="protocol_source",
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
        run_id="protocol_replay",
        replay_source=source.episode,
    )

    assert all(
        record.protocol_error == "ModelOutputError: invalid model command"
        for record in source.turns
    )
    assert tuple(
        (record.outcome.accepted, record.outcome.reason) for record in replay.turns
    ) == tuple((record.outcome.accepted, record.outcome.reason) for record in source.turns)
    assert replay.episode.events == source.episode.events
    assert replay.episode.snapshots == source.episode.snapshots
    assert replay.episode.score == source.episode.score


@pytest.mark.asyncio
async def test_replay_drift_terminates_instead_of_producing_a_score() -> None:
    scenario = _scenario()
    runtime = EpisodeRuntime(scenario)
    source = await runtime.run(
        _baseline_agents(scenario),
        9,
        run_id="drift_source",
    )
    tampered = (
        source.turns[0].model_copy(update={"observation_hash": "tampered"}),
        *source.turns[1:],
    )

    with pytest.raises(ReplayDriftError, match="observation drift"):
        await runtime.run(
            {
                company.company_id: ReplayCompanyAgent(
                    company.company_id,
                    tampered,
                )
                for company in scenario.companies
            },
            9,
            run_id="drift_replay",
            replay_source=source.episode,
        )


@pytest.mark.asyncio
async def test_replay_rejects_command_outcome_drift() -> None:
    scenario = _scenario()
    runtime = EpisodeRuntime(scenario)
    source = await runtime.run(
        _baseline_agents(scenario),
        9,
        run_id="outcome_source",
    )
    source_record = source.turns[-1]
    tampered_record = source_record.model_copy(
        update={
            "outcome": source_record.outcome.model_copy(
                update={"reason": "tampered source outcome"}
            )
        }
    )
    tampered = (*source.turns[:-1], tampered_record)

    with pytest.raises(ReplayDriftError, match="outcome drift"):
        await runtime.run(
            {
                company.company_id: ReplayCompanyAgent(
                    company.company_id,
                    tampered,
                )
                for company in scenario.companies
            },
            9,
            run_id="outcome_replay",
            replay_source=source.episode,
        )


@pytest.mark.asyncio
async def test_replay_rejects_final_result_projection_drift() -> None:
    scenario = _scenario()
    runtime = EpisodeRuntime(scenario)
    source = await runtime.run(
        _baseline_agents(scenario),
        9,
        run_id="projection_source",
    )
    tampered_source = source.episode.model_copy(
        update={
            "score": source.episode.score.model_copy(
                update={"efficiency": source.episode.score.efficiency + 1}
            )
        }
    )

    with pytest.raises(ReplayDriftError, match="final events, snapshots, or score"):
        await runtime.run(
            {
                company.company_id: ReplayCompanyAgent(
                    company.company_id,
                    source.turns,
                )
                for company in scenario.companies
            },
            9,
            run_id="projection_replay",
            replay_source=tampered_source,
        )


@pytest.mark.asyncio
async def test_replay_rejects_an_unconsumed_source_turn_before_scoring() -> None:
    scenario = _scenario()
    runtime = EpisodeRuntime(scenario)
    source = await runtime.run(
        _baseline_agents(scenario),
        9,
        run_id="overlong_source",
    )
    source_record = next(
        record for record in reversed(source.turns) if record.turn.company_id == "farm_a"
    )
    extra_turn = source_record.turn.model_copy(update={"turn_id": "overlong_source.farm_a.extra"})
    extra_envelope = source_record.envelope.model_copy(
        update={
            "turn_id": extra_turn.turn_id,
            "command_id": f"{extra_turn.turn_id}.command",
        }
    )
    extra_outcome = source_record.outcome.model_copy(
        update={
            "turn_id": extra_turn.turn_id,
            "command_id": extra_envelope.command_id,
            "apply_sequence": max(record.outcome.apply_sequence for record in source.turns) + 1,
        }
    )
    overlong = (
        *source.turns,
        source_record.model_copy(
            update={
                "turn": extra_turn,
                "envelope": extra_envelope,
                "outcome": extra_outcome,
            }
        ),
    )

    with pytest.raises(ReplayDriftError, match="unconsumed turn"):
        await runtime.run(
            {
                company.company_id: ReplayCompanyAgent(
                    company.company_id,
                    overlong,
                )
                for company in scenario.companies
            },
            9,
            run_id="overlong_replay",
            replay_source=source.episode,
        )


class _SlowAgent:
    metadata = BaselineCompanyAgent.metadata

    async def act(self, turn: AgentTurn) -> CompanyCommand:
        await asyncio.sleep(60)
        return Wait()


@pytest.mark.asyncio
async def test_agent_timeout_fails_the_episode_as_infrastructure() -> None:
    scenario = _scenario()
    agents = _baseline_agents(scenario)
    agents["farm_a"] = _SlowAgent()

    with pytest.raises(PolicyInfrastructureError, match="Agent turn timed out"):
        await EpisodeRuntime(
            scenario,
            agent_timeout_seconds=0.001,
        ).run(
            agents,
            9,
            run_id="agent_timeout",
        )


class _UnexpectedFailureAgent:
    metadata = BaselineCompanyAgent.metadata

    async def act(self, turn: AgentTurn) -> CompanyCommand:
        raise RuntimeError("internal implementation defect")


@pytest.mark.asyncio
async def test_unexpected_agent_failure_cannot_be_scored_as_protocol_error() -> None:
    scenario = _scenario()
    agents = _baseline_agents(scenario)
    agents["farm_a"] = _UnexpectedFailureAgent()

    with pytest.raises(
        PolicyInfrastructureError,
        match="unexpected Agent failure: RuntimeError",
    ):
        await EpisodeRuntime(scenario).run(
            agents,
            9,
            run_id="unexpected_agent_failure",
        )


class _CancellationProbe:
    def __init__(self, sibling_count: int) -> None:
        self.sibling_count = sibling_count
        self.ready = asyncio.Event()
        self.started = 0
        self.cancelled = 0
        self.completed = 0


class _FailAfterSiblingsStartAgent:
    metadata = BaselineCompanyAgent.metadata

    def __init__(self, probe: _CancellationProbe) -> None:
        self._probe = probe

    async def act(self, turn: AgentTurn) -> CompanyCommand:
        await self._probe.ready.wait()
        raise PolicyInfrastructureError("provider unavailable")


class _CancellableAgent:
    metadata = BaselineCompanyAgent.metadata

    def __init__(self, probe: _CancellationProbe) -> None:
        self._probe = probe

    async def act(self, turn: AgentTurn) -> CompanyCommand:
        self._probe.started += 1
        if self._probe.started == self._probe.sibling_count:
            self._probe.ready.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self._probe.cancelled += 1
            raise
        self._probe.completed += 1
        return Wait()


@pytest.mark.asyncio
async def test_bucket_failure_cancels_and_joins_sibling_agent_calls() -> None:
    scenario = _scenario()
    probe = _CancellationProbe(len(scenario.companies) - 1)
    agents: dict[str, CompanyAgent] = {
        scenario.companies[0].company_id: _FailAfterSiblingsStartAgent(probe),
        **{company.company_id: _CancellableAgent(probe) for company in scenario.companies[1:]},
    }

    with pytest.raises(PolicyInfrastructureError, match="provider unavailable"):
        await EpisodeRuntime(scenario).run(
            agents,
            9,
            run_id="bucket_failure",
        )

    assert probe.started == probe.cancelled == probe.sibling_count
    assert probe.completed == 0


class _InterruptingStore:
    def __init__(self, repository: MemoryRunRepository, after_calls: int) -> None:
        self.repository = repository
        self.after_calls = after_calls
        self.calls = 0

    def save_progress(
        self,
        records: tuple[TurnRecord, ...],
        system_steps: tuple[SystemStepRecord, ...],
        checkpoint: RunCheckpoint,
    ) -> None:
        self.repository.save_progress(records, system_steps, checkpoint)
        self.calls += 1
        if self.calls == self.after_calls:
            raise RuntimeError("simulated process stop after durable checkpoint")


class _InterruptAtRawClear:
    def __init__(self, repository: MemoryRunRepository) -> None:
        self.repository = repository

    def save_progress(
        self,
        records: tuple[TurnRecord, ...],
        system_steps: tuple[SystemStepRecord, ...],
        checkpoint: RunCheckpoint,
    ) -> None:
        self.repository.save_progress(records, system_steps, checkpoint)
        if any(
            step.kind is SystemEventKind.MARKET_CLEAR and "raw" in step.scheduled_event_id
            for step in system_steps
        ):
            raise RuntimeError("interrupted after raw market minute")


class _FailAtDayClose:
    def __init__(self, repository: MemoryRunRepository) -> None:
        self.repository = repository

    def save_progress(
        self,
        records: tuple[TurnRecord, ...],
        system_steps: tuple[SystemStepRecord, ...],
        checkpoint: RunCheckpoint,
    ) -> None:
        if any(step.kind is SystemEventKind.DAY_CLOSE for step in system_steps):
            raise RuntimeError("day close persistence failed")
        self.repository.save_progress(records, system_steps, checkpoint)


@pytest.mark.asyncio
async def test_protocol_continuation_survives_checkpoint_resume() -> None:
    scenario = _scenario()
    runtime = EpisodeRuntime(scenario)
    expected = await runtime.run(
        {company.company_id: _BrokenAgent() for company in scenario.companies},
        9,
        run_id="protocol_resume",
    )
    repository = MemoryRunRepository()

    with pytest.raises(RuntimeError, match="simulated process stop"):
        await runtime.run(
            {company.company_id: _BrokenAgent() for company in scenario.companies},
            9,
            run_id="protocol_resume",
            store=_InterruptingStore(repository, after_calls=1),
        )
    checkpoint = repository.get_checkpoint("protocol_resume")
    assert checkpoint is not None
    continuation_time = scenario.runtime.open_minute + scenario.runtime.command_duration_minutes
    continuations = tuple(
        event
        for event in checkpoint.scheduler.pending_events
        if event.kind is SystemEventKind.COMPANY_WAKE
        and event.at.minute_of_day == continuation_time
    )
    assert len(continuations) == len(scenario.companies)
    assert all(event.wake_reasons == (WakeReason.CONTINUE,) for event in continuations)

    resumed = await runtime.run(
        {company.company_id: _BrokenAgent() for company in scenario.companies},
        9,
        run_id="protocol_resume",
        store=repository,
        checkpoint=checkpoint,
    )

    assert resumed.turns == expected.turns
    assert resumed.episode.events == expected.episode.events
    assert resumed.episode.snapshots == expected.episode.snapshots
    assert resumed.episode.score == expected.episode.score


@pytest.mark.asyncio
async def test_market_clear_checkpoint_keeps_same_minute_continuation_wake() -> None:
    scenario = _scenario()
    runtime = EpisodeRuntime(scenario)
    agents = {company.company_id: _ContinueFarmAgent() for company in scenario.companies}
    expected = await runtime.run(agents, 42, run_id="minute_atomic")
    repository = MemoryRunRepository()

    with pytest.raises(RuntimeError, match="raw market minute"):
        await runtime.run(
            {company.company_id: _ContinueFarmAgent() for company in scenario.companies},
            42,
            run_id="minute_atomic",
            store=_InterruptAtRawClear(repository),
        )
    checkpoint = repository.get_checkpoint("minute_atomic")
    assert checkpoint is not None
    clear_turns = tuple(
        record
        for record in checkpoint.turns
        if record.turn.company_id.startswith("farm_")
        and record.turn.sim_time.minute_of_day == scenario.runtime.raw_market_clear_minute
    )
    assert len(clear_turns) == sum(
        company.company_id.startswith("farm_") for company in scenario.companies
    )
    assert all(
        set(record.turn.wake_reasons) == {WakeReason.CONTINUE, WakeReason.MARKET_CLEARED}
        for record in clear_turns
    )

    resumed = await runtime.run(
        {company.company_id: _ContinueFarmAgent() for company in scenario.companies},
        42,
        run_id="minute_atomic",
        store=repository,
        checkpoint=checkpoint,
    )
    assert tuple(
        (record.turn.wake_reasons, record.envelope.command) for record in resumed.turns
    ) == tuple((record.turn.wake_reasons, record.envelope.command) for record in expected.turns)
    assert repository.list_system_steps("minute_atomic") == (
        repository.get_checkpoint("minute_atomic").system_steps
    )


@pytest.mark.asyncio
async def test_day_callback_runs_only_after_durable_close_checkpoint() -> None:
    scenario = _scenario()
    repository = MemoryRunRepository()
    completed_days: list[int] = []

    with pytest.raises(RuntimeError, match="day close persistence failed"):
        await EpisodeRuntime(scenario).run(
            _baseline_agents(scenario),
            42,
            run_id="close_atomic",
            store=_FailAtDayClose(repository),
            on_day_completed=_append_day(completed_days),
        )

    assert completed_days == []
    assert all(
        step.kind is not SystemEventKind.DAY_CLOSE
        for step in repository.list_system_steps("close_atomic")
    )


def _append_day(days: list[int]):
    async def append(day: int) -> None:
        days.append(day)

    return append


class _MetadataAgent:
    def __init__(self, metadata: PolicyMetadata) -> None:
        self.metadata = metadata
        self._delegate = BaselineCompanyAgent()

    async def act(self, turn: AgentTurn) -> CompanyCommand:
        return await self._delegate.act(turn)


def _metadata_agents(
    scenario: ScenarioSpec,
    fingerprint: str,
) -> dict[str, CompanyAgent]:
    metadata = PolicyMetadata(
        name="checkpoint-policy",
        version="2",
        kind=PolicyKind.BASELINE,
        provider="scripted",
        model="scripted-v2",
        prompt_version="test",
        config_fingerprint=fingerprint,
    )
    return {company.company_id: _MetadataAgent(metadata) for company in scenario.companies}


@pytest.mark.asyncio
async def test_checkpoint_rejects_changed_policy_metadata() -> None:
    scenario = _scenario()
    runtime = EpisodeRuntime(scenario)
    repository = MemoryRunRepository()

    with pytest.raises(RuntimeError, match="simulated process stop"):
        await runtime.run(
            _metadata_agents(scenario, "config-a"),
            31,
            run_id="policy_resume",
            store=_InterruptingStore(repository, after_calls=2),
        )
    checkpoint = repository.get_checkpoint("policy_resume")
    assert checkpoint is not None

    with pytest.raises(ValueError, match="policy metadata"):
        await runtime.run(
            _metadata_agents(scenario, "config-b"),
            31,
            run_id="policy_resume",
            store=repository,
            checkpoint=checkpoint,
        )


@pytest.mark.asyncio
async def test_checkpoint_resume_matches_uninterrupted_execution() -> None:
    scenario = _scenario(days=2)
    runtime = EpisodeRuntime(scenario)
    expected = await runtime.run(
        _baseline_agents(scenario),
        31,
        run_id="resume_run",
    )
    repository = MemoryRunRepository()
    interrupting = _InterruptingStore(repository, after_calls=5)

    with pytest.raises(RuntimeError, match="simulated process stop"):
        await runtime.run(
            _baseline_agents(scenario),
            31,
            run_id="resume_run",
            store=interrupting,
        )
    checkpoint = repository.get_checkpoint("resume_run")
    assert checkpoint is not None

    resumed = await runtime.run(
        _baseline_agents(scenario),
        31,
        run_id="resume_run",
        store=repository,
        checkpoint=checkpoint,
    )

    assert resumed.turns == expected.turns
    assert resumed.episode.events == expected.episode.events
    assert resumed.episode.snapshots == expected.episode.snapshots
    assert resumed.episode.score == expected.episode.score
    assert resumed.episode.started_at == checkpoint.episode_started_at


@pytest.mark.asyncio
async def test_busy_company_checkpoint_resume_preserves_deferred_wakes() -> None:
    scenario = _scenario()
    runtime = EpisodeRuntime(scenario)
    expected = await runtime.run(
        _busy_agents(scenario),
        21,
        run_id="busy_resume",
    )
    repository = MemoryRunRepository()

    with pytest.raises(RuntimeError, match="simulated process stop"):
        await runtime.run(
            _busy_agents(scenario),
            21,
            run_id="busy_resume",
            store=_InterruptingStore(repository, after_calls=2),
        )
    checkpoint = repository.get_checkpoint("busy_resume")
    assert checkpoint is not None
    assert next(
        cursor for cursor in checkpoint.cursors if cursor.company_id == "farm_a"
    ).available_at == SimTime(
        absolute_minute=(
            scenario.runtime.raw_market_clear_minute
            - 10
            + scenario.runtime.command_duration_minutes
        )
    )

    resumed = await runtime.run(
        _busy_agents(scenario),
        21,
        run_id="busy_resume",
        store=repository,
        checkpoint=checkpoint,
    )

    assert resumed.turns == expected.turns
    assert resumed.episode.events == expected.episode.events
    assert resumed.episode.snapshots == expected.episode.snapshots
    assert resumed.episode.score == expected.episode.score


@pytest.mark.asyncio
async def test_replay_checkpoint_resume_advances_each_source_stream() -> None:
    scenario = _scenario()
    runtime = EpisodeRuntime(scenario)
    source = await runtime.run(
        _baseline_agents(scenario),
        31,
        run_id="replay_resume_source",
    )

    def replay_agents(completed: tuple[TurnRecord, ...] = ()) -> dict[str, CompanyAgent]:
        return {
            company.company_id: ReplayCompanyAgent(
                company.company_id,
                source.turns,
                completed_turns=sum(
                    record.turn.company_id == company.company_id for record in completed
                ),
            )
            for company in scenario.companies
        }

    expected = await runtime.run(
        replay_agents(),
        31,
        run_id="replay_resume",
        replay_source=source.episode,
    )
    repository = MemoryRunRepository()
    with pytest.raises(RuntimeError, match="simulated process stop"):
        await runtime.run(
            replay_agents(),
            31,
            run_id="replay_resume",
            store=_InterruptingStore(repository, after_calls=4),
            replay_source=source.episode,
        )
    checkpoint = repository.get_checkpoint("replay_resume")
    assert checkpoint is not None

    resumed = await runtime.run(
        replay_agents(checkpoint.turns),
        31,
        run_id="replay_resume",
        store=repository,
        checkpoint=checkpoint,
        replay_source=source.episode,
    )

    assert resumed.turns == expected.turns
    assert resumed.episode.events == expected.episode.events
    assert resumed.episode.snapshots == expected.episode.snapshots
    assert resumed.episode.score == expected.episode.score


class _OutsideHoursWaitAgent:
    metadata = BaselineCompanyAgent.metadata

    async def act(self, turn: AgentTurn) -> CompanyCommand:
        return Wait(
            until=SimTime(
                absolute_minute=turn.sim_time.day * 24 * 60 + 23 * 60,
            )
        )


@pytest.mark.asyncio
async def test_wait_cannot_schedule_an_agent_turn_outside_business_hours() -> None:
    scenario = _scenario()
    execution = await EpisodeRuntime(scenario).run(
        {company.company_id: _OutsideHoursWaitAgent() for company in scenario.companies},
        4,
        run_id="bounded_wait",
    )

    assert all(not record.outcome.accepted for record in execution.turns)
    assert all(
        record.outcome.reason == "wait deadline must be inside business hours"
        for record in execution.turns
    )
    assert all(
        scenario.runtime.open_minute
        <= record.turn.sim_time.minute_of_day
        < scenario.runtime.close_minute
        for record in execution.turns
    )
    assert all(
        WakeReason.WAIT_EXPIRED not in record.turn.wake_reasons for record in execution.turns
    )


@pytest.mark.asyncio
async def test_visible_events_never_leak_another_company_private_event() -> None:
    scenario = _scenario()
    execution = await EpisodeRuntime(scenario).run(
        _baseline_agents(scenario),
        42,
        run_id="private",
    )

    for record in execution.turns:
        company_id = record.turn.company_id
        for event in record.turn.visible_events:
            if isinstance(event, TradeExecutedEvent):
                assert company_id in {event.seller_id, event.buyer_id}
            elif isinstance(event, CompanyEvent):
                assert event.company_id == company_id


@pytest.mark.asyncio
async def test_v2_sqlite_round_trip_retains_journal_and_clears_checkpoint(
    tmp_path: Path,
) -> None:
    scenario = _scenario()
    database = tmp_path / "v2.sqlite3"
    with SQLiteRunRepository(database) as repository:
        execution = await EpisodeRuntime(scenario).run(
            _baseline_agents(scenario),
            44,
            run_id="sqlite_v2",
            store=repository,
        )
        assert repository.get_checkpoint("sqlite_v2") is not None
        completed_job = RunJob(
            run_id="sqlite_v2",
            mode=PolicyKind.BASELINE,
            status=RunStatus.COMPLETED,
            seed=44,
            scenario_id=scenario.scenario_id,
            current_day=scenario.days,
            total_days=scenario.days,
            submitted_at=execution.episode.started_at,
            started_at=execution.episode.started_at,
            finished_at=execution.episode.finished_at,
        )
        repository.complete_job(execution.episode, completed_job)

        assert repository.get("sqlite_v2") == execution.episode
        assert repository.list_turns("sqlite_v2") == execution.turns
        assert repository.get_checkpoint("sqlite_v2") is None

    with SQLiteRunRepository(database) as reopened:
        assert reopened.get("sqlite_v2") == execution.episode
        assert reopened.list_turns("sqlite_v2") == execution.turns
