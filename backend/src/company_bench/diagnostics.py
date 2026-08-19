"""Safe diagnostics shared across external adapter boundaries."""

from collections.abc import Iterable


def bounded_error(
    error: Exception,
    limit: int = 500,
    *,
    secrets: Iterable[str] = (),
) -> str:
    """Return a typed error message without unbounded provider payloads."""
    detail = str(error).strip()
    for secret in secrets:
        if secret:
            detail = detail.replace(secret, "[REDACTED]")
    message = f"{type(error).__name__}: {detail}" if detail else type(error).__name__
    return message[:limit]
