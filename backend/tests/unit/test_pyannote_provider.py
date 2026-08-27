"""Unit tests for the pyannote.ai STT Orchestration provider.

Network-free: every ``httpx`` call (upload-URL POST, audio PUT, job-submission POST,
polling GET) is mocked, following the network-free provider-test pattern of
``tests/unit/test_gladia_provider.py``. What is pinned here, in order:

1. **Model resolution** (`_resolve_models`) and **`_parse_response`**'s 3 output shapes
   (turn+word happy path, diarization-only fallback, empty output), against the documented
   pyannote.ai job `output` schema (turnLevelTranscription / wordLevelTranscription, where
   each entry's token key is "text", per the get-job TranscriptionSegment schema) — plus the
   word-to-turn time-window tolerance boundary (`start >= turn_start - 0.01` and
   `start < turn_end + 0.01`, so the lower edge is inclusive and the upper edge is not) and
   the case where a turn matches no words, which must leave `avg_confidence` explicitly
   `None` rather than `0.0` or omitted.
2. **`transcribe()`'s full 5-step orchestration**: the upload-URL request, the audio PUT to
   that URL, job-submission body construction (`numSpeakers`/`minSpeakers`/`maxSpeakers`
   logic), job-submission error handling via `_err_detail`, the polling loop (poll-error
   retry, `status == "failed"` handling, progress-callback sequencing), and final
   `ASRResult`/`has_speakers` assembly.
3. **The 300-second poll timeout — the SHORTEST of any provider in this codebase.**
   Contrast Gladia's 7200s (`gladia_provider.py`, 720 x 10s) and Azure's 7200s
   (`azure_provider.py`). A long file fails on pyannote.ai before any other cloud ASR
   provider this app supports.
4. **`validate_connection()`**: no-key, 200/401/other-status, `ImportError` (httpx
   unavailable), and the generic-exception-gets-sanitized path.
5. **`_err_detail()`**: the `httpx.HTTPStatusError` body-extraction branch, its failure
   fallback, and the non-`HTTPStatusError` path that appends no body at all.
6. **Rate-limit taxonomy (issue Lane 5)**: the three PRE-poll HTTP call sites (upload-URL
   request, audio PUT, job submission) classify an HTTP 429 as `ASRRateLimitedError` via
   `_raise_if_rate_limited`; a 429 seen DURING polling deliberately stays transient (see
   the existing poll-error retry above) rather than being classified — that distinction is
   pinned by `test_poll_429_stays_transient_and_is_retried_not_raised`.
"""

from __future__ import annotations

import logging
import sys
from unittest.mock import patch

import httpx
import pytest

import app.services.asr.pyannote_provider as pyannote_provider_module
from app.services.asr.pyannote_provider import PyAnnoteProvider
from app.services.asr.types import ASRConfig


def _provider(model_name: str = "parakeet", api_key: str = "test-key") -> PyAnnoteProvider:
    return PyAnnoteProvider(api_key=api_key, model_name=model_name)


def _make_audio_file(tmp_path) -> str:
    p = tmp_path / "sample.wav"
    p.write_bytes(b"RIFF....fakeaudiobytes")
    return str(p)


class _FakeResponse:
    """Minimal stand-in for ``httpx.Response`` — same pattern as ``test_gladia_provider.py``."""

    def __init__(self, json_data: dict | None = None, text: str = "", status_code: int = 200):
        self._json = json_data if json_data is not None else {}
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            req = httpx.Request("POST", f"{pyannote_provider_module._BASE_URL}/v1/diarize")
            raise httpx.HTTPStatusError(f"{self.status_code} error", request=req, response=self)

    def json(self) -> dict:
        return self._json


