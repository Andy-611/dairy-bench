"""Run one real Codex V2 company command without starting the web application."""

import asyncio

from company_bench.agent_models import PolicyInfrastructureError
from company_bench.agents import COMMAND_PROMPT_VERSION, LlmCompanyAgent
from company_bench.codex_gateway import CodexAgentConfig, CodexModelGateway
from company_bench.dairy_scenario import DAIRY_S12_V2_SCENARIO
from company_bench.engine import EconomyEngine
from company_bench.models import PolicyKind, PolicyMetadata
from company_bench.repository import MemoryRunRepository
from company_bench.runtime_models import AgentTurn, SimTime, WakeReason


async def main() -> None:
    """Verify authentication, structured output, audit, and cleanup."""
    config = CodexAgentConfig.from_environment()
    if config is None:
        raise SystemExit("Set DAIRY_BENCH_CODEX_ENABLED=true before running this smoke test.")

    repository = MemoryRunRepository()
    engine = EconomyEngine()
    economy = engine.open_day(engine.initial_state(DAIRY_S12_V2_SCENARIO, seed=42))
    company_id = DAIRY_S12_V2_SCENARIO.companies[0].company_id
    turn = AgentTurn(
        turn_id=f"codex_smoke.{company_id}.t1",
        company_id=company_id,
        sim_time=SimTime.at(day=0, hour=9),
        state_version=economy.state_version,
        wake_reasons=(WakeReason.DAY_OPEN,),
        observation=engine.observe_active(economy, company_id),
    )
    gateway = CodexModelGateway(config, company_id)
    agent = LlmCompanyAgent(
        run_id="codex_smoke",
        company_id=company_id,
        gateway=gateway,
        audit_sink=repository,
        metadata=PolicyMetadata(
            name="codex-company-agent",
            kind=PolicyKind.CODEX,
            provider="codex",
            model=config.model,
            prompt_version=COMMAND_PROMPT_VERSION,
            config_fingerprint=config.fingerprint,
        ),
        memory_token_budget=DAIRY_S12_V2_SCENARIO.runtime.compaction_trigger_tokens,
        max_prompt_tokens=DAIRY_S12_V2_SCENARIO.runtime.max_prompt_tokens,
    )

    try:
        command = await agent.act(turn)
    except PolicyInfrastructureError as error:
        raise SystemExit(str(error)) from None
    finally:
        await gateway.close()

    invocation = repository.list_invocations("codex_smoke")[0]
    print(command.model_dump_json(indent=2))
    print(
        f"model={invocation.model} "
        f"latency_ms={invocation.latency_ms} "
        f"tokens={invocation.usage.total_tokens}"
    )


if __name__ == "__main__":
    asyncio.run(main())
