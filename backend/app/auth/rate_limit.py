"""
Rate limiting for authentication endpoints.

Uses slowapi with Redis backend for distributed rate limiting.
Falls back to in-memory storage if Redis is unavailable.

Configuration is managed via settings:
- RATE_LIMIT_AUTH_PER_MINUTE: Rate limit for auth endpoints (default: 10)
- RATE_LIMIT_API_PER_MINUTE: Rate limit for general API endpoints (default: 100)
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


def _get_redis_storage() -> str | None:
    """
    Get Redis storage URI for rate limiting.

    Returns:
        Redis URI string if available, None otherwise.
    """
    try:
        import redis

        # Build Redis URL from settings
        redis_url = settings.REDIS_URL

        # Test connection
        client = redis.from_url(redis_url, socket_timeout=2)
        client.ping()
        logger.info("Rate limiter using Redis backend: %s", settings.REDIS_HOST)
        return redis_url
    except Exception as e:
        logger.warning(
            "Redis unavailable for rate limiting, falling back to memory storage: %s", str(e)
        )
        return None


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

        return resolve_client_ip(request)

    return key_func


def _create_limiter() -> Limiter:
    """
    Create and configure the rate limiter instance.

    Uses Redis backend if available, otherwise falls back to in-memory storage.

    Returns:
        Configured Limiter instance.
    """
    storage_uri = _get_redis_storage()

    limiter = Limiter(
        key_func=_get_key_func(),
        storage_uri=storage_uri,
        enabled=settings.RATE_LIMIT_ENABLED,
        default_limits=[],  # No default limits; applied per-endpoint
        headers_enabled=True,  # Add rate limit headers to responses
        strategy="fixed-window",  # Simple fixed window strategy
    )

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
