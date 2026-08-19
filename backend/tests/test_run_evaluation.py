from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from company_bench.agents.company import BaselineCompanyAgent
from company_bench.domain.models import EpisodeResult, PolicyKind, PolicyProfileId, ScenarioSpec
from company_bench.domain.scenario import DAIRY_S9_SCENARIO
from company_bench.economy.oracle import ORACLE_VERSION, OracleResult
from company_bench.economy.scoring import Evaluator
from company_bench.runs.evaluation import RunEvaluationProjector
from company_bench.runs.models import (
    PolicyInvocation,
    RunCheckpoint,
    RunJob,
    RunRecovery,
    RunStatus,
)
from company_bench.runtime.episode import EpisodeRuntime
from company_bench.runtime.models import SystemStepRecord, TurnRecord
from company_bench.storage.memory import InMemoryRunRepository


class _FixedOracle:
    """Return a deterministic positive reference for projection tests."""

    def evaluate(
        self,
        _: ScenarioSpec,
        __: int,
        ___: int | None = None,
    ) -> OracleResult:
        return OracleResult(
            oracle_version=ORACLE_VERSION,
            input_hash="projection-test-oracle",
            solver="test",
            solver_status="optimal",
            enterprise_surplus_upper_bound=Decimal("1000000"),
        )


@dataclass(slots=True)
class _StopDuringSecondWeek:
    """Persist a partial second week, then simulate an arbitrary stop."""

    repository: InMemoryRunRepository

    def save_progress(
        self,
        turns: tuple[TurnRecord, ...],
        system_steps: tuple[SystemStepRecord, ...],
        checkpoint: RunCheckpoint,
    ) -> None:
        self.repository.save_progress(turns, system_steps, checkpoint)
        if checkpoint.economy.base_state.completed_weeks == 1 and any(
            turn.turn.sim_day.week == 2 for turn in turns
        ):
            raise RuntimeError("stopped during week two")


@dataclass(frozen=True, slots=True)
class _JoblessEvaluationSource:
    """Expose orphan persisted data to verify fail-closed projection."""

    episode: EpisodeResult | None = None
    recovery: RunRecovery | None = None

    def get(self, _: str) -> EpisodeResult | None:
        return self.episode

    def get_job(self, _: str) -> RunJob | None:
        return None

    def load_recovery(self, _: str) -> RunRecovery | None:
        return self.recovery

    def list_invocations(self, _: str) -> tuple[PolicyInvocation, ...]:
        return ()


@pytest.mark.asyncio
async def test_projection_scores_only_the_latest_fully_settled_week() -> None:
    scenario = ScenarioSpec.model_validate_json(
        DAIRY_S9_SCENARIO.model_copy(update={"weeks": 2}).model_dump_json()
    )
    repository = InMemoryRunRepository()
    run_id = "partial_evaluation"
    submitted_at = datetime.now(UTC)
    repository.save_job(
        RunJob(
            run_id=run_id,
            profile_id=PolicyProfileId.BASELINE,
            kind=PolicyKind.BASELINE,
            status=RunStatus.STOPPED,
            seed=42,
            scenario_id=scenario.scenario_id,
            current_absolute_day=7,
            total_weeks=scenario.weeks,
            submitted_at=submitted_at,
            started_at=submitted_at,
            finished_at=submitted_at,
        )
    )
    agents = {company.company_id: BaselineCompanyAgent() for company in scenario.companies}

    with pytest.raises(RuntimeError, match="stopped during week two"):
        await EpisodeRuntime(scenario).run(
            agents,
            42,
            run_id=run_id,
            store=_StopDuringSecondWeek(repository),
        )

    evaluation = RunEvaluationProjector(
        repository,
        Evaluator(oracle=_FixedOracle()),
    ).read(run_id)

    assert evaluation is not None
    assert evaluation.provisional
    assert evaluation.completed_weeks == 1
    assert evaluation.agent_usage is None
    assert tuple(snapshot.week for snapshot in evaluation.snapshots) == (1,)
    assert evaluation.score.companies
    assert evaluation.quality.protocol.total_turn_count == len(
        tuple(turn for turn in repository.list_turns(run_id) if turn.turn.sim_day.week == 1)
    )


@pytest.mark.asyncio
async def test_projection_rejects_completed_episode_without_run_job() -> None:
    scenario = DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1})
    execution = await EpisodeRuntime(scenario).run(
        {company.company_id: BaselineCompanyAgent() for company in scenario.companies},
        11,
        run_id="orphan_evaluation_result",
    )

    with pytest.raises(ValueError, match="missing its RunJob"):
        projector = RunEvaluationProjector(
            _JoblessEvaluationSource(episode=execution.episode),
            Evaluator(),
        )
        projector.read(execution.episode.run_id)


@pytest.mark.asyncio
async def test_projection_rejects_checkpoint_without_run_job() -> None:
    scenario = DAIRY_S9_SCENARIO.model_copy(update={"weeks": 1})
    repository = InMemoryRunRepository()
    execution = await EpisodeRuntime(scenario).run(
        {company.company_id: BaselineCompanyAgent() for company in scenario.companies},
        12,
        run_id="orphan_evaluation_checkpoint",
        store=repository,
    )
    recovery = repository.load_recovery(execution.episode.run_id)
    assert recovery is not None

    with pytest.raises(ValueError, match="checkpoint is missing its RunJob"):
        RunEvaluationProjector(_JoblessEvaluationSource(recovery=recovery), Evaluator()).read(
            execution.episode.run_id
        )
