"""Central project-local paths for all mutable Dairy Bench runtime data."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
        """Return the single current V4 SQLite database path."""
        return self.root / "data" / "runs.sqlite3"

    @property
    def artifacts(self) -> Path:
        """Return the durable per-invocation trace directory."""
        return self.root / "artifacts"

    @property
    def codex_home(self) -> Path:
        """Return the benchmark-isolated Codex home directory."""
        return self.root / "codex"

    @property
    def credentials(self) -> Path:
        """Return the encrypted local provider-credential directory."""
        return self.root / "credentials"


def _repository_root() -> Path:
    """Return the Dairy Bench Git project root."""
    return Path(__file__).resolve().parents[3]
