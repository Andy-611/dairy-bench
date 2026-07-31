from typing import Final

from company_bench.dairy_scenario import DAIRY_S12_V2_SCENARIO
from company_bench.models import ScenarioSpec

LEGACY_S12_SCENARIO: Final[ScenarioSpec] = DAIRY_S12_V2_SCENARIO.model_copy(
    update={
        "scenario_id": "test.dairy.s12.v1",
        "version": 1,
    }
)
