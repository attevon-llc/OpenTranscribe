"""
Rate limiting for authentication endpoints.

Uses slowapi with Redis backend for distributed rate limiting.
Falls back to in-memory storage if Redis is unavailable.

Configuration is managed via settings:
- RATE_LIMIT_AUTH_PER_MINUTE: Rate limit for auth endpoints (default: 10)
- RATE_LIMIT_API_PER_MINUTE: Rate limit for general API endpoints (default: 100)
- RATE_LIMIT_LLM_OUTBOUND_PER_MINUTE: Rate limit for handlers that fetch a caller-supplied
  LLM base_url (default: 10)
- RATE_LIMIT_ENABLED: Enable/disable rate limiting (default: True)
- RATE_LIMIT_TRUSTED_PROXIES: Comma-separated list of trusted proxy IPs/CIDRs
"""

import logging
from collections.abc import Callable

from fastapi import Request
from fastapi import Response
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from starlette.responses import JSONResponse

from app.core.config import settings

logger = logging.getLogger(__name__)


def _record_degradation(control: str, fallback: str) -> None:
    """Count a security control running without its shared state store.

    Imported lazily and never allowed to raise — a broken metrics backend must not be
    able to turn into a failed request. Same contract as
    ``lockout._record_degradation`` / ``token_service._record_degradation``.

    Args:
        control: The security control that degraded.
        fallback: What it used instead (``local`` = per-process approximation).
    """
    try:
        from app.core.metrics import security_state_degraded_total

        security_state_degraded_total.labels(control=control, fallback=fallback).inc()
    except Exception:  # pragma: no cover - metrics must never break a request
        logger.debug("Could not record security degradation metric", exc_info=True)


def _redis_reachable() -> bool:
    """Probe Redis once, for the startup log line only.

    The result deliberately does **not** decide the limiter's storage — see
    ``_create_limiter``.

    Returns:
        True if Redis answered a PING.
    """
    try:
        import redis

        client = redis.from_url(settings.REDIS_URL, socket_timeout=2)
        client.ping()
        logger.info("Rate limiter using Redis backend: %s", settings.REDIS_HOST)
        return True
    except Exception as e:
        logger.warning(
            "Redis unreachable at rate-limiter startup; limiting will run per-process "
            "until it recovers: %s",
            str(e),
        )
        return False


def _get_key_func() -> Callable[[Request], str]:
    """
    Get the key function for rate limiting.

    Uses the client's IP address as the rate limit key.
    Only trusts X-Forwarded-For header from configured trusted proxies.

    Returns:
        A callable that extracts the client identifier from a request.
    """

    def key_func(request: Request) -> str:
        # Shared with the audit log and login records so all three bucket a request the
        # same way. Note this walks the forwarded chain right-to-left; taking the FIRST
        # X-Forwarded-For entry (as this did) lets a client prepend its own value and
        # pick its own rate-limit bucket when more than one proxy is in front of the
        # app (issue #284 A0.5).
        from app.utils.client_ip import resolve_client_ip

        # slowapi flips this flag when the shared store is unreachable and again when
        # it recovers. Reading it here (a bool, on the one function that runs for every
        # rate-limited request) is what makes the degraded window visible in Prometheus
        # instead of only in a log line nobody alerts on.
        if getattr(limiter, "_storage_dead", False):
            _record_degradation("rate_limit", "local")

        return resolve_client_ip(request)

    return key_func


def user_or_ip_key(request: Request) -> str:
    """Rate-limit key: the authenticated user's id, falling back to client IP.

    Used for handlers where a per-IP bucket is meaningless — a router with no
    admin gate, behind nginx, where every request currently shares one IP-derived
    bucket per proxy hop until ``RATE_LIMIT_TRUSTED_PROXIES`` is configured (issue
    #668). Keying on the user id sidesteps that: it is set by
    ``get_current_active_user`` onto ``request.state.user_id`` while resolving the
    endpoint's own ``current_user`` dependency, which FastAPI runs *before* calling
    the ``@limiter.limit``-wrapped endpoint function, so it is already present by
    the time this key func runs. Falls back to the shared IP resolver for any
    request that never resolved a user (should not happen on an authenticated
    route, but a key func must never raise).

    Args:
        request: The current request.

    Returns:
        ``f"user:{id}"`` when a user id is known, else the resolved client IP.
    """
    user_id = getattr(request.state, "user_id", None)
    if user_id is not None:
        return f"user:{user_id}"

    from app.utils.client_ip import resolve_client_ip

    return resolve_client_ip(request)


