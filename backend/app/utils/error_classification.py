"""
Error classification system for media file processing.

Categorizes errors as permanent or retriable to enable smart retry decisions
and prevent wasting retries on permanent failures like private/removed videos.
"""

import logging
import random
import re
from enum import Enum

logger = logging.getLogger(__name__)

# "oom" only as its own token ("OOM", "oom-killer", "OOMKilled"), never inside "boom"/"zoom".
_OOM_WORD = re.compile(r"\boom")


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
    # The input itself is unusable: corrupt or undecodable media, no audio track, no
    # speech, an unsupported format. Running it again produces the same failure, so it
    # fails fast and is never retried automatically. Set by
    # ``ErrorCategorizationService.classify_failure`` from the user-facing reason.
    INVALID_MEDIA = "invalid_media"
    # A transient failure that outlasted every automatic retry (the error-retry budget, or the
    # infrastructure-requeue cap). Terminal for the automatic paths; the user's own Retry
    # button still works, because a manual retry clears the category.
    RETRIES_EXHAUSTED = "retries_exhausted"

    # Auth/Rate limit hybrid - retry with very long backoff
    AUTH_OR_RATE_LIMIT = "auth_or_rate_limit"

    # System errors - retry with same parameters
    SYSTEM_ERROR = "system_error"
    WORKER_LOST = "worker_lost"
    DUPLICATE_KEY = "duplicate_key"

    # Resource errors - retry with reduced resources
    OOM_ERROR = "oom"
    # GPU memory exhausted. Its own code because the GPU-OOM backoff path
    # (`identify_oom_error_files`) must key off a stored code, and a host-RAM OOM
    # must not enter it (issue #959).
    GPU_OOM = "gpu_oom"

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
        ErrorCategory.GPU_OOM,
        ErrorCategory.NETWORK_ERROR,
        ErrorCategory.TEMPORARY_SERVICE_ERROR,
        ErrorCategory.UNKNOWN,
    }
)


#: Retriable categories whose cause is the infrastructure (a worker, a GPU, the network),
#: never the input. A classification that lands here outranks an input-shaped user reason:
#: "connection reset while decoding" is a lost connection, not a corrupt file.
INFRASTRUCTURE_CATEGORIES: frozenset[ErrorCategory] = frozenset(
    {
        ErrorCategory.SYSTEM_ERROR,
        ErrorCategory.WORKER_LOST,
        ErrorCategory.OOM_ERROR,
        ErrorCategory.GPU_OOM,
        ErrorCategory.NETWORK_ERROR,
        ErrorCategory.TEMPORARY_SERVICE_ERROR,
    }
)


def is_transient(error_category: ErrorCategory) -> bool:
    """Whether a failure of this category is worth running again unchanged.

    The two-class rule every automatic retry follows: a TRANSIENT failure (the
    infrastructure failed, or the cause is unknown) is requeued within minutes up to the
    retry limit; a PERMANENT one (the input is unusable, the content is gone, the user
    cancelled) fails at once with its reason and is never retried.
    """
    return error_category in RETRIABLE_CATEGORIES


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
    if "cuda" in msg_lower and "out of memory" in msg_lower:
        return ErrorCategory.GPU_OOM
    if "out of memory" in msg_lower or _OOM_WORD.search(msg_lower):
        return ErrorCategory.OOM_ERROR

    # Temporary service errors (check before network to match HTTP status codes first)
    if any(x in msg_lower for x in ["503", "502", "504"]):
        return ErrorCategory.TEMPORARY_SERVICE_ERROR

    # Network errors
    if any(x in msg_lower for x in ["timeout", "connection", "network", "rate limit"]):
        return ErrorCategory.NETWORK_ERROR

    return ErrorCategory.UNKNOWN


def stored_category(value: str | None) -> ErrorCategory:
    """Read the retry category a failure site persisted to ``media_file.error_category``.

    Retry policy keys off this stored code, never off ``last_error_message`` (issue #959):
    that column now holds a fixed user-facing sentence, and re-classifying prose after the
    fact would make rewording a message silently change retry behaviour. A NULL or
    unrecognised value is UNKNOWN — retriable, the same default ``categorize_error`` gives
    an empty message.
    """
    if not value:
        return ErrorCategory.UNKNOWN
    try:
        return ErrorCategory(value)
    except ValueError:
        logger.warning(f"Unrecognised stored error_category {value!r}; treating as unknown")
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


#: Exponential-backoff parameters for an automatic transcription retry, per category:
#: ``(first delay, ceiling)`` in seconds. Short on purpose — a requeued file should be
#: running again within minutes — but long enough that a GPU still full from the run that
#: just ran out of memory, or a dependency that just dropped a connection, has time to recover.
_BACKOFF_SECONDS: dict[ErrorCategory, tuple[int, int]] = {
    ErrorCategory.NETWORK_ERROR: (30, 300),
    ErrorCategory.TEMPORARY_SERVICE_ERROR: (30, 300),
    ErrorCategory.GPU_OOM: (60, 600),
    ErrorCategory.OOM_ERROR: (60, 600),
}
_DEFAULT_BACKOFF_SECONDS = (15, 240)


def transient_retry_delay(error_category: ErrorCategory, retry_count: int) -> int:
    """Seconds to wait before automatic retry number ``retry_count + 1``.

    Exponential backoff with "equal jitter": half the delay is fixed and half random, so
    files that failed together (one OOM, one dropped connection) do not all come back at the
    same instant and fail together again.
    """
    base, ceiling = _BACKOFF_SECONDS.get(error_category, _DEFAULT_BACKOFF_SECONDS)
    delay = min(base * (2 ** max(0, retry_count)), ceiling)
    half = delay / 2
    return int(half + random.uniform(0, half))  # noqa: S311  # nosec B311 - jitter, not crypto