def _dispatch_post(
    upload_url_resp: _FakeResponse, job_resp: _FakeResponse, job_capture: dict[str, dict]
):
    """Build an ``httpx.post`` side_effect that routes by URL and records the job body."""

    def _post(url, **kwargs):
        if url.endswith("/v1/media/input"):
            return upload_url_resp
        if url.endswith("/v1/diarize"):
            job_capture["body"] = kwargs.get("json") or {}
            return job_resp
        raise AssertionError(f"unexpected POST to {url}")

    return _post


def _quick_succeed_output() -> dict:
    return {
        "turnLevelTranscription": [
            {"start": 0.0, "end": 1.0, "text": "hi", "speaker": "SPEAKER_00"},
        ],
    }


def _run_transcribe_capture_job_body(tmp_path, config: ASRConfig) -> dict:
    """Drive a full success-path ``transcribe()`` call and return the submitted job body."""
    audio_path = _make_audio_file(tmp_path)
    provider = _provider()
    job_capture: dict[str, dict] = {}
    upload_url_resp = _FakeResponse(json_data={"url": "https://upload.example.com/put-url"})
    job_resp = _FakeResponse(json_data={"jobId": "job-1"})
    succeeded_resp = _FakeResponse(
        json_data={"status": "succeeded", "output": _quick_succeed_output()}
    )
    with (
        patch("httpx.post", side_effect=_dispatch_post(upload_url_resp, job_resp, job_capture)),
        patch("httpx.put", return_value=_FakeResponse()),
        patch("httpx.get", return_value=succeeded_resp),
        patch("time.sleep"),
    ):
        provider.transcribe(audio_path, config)
    return job_capture["body"]


def _progress_recorder():
    calls: list[tuple[float, str]] = []

    def _cb(frac: float, msg: str) -> None:
        calls.append((frac, msg))

    return _cb, calls


def test_model_resolution_uses_correct_pyannote_ids():
    # Transcription model ids are the live API's transcriptionConfig.model enum values
    # (verified against the API — NO "nvidia-" prefix despite the docs); diarization must
    # be precision-2 (the only model that supports transcription).
    assert _provider("parakeet")._resolve_models() == (
        "precision-2",
        "parakeet-tdt-0.6b-v3",
    )
    assert _provider("whisper-large-v3-turbo")._resolve_models() == (
        "precision-2",
        "faster-whisper-large-v3-turbo",
    )
    # Unknown model falls back to parakeet.
    assert _provider("bogus")._resolve_models()[1] == "parakeet-tdt-0.6b-v3"


def test_parse_response_reads_text_field_and_attaches_words():
    output = {
        "turnLevelTranscription": [
            {"start": 0.5, "end": 2.3, "text": "Hello, how are you?", "speaker": "SPEAKER_00"},
            {"start": 2.5, "end": 4.0, "text": "I am fine thanks", "speaker": "SPEAKER_01"},
        ],
        "wordLevelTranscription": [
            {"start": 0.5, "end": 0.8, "text": "Hello,", "speaker": "SPEAKER_00"},
            {"start": 0.9, "end": 1.2, "text": "how", "speaker": "SPEAKER_00"},
            {"start": 1.3, "end": 1.6, "text": "are", "speaker": "SPEAKER_00"},
            {"start": 1.7, "end": 2.3, "text": "you?", "speaker": "SPEAKER_00"},
            {"start": 2.5, "end": 2.8, "text": "I", "speaker": "SPEAKER_01"},
            {"start": 2.9, "end": 3.2, "text": "am", "speaker": "SPEAKER_01"},
            {"start": 3.3, "end": 3.6, "text": "fine", "speaker": "SPEAKER_01"},
            {"start": 3.7, "end": 4.0, "text": "thanks", "speaker": "SPEAKER_01"},
        ],
    }
    segments = _provider()._parse_response(output)

    assert len(segments) == 2
    assert segments[0].text == "Hello, how are you?"
    assert segments[1].text == "I am fine thanks"
    # Two distinct, non-null speakers.
    assert segments[0].speaker and segments[1].speaker
    assert segments[0].speaker != segments[1].speaker
    # Words must be populated from the "text" key — the regression this guards against is
    # reading "word" (absent), which would leave every token an empty string.
    assert [w.word for w in segments[0].words] == ["Hello,", "how", "are", "you?"]
    assert [w.word for w in segments[1].words] == ["I", "am", "fine", "thanks"]
    assert all(w.word for seg in segments for w in seg.words)


