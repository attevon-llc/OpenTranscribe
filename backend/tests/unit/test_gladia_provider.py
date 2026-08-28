"""Unit tests for ``app/services/asr/gladia_provider.py`` — network-free.

All HTTP is mocked: every outbound call now goes through
``app.utils.url_validation.resolve_pinned_target`` + ``pinned_requests_session`` (issue
``fix/gladia-ssrf-result-url``), so the mocking seam moved from the module-level
``requests.post``/``requests.get`` functions to the pinned **session** those helpers hand
back — ``stub_pinned_session`` (``tests/helpers.py``) replaces the session factory with a
``MagicMock`` whose ``.post``/``.get`` are configured per test, exactly as
``tests/test_mediacms_provider.py`` already does for the sibling SSRF fix (issue #444/#594).
``stub_public_dns`` fakes DNS for ``api.gladia.io`` so the (still-real)
``resolve_pinned_target`` validation step needs no network access. ``time.sleep`` is patched
so the poll-loop tests run in milliseconds instead of minutes.

What is pinned here, in order:

1. **Vocabulary truncation now logs a warning (real defect, fixed).**
   ``config.vocabulary[:100]`` still drops any term past index 100 — Gladia's API caps
   ``custom_vocabulary`` at 100 terms — but the truncation is no longer silent: when more than
   100 terms are submitted, a ``logger.warning`` now states how many terms were submitted and
   how many were dropped, matching the style of ``aws_provider.py`` (~L193-203), which logs a
   warning when it cannot fully honor a submitted vocabulary. Pinned by asserting exactly one
   warning-level record naming both counts, while the actually-sent vocabulary (still the first
   100 terms) is unchanged.
2. **``_err_detail`` (L47-58) truncates the raw response body to 500 chars, THEN sanitizes.**
   Pinned by constructing the exact two-step string and asserting the helper's output matches it
   byte-for-byte — proving the order is truncate-then-redact, not redact-then-truncate.
3. **Speaker labels.** Gladia's ``"speaker_0"``-style label is 0-indexed via
   ``normalize_speaker_label``'s underscore branch, and the code only normalizes when
   ``speaker`` is present (``u.get("speaker") is not None``) — a missing key must not raise.
4. **Poll loop timeout.** Caps at exactly 720 iterations (x the loop's 10s sleep = 7200s) and
   raises with a message stating that figure.
5. **File handle safety.** The upload audio file is opened via ``with open(...)`` and is closed
   even when the upload request raises.
6. **``status: "error"`` during polling** raises ``RuntimeError`` with the API key sanitized out
   even when the upstream error message itself echoes the key.
7. **Missing ``result_url``** in the job-submission response raises a specific
   ``RuntimeError("Gladia did not return a result_url")`` before any polling starts.
8. **API key never leaks from the job-submission failure path** (upload failure and poll
   failure are covered by (2) and (6) respectively).
9. **``GLADIA_API_BASE_URL`` env override** is resolved once per instance at construction
   time, and the default is unchanged when the var is unset. The shipped default
   (``https://api.gladia.io``) is exempt from the SSRF guard below (10-12) — resolving it
   would mean every test in this module pays a live DNS lookup.
10. **Poll loop has no consecutive-failure cap** — current behavior, pinned as a
    characterization test, not a target to enforce.
11. **``base_url`` (``UserASRSettings.base_url``, issue #594) is threaded into the real
    request** — a configured base URL is what ``requests.post``/``.get`` are actually
    called against, not just recorded and ignored.
12. **SSRF guard on a non-default base URL.** A private/loopback target is refused at
    construction (``ASRConfigurationError``) unless ``ASR_ALLOW_PRIVATE_ENDPOINTS`` is
    set — mirroring ``LLM_ALLOW_PRIVATE_ENDPOINTS`` — and the same target is accepted
    once the flag is on. The env-var override (9) goes through the identical guard.
13. **``result_url`` — vendor-response data, not operator config — is validated before it
    is EVER fetched**, even when ``base_url`` was the real default. A private/loopback
    ``result_url`` is refused with ``RuntimeError`` and the poll GET is never attempted at
    all (the credential-leak half: the API key header is never sent, because no request is
    made).
14. **Every outbound call passes ``allow_redirects=False``** — upload, job submission, and
    poll all send it explicitly, so a URL that passes validation and then answers with a
    redirect to an internal target is never silently followed (the pin covers exactly one
    hop).
15. **A metadata ``result_url`` is refused even when ``ASR_ALLOW_PRIVATE_ENDPOINTS`` is
    set** — same invariant as the ``base_url`` guard (12): the flag loosens the address
    range, never the cloud-metadata carve-out.

Following the characterization-test convention of ``tests/unit/test_chunking_service.py`` /
``tests/unit/test_transcription_storage.py``, and the network-free provider-test pattern of
``tests/unit/test_pyannote_provider.py``.
"""

