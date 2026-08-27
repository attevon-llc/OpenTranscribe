"""Unit tests for the standalone pyannote.ai cloud diarization provider.

Distinct from ``tests/unit/test_pyannote_provider.py``, which covers the STT
orchestration provider at ``app/services/asr/pyannote_provider.py``. This file covers
``PyAnnoteCloudDiarizationProvider`` at ``app/services/diarization/pyannote_provider.py``
(``source == "pyannote"``), a diarization-only client: upload -> submit job -> poll ->
parse ``output.diarization``.

Network-free, mirroring the mocking pattern of ``test_pyannote_provider.py``: no
respx/pytest-httpx in this repo, just ``unittest.mock.patch`` on module-level ``httpx``
functions plus a local ``_FakeResponse`` duck-type, and ``time.sleep`` always patched so
polling tests don't take 5 real minutes.

Two flagged real production issues are TEST-PINNED here (current behavior only, not
fixed):

(a) ``_handle_terminal_status``'s failed-job path interpolates ``output["error"]``
    straight into the exception message with **no ``_sanitize_error`` call** — unlike
    every other error path in this provider (upload URL, audio upload, job submission,
    401s, all call ``self._sanitize_error``/``_safe_response_text``). See
    ``pyannote_provider.py`` around ``_handle_terminal_status``:
    ``error_msg = output.get("error") or "unknown error"`` then
    ``raise RuntimeError(f"...failed: {error_msg}")`` — a secret-looking string placed in
    ``error`` by a malicious/broken API response would leak unsanitized into logs/exceptions.
    Pinned by ``test_failed_job_error_message_is_not_sanitized_current_behavior``.

(b) ``_parse_segments`` does ``float(start)`` / ``float(end)`` with no try/except, unlike
    the ``start is None or end is None`` guard immediately above it which DOES catch the
    missing-field case gracefully. A non-numeric (but non-None) timestamp raises an
    unhandled ``ValueError`` instead of being skipped like a missing one. Pinned by
    ``test_non_numeric_timestamp_raises_unhandled_value_error_current_behavior``.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import httpx
import pytest

import app.services.diarization.pyannote_provider as mod
from app.services.asr.base import normalize_speaker_label
from app.services.diarization.pyannote_provider import PyAnnoteCloudDiarizationProvider
from app.services.diarization.types import DiarizeConfig


def _provider(model_name: str = "precision-2", api_key: str = "test-key"):
    return PyAnnoteCloudDiarizationProvider(api_key=api_key, model_name=model_name)


def _make_audio_file(tmp_path) -> str:
    p = tmp_path / "sample.wav"
    p.write_bytes(b"RIFF....fakeaudiobytes")
    return str(p)


class _FakeResponse:
    """Minimal stand-in for ``httpx.Response``."""

    def __init__(self, json_data: dict | None = None, text: str = "", status_code: int = 200):
        self._json = json_data if json_data is not None else {}
        self.text = text
        self.status_code = status_code

    def json(self) -> dict:
        return self._json


def _dispatch_post(
    upload_url_resp: _FakeResponse, job_resp: _FakeResponse, job_capture: dict[str, dict]
):
    """Build an ``httpx.post`` side_effect routing by URL and recording the job body."""

    def _post(url, **kwargs):
        if url.endswith("/v1/media/input"):
            return upload_url_resp
        if url.endswith("/v1/diarize"):
            job_capture["body"] = kwargs.get("json") or {}
            return job_resp
        raise AssertionError(f"unexpected POST to {url}")

    return _post


def _succeeded_output(entries=None) -> dict:
    if entries is None:
        entries = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.5},
            {"speaker": "SPEAKER_01", "start": 1.5, "end": 3.0},
        ]
    return {"status": "succeeded", "output": {"diarization": entries}}


def _run_diarize(
    tmp_path,
    config: DiarizeConfig | None = None,
    get_side_effect=None,
    get_return_value=None,
    job_capture: dict[str, dict] | None = None,
    progress_callback=None,
    provider: PyAnnoteCloudDiarizationProvider | None = None,
):
    audio_path = _make_audio_file(tmp_path)
    provider = provider or _provider()
    config = config or DiarizeConfig()
    job_capture = job_capture if job_capture is not None else {}
    upload_url_resp = _FakeResponse(json_data={"url": "https://upload.example.com/put-url"})
    job_resp = _FakeResponse(json_data={"jobId": "job-1"})

    get_kwargs = {}
    if get_side_effect is not None:
        get_kwargs["side_effect"] = get_side_effect
    else:
        get_kwargs["return_value"] = get_return_value or _FakeResponse(
            json_data=_succeeded_output()
        )

    with (
        patch("httpx.post", side_effect=_dispatch_post(upload_url_resp, job_resp, job_capture)),
        patch("httpx.put", return_value=_FakeResponse()),
        patch("httpx.get", **get_kwargs),
        patch("time.sleep"),
    ):
        result = provider.diarize(audio_path, config, progress_callback=progress_callback)
    return result, job_capture


# ── 1. Happy path ────────────────────────────────────────────────────────────────────


def test_happy_path_succeeds_on_first_poll(tmp_path):
    result, job_capture = _run_diarize(tmp_path)

    assert result.provider_name == "pyannote"
    assert result.model_name == "precision-2"
    assert result.num_speakers == 2
    assert len(result.segments) == 2
    seg0, seg1 = result.segments
    assert seg0.speaker == normalize_speaker_label("SPEAKER_00")
    assert seg1.speaker == normalize_speaker_label("SPEAKER_01")
    assert seg0.start == 0.0
    assert seg0.end == 1.5
    assert seg1.start == 1.5
    assert seg1.end == 3.0
    assert result.metadata["job_id"] == "job-1"
    assert "elapsed_ms" in result.metadata
    assert job_capture["body"]["transcription"] is False


# ── 2. Sorted by start ───────────────────────────────────────────────────────────────


def test_segments_sorted_by_start(tmp_path):
    entries = [
        {"speaker": "SPEAKER_01", "start": 5.0, "end": 6.0},
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.0},
        {"speaker": "SPEAKER_02", "start": 2.0, "end": 3.0},
    ]
    result, _ = _run_diarize(
        tmp_path, get_return_value=_FakeResponse(json_data=_succeeded_output(entries))
    )
    starts = [s.start for s in result.segments]
    assert starts == sorted(starts)


# ── 3. Succeeds on nth poll ──────────────────────────────────────────────────────────


def test_succeeds_on_nth_poll(tmp_path):
    audio_path = _make_audio_file(tmp_path)
    provider = _provider()
    config = DiarizeConfig()
    job_capture: dict[str, dict] = {}
    upload_url_resp = _FakeResponse(json_data={"url": "https://upload.example.com/put-url"})
    job_resp = _FakeResponse(json_data={"jobId": "job-1"})
    running = _FakeResponse(json_data={"status": "running"})
    succeeded = _FakeResponse(json_data=_succeeded_output())

    with (
        patch("httpx.post", side_effect=_dispatch_post(upload_url_resp, job_resp, job_capture)),
        patch("httpx.put", return_value=_FakeResponse()),
        patch("httpx.get", side_effect=[running, running, succeeded]) as mock_get,
        patch("time.sleep"),
    ):
        result = provider.diarize(audio_path, config)

    assert result.num_speakers == 2
    assert mock_get.call_count == 3


# ── 4. Job body construction ─────────────────────────────────────────────────────────


def test_job_body_num_speakers(tmp_path):
    config = DiarizeConfig(num_speakers=3, min_speakers=1, max_speakers=20)
    _, job_capture = _run_diarize(tmp_path, config=config)
    body = job_capture["body"]
    assert body["numSpeakers"] == 3
    assert "minSpeakers" not in body
    assert "maxSpeakers" not in body
    assert body["transcription"] is False


def test_job_body_min_max_speakers(tmp_path):
    config = DiarizeConfig(num_speakers=None, min_speakers=2, max_speakers=6)
    _, job_capture = _run_diarize(tmp_path, config=config)
    body = job_capture["body"]
    assert "numSpeakers" not in body
    assert body["minSpeakers"] == 2
    assert body["maxSpeakers"] == 6


def test_job_body_library_defaults(tmp_path):
    # DiarizeConfig()'s defaults are min_speakers=1, max_speakers=20, num_speakers=None.
    # min_speakers=1 is not > 1 -> minSpeakers absent; max_speakers=20 is < 50 -> present.
    config = DiarizeConfig()
    _, job_capture = _run_diarize(tmp_path, config=config)
    body = job_capture["body"]
    assert "numSpeakers" not in body
    assert "minSpeakers" not in body
    assert body["maxSpeakers"] == 20
    assert body["transcription"] is False


# ── 5. Progress callback sequencing ──────────────────────────────────────────────────


def test_progress_callback_sequencing(tmp_path):
    calls: list[tuple[float, str]] = []

    def _cb(frac: float, msg: str) -> None:
        calls.append((frac, msg))

    _run_diarize(tmp_path, progress_callback=_cb)

    assert len(calls) >= 2
    fractions = [c[0] for c in calls]
    assert fractions == sorted(fractions)
    assert calls[-1][0] == 1.0


# ── 6. Failed job (also pins finding (a)) ────────────────────────────────────────────


def test_failed_job_raises_runtime_error_with_job_id_and_message(tmp_path):
    failed_resp = _FakeResponse(
        json_data={"status": "failed", "output": {"error": "audio too short"}}
    )
    with pytest.raises(RuntimeError, match="failed") as excinfo:
        _run_diarize(tmp_path, get_return_value=failed_resp)

    message = str(excinfo.value)
    assert "job-1" in message
    assert "audio too short" in message


def test_failed_job_error_message_is_not_sanitized_current_behavior(tmp_path):
    """Pins finding (a): output['error'] is interpolated with NO _sanitize_error call.

    Unlike every other error path in this provider, a secret-looking string placed in
    the API's ``output.error`` field is NOT stripped before landing in the exception
    message. This test pins the CURRENT (unsanitized) behavior — it is not asserting
    this is correct.
    """
    secret_looking_error = "job failed: Bearer sk-super-secret-abc123 rejected"
    failed_resp = _FakeResponse(
        json_data={"status": "failed", "output": {"error": secret_looking_error}}
    )
    with pytest.raises(RuntimeError) as excinfo:
        _run_diarize(tmp_path, get_return_value=failed_resp)

    # Current behavior: the secret-looking string leaks through verbatim, unsanitized.
    assert secret_looking_error in str(excinfo.value)
    assert "sk-super-secret-abc123" in str(excinfo.value)


# ── 7. Canceled job ──────────────────────────────────────────────────────────────────


def test_canceled_job_raises_runtime_error(tmp_path):
    canceled_resp = _FakeResponse(json_data={"status": "canceled", "output": {}})
    with pytest.raises(RuntimeError, match="canceled"):
        _run_diarize(tmp_path, get_return_value=canceled_resp)


# ── 8. Timeout ───────────────────────────────────────────────────────────────────────


def test_timeout_raises_and_terminates(tmp_path):
    running = _FakeResponse(json_data={"status": "running"})
    audio_path = _make_audio_file(tmp_path)
    provider = _provider()
    config = DiarizeConfig()
    job_capture: dict[str, dict] = {}
    upload_url_resp = _FakeResponse(json_data={"url": "https://upload.example.com/put-url"})
    job_resp = _FakeResponse(json_data={"jobId": "job-1"})

    with (
        patch("httpx.post", side_effect=_dispatch_post(upload_url_resp, job_resp, job_capture)),
        patch("httpx.put", return_value=_FakeResponse()),
        patch("httpx.get", return_value=running) as mock_get,
        patch("time.sleep"),
        pytest.raises(RuntimeError, match="300s") as excinfo,
    ):
        provider.diarize(audio_path, config)

    assert "150 polls" in str(excinfo.value)
    assert mock_get.call_count == mod._POLL_MAX_ATTEMPTS


# ── 9. Poll transient errors retried ─────────────────────────────────────────────────


def test_poll_httpx_error_retried_then_succeeds(tmp_path):
    succeeded = _FakeResponse(json_data=_succeeded_output())
    result, _ = _run_diarize(
        tmp_path,
        get_side_effect=[httpx.HTTPError("blip"), httpx.HTTPError("blip2"), succeeded],
    )
    assert result.num_speakers == 2


def test_poll_http_500_retried_then_succeeds(tmp_path):
    err500 = _FakeResponse(status_code=500)
    succeeded = _FakeResponse(json_data=_succeeded_output())
    result, _ = _run_diarize(tmp_path, get_side_effect=[err500, succeeded])
    assert result.num_speakers == 2


def test_poll_401_raises_immediately(tmp_path):
    audio_path = _make_audio_file(tmp_path)
    provider = _provider()
    config = DiarizeConfig()
    job_capture: dict[str, dict] = {}
    upload_url_resp = _FakeResponse(json_data={"url": "https://upload.example.com/put-url"})
    job_resp = _FakeResponse(json_data={"jobId": "job-1"})
    unauthorized = _FakeResponse(status_code=401)

    with (
        patch("httpx.post", side_effect=_dispatch_post(upload_url_resp, job_resp, job_capture)),
        patch("httpx.put", return_value=_FakeResponse()),
        patch("httpx.get", return_value=unauthorized) as mock_get,
        patch("time.sleep"),
        pytest.raises(RuntimeError, match="invalid API key"),
    ):
        provider.diarize(audio_path, config)

    assert mock_get.call_count == 1


# ── 10. Malformed responses ──────────────────────────────────────────────────────────


def test_upload_url_missing_url_field_raises(tmp_path):
    audio_path = _make_audio_file(tmp_path)
    provider = _provider()
    config = DiarizeConfig()
    upload_url_resp = _FakeResponse(json_data={})  # missing "url"

    with (
        patch("httpx.post", return_value=upload_url_resp),
        pytest.raises(RuntimeError, match="missing 'url'"),
    ):
        provider.diarize(audio_path, config)


def test_diarize_submission_missing_job_id_raises(tmp_path):
    audio_path = _make_audio_file(tmp_path)
    provider = _provider()
    config = DiarizeConfig()
    job_capture: dict[str, dict] = {}
    upload_url_resp = _FakeResponse(json_data={"url": "https://upload.example.com/put-url"})
    job_resp = _FakeResponse(json_data={})  # missing "jobId"

    with (
        patch("httpx.post", side_effect=_dispatch_post(upload_url_resp, job_resp, job_capture)),
        patch("httpx.put", return_value=_FakeResponse()),
        pytest.raises(RuntimeError, match="missing 'jobId'"),
    ):
        provider.diarize(audio_path, config)


def test_succeeded_job_empty_output_raises(tmp_path):
    succeeded_resp = _FakeResponse(json_data={"status": "succeeded", "output": {}})
    with pytest.raises(RuntimeError, match="no diarization segments"):
        _run_diarize(tmp_path, get_return_value=succeeded_resp)


def test_succeeded_job_all_entries_missing_timestamps_raises(tmp_path):
    entries = [
        {"speaker": "SPEAKER_00"},  # no start/end at all
        {"speaker": "SPEAKER_01", "start": None, "end": None},
    ]
    succeeded_resp = _FakeResponse(json_data=_succeeded_output(entries))
    with pytest.raises(RuntimeError, match="invalid data"):
        _run_diarize(tmp_path, get_return_value=succeeded_resp)


def test_succeeded_job_one_valid_one_missing_end_returns_one_segment(tmp_path, caplog):
    entries = [
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.0},
        {"speaker": "SPEAKER_01", "start": 2.0},  # missing "end"
    ]
    succeeded_resp = _FakeResponse(json_data=_succeeded_output(entries))
    with caplog.at_level(logging.WARNING):
        result, _ = _run_diarize(tmp_path, get_return_value=succeeded_resp)

    assert len(result.segments) == 1
    assert result.segments[0].start == 0.0
    assert any("skipping segment" in r.getMessage().lower() for r in caplog.records)


def test_non_numeric_timestamp_raises_unhandled_value_error_current_behavior(tmp_path):
    """Pins finding (b): a non-numeric (but non-None) timestamp is NOT caught like a
    missing one — ``float(start)``/``float(end)`` in ``_parse_segments`` has no
    try/except, so it raises a raw ``ValueError``, not the ``RuntimeError`` used for the
    missing-field case. This test pins the CURRENT (unhandled) behavior.
    """
    entries = [{"speaker": "SPEAKER_00", "start": "not-a-number", "end": 1.0}]
    succeeded_resp = _FakeResponse(json_data=_succeeded_output(entries))
    with pytest.raises(ValueError):
        _run_diarize(tmp_path, get_return_value=succeeded_resp)


# ── 11. Pre-flight errors ────────────────────────────────────────────────────────────


def test_missing_file_raises_file_not_found():
    provider = _provider()
    config = DiarizeConfig()
    with pytest.raises(FileNotFoundError, match="Audio file not found"):
        provider.diarize("/nonexistent/path/audio.wav", config)


def test_no_api_key_on_provider_or_config_raises(tmp_path):
    audio_path = _make_audio_file(tmp_path)
    provider = PyAnnoteCloudDiarizationProvider(api_key="", model_name="precision-2")
    config = DiarizeConfig(api_key=None)
    with pytest.raises(RuntimeError, match="API key is required"):
        provider.diarize(audio_path, config)


# ── 12. validate_connection ──────────────────────────────────────────────────────────


def test_validate_connection_no_key():
    provider = PyAnnoteCloudDiarizationProvider(api_key="", model_name="precision-2")
    ok, message, ms = provider.validate_connection()
    assert ok is False
    assert "API key is required" in message
    assert ms >= 0


def test_validate_connection_success():
    provider = _provider()
    with (
        patch("httpx.get", return_value=_FakeResponse(status_code=200)),
        patch("time.time", side_effect=[0.0, 0.125]),
    ):
        ok, message, ms = provider.validate_connection()
    assert ok is True
    assert message == "Connected to pyannote.ai"
    assert ms == pytest.approx(125.0)


def test_validate_connection_401():
    provider = _provider()
    with patch("httpx.get", return_value=_FakeResponse(status_code=401)):
        ok, message, ms = provider.validate_connection()
    assert ok is False
    assert "Invalid pyannote.ai API key" in message


def test_validate_connection_503():
    provider = _provider()
    with patch("httpx.get", return_value=_FakeResponse(status_code=503)):
        ok, message, ms = provider.validate_connection()
    assert ok is False
    assert "HTTP 503" in message


def test_validate_connection_exception_is_sanitized():
    provider = _provider(api_key="sk-super-secret-999")
    boom = Exception("connection reset while using key sk-super-secret-999")
    with patch("httpx.get", side_effect=boom):
        ok, message, ms = provider.validate_connection()
    assert ok is False
    assert message.startswith("Connection failed:")
    assert "sk-super-secret-999" not in message
    assert ms > 0


def test_validate_connection_response_time_positive():
    provider = _provider()
    with (
        patch("httpx.get", return_value=_FakeResponse(status_code=200)),
        patch("time.time", side_effect=[0.0, 0.125]),
    ):
        _, _, ms = provider.validate_connection()
    assert isinstance(ms, float)
    assert ms == pytest.approx(125.0)


# ── 13. Delegation to shared helpers ─────────────────────────────────────────────────


def test_speaker_label_delegates_to_shared_normalizer(tmp_path):
    entries = [{"speaker": "speaker_1", "start": 0.0, "end": 1.0}]
    result, _ = _run_diarize(
        tmp_path, get_return_value=_FakeResponse(json_data=_succeeded_output(entries))
    )
    assert result.segments[0].speaker == normalize_speaker_label("speaker_1")


def test_validate_connection_delegates_sanitization_to_shared_helper():
    provider = _provider(api_key="sk-shared-helper-secret")
    boom = Exception("failure using sk-shared-helper-secret in transit")
    with patch("httpx.get", side_effect=boom):
        _, message, _ = provider.validate_connection()
    assert "sk-shared-helper-secret" not in message
