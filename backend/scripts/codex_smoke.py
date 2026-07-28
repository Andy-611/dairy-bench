"""Run one real Codex company decision without starting the web application."""

import asyncio

from company_bench.agent_models import PolicyInfrastructureError
from company_bench.agent_policy import PROMPT_VERSION, LlmCompanyPolicy
from company_bench.codex_gateway import CodexAgentConfig, CodexModelGateway
from company_bench.dairy_scenario import DAIRY_V1_SCENARIO
from company_bench.engine import EconomyEngine
from company_bench.models import PolicyKind, PolicyMetadata
from company_bench.repository import MemoryRunRepository


async def main() -> None:
    """Verify authentication, structured output, audit, and cleanup."""
    config = CodexAgentConfig.from_environment()
    if config is None:
        raise SystemExit("Set DAIRY_BENCH_CODEX_ENABLED=true before running this smoke test.")

    repository = MemoryRunRepository()
    engine = EconomyEngine()
    state = engine.initial_state(DAIRY_V1_SCENARIO, seed=42)
    observation = engine.observe(state)[0]
    gateway = CodexModelGateway(config, observation.company_id)
    policy = LlmCompanyPolicy(
        run_id="codex_smoke",
        company_id=observation.company_id,
        gateway=gateway,
        audit_sink=repository,
        metadata=PolicyMetadata(
            name="codex-company-agent",
            kind=PolicyKind.CODEX,
            provider="codex",
            model=config.model,
            prompt_version=PROMPT_VERSION,
            config_fingerprint=config.fingerprint,
        ),
    )

    try:
        decision = await policy.decide(observation)
    except PolicyInfrastructureError as error:
        raise SystemExit(str(error)) from None
    finally:
        await gateway.close()

    invocation = repository.list_invocations("codex_smoke")[0]
    print(decision.model_dump_json(indent=2))
    print(
        f"model={invocation.model} "
        f"latency_ms={invocation.latency_ms} "
        f"tokens={invocation.usage.total_tokens}"
    )


if __name__ == "__main__":
    asyncio.run(main())
