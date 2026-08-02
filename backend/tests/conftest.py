import pytest

from company_bench.dairy_scenario import DAIRY_S9_V3_SCENARIO
from company_bench.engine import EconomyEngine
from company_bench.models import CompanyObservation


@pytest.fixture
def first_observation() -> CompanyObservation:
    """Return the first company's bounded day-one observation."""
    engine = EconomyEngine()
    state = engine.initial_state(DAIRY_S9_V3_SCENARIO, seed=42)
    return engine.observe(state)[0]
