"""Tests for error classification system."""

from app.utils.error_classification import ErrorCategory
from app.utils.error_classification import categorize_error
from app.utils.error_classification import get_retry_delay
from app.utils.error_classification import should_retry


class TestCategorizeError:
    """Tests for categorize_error()."""

    def test_empty_message(self):
        assert categorize_error("") == ErrorCategory.UNKNOWN
        assert categorize_error(None) == ErrorCategory.UNKNOWN  # type: ignore[arg-type]

    # Permanent failures
    def test_private_video(self):
        assert categorize_error("Private video") == ErrorCategory.PRIVATE_OR_REMOVED

    def test_removed_video(self):
        assert categorize_error("This video has been removed") == ErrorCategory.PRIVATE_OR_REMOVED

    def test_private_video_bracket(self):
        assert categorize_error("[Private video]") == ErrorCategory.PRIVATE_OR_REMOVED

    def test_deleted_video(self):
        assert (
            categorize_error("Video has been deleted by the user")
            == ErrorCategory.PRIVATE_OR_REMOVED
        )

    def test_no_longer_available(self):
        assert (
            categorize_error("This content is no longer available")
            == ErrorCategory.PRIVATE_OR_REMOVED
        )

    def test_file_too_large(self):
        assert (
            categorize_error("Video is too long. Maximum duration") == ErrorCategory.FILE_TOO_LARGE
        )

    def test_user_cancelled(self):
        assert categorize_error("Task cancelled by user") == ErrorCategory.USER_CANCELLED

    # Auth/Rate limit
    def test_sign_in_required(self):
        assert categorize_error("Sign-in required to access") == ErrorCategory.AUTH_OR_RATE_LIMIT

    def test_log_in_required(self):
        assert categorize_error("You need to log in") == ErrorCategory.AUTH_OR_RATE_LIMIT

    def test_requires_authentication(self):
        assert (
            categorize_error("This video requires authentication")
            == ErrorCategory.AUTH_OR_RATE_LIMIT
        )

    # System errors
    def test_unique_violation(self):
        assert (
            categorize_error("UniqueViolation: duplicate key value") == ErrorCategory.DUPLICATE_KEY
        )

    def test_duplicate_key(self):
        assert categorize_error("duplicate key constraint on task") == ErrorCategory.DUPLICATE_KEY

    def test_worker_lost(self):
        assert categorize_error("Worker lost connection") == ErrorCategory.WORKER_LOST

    def test_worker_crashed(self):
        assert categorize_error("Worker crashed during processing") == ErrorCategory.WORKER_LOST

    # Resource errors
    def test_cuda_oom(self):
        # Its own code: the GPU-OOM backoff path keys off it, and a host OOM must not (#959).
        assert categorize_error("CUDA out of memory") == ErrorCategory.GPU_OOM

    def test_host_out_of_memory_is_not_gpu_oom(self):
        assert categorize_error("Worker ran out of memory") == ErrorCategory.OOM_ERROR

    def test_oom(self):
        assert categorize_error("Process killed: OOM") == ErrorCategory.OOM_ERROR

    def test_oom_killer_and_oomkilled(self):
        assert categorize_error("oom-killer invoked") == ErrorCategory.OOM_ERROR
        assert categorize_error("container OOMKilled") == ErrorCategory.OOM_ERROR

    def test_oom_inside_a_word_is_not_oom(self):
        # A bare substring match classified "boom"/"room"/"zoom" as out-of-memory.
        assert categorize_error("boom") == ErrorCategory.UNKNOWN
        assert categorize_error("no room left in the zoom recording") == ErrorCategory.UNKNOWN

    # Network errors
    def test_connection_timeout(self):
        assert categorize_error("Connection timeout after 30s") == ErrorCategory.NETWORK_ERROR

    def test_network_error(self):
        assert categorize_error("Network error occurred") == ErrorCategory.NETWORK_ERROR

    def test_rate_limit(self):
        assert categorize_error("Rate limit exceeded") == ErrorCategory.NETWORK_ERROR

    # Temporary service errors
    def test_503_error(self):
        assert (
            categorize_error("HTTP Error 503: Service Unavailable")
            == ErrorCategory.TEMPORARY_SERVICE_ERROR
        )

    def test_502_error(self):
        assert categorize_error("502 Bad Gateway") == ErrorCategory.TEMPORARY_SERVICE_ERROR

    def test_504_error(self):
        assert categorize_error("504 Gateway Timeout") == ErrorCategory.TEMPORARY_SERVICE_ERROR

    # Default to UNKNOWN for unrecognized messages
    def test_unknown_error(self):
        assert categorize_error("Something completely unexpected happened") == ErrorCategory.UNKNOWN


