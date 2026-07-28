"""Safe diagnostics shared across external adapter boundaries."""


def bounded_error(error: Exception, limit: int = 500) -> str:
    """Return a typed error message without unbounded provider payloads."""
    detail = str(error).strip()
    message = f"{type(error).__name__}: {detail}" if detail else type(error).__name__
    return message[:limit]