def test_parse_response_falls_back_to_diarization_only():
    output = {
        "diarization": [
            {"start": 0.0, "end": 1.5, "speaker": "SPEAKER_00"},
            {"start": 1.5, "end": 3.0, "speaker": "SPEAKER_01"},
        ]
    }
    segments = _provider()._parse_response(output)
    assert len(segments) == 2
    assert all(s.text == "" for s in segments)
    assert segments[0].speaker and segments[1].speaker


def test_parse_response_empty_output_is_safe():
    segments = _provider()._parse_response({})
    assert len(segments) == 1
    assert segments[0].text == ""


def test_parse_response_word_boundary_is_inclusive_low_exclusive_high():
    turn_start, turn_end = 10.0, 20.0
    output = {
        "turnLevelTranscription": [
            {
                "start": turn_start,
                "end": turn_end,
                "text": "boundary turn",
                "speaker": "SPEAKER_00",
            },
        ],
        "wordLevelTranscription": [
            # Exactly at the lower tolerance boundary (turn_start - 0.01) -> included (>=).
            {"start": 9.99, "end": 10.1, "text": "included_low", "speaker": "SPEAKER_00"},
            # 0.01 further out than the boundary -> excluded.
            {"start": 9.98, "end": 10.0, "text": "excluded_low", "speaker": "SPEAKER_00"},
            # Just inside the upper tolerance boundary -> included (< turn_end + 0.01).
            {"start": 20.005, "end": 20.2, "text": "included_high", "speaker": "SPEAKER_00"},
            # Exactly at the upper tolerance boundary -> excluded (strict <, not <=).
            {"start": 20.01, "end": 20.3, "text": "excluded_high", "speaker": "SPEAKER_00"},
        ],
    }
    segments = _provider()._parse_response(output)
    assert len(segments) == 1
    words = [w.word for w in segments[0].words]
    assert words == ["included_low", "included_high"]


def test_parse_response_turn_with_no_matching_words_has_none_confidence():
    output = {
        "turnLevelTranscription": [
            {"start": 0.0, "end": 1.0, "text": "no words attach here", "speaker": "SPEAKER_00"},
        ],
        "wordLevelTranscription": [
            # Falls well outside the turn's [start-0.01, end+0.01) window.
            {"start": 50.0, "end": 50.5, "text": "elsewhere", "speaker": "SPEAKER_00"},
        ],
    }
    segments = _provider()._parse_response(output)
    assert len(segments) == 1
    assert segments[0].words == []
    # avg_confidence must stay explicitly None, not 0.0 or a falsy default, when no
    # word falls inside the turn's window.
    assert segments[0].confidence is None


# ── validate_connection() ────────────────────────────────────────────────────────────────


def test_validate_connection_requires_api_key():
    provider = PyAnnoteProvider(api_key="", model_name="parakeet")
    ok, message, ms = provider.validate_connection()
    assert ok is False
    assert message == "API key is required for pyannote.ai"
    assert ms >= 0


def test_validate_connection_success():
    provider = _provider()
    with patch("httpx.get", return_value=_FakeResponse(status_code=200)):
        ok, message, ms = provider.validate_connection()
    assert ok is True
    assert message == "Connected to pyannote.ai"
    assert ms >= 0


def test_validate_connection_invalid_key_returns_401_message():
    provider = _provider()
    with patch("httpx.get", return_value=_FakeResponse(status_code=401)):
        ok, message, ms = provider.validate_connection()
    assert ok is False
    assert message == "Invalid API key"


