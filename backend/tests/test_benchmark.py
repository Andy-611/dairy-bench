"""Tests at the benchmark and application interfaces."""

import asyncio

import pytest

from company_bench.application import DairyBenchmark, RunService
from company_bench.dairy_scenario import DAIRY_S12_V2_SCENARIO
from company_bench.policies import BaselinePolicy
from company_bench.repository import MemoryRunRepository
from tests.scenarios import LEGACY_S12_SCENARIO


def _policies(benchmark: DairyBenchmark) -> dict[str, BaselinePolicy]:
    return {company.company_id: BaselinePolicy() for company in benchmark.scenario.companies}


def test_complete_episode_has_one_decision_per_company_per_day() -> None:
    benchmark = DairyBenchmark(LEGACY_S12_SCENARIO)

    result = asyncio.run(benchmark.run(_policies(benchmark), seed=42))

    assert len(result.snapshots) == 30
    assert len(result.scenario.companies) == 12
    assert len(result.decisions) == (len(result.scenario.companies) * result.scenario.days)
    assert result.snapshots[-1].day == 30
    assert result.score.consumer_fill_rate > 0
    assert result.score.total_trade_quantity > 0


def test_same_seed_replays_the_same_economic_result() -> None:
    benchmark = DairyBenchmark(LEGACY_S12_SCENARIO)

    first = asyncio.run(benchmark.run(_policies(benchmark), seed=7))
    second = asyncio.run(benchmark.run(_policies(benchmark), seed=7))

    assert first.decisions == second.decisions
    assert first.events == second.events
    assert first.snapshots == second.snapshots
    assert first.score == second.score
    assert first.run_id != second.run_id


def test_run_service_persists_the_completed_episode() -> None:
    repository = MemoryRunRepository()
    service = RunService(
        repository,
        DairyBenchmark(LEGACY_S12_SCENARIO),
    )

    result = asyncio.run(service.run(seed=42))

    assert service.get_run(result.run_id) == result
    assert service.list_runs()[0].run_id == result.run_id


def test_explicit_empty_policy_set_is_rejected() -> None:
    service = RunService(
        MemoryRunRepository(),
        DairyBenchmark(LEGACY_S12_SCENARIO),
    )

    with pytest.raises(ValueError, match="policies must match"):
        asyncio.run(service.run(seed=42, policies={}))


def test_daily_benchmark_rejects_event_driven_scenario() -> None:
    with pytest.raises(ValueError, match="does not implement event-driven V2"):
        DairyBenchmark(DAIRY_S12_V2_SCENARIO)
