"""Guard the retired daily benchmark boundary."""

import pytest

from company_bench.application import DairyBenchmark
from company_bench.dairy_scenario import DAIRY_S9_SCENARIO


def test_daily_benchmark_rejects_the_v3_scenario() -> None:
    with pytest.raises(ValueError, match="does not implement event-driven V3"):
        DairyBenchmark(DAIRY_S9_SCENARIO)