def test_validate_connection_other_http_status_is_reported_verbatim():
    provider = _provider()
    with patch("httpx.get", return_value=_FakeResponse(status_code=503)):
        ok, message, ms = provider.validate_connection()
    assert ok is False
    assert message == "pyannote.ai returned HTTP 503"


def test_validate_connection_import_error_when_httpx_unavailable(monkeypatch):
    provider = _provider()
    # `import httpx` inside validate_connection() raises ImportError when the module
    # is unimportable; sys.modules[name] = None reproduces that without uninstalling it.
    monkeypatch.setitem(sys.modules, "httpx", None)
    ok, message, ms = provider.validate_connection()
    assert ok is False
    assert message == "httpx package not installed (required for pyannote.ai)"
    assert ms >= 0


def test_validate_connection_generic_exception_is_sanitized():
    provider = _provider(api_key="sk-super-secret-999")
    boom = Exception("connection reset while using key sk-super-secret-999")
    with patch("httpx.get", side_effect=boom):
        ok, message, ms = provider.validate_connection()
    assert ok is False
    assert message.startswith("Connection failed:")
    assert "sk-super-secret-999" not in message
    assert "***" in message


# ── _err_detail() ─────────────────────────────────────────────────────────────────────────


def test_err_detail_includes_http_status_error_response_body():
    provider = _provider(api_key="test-key")
    req = httpx.Request("POST", f"{pyannote_provider_module._BASE_URL}/v1/diarize")
    resp = httpx.Response(400, request=req, text="numSpeakers must be a positive integer")
    exc = httpx.HTTPStatusError("Client error '400 Bad Request'", request=req, response=resp)

    detail = provider._err_detail(exc)

    assert "numSpeakers must be a positive integer" in detail
    assert "400 Bad Request" in detail


def test_err_detail_falls_back_when_response_body_read_fails():
    provider = _provider()
    req = httpx.Request("POST", f"{pyannote_provider_module._BASE_URL}/v1/diarize")

    class _BrokenTextResponse:
        @property
        def text(self):
            raise RuntimeError("body already consumed")

    exc = httpx.HTTPStatusError("Client error", request=req, response=_BrokenTextResponse())

    detail = provider._err_detail(exc)

    # No body could be read, so the detail is exactly the sanitized base message — no
    # crash, and no dangling " — " separator with nothing after it.
    assert detail == provider._sanitize_error(str(exc), provider._api_key)
    assert "—" not in detail


def test_err_detail_does_not_append_body_for_non_http_status_errors():
    provider = _provider()
    exc = ValueError("not an HTTP error at all")

    detail = provider._err_detail(exc)

    assert detail == "not an HTTP error at all"
    assert "—" not in detail


# ── transcribe(): step 1-2 (upload URL + audio PUT) ─────────────────────────────────────


def test_transcribe_raises_file_not_found_before_any_network_call():
    provider = _provider()
    config = ASRConfig(language="en")
    with pytest.raises(FileNotFoundError, match="Audio file not found"):
        provider.transcribe("/nonexistent/path/audio.wav", config)


def test_transcribe_upload_url_request_failure_is_wrapped(tmp_path):
    audio_path = _make_audio_file(tmp_path)
    provider = _provider()
    config = ASRConfig(language="en")

    with (
        patch("httpx.post", side_effect=Exception("DNS resolution failed")) as mock_post,
        patch("httpx.put") as mock_put,
        pytest.raises(RuntimeError, match="pyannote.ai upload URL request failed"),
    ):
        provider.transcribe(audio_path, config)

    mock_post.assert_called_once()  # never reaches job submission (2nd POST)
    mock_put.assert_not_called()  # never reaches step 2 at all


