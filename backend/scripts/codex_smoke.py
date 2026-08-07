"""Run one real Codex V4 company command without starting the web application."""

import asyncio

from company_bench.agents.company import COMMAND_PROMPT_VERSION, LlmCompanyAgent
from company_bench.agents.contracts import PolicyInfrastructureError
from company_bench.agents.providers.codex.gateway import CodexAgentConfig, CodexModelGateway
from company_bench.domain.models import PolicyKind, PolicyMetadata
from company_bench.domain.scenario import DAIRY_S9_SCENARIO
from company_bench.economy.engine import EconomyEngine
from company_bench.runtime.models import AgentTurn, SimTime, WakeReason
from company_bench.storage.store import InMemoryRunStore


async def main() -> None:
    """Verify authentication, structured output, audit, and cleanup."""
    config = CodexAgentConfig.from_environment()
    if config is None:
        raise SystemExit("Set DAIRY_BENCH_CODEX_ENABLED=true before running this smoke test.")

    repository = InMemoryRunStore()
    engine = EconomyEngine()
    scenario = DAIRY_S9_SCENARIO
    economy = engine.open_day(engine.initial_state(scenario, seed=42))
    company_id = scenario.companies[0].company_id
    observation = engine.observe_active(economy, company_id)
    turn = AgentTurn(
        turn_id=f"codex_smoke.{company_id}.t1",
        company_id=company_id,
        sim_time=SimTime.at(day=0, hour=9),
        state_version=economy.state_version,
        turn_number_today=1,
        turn_limit_today=scenario.runtime.max_turns_per_company_day,
        wake_reasons=(WakeReason.DAY_OPEN,),
        observation=observation,
        available_cash=observation.cash,
        reserved_cash=engine.reserved_cash(economy, company_id),
        marked_surplus=engine.marked_surplus(economy, company_id),
        inventory_expiry=engine.inventory_expiry(economy, company_id),
        open_orders=engine.company_orders(economy, company_id),
        order_books=engine.order_books(economy, company_id),
        pending_deliveries=engine.pending_delivery_views(economy, company_id),
        active_operation=engine.operation_view(economy, company_id),
        remaining_operation_capacity=engine.remaining_operation_capacity(
            economy,
            company_id,
        ),
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
        memory_token_budget=scenario.runtime.compaction_trigger_tokens,
        max_prompt_tokens=scenario.runtime.max_prompt_tokens,
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
