"""Tests for bounded application execution settings."""

import pytest
from pydantic import ValidationError

from company_bench.settings import ExecutionLimits


def test_execution_limits_default_to_100(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAIRY_BENCH_MAX_CONCURRENT_RUNS", raising=False)
    monkeypatch.delenv("DAIRY_BENCH_MAX_CONCURRENT_NEWAPI_REQUESTS", raising=False)

    limits = ExecutionLimits.from_environment()

    assert limits.max_concurrent_runs == 100
    assert limits.max_concurrent_newapi_requests == 100


@pytest.mark.parametrize(
    "name",
    (
        "DAIRY_BENCH_MAX_CONCURRENT_RUNS",
        "DAIRY_BENCH_MAX_CONCURRENT_NEWAPI_REQUESTS",
    ),
)
def test_execution_limits_reject_values_above_100(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    monkeypatch.setenv(name, "101")

    with pytest.raises(ValidationError, match="less than or equal to 100"):
        ExecutionLimits.from_environment()