def test_transcribe_audio_upload_put_failure_is_wrapped(tmp_path):
    audio_path = _make_audio_file(tmp_path)
    provider = _provider()
    config = ASRConfig(language="en")
    upload_url_resp = _FakeResponse(json_data={"url": "https://upload.example.com/put-url"})

    with (
        patch("httpx.post", return_value=upload_url_resp) as mock_post,
        patch("httpx.put", side_effect=Exception("connection reset")),
        pytest.raises(RuntimeError, match="pyannote.ai audio upload failed"),
    ):
        provider.transcribe(audio_path, config)

    mock_post.assert_called_once()  # job submission (2nd POST) never reached


# ── transcribe(): step 3 (job submission body + error handling) ────────────────────────


def test_job_body_uses_num_speakers_hint_when_configured(tmp_path):
    config = ASRConfig(language="en", num_speakers=3, min_speakers=1, max_speakers=20)
    body = _run_transcribe_capture_job_body(tmp_path, config)
    assert body["numSpeakers"] == 3
    assert "minSpeakers" not in body
    assert "maxSpeakers" not in body


def test_job_body_uses_min_max_speaker_hints_when_num_speakers_not_set(tmp_path):
    config = ASRConfig(language="en", num_speakers=None, min_speakers=2, max_speakers=5)
    body = _run_transcribe_capture_job_body(tmp_path, config)
    assert "numSpeakers" not in body
    assert body["minSpeakers"] == 2
    assert body["maxSpeakers"] == 5


def test_job_body_omits_speaker_hints_at_library_defaults(tmp_path):
    # ASRConfig()'s defaults are min_speakers=1, max_speakers=20, num_speakers=None —
    # none of the three speaker-hint conditions fire.
    config = ASRConfig(language="en")
    body = _run_transcribe_capture_job_body(tmp_path, config)
    assert "numSpeakers" not in body
    assert "minSpeakers" not in body
    assert "maxSpeakers" not in body
    # Base fields are always present regardless of the speaker-hint branch taken.
    assert body["model"] == "precision-2"
    assert body["transcriptionConfig"] == {"model": "parakeet-tdt-0.6b-v3"}
    assert body["transcription"] is True
    assert body["confidence"] is True


def test_transcribe_job_submission_error_surfaces_response_body_via_err_detail(tmp_path):
    audio_path = _make_audio_file(tmp_path)
    provider = _provider(api_key="secret-key-xyz")
    config = ASRConfig(language="en")
    upload_url_resp = _FakeResponse(json_data={"url": "https://upload.example.com/put-url"})
    job_resp = _FakeResponse(status_code=400, text="Invalid numSpeakers: must be >= 1")
    job_capture: dict[str, dict] = {}

    with (
        patch("httpx.post", side_effect=_dispatch_post(upload_url_resp, job_resp, job_capture)),
        patch("httpx.put", return_value=_FakeResponse()),
        pytest.raises(RuntimeError) as excinfo,
    ):
        provider.transcribe(audio_path, config)

    message = str(excinfo.value)
    assert "pyannote.ai job submission failed" in message
    assert "Invalid numSpeakers: must be >= 1" in message
    assert "secret-key-xyz" not in message


# ── transcribe(): step 4 (polling loop) ─────────────────────────────────────────────────


def test_transcribe_poll_error_is_retried_not_fatal(tmp_path, caplog):
    audio_path = _make_audio_file(tmp_path)
    provider = _provider()
    config = ASRConfig(language="en")
    job_capture: dict[str, dict] = {}
    upload_url_resp = _FakeResponse(json_data={"url": "https://upload.example.com/put-url"})
    job_resp = _FakeResponse(json_data={"jobId": "job-1"})
    succeeded_resp = _FakeResponse(
        json_data={"status": "succeeded", "output": _quick_succeed_output()}
    )

    with (
        patch("httpx.post", side_effect=_dispatch_post(upload_url_resp, job_resp, job_capture)),
        patch("httpx.put", return_value=_FakeResponse()),
        patch(
            "httpx.get", side_effect=[Exception("transient network blip"), succeeded_resp]
        ) as mock_get,
        patch("time.sleep"),
        caplog.at_level(logging.WARNING),
    ):
        result = provider.transcribe(audio_path, config)

    # The transient poll error did not abort the job — it retried and completed.
    assert mock_get.call_count == 2
    assert result.segments[0].text == "hi"
    assert any("poll error" in r.getMessage().lower() for r in caplog.records)


