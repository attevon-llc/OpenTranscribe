"""Bounded route-label resolution for Prometheus metrics.

``route_label`` maps a FastAPI route template path to a metric label value.
The cache key space is bounded because input must come from
``request.scope["route"].path`` (the registered route template, e.g.
``/api/files/{file_id}``) — never a raw request path with concrete ids, which
would explode cardinality. Unrouted requests (404s, pre-routing failures) pass
``None`` and get ``"unknown"`` without caching.
"""

from functools import lru_cache

UNKNOWN_ROUTE = "unknown"


@lru_cache(maxsize=1024)
def route_label(path: str | None) -> str:
    """Return the route-template label for a request, ``"unknown"`` when absent.

    Args:
        path: ``request.scope["route"].path`` (route template) or ``None``.

    Returns:
        The template string, or ``"unknown"`` when no route matched.
    """
    if not path:
        return UNKNOWN_ROUTE
    return path
