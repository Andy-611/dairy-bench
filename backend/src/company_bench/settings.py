"""Central Dairy Bench runtime paths and execution limits."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field

from company_bench.domain.models import StrictModel

MAX_PARALLELISM = 100


def require_parallelism(value: int, name: str) -> int:
    """Validate one direct concurrency constructor argument."""
    if not 1 <= value <= MAX_PARALLELISM:
        raise ValueError(f"{name} must be between 1 and {MAX_PARALLELISM}")
    return value


class ExecutionLimits(StrictModel):
    """Bound application-level run and provider concurrency."""

    max_concurrent_runs: int = Field(default=MAX_PARALLELISM, ge=1, le=MAX_PARALLELISM)
    max_concurrent_newapi_requests: int = Field(
        default=MAX_PARALLELISM,
        ge=1,
        le=MAX_PARALLELISM,
    )

    @classmethod
    def from_environment(cls) -> ExecutionLimits:
        """Load optional lower limits while keeping 100 as the hard ceiling."""
        return cls(
            max_concurrent_runs=_environment_limit("DAIRY_BENCH_MAX_CONCURRENT_RUNS"),
            max_concurrent_newapi_requests=_environment_limit(
                "DAIRY_BENCH_MAX_CONCURRENT_NEWAPI_REQUESTS"
            ),
        )


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Resolve every mutable runtime path from one project-local root."""

    root: Path

    @classmethod
    def from_environment(cls) -> RuntimePaths:
        """Use DAIRY_BENCH_HOME or the repository's ignored runtime directory."""
        configured = os.getenv("DAIRY_BENCH_HOME", "").strip()
        root = Path(configured) if configured else _repository_root() / ".dairy-bench"
        return cls(root.expanduser().resolve())

    @property
    def database(self) -> Path:
        """Return the current weekly-runtime SQLite database path."""
        return self.root / "data" / "runs-v6.sqlite3"

    @property
    def model_capabilities(self) -> Path:
        """Return the versioned NewAPI model-capability catalog path."""
        return self.root / "credentials" / "newapi-model-capabilities.json"


def _repository_root() -> Path:
    """Return the Dairy Bench Git project root."""
    return Path(__file__).resolve().parents[3]


def _environment_limit(name: str) -> int:
    """Read one integer concurrency limit with the shared default."""
    return int(os.getenv(name, str(MAX_PARALLELISM)))