from __future__ import annotations

import logging
from typing import cast
from unittest.mock import MagicMock

import pytest
import requests

from app.core.config import settings
from app.core.exceptions import ASRConfigurationError
from app.services.asr.base import normalize_speaker_label
from app.services.asr.gladia_provider import GladiaProvider
from app.services.asr.types import ASRConfig
from tests.helpers import stub_pinned_session
from tests.helpers import stub_public_dns

_BASE = "https://api.gladia.io"
_MODULE = "app.services.asr.gladia_provider"


@pytest.fixture(autouse=True)
def _gladia_host_dns(monkeypatch):
    """Let ``api.gladia.io`` (the real default base URL) resolve without network access.

    Every outbound Gladia request is now re-validated by
    ``app.utils.url_validation.resolve_pinned_target`` immediately before it is pinned and
    sent, so any test using the real default host needs it to resolve to something public —
    exactly the reason ``tests/test_mediacms_provider.py`` stubs its own hosts this way. The
    session itself is still separately mocked per test via ``stub_pinned_session``; this
    fixture only covers the DNS half of validation.
    """
    stub_public_dns(monkeypatch, domain="api.gladia.io")


def _make_provider(api_key: str = "test-key") -> GladiaProvider:
    return GladiaProvider(api_key=api_key)


def _make_audio_file(tmp_path) -> str:
    p = tmp_path / "sample.wav"
    p.write_bytes(b"RIFF....fakeaudiobytes")
    return str(p)


