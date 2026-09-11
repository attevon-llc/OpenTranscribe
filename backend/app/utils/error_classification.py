"""
Error classification system for media file processing.

Categorizes errors as permanent or retriable to enable smart retry decisions
and prevent wasting retries on permanent failures like private/removed videos.
"""

import logging
from enum import Enum

logger = logging.getLogger(__name__)


class ErrorCategory(Enum):
    """Categories for classifying processing errors.

    Retry policy only; persisted to `media_file.error_category`; NEVER serialized to a
    client — the user-facing vocabulary is `UserErrorReason`
    (`app.services.error_categorization_service`), on the wire as `error_reason`.
    """

    # Permanent failures - do not retry
    PRIVATE_OR_REMOVED = "private_removed"
    USER_CANCELLED = "user_cancelled"
    FILE_TOO_LARGE = "file_too_large"

    # Auth/Rate limit hybrid - retry with very long backoff
    AUTH_OR_RATE_LIMIT = "auth_or_rate_limit"

    # System errors - retry with same parameters
    SYSTEM_ERROR = "system_error"
    WORKER_LOST = "worker_lost"
    DUPLICATE_KEY = "duplicate_key"

    # Resource errors - retry with reduced resources
    OOM_ERROR = "oom"

    # Transient errors - retry with backoff
    NETWORK_ERROR = "network"
    TEMPORARY_SERVICE_ERROR = "temporary"

    # Unknown - default to retriable system error
    UNKNOWN = "unknown"


# Categories that are eligible for retry (all non-permanent categories).
# This is the single source of truth for retry eligibility checks.
RETRIABLE_CATEGORIES: frozenset[ErrorCategory] = frozenset(
    {
        ErrorCategory.AUTH_OR_RATE_LIMIT,
        ErrorCategory.SYSTEM_ERROR,
        ErrorCategory.WORKER_LOST,
        ErrorCategory.DUPLICATE_KEY,
        ErrorCategory.OOM_ERROR,
        ErrorCategory.NETWORK_ERROR,
        ErrorCategory.TEMPORARY_SERVICE_ERROR,
        ErrorCategory.UNKNOWN,
    }
)


def categorize_error(error_message: str) -> ErrorCategory:
    """Categorize error message to determine retry strategy.

    Args:
        error_message: The error message string to classify.

    Returns:
        ErrorCategory indicating the type of failure.
    """
    if not error_message:
        return ErrorCategory.UNKNOWN

    msg_lower = error_message.lower()

    # Permanent failures - truly private/removed content
    if any(
        x in msg_lower
        for x in [
            "private video",
            "removed",
            "[private video]",
            "been deleted",
            "no longer available",
        ]
    ):
        return ErrorCategory.PRIVATE_OR_REMOVED
    if "too long" in msg_lower and "maximum" in msg_lower:
        return ErrorCategory.FILE_TOO_LARGE
    if "cancelled by user" in msg_lower:
        return ErrorCategory.USER_CANCELLED

    # Auth/Rate limit (could be YouTube throttling, retry with very long backoff)
    if any(
        x in msg_lower
        for x in ["sign-in", "log in", "logged-in", "requires authentication", "sign in"]
    ):
        return ErrorCategory.AUTH_OR_RATE_LIMIT

    # System errors
    if "uniqueviolation" in msg_lower or "duplicate key" in msg_lower:
        return ErrorCategory.DUPLICATE_KEY
    if "worker lost" in msg_lower or "worker crashed" in msg_lower:
        return ErrorCategory.WORKER_LOST

    # Resource errors
    if (
        "out of memory" in msg_lower
        or "oom" in msg_lower
        or ("cuda" in msg_lower and "out of memory" in msg_lower)
    ):
        return ErrorCategory.OOM_ERROR

    # Temporary service errors (check before network to match HTTP status codes first)
    if any(x in msg_lower for x in ["503", "502", "504"]):
        return ErrorCategory.TEMPORARY_SERVICE_ERROR

    # Network errors
    if any(x in msg_lower for x in ["timeout", "connection", "network", "rate limit"]):
        return ErrorCategory.NETWORK_ERROR

    return ErrorCategory.UNKNOWN


def should_retry(error_category: ErrorCategory, retry_count: int, max_retries: int = 3) -> bool:
    """Determine if file should be retried based on error category and retry count.

    Args:
        error_category: The classified error type.
        retry_count: Number of retries already attempted.
        max_retries: Maximum retries allowed for retriable errors.

    Returns:
        True if the file should be retried, False otherwise.
    """
    # Only retry categories in RETRIABLE_CATEGORIES (the single source of truth)
    if error_category not in RETRIABLE_CATEGORIES:
        return False

    # Auth/rate limit: retry up to 2 times (could be YouTube throttling)
    if error_category == ErrorCategory.AUTH_OR_RATE_LIMIT:
        return retry_count < 2

    # All other retriable errors: retry up to max_retries
    return retry_count < max_retries


def get_retry_delay(error_category: ErrorCategory, retry_count: int) -> int:
    """Get retry delay in seconds based on error type and attempt number.

    Args:
        error_category: The classified error type.
        retry_count: Number of retries already attempted.

    Returns:
        Delay in seconds before next retry attempt.
    """
    # Very long backoff for auth/rate limit: 1hr, 3hr
    if error_category == ErrorCategory.AUTH_OR_RATE_LIMIT:
        return int(3600 * (1 + retry_count * 2))

    # Exponential backoff for network errors: 30s, 60s, 120s (max 5min)
    if error_category in (ErrorCategory.NETWORK_ERROR, ErrorCategory.TEMPORARY_SERVICE_ERROR):
        return int(min(30 * (2**retry_count), 300))

    # Immediate retry for system errors (task will be queued anyway)
    return 0
