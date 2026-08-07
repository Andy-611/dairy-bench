import pytest

from company_bench.domain.models import CompanyObservation
from company_bench.domain.scenario import DAIRY_S9_SCENARIO
from company_bench.economy.engine import EconomyEngine


@pytest.fixture
def first_observation() -> CompanyObservation:
    """Return the first company's bounded day-one observation."""
    engine = EconomyEngine()
    state = engine.initial_state(DAIRY_S9_SCENARIO, seed=42)
    return engine.observe(state)[0]
