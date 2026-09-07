"""Unit tests for the retryable-error taxonomy (``app/services/asr/errors.py``) and its
wiring into the cloud-ASR Celery task (``app/tasks/transcription/core.py``).

Network-free: no provider SDK is imported here. What is pinned:

1. ``ASRRateLimitedError`` is a ``RuntimeError`` subclass (so unclassified call sites keep
   working unmodified) and carries ``provider``/``retry_after``.
2. ``http_status_of`` across the four exception shapes this repo's providers actually raise
   (``status_code``, ``status``, ``response.status_code``, ``code``), plus the ``None`` case.
3. ``retry_after_of`` on the seconds form, the HTTP-date form (must return ``None`` — parsing
   that form is explicitly out of scope), and the absent-header case.
4. ``transcribe_gpu_task`` calls ``self.retry`` for an ``ASRRateLimitedError`` and does NOT
   for a plain ``RuntimeError`` — asserted on the retry call itself, not on
   ``autoretry_for``'s contents (which deliberately does not carry the new type).
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.services.asr.errors import ASRProviderError
from app.services.asr.errors import ASRRateLimitedError
from app.services.asr.errors import http_status_of
from app.services.asr.errors import is_rate_limit_status
from app.services.asr.errors import retry_after_of


class _FakeSDKError(Exception):
    """Stand-in for a real vendor SDK exception.

    Real provider SDK exceptions (openai.RateLimitError, google.api_core exceptions,
    httpx/requests response-bearing errors, ...) declare these attributes themselves;
    plain ``Exception`` does not, so these are declared here purely so tests can assign
    them per-instance (``exc.status_code = 429``) and type-check — the classifier under
    test (``http_status_of``/``retry_after_of``) reads them via ``getattr`` at runtime
    regardless of what a static type says.
    """

    status_code: int
    status: int
    response: object
    code: object


class TestASRRateLimitedError:
    def test_is_a_runtime_error_subclass(self):
        exc = ASRRateLimitedError("throttled")
        assert isinstance(exc, RuntimeError)
        assert isinstance(exc, ASRProviderError)

    def test_carries_provider_and_retry_after(self):
        exc = ASRRateLimitedError("throttled", provider="deepgram", retry_after=30.0)
        assert exc.provider == "deepgram"
        assert exc.retry_after == 30.0

    def test_retry_after_defaults_to_none(self):
        exc = ASRRateLimitedError("throttled", provider="openai")
        assert exc.retry_after is None

    def test_message_is_preserved(self):
        exc = ASRRateLimitedError("deepgram: too many requests")
        assert str(exc) == "deepgram: too many requests"


class TestASRProviderErrorBase:
    def test_unclassified_error_stays_a_plain_runtime_error(self):
        """A generic ASRProviderError (not the rate-limit subclass) must not be mistaken
        for a retryable error by an isinstance(exc, ASRRateLimitedError) check — that is
        the fail-closed guarantee the module docstring promises.
        """
        exc = ASRProviderError("some other provider failure", provider="aws")
        assert isinstance(exc, RuntimeError)
        assert not isinstance(exc, ASRRateLimitedError)


class TestHttpStatusOf:
    def test_status_code_attribute(self):
        exc = _FakeSDKError("boom")
        exc.status_code = 429
        assert http_status_of(exc) == 429

    def test_status_attribute(self):
        """google.api_core exceptions expose `.status`, not `.status_code`."""
        exc = _FakeSDKError("boom")
        exc.status = 429
        assert http_status_of(exc) == 429

    def test_response_status_code_attribute(self):
        """httpx/requests-style exceptions carry status on a nested `.response`."""
        exc = _FakeSDKError("boom")
        exc.response = MagicMock(status_code=429)
        assert http_status_of(exc) == 429

    def test_code_attribute(self):
        exc = _FakeSDKError("boom")
        exc.code = 429
        assert http_status_of(exc) == 429

    def test_no_status_anywhere_returns_none(self):
        assert http_status_of(Exception("boom")) is None

    def test_non_int_status_code_is_ignored(self):
        """Some SDKs set `.code` to a string error code (e.g. botocore's
        `ThrottlingException`) — that must not be misread as an HTTP status.
        """
        exc = _FakeSDKError("boom")
        exc.code = "ThrottlingException"
        assert http_status_of(exc) is None

    def test_status_code_takes_priority_over_response_status_code(self):
        exc = _FakeSDKError("boom")
        exc.status_code = 429
        exc.response = MagicMock(status_code=500)
        assert http_status_of(exc) == 429


class TestIsRateLimitStatus:
    def test_429_is_a_rate_limit(self):
        assert is_rate_limit_status(429) is True

    def test_500_is_not_a_rate_limit(self):
        assert is_rate_limit_status(500) is False

    def test_none_is_not_a_rate_limit(self):
        assert is_rate_limit_status(None) is False


class TestRetryAfterOf:
    def test_seconds_form(self):
        exc = _FakeSDKError("boom")
        exc.response = MagicMock(headers={"Retry-After": "30"})
        assert retry_after_of(exc) == 30.0

    def test_seconds_form_with_decimal(self):
        exc = _FakeSDKError("boom")
        exc.response = MagicMock(headers={"Retry-After": "2.5"})
        assert retry_after_of(exc) == 2.5

    def test_http_date_form_returns_none(self):
        """The HTTP-date form is explicitly out of scope — parsing it correctly needs
        timezone/clock-skew handling the bounded-backoff fallback already covers more
        simply.
        """
        exc = _FakeSDKError("boom")
        exc.response = MagicMock(headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        assert retry_after_of(exc) is None

    def test_absent_header_returns_none(self):
        exc = _FakeSDKError("boom")
        exc.response = MagicMock(headers={})
        assert retry_after_of(exc) is None

    def test_no_response_attribute_returns_none(self):
        assert retry_after_of(Exception("boom")) is None

    def test_response_with_no_headers_attribute_returns_none(self):
        exc = _FakeSDKError("boom")
        exc.response = object()
        assert retry_after_of(exc) is None


# ── transcribe_gpu_task retry wiring ────────────────────────────────────────────────────


@pytest.fixture
def gpu_task_preprocess_context():
    return {
        "task_id": "task-taxonomy-1",
        "file_uuid": "11111111-1111-1111-1111-111111111111",
        "file_id": 1,
        "user_id": 1,
        "storage_path": "files/test.mp3",
        "file_name": "test.mp3",
        "content_type": "audio/mpeg",
        "diarization_source": "provider",
    }


class _FakeProvider:
    provider_name = "deepgram"


def _run_gpu_task_with_cloud_pipeline_raising(preprocess_context, error: Exception):
    """Drive transcribe_gpu_task's cloud-ASR branch down to a raised `error`, with every
    non-taxonomy seam patched to plain no-ops/None so only the exception-handling code
    under test actually executes.
    """
    from app.tasks.transcription import core as core_module

    # `transcribe_gpu_task.__wrapped__` is bound to the real celery.Task instance (the
    # `self` parameter is supplied implicitly), so `self.retry`/`self.request` are patched
    # directly on the task object rather than on a stand-in passed positionally.
    task = core_module.transcribe_gpu_task
    fake_self = task

    def _fake_retry(*, exc, countdown, max_retries):
        # Mirror celery.Task.retry: it raises rather than returning.
        raise _RetrySentinelError(exc=exc, countdown=countdown, max_retries=max_retries)

    with (
        # download_temp_audio is imported LOCALLY inside transcribe_gpu_task, so it is
        # never a `core_module` attribute — patch it at its source module instead.
        patch("app.services.minio_service.download_temp_audio", lambda *a, **kw: None),
        patch.object(core_module, "session_scope") as mock_scope,
        patch.object(core_module, "get_refreshed_object", return_value=None),
        patch.object(core_module, "send_progress_notification"),
        patch.object(core_module, "_resolve_asr_provider_or_none", return_value=_FakeProvider()),
        patch.object(core_module, "_run_cloud_asr_pipeline", side_effect=error),
        patch.object(core_module, "update_task_status"),
        patch.object(core_module, "_get_user_friendly_error_message", return_value="failed"),
        patch.object(core_module, "_handle_transcription_failure"),
        patch("tempfile.TemporaryDirectory") as mock_tmpdir,
        patch.object(task, "retry", side_effect=_fake_retry) as mock_retry,
        patch.object(type(task.request), "retries", 0, create=True),
    ):
        mock_scope.return_value.__enter__.return_value = MagicMock()
        # download_temp_audio is patched to a no-op above, so this string is never used
        # as a real filesystem path — only returned by the mocked TemporaryDirectory.
        mock_tmpdir.return_value.__enter__.return_value = "/tmp/fake"  # noqa: S108
        try:
            task.__wrapped__(preprocess_context)
        except _RetrySentinelError as sentinel:
            return mock_retry, sentinel
        except Exception:
            import traceback

            traceback.print_exc()
            return mock_retry, None
    return mock_retry, None


class _RetrySentinelError(Exception):
    def __init__(self, *, exc, countdown, max_retries):
        super().__init__("retry")
        self.exc = exc
        self.countdown = countdown
        self.max_retries = max_retries


class TestTranscribeGpuTaskRetryWiring:
    def test_asr_rate_limited_error_triggers_self_retry(self, gpu_task_preprocess_context):
        rate_limited = ASRRateLimitedError("throttled", provider="deepgram", retry_after=5.0)
        mock_retry, sentinel = _run_gpu_task_with_cloud_pipeline_raising(
            gpu_task_preprocess_context, rate_limited
        )
        mock_retry.assert_called_once()
        assert sentinel is not None
        assert sentinel.exc is rate_limited
        assert sentinel.countdown == 5.0

    def test_plain_runtime_error_does_not_trigger_self_retry(self, gpu_task_preprocess_context):
        plain_error = RuntimeError("some other cloud ASR failure")
        mock_retry, sentinel = _run_gpu_task_with_cloud_pipeline_raising(
            gpu_task_preprocess_context, plain_error
        )
        mock_retry.assert_not_called()
        assert sentinel is None