class TestShouldRetry:
    """Tests for should_retry()."""

    def test_permanent_failures_never_retry(self):
        assert should_retry(ErrorCategory.PRIVATE_OR_REMOVED, 0) is False
        assert should_retry(ErrorCategory.USER_CANCELLED, 0) is False
        assert should_retry(ErrorCategory.FILE_TOO_LARGE, 0) is False

    def test_auth_rate_limit_retry_twice(self):
        assert should_retry(ErrorCategory.AUTH_OR_RATE_LIMIT, 0) is True
        assert should_retry(ErrorCategory.AUTH_OR_RATE_LIMIT, 1) is True
        assert should_retry(ErrorCategory.AUTH_OR_RATE_LIMIT, 2) is False

    def test_system_error_respects_max_retries(self):
        assert should_retry(ErrorCategory.SYSTEM_ERROR, 0) is True
        assert should_retry(ErrorCategory.SYSTEM_ERROR, 2) is True
        assert should_retry(ErrorCategory.SYSTEM_ERROR, 3) is False

    def test_duplicate_key_retriable(self):
        assert should_retry(ErrorCategory.DUPLICATE_KEY, 0) is True

    def test_oom_retriable(self):
        assert should_retry(ErrorCategory.OOM_ERROR, 0) is True
        assert should_retry(ErrorCategory.OOM_ERROR, 3) is False

    def test_network_retriable(self):
        assert should_retry(ErrorCategory.NETWORK_ERROR, 0) is True
        assert should_retry(ErrorCategory.NETWORK_ERROR, 3) is False

    def test_custom_max_retries(self):
        assert should_retry(ErrorCategory.SYSTEM_ERROR, 4, max_retries=5) is True
        assert should_retry(ErrorCategory.SYSTEM_ERROR, 5, max_retries=5) is False

    def test_unknown_retriable(self):
        assert should_retry(ErrorCategory.UNKNOWN, 0) is True


class TestGetRetryDelay:
    """Tests for get_retry_delay()."""

    def test_auth_rate_limit_long_backoff(self):
        # 1 hour for first retry
        assert get_retry_delay(ErrorCategory.AUTH_OR_RATE_LIMIT, 0) == 3600
        # 3 hours for second retry
        assert get_retry_delay(ErrorCategory.AUTH_OR_RATE_LIMIT, 1) == 10800

    def test_network_exponential_backoff(self):
        assert get_retry_delay(ErrorCategory.NETWORK_ERROR, 0) == 30
        assert get_retry_delay(ErrorCategory.NETWORK_ERROR, 1) == 60
        assert get_retry_delay(ErrorCategory.NETWORK_ERROR, 2) == 120

    def test_network_backoff_capped_at_5min(self):
        assert get_retry_delay(ErrorCategory.NETWORK_ERROR, 10) == 300

    def test_temporary_service_exponential_backoff(self):
        assert get_retry_delay(ErrorCategory.TEMPORARY_SERVICE_ERROR, 0) == 30
        assert get_retry_delay(ErrorCategory.TEMPORARY_SERVICE_ERROR, 1) == 60

    def test_system_error_immediate_retry(self):
        assert get_retry_delay(ErrorCategory.SYSTEM_ERROR, 0) == 0
        assert get_retry_delay(ErrorCategory.DUPLICATE_KEY, 0) == 0
        assert get_retry_delay(ErrorCategory.WORKER_LOST, 0) == 0

    def test_oom_immediate_retry(self):
        assert get_retry_delay(ErrorCategory.OOM_ERROR, 0) == 0
