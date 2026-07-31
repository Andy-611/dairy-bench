import pytest

from company_bench.agent_gateway import ScriptedModelGateway
from company_bench.agent_models import ModelOutputError
from company_bench.agents import LlmCompanyAgent, observation_hash
from company_bench.dairy_scenario import DAIRY_S12_V2_SCENARIO
from company_bench.models import (
    CompanyObservation,
    NoOpDecision,
    PolicyKind,
    PolicyMetadata,
)
from company_bench.repository import MemoryRunRepository
from company_bench.run_models import InvocationOutcome
from company_bench.runtime import EpisodeRuntime
from company_bench.runtime_models import (
    AgentTurn,
    CommandEnvelope,
    CommandOutcome,
    CommandStatus,
    CompanyCommand,
    Produce,
    SimTime,
    TurnRecord,
    Wait,
    WakeReason,
)


def _llm_agent(
    run_id: str,
    command: CompanyCommand,
    *,
    company_id: str = "farm_a",
    repository: MemoryRunRepository | None = None,
    memory_token_budget: int = 12_288,
    max_prompt_tokens: int = 16_384,
) -> tuple[LlmCompanyAgent, ScriptedModelGateway, MemoryRunRepository]:
    """Create one isolated scripted V2 Agent and its audit repository."""
    audit_repository = repository if repository is not None else MemoryRunRepository()
    gateway = ScriptedModelGateway(
        lambda _: NoOpDecision(),
        command_factory=lambda _: command,
    )
    agent = LlmCompanyAgent(
        run_id=run_id,
        company_id=company_id,
        gateway=gateway,
        audit_sink=audit_repository,
        metadata=PolicyMetadata(
            name="bounded-agent",
            kind=PolicyKind.OPENAI,
            provider="scripted",
            model="scripted-v2",
            prompt_version="test",
        ),
        memory_token_budget=memory_token_budget,
        max_prompt_tokens=max_prompt_tokens,
    )
    return agent, gateway, audit_repository


def _turn(run_id: str, observation: CompanyObservation) -> AgentTurn:
    """Create one runtime-owned farm turn."""
    return AgentTurn(
        turn_id=f"{run_id}.farm_a.t1",
        company_id="farm_a",
        sim_time=SimTime(absolute_minute=540),
        state_version=0,
        wake_reasons=(WakeReason.DAY_OPEN,),
        observation=observation,
    )


@pytest.mark.asyncio
async def test_llm_agent_rejects_an_oversized_prompt_before_calling_provider(
    first_observation: CompanyObservation,
) -> None:
    agent, gateway, repository = _llm_agent(
        "prompt_limit",
        Wait(),
        memory_token_budget=100,
        max_prompt_tokens=200,
    )
    turn = _turn("prompt_limit", first_observation)

    with pytest.raises(ModelOutputError, match="prompt-token limit"):
        await agent.act(turn)

    assert gateway.command_requests == []
    invocation = repository.list_invocations("prompt_limit")[0]
    assert invocation.outcome is InvocationOutcome.AGENT_ERROR
    assert invocation.domain_turn_id == turn.turn_id


@pytest.mark.asyncio
async def test_llm_agent_audits_and_remembers_one_complete_turn(
    first_observation: CompanyObservation,
) -> None:
    run_id = "complete_cycle"
    command = Produce(product="raw_milk", quantity="12")
    agent, gateway, repository = _llm_agent(run_id, command)
    turn = _turn(run_id, first_observation)

    assert await agent.act(turn) == command

    envelope = CommandEnvelope(
        turn_id=turn.turn_id,
        command_id=f"{turn.turn_id}.command",
        company_id=turn.company_id,
        issued_at=turn.sim_time,
        state_version=turn.state_version,
        command=command,
    )
    outcome = CommandOutcome(
        turn_id=turn.turn_id,
        command_id=envelope.command_id,
        company_id=turn.company_id,
        occurred_at=turn.sim_time,
        status=CommandStatus.ACCEPTED,
        accepted=True,
        resulting_state_version=1,
        apply_sequence=1,
        next_available_at=turn.sim_time.plus(30),
    )
    record = TurnRecord(
        run_id=run_id,
        turn=turn,
        envelope=envelope,
        outcome=outcome,
        observation_hash=observation_hash(turn),
    )
    agent.remember(record)

    assert len(gateway.command_requests) == 1
    checkpoint = agent.checkpoint()
    assert checkpoint.revision == 1
    assert len(checkpoint.exchanges) == 1
    assert checkpoint.exchanges[0].model_dump() == record.model_dump()
    invocation = repository.list_invocations(run_id)[0]
    assert invocation.outcome is InvocationOutcome.SUCCESS
    assert invocation.command == command
    assert invocation.command_outcome == outcome
    assert invocation.apply_sequence == 1


@pytest.mark.asyncio
async def test_retried_domain_turn_preserves_each_physical_provider_call(
    first_observation: CompanyObservation,
) -> None:
    run_id = "physical_retry"
    repository = MemoryRunRepository()
    first, _, _ = _llm_agent(
        run_id,
        Wait(),
        repository=repository,
    )
    second, _, _ = _llm_agent(
        run_id,
        Wait(),
        repository=repository,
    )
    turn = _turn(run_id, first_observation)

    await first.act(turn)
    await second.act(turn)

    invocations = repository.list_invocations(run_id)
    assert len(invocations) == 2
    assert len({invocation.invocation_id for invocation in invocations}) == 2
    assert {invocation.domain_turn_id for invocation in invocations} == {turn.turn_id}


@pytest.mark.asyncio
async def test_llm_agents_complete_a_runtime_day_with_audited_memory() -> None:
    scenario = DAIRY_S12_V2_SCENARIO.model_copy(update={"days": 1})
    run_id = "llm_runtime_cycle"
    repository = MemoryRunRepository()
    agents: dict[str, LlmCompanyAgent] = {}
    for company in scenario.companies:
        agent, _, _ = _llm_agent(
            run_id,
            Wait(),
            company_id=company.company_id,
            repository=repository,
        )
        agents[company.company_id] = agent

    execution = await EpisodeRuntime(scenario).run(
        agents,
        42,
        run_id=run_id,
        store=repository,
    )

    invocations = repository.list_invocations(run_id)
    checkpoint = repository.get_checkpoint(run_id)
    assert checkpoint is not None
    assert tuple(state.company_id for state in checkpoint.agent_states) == tuple(
        company.company_id for company in scenario.companies
    )
    assert len(invocations) == len(execution.turns)
    assert all(invocation.outcome is InvocationOutcome.SUCCESS for invocation in invocations)
    assert all(invocation.command_outcome is not None for invocation in invocations)
    assert len({invocation.prompt_hash for invocation in invocations}) == len(invocations)
    assert {company_id: agent.checkpoint().revision for company_id, agent in agents.items()} == {
        company.company_id: sum(
            record.turn.company_id == company.company_id for record in execution.turns
        )
        for company in scenario.companies
    }