@pytest.mark.parametrize(
    ("output", "expected_fragment"),
    [
        ({"error": "quota exceeded"}, "quota exceeded"),
        ({"warning": "audio too quiet"}, "audio too quiet"),
        ({}, "unknown error"),
    ],
)
def test_transcribe_poll_status_failed_raises_with_output_message(
    tmp_path, output, expected_fragment
):
    audio_path = _make_audio_file(tmp_path)
    provider = _provider()
    config = ASRConfig(language="en")
    job_capture: dict[str, dict] = {}
    upload_url_resp = _FakeResponse(json_data={"url": "https://upload.example.com/put-url"})
    job_resp = _FakeResponse(json_data={"jobId": "job-1"})
    failed_resp = _FakeResponse(json_data={"status": "failed", "output": output})

    with (
        patch("httpx.post", side_effect=_dispatch_post(upload_url_resp, job_resp, job_capture)),
        patch("httpx.put", return_value=_FakeResponse()),
        patch("httpx.get", return_value=failed_resp),
        patch("time.sleep"),
        pytest.raises(RuntimeError, match="pyannote.ai transcription failed") as excinfo,
    ):
        provider.transcribe(audio_path, config)

    assert expected_fragment in str(excinfo.value)


def test_poll_timeout_constant_is_the_shortest_of_any_provider():
    # Pinned at 300s (5 min). Contrast Gladia's 7200s (720 x 10s sleep,
    # gladia_provider.py) and Azure's 7200s (azure_provider.py `done.wait(timeout=7200)`)
    # — pyannote.ai fails a long file first among this app's cloud ASR providers.
    assert pyannote_provider_module._POLL_TIMEOUT == 300.0


def test_transcribe_raises_when_poll_timeout_exceeded_before_any_poll_request(
    tmp_path, monkeypatch
):
    audio_path = _make_audio_file(tmp_path)
    provider = _provider()
    config = ASRConfig(language="en")
    job_capture: dict[str, dict] = {}
    upload_url_resp = _FakeResponse(json_data={"url": "https://upload.example.com/put-url"})
    job_resp = _FakeResponse(json_data={"jobId": "job-1"})

    # elapsed = time.time() - poll_start is always >= 0.0, so a negative cap guarantees
    # the very first loop iteration times out — no need to fake the wall clock.
    monkeypatch.setattr(pyannote_provider_module, "_POLL_TIMEOUT", -1.0)

    with (
        patch("httpx.post", side_effect=_dispatch_post(upload_url_resp, job_resp, job_capture)),
        patch("httpx.put", return_value=_FakeResponse()),
        patch("httpx.get") as mock_get,
        patch("time.sleep") as mock_sleep,
        pytest.raises(RuntimeError, match="pyannote.ai transcription timed out after"),
    ):
        provider.transcribe(audio_path, config)

    # The timeout check runs BEFORE the sleep and BEFORE the first poll request.
    mock_get.assert_not_called()
    mock_sleep.assert_not_called()


# ── transcribe(): full success — progress sequencing + final ASRResult assembly ────────


