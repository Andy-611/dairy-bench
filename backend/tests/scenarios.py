from typing import Final

from company_bench.dairy_scenario import DAIRY_S9_V3_SCENARIO
from company_bench.models import ScenarioSpec

LEGACY_S9_SCENARIO: Final[ScenarioSpec] = DAIRY_S9_V3_SCENARIO.model_copy(
    update={
        "scenario_id": "test.dairy.s9.v1",
        "version": 1,
    }
)
