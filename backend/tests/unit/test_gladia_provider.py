"""Unit tests for ``app/services/asr/gladia_provider.py`` — network-free.

All HTTP is mocked (``requests.post`` / ``requests.get``); ``time.sleep`` is patched so the
poll-loop tests run in milliseconds instead of minutes. What is pinned here, in order:

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

Following the characterization-test convention of ``tests/unit/test_chunking_service.py`` /
``tests/unit/test_transcription_storage.py``, and the network-free provider-test pattern of
``tests/unit/test_pyannote_provider.py``.
"""

from __future__ import annotations

import logging
from typing import cast
from unittest.mock import patch

import pytest
import requests

from app.core.config import settings
from app.core.exceptions import ASRConfigurationError
from app.services.asr.base import normalize_speaker_label
from app.services.asr.gladia_provider import GladiaProvider
from app.services.asr.types import ASRConfig

_BASE = "https://api.gladia.io"


class _FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(self, json_data: dict | None = None, text: str = "", status_code: int = 200):
        self._json = json_data if json_data is not None else {}
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            # _FakeResponse duck-types requests.Response for gladia_provider.py's
            # purposes (only .text/.status_code are ever read back off it).
            raise requests.exceptions.HTTPError(
                f"{self.status_code} error", response=cast(requests.Response, self)
            )

    def json(self) -> dict:
        return self._json


def _make_provider(api_key: str = "test-key") -> GladiaProvider:
    return GladiaProvider(api_key=api_key)


def _make_audio_file(tmp_path) -> str:
    p = tmp_path / "sample.wav"
    p.write_bytes(b"RIFF....fakeaudiobytes")
    return str(p)


def _dispatch_post(upload_resp: _FakeResponse, job_resp: _FakeResponse, job_capture: dict):
    """Build a ``requests.post`` side_effect that routes by URL and records the job body."""

    def _post(url, **kwargs):
        if url.endswith("/v2/upload"):
            return upload_resp
        if url.endswith("/v2/transcription"):
            job_capture["body"] = kwargs.get("json")
            return job_resp
        raise AssertionError(f"unexpected POST to {url}")

    return _post


# ── 1. Vocabulary truncation now logs a warning ─────────────────────────────────────────


def test_vocabulary_truncation_logs_a_warning(tmp_path, caplog):
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

    with (
        patch("requests.post", side_effect=_dispatch_post(upload_resp, job_resp, job_capture)),
        patch("requests.get", return_value=done_resp),
        patch("time.sleep"),
        caplog.at_level(logging.WARNING),
    ):
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


def test_transcribe_normalizes_speakers_and_tolerates_a_missing_speaker_field(tmp_path):
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

    with (
        patch("requests.post", side_effect=_dispatch_post(upload_resp, job_resp, job_capture)),
        patch("requests.get", return_value=done_resp),
        patch("time.sleep"),
    ):
        result = provider.transcribe(audio_path, config)

    assert len(result.segments) == 3
    assert result.segments[0].speaker == "SPEAKER_00"
    assert result.segments[1].speaker == "SPEAKER_05"
    assert result.segments[2].speaker is None
    assert result.language == "en"
    assert result.has_speakers is True


# ── 4. Poll loop timeout ────────────────────────────────────────────────────────────────


def test_poll_loop_times_out_after_720_attempts(tmp_path):
    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider()
    config = ASRConfig(enable_diarization=False, language="en")

    upload_resp = _FakeResponse(json_data={"audio_url": "https://cdn.gladia.io/audio123"})
    job_resp = _FakeResponse(json_data={"result_url": f"{_BASE}/v2/transcription/job1"})
    # Never reaches "done" or "error" — the loop must exhaust its cap.
    stuck_resp = _FakeResponse(json_data={"status": "processing"})
    job_capture: dict = {}

    with (
        patch("requests.post", side_effect=_dispatch_post(upload_resp, job_resp, job_capture)),
        patch("requests.get", return_value=stuck_resp) as mock_get,
        patch("time.sleep") as mock_sleep,
        pytest.raises(RuntimeError, match=r"Gladia transcription timed out after 7200 seconds"),
    ):
        provider.transcribe(audio_path, config)

    assert mock_get.call_count == 720
    assert mock_sleep.call_count == 720


# ── 5. File handle safety ───────────────────────────────────────────────────────────────


def test_audio_file_handle_is_closed_when_upload_raises(tmp_path):
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

    with (
        patch("builtins.open", side_effect=_tracking_open),
        patch("requests.post", side_effect=_raise_post),
        pytest.raises(RuntimeError, match="Gladia upload failed"),
    ):
        provider.transcribe(audio_path, config)

    assert opened, "audio file was never opened via open()"
    assert opened[-1].closed, "audio file handle leaked on the upload exception path"