class _FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(
        self,
        json_data: dict | None = None,
        text: str = "",
        status_code: int = 200,
        headers: dict | None = None,
    ):
        self._json = json_data if json_data is not None else {}
        self.text = text
        self.status_code = status_code
        self.headers = headers if headers is not None else {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            # _FakeResponse duck-types requests.Response for gladia_provider.py's
            # purposes (only .text/.status_code are ever read back off it).
            raise requests.exceptions.HTTPError(
                f"{self.status_code} error", response=cast(requests.Response, self)
            )

    def json(self) -> dict:
        return self._json


def _mock_session(monkeypatch, *, post_side_effect=None, get_side_effect=None) -> MagicMock:
    """Build a pinned-session mock and install it via ``stub_pinned_session``.

    Every ``_guarded_request``/poll-loop call in ``gladia_provider.py`` opens a pinned
    session through ``app.utils.url_validation.pinned_requests_session`` — patched here to
    always hand back this ONE mock, so a single object's ``.post``/``.get`` covers upload,
    job submission, and polling within one test, matching how the real code reuses (or
    re-opens) sessions per call.
    """
    session = MagicMock()
    if post_side_effect is not None:
        session.post.side_effect = post_side_effect
    if get_side_effect is not None:
        session.get.side_effect = get_side_effect
    stub_pinned_session(monkeypatch, _MODULE, session)
    return session


def _dispatch_post(upload_resp: _FakeResponse, job_resp: _FakeResponse, job_capture: dict):
    """Build a ``session.post`` side_effect that routes by URL and records the job body."""

    def _post(url, **kwargs):
        if url.endswith("/v2/upload"):
            return upload_resp
        if url.endswith("/v2/transcription"):
            job_capture["body"] = kwargs.get("json")
            return job_resp
        raise AssertionError(f"unexpected POST to {url}")

    return _post


# ── 1. Vocabulary truncation now logs a warning ─────────────────────────────────────────


def test_vocabulary_truncation_logs_a_warning(tmp_path, caplog, monkeypatch):
    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider()
    vocabulary = [f"term-{i}" for i in range(150)]
    config = ASRConfig(enable_diarization=False, language="en", vocabulary=vocabulary)

    job_capture: dict = {}
    upload_resp = _FakeResponse(json_data={"audio_url": "https://cdn.gladia.io/audio123"})
    job_resp = _FakeResponse(json_data={"result_url": f"{_BASE}/v2/transcription/job1"})
    done_resp = _FakeResponse(
        json_data={
            "status": "done",
            "result": {"transcription": {"utterances": [], "languages": ["en"]}},
        }
    )
    _mock_session(
        monkeypatch,
        post_side_effect=_dispatch_post(upload_resp, job_resp, job_capture),
        get_side_effect=lambda *a, **k: done_resp,
    )
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

    with caplog.at_level(logging.WARNING):
        result = provider.transcribe(audio_path, config)

    sent_vocab = job_capture["body"]["custom_vocabulary"]
    assert len(sent_vocab) == 100
    assert sent_vocab == vocabulary[:100]
    assert result.provider_name == "gladia"

    # The fix: exactly one warning-level record identifies the truncation, naming both
    # the submitted count and the dropped count.
    vocab_related = [r for r in caplog.records if "vocab" in r.getMessage().lower()]
    assert len(vocab_related) == 1
    record = vocab_related[0]
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "150" in message
    assert "50" in message


# ── 2. _err_detail: truncate-then-sanitize order ────────────────────────────────────────


def test_err_detail_truncates_body_before_sanitizing():
    provider = _make_provider(api_key="test-secret-value-for-sanitization-check")
    secret = provider._api_key

    # The secret sits well inside the first 500 chars of a much longer raw body, followed
    # by 700 filler characters that must NOT survive the 500-char cap.
    raw_body = f"upstream said: leaked_key={secret} details=" + ("x" * 700)
    exc = requests.exceptions.HTTPError("500 Server Error")
    exc.response = _FakeResponse(text=raw_body)

    detail = provider._err_detail(exc)

    # Directly pin the algorithm: concatenate-and-truncate to 500 chars, THEN sanitize.
    expected = provider._sanitize_error(f"{exc!s} — {raw_body[:500]}", secret)
    assert detail == expected

    # And the practical guarantee that order gives us: the secret, which lives inside the
    # truncated window, is still redacted in the final message.
    assert secret not in detail
    assert "***" in detail
    # Truncation genuinely happened: the tail filler beyond byte 500 of the raw body is gone.
    assert "x" * 700 not in detail


# ── 3. Speaker label normalization ──────────────────────────────────────────────────────


def test_normalize_speaker_label_handles_gladias_speaker_n_format():
    assert normalize_speaker_label("speaker_0") == "SPEAKER_00"
    assert normalize_speaker_label("speaker_5") == "SPEAKER_05"
    assert normalize_speaker_label(None) is None


def test_transcribe_normalizes_speakers_and_tolerates_a_missing_speaker_field(
    tmp_path, monkeypatch
):
    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider()
    config = ASRConfig(enable_diarization=True, language="en")

    upload_resp = _FakeResponse(json_data={"audio_url": "https://cdn.gladia.io/audio123"})
    job_resp = _FakeResponse(json_data={"result_url": f"{_BASE}/v2/transcription/job1"})
    utterances = [
        {"text": "hello", "start": 0.0, "end": 1.0, "speaker": "speaker_0", "words": []},
        {"text": "world", "start": 1.0, "end": 2.0, "speaker": "speaker_5", "words": []},
        # No "speaker" key at all — u.get("speaker") is None; must not raise.
        {"text": "no speaker here", "start": 2.0, "end": 3.0, "words": []},
    ]
    done_resp = _FakeResponse(
        json_data={
            "status": "done",
            "result": {"transcription": {"utterances": utterances, "languages": ["en"]}},
        }
    )
    job_capture: dict = {}
    _mock_session(
        monkeypatch,
        post_side_effect=_dispatch_post(upload_resp, job_resp, job_capture),
        get_side_effect=lambda *a, **k: done_resp,
    )
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

    result = provider.transcribe(audio_path, config)

    assert len(result.segments) == 3
    assert result.segments[0].speaker == "SPEAKER_00"
    assert result.segments[1].speaker == "SPEAKER_05"
    assert result.segments[2].speaker is None
    assert result.language == "en"
    assert result.has_speakers is True


# ── 4. Poll loop timeout ────────────────────────────────────────────────────────────────


def test_poll_loop_times_out_after_720_attempts(tmp_path, monkeypatch):
    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider()
    config = ASRConfig(enable_diarization=False, language="en")

    upload_resp = _FakeResponse(json_data={"audio_url": "https://cdn.gladia.io/audio123"})
    job_resp = _FakeResponse(json_data={"result_url": f"{_BASE}/v2/transcription/job1"})
    # Never reaches "done" or "error" — the loop must exhaust its cap.
    stuck_resp = _FakeResponse(json_data={"status": "processing"})
    job_capture: dict = {}
    session = _mock_session(
        monkeypatch,
        post_side_effect=_dispatch_post(upload_resp, job_resp, job_capture),
        get_side_effect=lambda *a, **k: stuck_resp,
    )
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

    with pytest.raises(RuntimeError, match=r"Gladia transcription timed out after 7200 seconds"):
        provider.transcribe(audio_path, config)

    assert session.get.call_count == 720


# ── 5. File handle safety ───────────────────────────────────────────────────────────────


def test_audio_file_handle_is_closed_when_upload_raises(tmp_path, monkeypatch):
    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider()
    config = ASRConfig(enable_diarization=False, language="en")

    opened: list = []
    real_open = open

    def _tracking_open(path, mode="r", *args, **kwargs):
        fh = real_open(path, mode, *args, **kwargs)
        if str(path) == audio_path:
            opened.append(fh)
        return fh

    def _raise_post(*_args, **_kwargs):
        raise requests.exceptions.ConnectionError("network is down")

    monkeypatch.setattr("builtins.open", _tracking_open)
    _mock_session(monkeypatch, post_side_effect=_raise_post)

    with pytest.raises(RuntimeError, match="Gladia upload failed"):
        provider.transcribe(audio_path, config)

    assert opened, "audio file was never opened via open()"
    assert opened[-1].closed, "audio file handle leaked on the upload exception path"


# ── 6. status: "error" during polling ───────────────────────────────────────────────────


def test_status_error_during_polling_raises_sanitized_runtime_error(tmp_path, monkeypatch):
    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider(api_key="super-secret-poll-key")
    config = ASRConfig(enable_diarization=False, language="en")

    upload_resp = _FakeResponse(json_data={"audio_url": "https://cdn.gladia.io/audio123"})
    job_resp = _FakeResponse(json_data={"result_url": f"{_BASE}/v2/transcription/job1"})
    error_resp = _FakeResponse(
        json_data={
            "status": "error",
            "error_message": "auth rejected for key super-secret-poll-key",
        }
    )
    job_capture: dict = {}
    _mock_session(
        monkeypatch,
        post_side_effect=_dispatch_post(upload_resp, job_resp, job_capture),
        get_side_effect=lambda *a, **k: error_resp,
    )
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

    with pytest.raises(RuntimeError, match="Gladia error") as excinfo:
        provider.transcribe(audio_path, config)

    assert "super-secret-poll-key" not in str(excinfo.value)


# ── 7. Missing result_url in the submit response ────────────────────────────────────────


def test_missing_result_url_raises_runtime_error(tmp_path, monkeypatch):
    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider()
    config = ASRConfig(enable_diarization=False, language="en")

    upload_resp = _FakeResponse(json_data={"audio_url": "https://cdn.gladia.io/audio123"})
    job_resp_no_url = _FakeResponse(json_data={})
    job_capture: dict = {}
    _mock_session(
        monkeypatch,
        post_side_effect=_dispatch_post(upload_resp, job_resp_no_url, job_capture),
    )

    with pytest.raises(RuntimeError, match="Gladia did not return a result_url"):
        provider.transcribe(audio_path, config)


# ── 8. API key never leaks from the job-submission failure path ────────────────────────


def test_api_key_never_leaks_on_job_submission_failure(tmp_path, monkeypatch):
    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider(api_key="super-secret-submit-key")
    config = ASRConfig(enable_diarization=False, language="en")

    upload_resp = _FakeResponse(json_data={"audio_url": "https://cdn.gladia.io/audio123"})

    def _post(url, **kwargs):
        if url.endswith("/v2/upload"):
            return upload_resp
        if url.endswith("/v2/transcription"):
            raise requests.exceptions.ConnectionError(
                "could not submit job for key super-secret-submit-key"
            )
        raise AssertionError(f"unexpected POST to {url}")

    _mock_session(monkeypatch, post_side_effect=_post)

    with pytest.raises(RuntimeError) as excinfo:
        provider.transcribe(audio_path, config)

    assert "super-secret-submit-key" not in str(excinfo.value)


# ── 9. GLADIA_API_BASE_URL env override ─────────────────────────────────────────────────


def test_base_url_env_override_is_honored(monkeypatch):
    monkeypatch.setattr(settings, "ASR_ALLOW_PRIVATE_ENDPOINTS", True, raising=False)
    monkeypatch.setenv("GLADIA_API_BASE_URL", "http://127.0.0.1:5198")
    provider = _make_provider()
    assert provider._base == "http://127.0.0.1:5198"


def test_base_url_default_is_unchanged_when_env_unset(monkeypatch):
    monkeypatch.delenv("GLADIA_API_BASE_URL", raising=False)
    provider = _make_provider()
    assert provider._base == "https://api.gladia.io"


# ── 11/12. base_url wiring + SSRF guard (issue #594) ────────────────────────────────────


def test_configured_base_url_is_used_in_the_outbound_request(monkeypatch, tmp_path):
    """A configured base_url is what the real HTTP calls are made against — not just
    recorded on the instance and ignored, which was the entire bug in #594.
    """
    monkeypatch.setattr(settings, "ASR_ALLOW_PRIVATE_ENDPOINTS", True, raising=False)
    provider = GladiaProvider(
        api_key="test-key", model_name="standard", base_url="http://127.0.0.1:5198"
    )
    assert provider._base == "http://127.0.0.1:5198"

    ok_resp = _FakeResponse({"status": "ok"})
    session = _mock_session(monkeypatch, get_side_effect=lambda *a, **k: ok_resp)

    provider.validate_connection()

    assert session.get.call_count == 1
    called_url = session.get.call_args.args[0]
    assert called_url == "http://127.0.0.1:5198/v2/live"


def test_private_base_url_is_refused_by_default(monkeypatch):
    """Fail closed (issue #594): a private/loopback base_url is refused at
    construction, before any request is attempted, unless explicitly allowed.
    """
    monkeypatch.setattr(settings, "ASR_ALLOW_PRIVATE_ENDPOINTS", False, raising=False)

    with pytest.raises(ASRConfigurationError, match="not a permitted outbound target"):
        GladiaProvider(api_key="test-key", base_url="http://127.0.0.1:5198")


def test_private_base_url_is_allowed_when_the_override_is_set(monkeypatch):
    """The same private base_url that #594's default-off guard refuses is accepted
    once ASR_ALLOW_PRIVATE_ENDPOINTS is on — mirrors LLM_ALLOW_PRIVATE_ENDPOINTS.
    """
    monkeypatch.setattr(settings, "ASR_ALLOW_PRIVATE_ENDPOINTS", True, raising=False)

    provider = GladiaProvider(api_key="test-key", base_url="http://127.0.0.1:5198")

    assert provider._base == "http://127.0.0.1:5198"


def test_metadata_base_url_is_refused_even_with_the_override_set(monkeypatch):
    """allow_private loosens the address RANGE, never the cloud-metadata carve-out —
    same invariant the LLM guard enforces (app/utils/url_validation.py).
    """
    monkeypatch.setattr(settings, "ASR_ALLOW_PRIVATE_ENDPOINTS", True, raising=False)

    with pytest.raises(ASRConfigurationError, match="not a permitted outbound target"):
        GladiaProvider(api_key="test-key", base_url="http://169.254.169.254")


# ── 10. Poll loop has no consecutive-failure cap (current behavior, not a target) ───────


def test_poll_loop_never_caps_consecutive_failures_current_behavior(tmp_path, monkeypatch):
    """Documents a known gap, not a target for this test to enforce.

    Every poll attempt raising an exception is logged and skipped (``continue``) with no
    cap on consecutive failures — the loop only stops once its 720-iteration budget is
    exhausted. Pinned so a future change adding a failure cap is a deliberate, visible
    diff rather than an accidental one.
    """
    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider()
    config = ASRConfig(enable_diarization=False, language="en")

    upload_resp = _FakeResponse(json_data={"audio_url": "https://cdn.gladia.io/audio123"})
    job_resp = _FakeResponse(json_data={"result_url": f"{_BASE}/v2/transcription/job1"})
    job_capture: dict = {}

    def _raise_get(*_a, **_k):
        raise requests.exceptions.ConnectionError("unreachable")

    _mock_session(
        monkeypatch,
        post_side_effect=_dispatch_post(upload_resp, job_resp, job_capture),
        get_side_effect=_raise_get,
    )
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

    with pytest.raises(RuntimeError, match="timed out"):
        provider.transcribe(audio_path, config)


# ── Rate-limit taxonomy (issue Lane 5) ────────────────────────────────────────


def test_upload_429_is_classified_as_asr_rate_limited(tmp_path, monkeypatch):
    from app.services.asr.errors import ASRRateLimitedError

    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider()
    config = ASRConfig(enable_diarization=False, language="en")
    upload_resp = _FakeResponse(status_code=429, text="rate limited")
    _mock_session(monkeypatch, post_side_effect=lambda *a, **k: upload_resp)

    with pytest.raises(ASRRateLimitedError) as excinfo:
        provider.transcribe(audio_path, config)

    assert excinfo.value.provider == "gladia"


def test_job_submission_429_is_classified_as_asr_rate_limited(tmp_path, monkeypatch):
    from app.services.asr.errors import ASRRateLimitedError

    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider()
    config = ASRConfig(enable_diarization=False, language="en")
    upload_resp = _FakeResponse(json_data={"audio_url": "https://cdn.gladia.io/audio123"})
    job_resp = _FakeResponse(status_code=429, text="rate limited")
    job_capture: dict = {}
    _mock_session(monkeypatch, post_side_effect=_dispatch_post(upload_resp, job_resp, job_capture))

    with pytest.raises(ASRRateLimitedError) as excinfo:
        provider.transcribe(audio_path, config)

    assert excinfo.value.provider == "gladia"


def test_poll_429_is_classified_as_asr_rate_limited_not_retried_forever(tmp_path, monkeypatch):
    """Unlike a transport error (covered by the no-cap characterization test above), a
    429 during polling is classified and raised immediately rather than looping — the
    Celery task level owns the retry/backoff policy for a vendor throttle.
    """
    from app.services.asr.errors import ASRRateLimitedError

    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider()
    config = ASRConfig(enable_diarization=False, language="en")
    upload_resp = _FakeResponse(json_data={"audio_url": "https://cdn.gladia.io/audio123"})
    job_resp = _FakeResponse(json_data={"result_url": f"{_BASE}/v2/transcription/job1"})
    job_capture: dict = {}
    poll_resp = _FakeResponse(status_code=429, text="rate limited")
    session = _mock_session(
        monkeypatch,
        post_side_effect=_dispatch_post(upload_resp, job_resp, job_capture),
        get_side_effect=lambda *a, **k: poll_resp,
    )
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

    with pytest.raises(ASRRateLimitedError) as excinfo:
        provider.transcribe(audio_path, config)

    assert excinfo.value.provider == "gladia"
    # Raised on the FIRST poll — proves it did not fall into the retry-and-continue path.
    assert session.get.call_count == 1


def test_upload_400_stays_a_plain_runtime_error(tmp_path, monkeypatch):
    """Negative control: a non-429 HTTP error must NOT be classified as retryable."""
    from app.services.asr.errors import ASRRateLimitedError

    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider()
    config = ASRConfig(enable_diarization=False, language="en")
    upload_resp = _FakeResponse(status_code=400, text="bad request")
    _mock_session(monkeypatch, post_side_effect=lambda *a, **k: upload_resp)

    with pytest.raises(RuntimeError, match="Gladia upload failed") as excinfo:
        provider.transcribe(audio_path, config)

    assert not isinstance(excinfo.value, ASRRateLimitedError)


# ── 13. result_url SSRF guard (NEW — the bug this branch fixes) ─────────────────────────


def test_private_result_url_is_refused_even_with_the_real_default_base_url(tmp_path, monkeypatch):
    """The headline defect: base_url was validated (issue #594), but result_url — data the
    server chose to return — was fetched with no check at all. A private/internal
    result_url must be refused, and refused BEFORE any poll GET is attempted, even though
    base_url here is the real, trusted default.
    """
    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider(api_key="do-not-leak-this-key")
    config = ASRConfig(enable_diarization=False, language="en")

    upload_resp = _FakeResponse(json_data={"audio_url": "https://cdn.gladia.io/audio123"})
    # A malicious/misbehaving result_url pointing at an internal service.
    job_resp = _FakeResponse(json_data={"result_url": "http://127.0.0.1:9999/steal-transcript"})
    job_capture: dict = {}
    session = _mock_session(
        monkeypatch, post_side_effect=_dispatch_post(upload_resp, job_resp, job_capture)
    )

    with pytest.raises(RuntimeError, match="result_url") as excinfo:
        provider.transcribe(audio_path, config)

    assert "permitted outbound target" in str(excinfo.value)
    # The credential-leak half of the bug: the poll GET (which would carry the API key
    # header via self._hdr()) must never be attempted at all — not attempted-then-failed.
    assert session.get.call_count == 0


def test_metadata_result_url_is_refused_even_with_allow_private_endpoints_set(
    tmp_path, monkeypatch
):
    """allow_private loosens the address RANGE, never the cloud-metadata carve-out — same
    invariant as the base_url guard (12), now proven for result_url too.
    """
    monkeypatch.setattr(settings, "ASR_ALLOW_PRIVATE_ENDPOINTS", True, raising=False)
    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider(api_key="do-not-leak-this-key")
    config = ASRConfig(enable_diarization=False, language="en")

    upload_resp = _FakeResponse(json_data={"audio_url": "https://cdn.gladia.io/audio123"})
    job_resp = _FakeResponse(json_data={"result_url": "http://169.254.169.254/latest/meta-data/"})
    job_capture: dict = {}
    session = _mock_session(
        monkeypatch, post_side_effect=_dispatch_post(upload_resp, job_resp, job_capture)
    )

    with pytest.raises(RuntimeError, match="result_url"):
        provider.transcribe(audio_path, config)

    assert session.get.call_count == 0


def test_result_url_is_accepted_and_polled_when_it_is_a_normal_public_address(
    tmp_path, monkeypatch
):
    """Negative control for (13): a legitimate result_url (same host family as base_url)
    is still fetched normally — the new guard must not block real traffic.
    """
    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider()
    config = ASRConfig(enable_diarization=False, language="en")

    upload_resp = _FakeResponse(json_data={"audio_url": "https://cdn.gladia.io/audio123"})
    job_resp = _FakeResponse(json_data={"result_url": f"{_BASE}/v2/transcription/job1"})
    done_resp = _FakeResponse(
        json_data={
            "status": "done",
            "result": {"transcription": {"utterances": [], "languages": ["en"]}},
        }
    )
    job_capture: dict = {}
    session = _mock_session(
        monkeypatch,
        post_side_effect=_dispatch_post(upload_resp, job_resp, job_capture),
        get_side_effect=lambda *a, **k: done_resp,
    )
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

    result = provider.transcribe(audio_path, config)

    assert result.provider_name == "gladia"
    assert session.get.call_count == 1


# ── 14. allow_redirects=False on every outbound call ────────────────────────────────────


def test_every_outbound_call_passes_allow_redirects_false(tmp_path, monkeypatch):
    """The pin covers exactly ONE hop. If any call site omitted ``allow_redirects=False``,
    a URL that passed validation and then answered with a redirect to an internal target
    would be followed with no check at all — closing the same gap
    ``app/services/llm_service.py`` closes for the LLM path (issue #444).
    """
    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider()
    config = ASRConfig(enable_diarization=False, language="en")

    upload_resp = _FakeResponse(json_data={"audio_url": "https://cdn.gladia.io/audio123"})
    job_resp = _FakeResponse(json_data={"result_url": f"{_BASE}/v2/transcription/job1"})
    done_resp = _FakeResponse(
        json_data={
            "status": "done",
            "result": {"transcription": {"utterances": [], "languages": ["en"]}},
        }
    )
    job_capture: dict = {}
    session = _mock_session(
        monkeypatch,
        post_side_effect=_dispatch_post(upload_resp, job_resp, job_capture),
        get_side_effect=lambda *a, **k: done_resp,
    )
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

    provider.transcribe(audio_path, config)

    assert session.post.call_count == 2  # upload, job submission
    for call in session.post.call_args_list:
        assert call.kwargs.get("allow_redirects") is False
    assert session.get.call_count == 1
    assert session.get.call_args.kwargs.get("allow_redirects") is False


# ── 15. A 3xx must not fall through as success (issue #620 LOW bucket) ──────────────────
#
# raise_for_status() only raises on >=400. Every call site sets allow_redirects=False, so a
# server answering with an unfollowed 3xx used to reach .json() on a redirect response body
# instead of a clear, named error.


def test_upload_redirect_names_redirect_and_status_code(tmp_path, monkeypatch):
    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider()
    config = ASRConfig(enable_diarization=False, language="en")
    upload_resp = _FakeResponse(
        status_code=302, headers={"Location": "https://evil.example/redirected"}
    )
    _mock_session(monkeypatch, post_side_effect=lambda *a, **k: upload_resp)

    with pytest.raises(RuntimeError, match="Gladia upload failed") as excinfo:
        provider.transcribe(audio_path, config)

    assert "redirect" in str(excinfo.value)
    assert "302" in str(excinfo.value)


def test_job_submission_redirect_names_redirect_and_status_code(tmp_path, monkeypatch):
    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider()
    config = ASRConfig(enable_diarization=False, language="en")

    upload_resp = _FakeResponse(json_data={"audio_url": "https://cdn.gladia.io/audio123"})
    job_resp = _FakeResponse(
        status_code=307, headers={"Location": "https://evil.example/redirected"}
    )
    job_capture: dict = {}
    _mock_session(
        monkeypatch,
        post_side_effect=_dispatch_post(upload_resp, job_resp, job_capture),
    )

    with pytest.raises(RuntimeError, match="Gladia job submission failed") as excinfo:
        provider.transcribe(audio_path, config)

    assert "redirect" in str(excinfo.value)
    assert "307" in str(excinfo.value)


def test_poll_redirect_is_logged_as_a_named_redirect_not_an_opaque_error(
    tmp_path, monkeypatch, caplog
):
    """The poll loop swallows-and-continues on any exception (documented, unchanged
    behavior — see test 6/test_poll_loop_never_caps_consecutive_failures_current_behavior),
    so a redirect does not raise out of transcribe() here. What must change is the log
    line: it must name the redirect and status code rather than an opaque JSON-parse
    failure from trying to .json() a redirect response body.
    """
    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider()
    config = ASRConfig(enable_diarization=False, language="en")

    upload_resp = _FakeResponse(json_data={"audio_url": "https://cdn.gladia.io/audio123"})
    job_resp = _FakeResponse(json_data={"result_url": f"{_BASE}/v2/transcription/job1"})
    redirect_resp = _FakeResponse(
        status_code=303, headers={"Location": "https://evil.example/redirected"}
    )
    job_capture: dict = {}
    _mock_session(
        monkeypatch,
        post_side_effect=_dispatch_post(upload_resp, job_resp, job_capture),
        get_side_effect=lambda *a, **k: redirect_resp,
    )
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

    with caplog.at_level("WARNING"):
        with pytest.raises(RuntimeError, match="Gladia transcription timed out"):
            provider.transcribe(audio_path, config)

    poll_warnings = [r.message for r in caplog.records if "Gladia poll error" in r.message]
    assert poll_warnings, "expected at least one 'Gladia poll error' warning"
    assert any("redirect" in msg and "303" in msg for msg in poll_warnings), (
        f"expected a poll warning naming the redirect and status code, got: {poll_warnings[:3]}"
    )
