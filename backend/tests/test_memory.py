"""Tests for company-owned, token-budgeted conversation memory."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from company_bench.agents.memory import (
    AgentCheckpoint,
    ConversationMemory,
    MemoryExchange,
    MemorySummary,
)
from company_bench.domain.models import CompanyObservation
from company_bench.domain.scenario import DAIRY_S9_SCENARIO
from company_bench.economy.engine import EconomyEngine
from company_bench.runtime.models import (
    AgentTurn,
    CommandEnvelope,
    CommandOutcome,
    CommandStatus,
    SimTime,
    TurnRecord,
    Wait,
    WakeReason,
)

RUN_ID = "memory_run"


@pytest.fixture(scope="module")
def observations() -> dict[str, CompanyObservation]:
    """Return one private day-one observation per company."""
    engine = EconomyEngine()
    state = engine.initial_state(DAIRY_S9_SCENARIO, seed=42)
    return {observation.company_id: observation for observation in engine.observe(state)}


def test_memory_exchange_keeps_only_decision_relevant_runtime_facts(
    observations: dict[str, CompanyObservation],
) -> None:
    exchange = _exchange(observations["farm_a"], 1)
    payload = exchange.model_dump()

    assert exchange.turn_id == "turn_farm_a_1"
    assert exchange.command == Wait()
    assert "turn" not in payload
    assert "observation" not in payload


def test_memory_rejects_cross_company_and_cross_run_state(
    observations: dict[str, CompanyObservation],
) -> None:
    memory = ConversationMemory(RUN_ID, "farm_a")
    memory.remember(_exchange(observations["farm_a"], 1))

    with pytest.raises(ValidationError, match="exchange company_id"):
        memory.remember(_exchange(observations["processor_a"], 2))

    checkpoint = memory.checkpoint()
    assert checkpoint.schema_version == 6
    with pytest.raises(ValueError, match="another company"):
        ConversationMemory.restore(RUN_ID, "farm_b", checkpoint)
    with pytest.raises(ValueError, match="another run"):
        ConversationMemory.restore("different_run", "farm_a", checkpoint)


def test_budget_compacts_old_cycles_and_always_keeps_latest(
    observations: dict[str, CompanyObservation],
) -> None:
    first = _exchange(observations["farm_a"], 1)
    second = _exchange(observations["farm_a"], 2)
    probe = ConversationMemory(RUN_ID, "farm_a", max_tokens=100_000, chars_per_token=1)
    probe.remember(first)
    one_exchange_tokens = probe.estimated_tokens
    probe.remember(second)
    two_exchange_tokens = probe.estimated_tokens
    budget = (one_exchange_tokens + two_exchange_tokens) // 2

    memory = ConversationMemory(
        RUN_ID,
        "farm_a",
        max_tokens=budget,
        chars_per_token=1,
        summarizer=_short_summary,
    )
    for index in range(1, 6):
        memory.remember(_exchange(observations["farm_a"], index))

    assert memory.summary is not None
    assert memory.summary.exchange_count == 4
    assert memory.summary.through_turn_id == "turn_farm_a_4"
    assert memory.exchanges == (_exchange(observations["farm_a"], 5),)
    assert memory.estimated_tokens <= memory.max_tokens


def test_checkpoint_round_trip_restores_exact_context(
    observations: dict[str, CompanyObservation],
) -> None:
    memory = ConversationMemory(
        RUN_ID,
        "farm_a",
        max_tokens=900,
        chars_per_token=1,
        summarizer=_short_summary,
    )
    for index in range(1, 5):
        memory.remember(_exchange(observations["farm_a"], index))

    serialized = memory.checkpoint().model_dump_json()
    checkpoint = AgentCheckpoint.model_validate_json(serialized)
    restored = ConversationMemory.restore(
        RUN_ID,
        "farm_a",
        checkpoint,
        summarizer=_short_summary,
    )

    assert restored.checkpoint() == checkpoint
    assert restored.context_json() == memory.context_json()
    restored.remember(_exchange(observations["farm_a"], 5))
    assert restored.checkpoint().revision == 5


def test_default_summary_is_deterministic(
    observations: dict[str, CompanyObservation],
) -> None:
    probe = ConversationMemory(RUN_ID, "farm_a", max_tokens=100_000, chars_per_token=1)
    probe.remember(_exchange(observations["farm_a"], 1))
    one_exchange_tokens = probe.estimated_tokens
    probe.remember(_exchange(observations["farm_a"], 2))
    budget = (one_exchange_tokens + probe.estimated_tokens) // 2

    def build() -> ConversationMemory:
        memory = ConversationMemory(
            RUN_ID,
            "farm_a",
            max_tokens=budget,
            chars_per_token=1,
        )
        for index in range(1, 4):
            memory.remember(_exchange(observations["farm_a"], index))
        return memory

    first = build()
    second = build()

    assert first.checkpoint() == second.checkpoint()
    assert first.summary is not None
    assert first.summary.exchange_count == 2
    assert first.summary.source_hash == second.summary.source_hash


def test_summarizer_failure_does_not_partially_mutate_memory(
    observations: dict[str, CompanyObservation],
) -> None:
    first = _exchange(observations["farm_a"], 1)
    second = _exchange(observations["farm_a"], 2)
    probe = ConversationMemory(RUN_ID, "farm_a", max_tokens=100_000, chars_per_token=1)
    probe.remember(first)
    budget = probe.estimated_tokens
    memory = ConversationMemory(
        RUN_ID,
        "farm_a",
        max_tokens=budget,
        chars_per_token=1,
        summarizer=_failing_summary,
    )
    memory.remember(first)
    before = memory.checkpoint()

    with pytest.raises(RuntimeError, match="summary failed"):
        memory.remember(second)

    assert memory.checkpoint() == before


def test_checkpoint_rejects_inconsistent_revision(
    observations: dict[str, CompanyObservation],
) -> None:
    exchange = _exchange(observations["farm_a"], 1)

    with pytest.raises(ValidationError, match="revision"):
        AgentCheckpoint(
            run_id=RUN_ID,
            company_id="farm_a",
            revision=2,
            max_tokens=100,
            chars_per_token=4,
            exchanges=(exchange,),
        )


def _exchange(
    observation: CompanyObservation,
    index: int,
    *,
    run_id: str = RUN_ID,
) -> MemoryExchange:
    company_id = observation.company_id
    sim_time = SimTime(absolute_minute=540 + index * 30)
    turn_id = f"turn_{company_id}_{index}"
    command_id = f"command_{company_id}_{index}"
    turn = AgentTurn(
        turn_id=turn_id,
        company_id=company_id,
        sim_time=sim_time,
        state_version=index - 1,
        turn_number_today=index,
        turn_limit_today=observation.runtime.max_turns_per_company_day,
        wake_reasons=(WakeReason.CONTINUE,),
        observation=observation,
        available_cash=observation.cash,
        marked_surplus=Decimal(),
    )
    envelope = CommandEnvelope(
        turn_id=turn_id,
        command_id=command_id,
        company_id=company_id,
        issued_at=sim_time,
        state_version=index - 1,
        command=Wait(),
    )
    outcome = CommandOutcome(
        turn_id=turn_id,
        command_id=command_id,
        company_id=company_id,
        occurred_at=sim_time,
        status=CommandStatus.ACCEPTED,
        accepted=True,
        resulting_state_version=index,
        apply_sequence=index,
        next_available_at=sim_time.plus(30),
    )
    return MemoryExchange.from_record(
        TurnRecord(
            run_id=run_id,
            turn=turn,
            envelope=envelope,
            outcome=outcome,
            observation_hash=f"observation_hash_{company_id}_{index}",
        )
    )


def _short_summary(
    _: MemorySummary | None,
    exchanges: tuple[MemoryExchange, ...],
) -> str:
    """Return a compact deterministic test summary."""
    return f"compacted through {exchanges[-1].turn_id}"


def _failing_summary(
    _: MemorySummary | None,
    __: tuple[MemoryExchange, ...],
) -> str:
    """Raise to verify atomic state replacement."""
    raise RuntimeError("summary failed")