def test_transcribe_full_success_assembles_result_and_progress_sequence(tmp_path):
    audio_path = _make_audio_file(tmp_path)
    provider = _provider("parakeet")
    config = ASRConfig(language="en")
    job_capture: dict[str, dict] = {}
    upload_url_resp = _FakeResponse(json_data={"url": "https://upload.example.com/put-url"})
    job_resp = _FakeResponse(json_data={"jobId": "job-abc123"})
    output = {
        "turnLevelTranscription": [
            {"start": 0.5, "end": 2.3, "text": "Hello, how are you?", "speaker": "SPEAKER_00"},
            {"start": 2.5, "end": 4.0, "text": "I am fine thanks", "speaker": "SPEAKER_01"},
        ],
        "wordLevelTranscription": [
            {"start": 0.5, "end": 0.8, "text": "Hello,", "speaker": "SPEAKER_00"},
            {"start": 0.9, "end": 1.2, "text": "how", "speaker": "SPEAKER_00"},
            {"start": 1.3, "end": 1.6, "text": "are", "speaker": "SPEAKER_00"},
            {"start": 1.7, "end": 2.3, "text": "you?", "speaker": "SPEAKER_00"},
            {"start": 2.5, "end": 2.8, "text": "I", "speaker": "SPEAKER_01"},
            {"start": 2.9, "end": 3.2, "text": "am", "speaker": "SPEAKER_01"},
            {"start": 3.3, "end": 3.6, "text": "fine", "speaker": "SPEAKER_01"},
            {"start": 3.7, "end": 4.0, "text": "thanks", "speaker": "SPEAKER_01"},
        ],
    }
    processing_resp = _FakeResponse(json_data={"status": "processing"})
    succeeded_resp = _FakeResponse(json_data={"status": "succeeded", "output": output})
    cb, calls = _progress_recorder()

    with (
        patch("httpx.post", side_effect=_dispatch_post(upload_url_resp, job_resp, job_capture)),
        patch("httpx.put", return_value=_FakeResponse()),
        patch("httpx.get", side_effect=[processing_resp, succeeded_resp]),
        patch("time.sleep"),
    ):
        result = provider.transcribe(audio_path, config, progress_callback=cb)

    # Final ASRResult, not just that the mocks were called.
    assert len(result.segments) == 2
    assert result.segments[0].text == "Hello, how are you?"
    assert [w.word for w in result.segments[0].words] == ["Hello,", "how", "are", "you?"]
    assert result.segments[1].text == "I am fine thanks"
    assert result.language == "en"
    assert result.has_speakers is True
    assert result.provider_name == "pyannote"
    assert result.model_name == "parakeet"

    # Progress-callback sequencing across the whole 5-step flow.
    assert calls[0] == (0.05, "Requesting upload URL from pyannote.ai...")
    assert calls[1] == (0.1, "Uploading audio to pyannote.ai...")
    assert calls[2] == (0.2, "Submitting pyannote.ai transcription job...")
    assert calls[3] == (0.3, "pyannote.ai transcription in progress...")
    assert calls[-2] == (0.9, "Parsing pyannote.ai results...")
    assert calls[-1] == (1.0, "pyannote.ai transcription complete")

    # Exactly one mid-poll progress update — for the "processing" iteration; the
    # "succeeded" iteration breaks before reaching the progress-callback line.
    processing_calls = [c for c in calls if "processing (processing)" in c[1]]
    assert len(processing_calls) == 1
    frac, _msg = processing_calls[0]
    assert 0.3 <= frac < 0.86
    assert len(calls) == 7


# ── Rate-limit taxonomy (issue Lane 5) — pre-poll sites only ────────────────────────────
#
# Only the three PRE-poll HTTP call sites (upload-URL request, audio PUT, job submission)
# classify 429 as ASRRateLimitedError. The poll loop deliberately does NOT — a 429 seen
# while polling an already-submitted job must stay transient (see the fail-fast tests
# below, which pin that only 401 and "canceled" become terminal there).


def test_upload_url_request_429_is_classified_as_asr_rate_limited(tmp_path):
    from app.services.asr.errors import ASRRateLimitedError

    audio_path = _make_audio_file(tmp_path)
    provider = _provider()
    config = ASRConfig(language="en")
    rate_limited_resp = _FakeResponse(status_code=429, text="rate limited")

    with (
        patch("httpx.post", return_value=rate_limited_resp),
        patch("httpx.put") as mock_put,
        pytest.raises(ASRRateLimitedError) as excinfo,
    ):
        provider.transcribe(audio_path, config)

    assert excinfo.value.provider == "pyannote"
    mock_put.assert_not_called()


