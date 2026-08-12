"""Provider-neutral recognition of explicit paid-quota exhaustion diagnostics."""

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class QuotaExhaustionMatcher:
    """Match only diagnostics that explicitly describe an exhausted balance."""

    markers: tuple[str, ...]

    def matches(self, message: str) -> bool:
        """Return whether a diagnostic explicitly reports exhausted paid quota."""
        normalized = " ".join(message.casefold().split())
        return any(marker in normalized for marker in self.markers)


QUOTA_EXHAUSTION_MATCHER: Final = QuotaExhaustionMatcher(
    markers=(
        "预扣费额度失败",
        "用户剩余额度",
        "余额不足",
        "额度不足",
        "insufficient balance",
        "insufficient credit",
        "insufficient quota",
        "credit balance is too low",
        "not enough balance",
        "billing hard limit has been reached",
    )
)