def _create_limiter() -> Limiter:
    """
    Create and configure the rate limiter instance.

    The limiter is **always** pointed at Redis, and slowapi's own in-memory fallback
    is enabled so a Redis outage degrades to per-process counting and then recovers.

    This used to probe Redis once, at import, and pass ``storage_uri=None`` — i.e.
    ``memory://`` — if that single probe failed. Because the module-level ``limiter``
    is bound into every ``@limiter.limit(...)`` decorator at import time, that choice
    was permanent for the process lifetime: a Redis blip during startup left the whole
    replica counting requests in its own memory forever, so N replicas behind a load
    balancer meant N x the configured auth rate limit and no shared throttle at all.

    With a real ``storage_uri``, slowapi marks the storage dead on the first failed
    operation, serves the same route limits from ``MemoryStorage``, and re-checks the
    backend on an exponential backoff (``Limiter.__should_check_backend``), clearing
    the flag as soon as Redis answers. That is the library's own re-probe — preferred
    over bolting a second one onto its internals.

    Returns:
        Configured Limiter instance.
    """
    _redis_reachable()

    kwargs = {
        "key_func": _get_key_func(),
        "enabled": settings.RATE_LIMIT_ENABLED,
        "default_limits": [],  # No default limits; applied per-endpoint
        "headers_enabled": True,  # Add rate limit headers to responses
        "strategy": "fixed-window",  # Simple fixed window strategy
        # Without this, a dead storage raises out of the limiter instead of degrading;
        # with it and an empty fallback list, slowapi re-evaluates the route's OWN
        # limits against in-memory storage, so limits stay enforced while degraded.
        "in_memory_fallback_enabled": True,
    }

    try:
        # Building the storage does NOT connect (limits is lazy), so an unreachable
        # Redis lands here happily and degrades at first use. This only catches a
        # genuinely unusable URI or a missing redis client library.
        limiter = Limiter(storage_uri=settings.REDIS_URL, **kwargs)  # type: ignore[arg-type]
    except Exception as e:
        logger.error(
            "Cannot build a Redis-backed rate limiter (%s); falling back to per-process "
            "counting. Rate limits will NOT be shared across replicas.",
            e,
        )
        _record_degradation("rate_limit", "local")
        limiter = Limiter(**kwargs)  # type: ignore[arg-type]

    if not settings.RATE_LIMIT_ENABLED:
        logger.info("Rate limiting is DISABLED via configuration")

    return limiter


# Global limiter instance
limiter = _create_limiter()


def get_auth_rate_limit() -> str:
    """
    Get the rate limit string for authentication endpoints.

    Returns:
        Rate limit string in slowapi format (e.g., "10/minute").
    """
    return f"{settings.RATE_LIMIT_AUTH_PER_MINUTE}/minute"


def get_api_rate_limit() -> str:
    """
    Get the rate limit string for general API endpoints.

    Returns:
        Rate limit string in slowapi format (e.g., "100/minute").
    """
    return f"{settings.RATE_LIMIT_API_PER_MINUTE}/minute"


def get_llm_outbound_rate_limit() -> str:
    """Rate limit string for handlers that fetch a caller-supplied LLM ``base_url``.

    Covers ``POST /llm-settings/test`` and the ``GET .../models`` discovery
    handlers (issue #676) — deliberately tighter than :func:`get_api_rate_limit`,
    see the ``RATE_LIMIT_LLM_OUTBOUND_PER_MINUTE`` docstring in ``core/config.py``.

    Returns:
        Rate limit string in slowapi format (e.g., "10/minute").
    """
    return f"{settings.RATE_LIMIT_LLM_OUTBOUND_PER_MINUTE}/minute"


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> Response:
    """
    Exception handler for rate limit exceeded errors.

    Logs the rate-limited request and returns a 429 Too Many Requests response.

    Args:
        request: The FastAPI request object.
        exc: The RateLimitExceeded exception.

    Returns:
        JSONResponse with 429 status code and error details.
    """
    # Same resolver as key_func, so the logged address matches the bucketed one.
    from app.utils.client_ip import resolve_client_ip

    client_ip = resolve_client_ip(request)

    # Log the rate-limited request
    logger.warning(
        "Rate limit exceeded: IP=%s, path=%s, method=%s, limit=%s",
        client_ip,
        request.url.path,
        request.method,
        exc.detail,
    )

    # Return a structured error response
    return JSONResponse(
        status_code=429,
        content={
            "detail": "Too many requests. Please try again later.",
            "retry_after": exc.detail,
        },
        headers={
            "Retry-After": str(_extract_retry_seconds(exc.detail)),
            "X-RateLimit-Limit": exc.detail,
        },
    )


def _extract_retry_seconds(limit_detail: str) -> int:
    """
    Extract retry seconds from rate limit detail string.

    Args:
        limit_detail: Rate limit detail string (e.g., "10 per 1 minute").

    Returns:
        Number of seconds to wait before retrying.
    """
    # Default retry time based on limit window
    if "minute" in limit_detail.lower():
        return 60
    elif "hour" in limit_detail.lower():
        return 3600
    elif "second" in limit_detail.lower():
        return 1
    elif "day" in limit_detail.lower():
        return 86400
    return 60  # Default to 1 minute