def test_audio_upload_put_429_is_classified_as_asr_rate_limited(tmp_path):
    from app.services.asr.errors import ASRRateLimitedError

    audio_path = _make_audio_file(tmp_path)
    provider = _provider()
    config = ASRConfig(language="en")
    upload_url_resp = _FakeResponse(json_data={"url": "https://upload.example.com/put-url"})
    rate_limited_resp = _FakeResponse(status_code=429, text="rate limited")

    with (
        patch("httpx.post", return_value=upload_url_resp),
        patch("httpx.put", return_value=rate_limited_resp),
        pytest.raises(ASRRateLimitedError) as excinfo,
    ):
        provider.transcribe(audio_path, config)

    assert excinfo.value.provider == "pyannote"


def test_job_submission_429_is_classified_as_asr_rate_limited(tmp_path):
    from app.services.asr.errors import ASRRateLimitedError

    audio_path = _make_audio_file(tmp_path)
    provider = _provider()
    config = ASRConfig(language="en")
    job_capture: dict[str, dict] = {}
    upload_url_resp = _FakeResponse(json_data={"url": "https://upload.example.com/put-url"})
    rate_limited_resp = _FakeResponse(status_code=429, text="rate limited")

    with (
        patch(
            "httpx.post",
            side_effect=_dispatch_post(upload_url_resp, rate_limited_resp, job_capture),
        ),
        patch("httpx.put", return_value=_FakeResponse()),
        pytest.raises(ASRRateLimitedError) as excinfo,
    ):
        provider.transcribe(audio_path, config)

    assert excinfo.value.provider == "pyannote"


def test_poll_429_stays_transient_and_is_retried_not_raised(tmp_path):
    """A 429 seen DURING polling must stay transient — the loop's own retry-and-continue
    is correct there, unlike the three pre-poll sites above. This is the interaction the
    Item 2 fail-fast rewrite (401/canceled only) depends on: widening the poll loop's
    terminal set to include 429 would break a job that is still legitimately running.
    """
    audio_path = _make_audio_file(tmp_path)
    provider = _provider()
    config = ASRConfig(language="en")
    job_capture: dict[str, dict] = {}
    upload_url_resp = _FakeResponse(json_data={"url": "https://upload.example.com/put-url"})
    job_resp = _FakeResponse(json_data={"jobId": "job-1"})
    rate_limited_resp = _FakeResponse(status_code=429, text="rate limited")
    succeeded_resp = _FakeResponse(
        json_data={"status": "succeeded", "output": _quick_succeed_output()}
    )

    with (
        patch("httpx.post", side_effect=_dispatch_post(upload_url_resp, job_resp, job_capture)),
        patch("httpx.put", return_value=_FakeResponse()),
        patch("httpx.get", side_effect=[rate_limited_resp, succeeded_resp]) as mock_get,
        patch("time.sleep"),
    ):
        result = provider.transcribe(audio_path, config)

    # The 429 during polling did not abort the job — it retried and completed.
    assert mock_get.call_count == 2
    assert result.segments[0].text == "hi"


def test_upload_url_request_400_stays_a_plain_runtime_error(tmp_path):
    """Negative control: a non-429 HTTP error must NOT be classified as retryable."""
    from app.services.asr.errors import ASRRateLimitedError

    audio_path = _make_audio_file(tmp_path)
    provider = _provider()
    config = ASRConfig(language="en")
    bad_request_resp = _FakeResponse(status_code=400, text="bad request")

    with (
        patch("httpx.post", return_value=bad_request_resp),
        patch("httpx.put"),
        pytest.raises(RuntimeError, match="pyannote.ai upload URL request failed") as excinfo,
    ):
        provider.transcribe(audio_path, config)

    assert not isinstance(excinfo.value, ASRRateLimitedError)