# ── 6. status: "error" during polling ───────────────────────────────────────────────────


def test_status_error_during_polling_raises_sanitized_runtime_error(tmp_path):
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

    with (
        patch("requests.post", side_effect=_dispatch_post(upload_resp, job_resp, job_capture)),
        patch("requests.get", return_value=error_resp),
        patch("time.sleep"),
        pytest.raises(RuntimeError, match="Gladia error") as excinfo,
    ):
        provider.transcribe(audio_path, config)

    assert "super-secret-poll-key" not in str(excinfo.value)


# ── 7. Missing result_url in the submit response ────────────────────────────────────────


def test_missing_result_url_raises_runtime_error(tmp_path):
    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider()
    config = ASRConfig(enable_diarization=False, language="en")

    upload_resp = _FakeResponse(json_data={"audio_url": "https://cdn.gladia.io/audio123"})
    job_resp_no_url = _FakeResponse(json_data={})
    job_capture: dict = {}

    with (
        patch(
            "requests.post",
            side_effect=_dispatch_post(upload_resp, job_resp_no_url, job_capture),
        ),
        pytest.raises(RuntimeError, match="Gladia did not return a result_url"),
    ):
        provider.transcribe(audio_path, config)


# ── 8. API key never leaks from the job-submission failure path ────────────────────────


def test_api_key_never_leaks_on_job_submission_failure(tmp_path):
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

    with (
        patch("requests.post", side_effect=_post),
        pytest.raises(RuntimeError) as excinfo,
    ):
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

    called_urls: list[str] = []

    def _get(url, **_kwargs):
        called_urls.append(url)
        return _FakeResponse({"status": "ok"})

    with patch("requests.get", side_effect=_get):
        provider.validate_connection()

    assert called_urls == ["http://127.0.0.1:5198/v2/live"]


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


def test_poll_loop_never_caps_consecutive_failures_current_behavior(tmp_path):
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

    with (
        patch("requests.post", side_effect=_dispatch_post(upload_resp, job_resp, job_capture)),
        patch("requests.get", side_effect=requests.exceptions.ConnectionError("unreachable")),
        patch("time.sleep"),
        pytest.raises(RuntimeError, match="timed out"),
    ):
        provider.transcribe(audio_path, config)


# ── Rate-limit taxonomy (issue Lane 5) ────────────────────────────────────────


def test_upload_429_is_classified_as_asr_rate_limited(tmp_path):
    from app.services.asr.errors import ASRRateLimitedError

    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider()
    config = ASRConfig(enable_diarization=False, language="en")
    upload_resp = _FakeResponse(status_code=429, text="rate limited")

    with (
        patch("requests.post", return_value=upload_resp),
        pytest.raises(ASRRateLimitedError) as excinfo,
    ):
        provider.transcribe(audio_path, config)

    assert excinfo.value.provider == "gladia"


def test_job_submission_429_is_classified_as_asr_rate_limited(tmp_path):
    from app.services.asr.errors import ASRRateLimitedError

    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider()
    config = ASRConfig(enable_diarization=False, language="en")
    upload_resp = _FakeResponse(json_data={"audio_url": "https://cdn.gladia.io/audio123"})
    job_resp = _FakeResponse(status_code=429, text="rate limited")
    job_capture: dict = {}

    with (
        patch("requests.post", side_effect=_dispatch_post(upload_resp, job_resp, job_capture)),
        pytest.raises(ASRRateLimitedError) as excinfo,
    ):
        provider.transcribe(audio_path, config)

    assert excinfo.value.provider == "gladia"


def test_poll_429_is_classified_as_asr_rate_limited_not_retried_forever(tmp_path):
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

    with (
        patch("requests.post", side_effect=_dispatch_post(upload_resp, job_resp, job_capture)),
        patch("requests.get", return_value=poll_resp) as mock_get,
        patch("time.sleep"),
        pytest.raises(ASRRateLimitedError) as excinfo,
    ):
        provider.transcribe(audio_path, config)

    assert excinfo.value.provider == "gladia"
    # Raised on the FIRST poll — proves it did not fall into the retry-and-continue path.
    assert mock_get.call_count == 1


def test_upload_400_stays_a_plain_runtime_error(tmp_path):
    """Negative control: a non-429 HTTP error must NOT be classified as retryable."""
    from app.services.asr.errors import ASRRateLimitedError

    audio_path = _make_audio_file(tmp_path)
    provider = _make_provider()
    config = ASRConfig(enable_diarization=False, language="en")
    upload_resp = _FakeResponse(status_code=400, text="bad request")

    with (
        patch("requests.post", return_value=upload_resp),
        pytest.raises(RuntimeError, match="Gladia upload failed") as excinfo,
    ):
        provider.transcribe(audio_path, config)

    assert not isinstance(excinfo.value, ASRRateLimitedError)
