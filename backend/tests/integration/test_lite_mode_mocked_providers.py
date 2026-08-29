"""Integration: the full upload->ASR->search->chat pipeline against MOCKED providers.

⚠️ This module uses MOCKED cloud ASR (``scripts/mock-asr-server.py``, a Gladia
stand-in) and a MOCKED LLM (``scripts/mock-llm-server.py``) — never a real
vendor. It exists to prove the app's real code paths (upload -> MinIO ->
download -> preprocessing -> multipart request construction -> polling ->
segment/speaker persistence -> search indexing -> chat retrieval/citations)
work end to end without a GPU, an API key, or a network egress, which is
exactly the "lite mode" deployment shape.

Requirements:
    ./opentr.sh start dev --with-mock-asr --with-mock-llm

Skips cleanly (not fails) when either mock container is unreachable.

Run:
    pytest backend/tests/integration/test_lite_mode_mocked_providers.py -v -m integration
"""

from __future__ import annotations

import contextlib
import socket
import wave
from pathlib import Path

import pytest
import requests

# Mirrors tests/e2e/conftest.py's TERMINAL_FAILURE_STATUSES. Not imported from
# there: tests/e2e is not an importable package (see that conftest's own
# docstring on why its cross-file imports are absolute-from-rootdir), and this
# integration test only needs the two literal values, not the whole module.
TERMINAL_FAILURE_STATUSES = frozenset({"error", "cancelled"})

MOCK_ASR_PORT = 5198
MOCK_LLM_PORT = 5199
SAMPLE_WAV = Path(__file__).resolve().parents[1] / "fixtures" / "media" / "sample_short.wav"
CANNED_SEGMENT_COUNT = 7
CANNED_SPEAKER_COUNT = 2
DISTINCTIVE_TOKEN = "Zylofenix"


def _reachable(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _mocks_running() -> bool:
    return _reachable(MOCK_ASR_PORT) and _reachable(MOCK_LLM_PORT)


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _mocks_running(),
        reason=(
            "mock-asr and/or mock-llm containers not running — "
            "start with './opentr.sh start dev --with-mock-asr --with-mock-llm'"
        ),
    ),
    # This module drives the ONE active-ASR-provider setting for the shared
    # admin user (UserSetting "active_asr_config_id" is per-user, not per-config)
    # and the pipeline through a single uploaded file at a time. Running two of
    # its tests concurrently under xdist races both, exactly like the
    # SystemSettings-key groups documented in backend/tests/CLAUDE.md.
    pytest.mark.xdist_group("lite_mode_mocked_providers"),
]

# Same fixed dev-stack admin credentials used throughout backend/tests/e2e
# (see backend/tests/CLAUDE.md's "shared identities" allow-list) — inlined
# rather than imported since tests/e2e is not an importable package.
_TEST_ADMIN_EMAIL = "admin@example.com"
_TEST_ADMIN_PASSWORD = "password"


@pytest.fixture(scope="module")
def backend_url() -> str:
    """Host-reachable base URL of the running dev backend.

    ``tests/integration`` shares the root conftest, which has no HTTP-facing
    ``backend_url`` fixture of its own (that one is e2e-only, scoped by
    ``--backend-url``/``base-url`` CLI options tests/e2e/pytest.ini registers).
    ``BACKEND_PORT`` mirrors the ``.env.example`` default and the compose port
    mapping (``docker-compose.yml``: ``${BACKEND_PORT:-5174}:8080``).
    """
    import os

    port = os.environ.get("BACKEND_PORT", "5174")
    return f"http://localhost:{port}"


@pytest.fixture(scope="module")
def api_session(backend_url: str) -> requests.Session:
    """An authenticated API session, CSRF-armed for mutations."""
    session = requests.Session()
    response = session.post(
        f"{backend_url}/api/auth/token",
        data={"username": _TEST_ADMIN_EMAIL, "password": _TEST_ADMIN_PASSWORD},
        timeout=30,
    )
    assert response.status_code == 200, f"Login failed: {response.status_code}"
    csrf_token = session.cookies.get("csrf_token")
    assert csrf_token, "login did not set a csrf_token cookie"
    session.headers["X-CSRF-Token"] = csrf_token
    return session


@pytest.fixture
def mock_asr_config(
    api_session: requests.Session, backend_url: str, register_mock_gladia_asr_config
):
    """Register the mock Gladia config as active, clean up on teardown."""
    config = register_mock_gladia_asr_config(api_session, f"{backend_url}/api")
    yield config


def _last_mock_asr_request() -> dict:
    resp = requests.get(f"http://127.0.0.1:{MOCK_ASR_PORT}/_mock/last-request", timeout=10)
    resp.raise_for_status()
    return dict(resp.json())


@pytest.fixture
def uploaded_file(api_session: requests.Session, backend_url: str, mock_asr_config):
    """Upload sample_short.wav through the mock ASR pipeline; delete on teardown."""
    import uuid as uuid_pkg

    name = f"lite-mode-test-{uuid_pkg.uuid4().hex[:8]}.wav"
    with SAMPLE_WAV.open("rb") as fh:
        resp = api_session.post(
            f"{backend_url}/api/files",
            files={"file": (name, fh, "audio/wav")},
            timeout=120,
        )
    assert resp.status_code == 200, f"Upload failed: {resp.status_code} {resp.text[:300]}"
    file_uuid = str(resp.json()["uuid"])

    try:
        yield file_uuid
    finally:
        try:
            resp = api_session.delete(f"{backend_url}/api/files/{file_uuid}", timeout=30)
            if resp.status_code not in (200, 204, 404):
                api_session.delete(f"{backend_url}/api/files/{file_uuid}/force", timeout=30)
        except requests.RequestException:
            with contextlib.suppress(requests.RequestException):
                api_session.delete(f"{backend_url}/api/files/{file_uuid}/force", timeout=30)


def _wait_for_indexed(
    api_session: requests.Session, backend_url: str, file_uuid: str, timeout_secs: int = 150
) -> bool:
    """Poll search for the distinctive token until this file appears (or timeout).

    Search indexing runs as a follow-on task after ``completed``, so a chat/search
    assertion made immediately after completion can race an empty index.
    """
    import time

    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        resp = api_session.get(
            f"{backend_url}/api/search", params={"q": DISTINCTIVE_TOKEN}, timeout=30
        )
        if resp.status_code == 200:
            hit_uuids = {r["file_uuid"] for r in resp.json().get("results", [])}
            if file_uuid in hit_uuids:
                return True
        time.sleep(3)
    return False


def _poll_status(
    api_session: requests.Session, backend_url: str, file_uuid: str, timeout_secs: int = 300
) -> str:
    import time

    deadline = time.time() + timeout_secs
    consecutive = 0
    status = "unknown"
    while time.time() < deadline:
        resp = api_session.get(f"{backend_url}/api/files/{file_uuid}", timeout=30)
        status = str(resp.json().get("status", "unknown")) if resp.status_code == 200 else "unknown"
        if status in TERMINAL_FAILURE_STATUSES:
            return status
        consecutive = consecutive + 1 if status == "completed" else 0
        if consecutive >= 2:
            return status
        time.sleep(3)
    return status


class TestMockedAsrHappyPath:
    """Upload through the real pipeline against the mock Gladia server."""

    def test_file_completes_with_canned_transcript(
        self, api_session: requests.Session, backend_url: str, uploaded_file: str
    ):
        status = _poll_status(api_session, backend_url, uploaded_file)
        assert status == "completed", f"file did not complete (status={status})"

        resp = api_session.get(f"{backend_url}/api/files/{uploaded_file}/segments", timeout=30)
        assert resp.status_code == 200
        segments = resp.json()["transcript_segments"]
        assert len(segments) == CANNED_SEGMENT_COUNT, (
            f"expected {CANNED_SEGMENT_COUNT} canned segments, got {len(segments)}"
        )
        assert any(DISTINCTIVE_TOKEN in seg["text"] for seg in segments), (
            "canned distinctive token not found in any segment text"
        )

    def test_two_distinct_speakers_via_segments_and_speakers_api(
        self, api_session: requests.Session, backend_url: str, uploaded_file: str
    ):
        status = _poll_status(api_session, backend_url, uploaded_file)
        assert status == "completed", f"file did not complete (status={status})"

        resp = api_session.get(f"{backend_url}/api/files/{uploaded_file}/segments", timeout=30)
        assert resp.status_code == 200
        segments = resp.json()["transcript_segments"]
        labels = {seg["speaker_label"] for seg in segments if seg.get("speaker_label")}
        assert len(labels) == CANNED_SPEAKER_COUNT, f"expected 2 distinct speakers, got {labels}"

        speakers_resp = api_session.get(f"{backend_url}/api/speakers", timeout=30)
        assert speakers_resp.status_code == 200

    def test_app_sent_correct_request_shape_to_mock(
        self, api_session: requests.Session, backend_url: str, uploaded_file: str
    ):
        status = _poll_status(api_session, backend_url, uploaded_file)
        assert status == "completed", f"file did not complete (status={status})"

        last_request = _last_mock_asr_request()
        transcription_request = last_request.get("transcription", {})
        assert "diarization" in transcription_request, "app did not send a diarization flag"
        assert transcription_request["diarization"] is True

    def test_real_wav_audio_bytes_reached_the_mock(
        self, api_session: requests.Session, backend_url: str, uploaded_file: str
    ):
        status = _poll_status(api_session, backend_url, uploaded_file)
        assert status == "completed", f"file did not complete (status={status})"

        with wave.open(str(SAMPLE_WAV), "rb") as wf:
            expected_duration = wf.getnframes() / float(wf.getframerate())

        last_request = _last_mock_asr_request()
        received_duration = last_request.get("upload", {}).get("duration")
        assert received_duration is not None, "mock did not record the received audio duration"
        assert abs(float(received_duration) - expected_duration) < 1.0, (
            f"received audio duration {received_duration} does not match "
            f"the source file's {expected_duration}"
        )

    def test_search_finds_the_distinctive_token(
        self, api_session: requests.Session, backend_url: str, uploaded_file: str
    ):
        status = _poll_status(api_session, backend_url, uploaded_file)
        assert status == "completed", f"file did not complete (status={status})"

        found = _wait_for_indexed(api_session, backend_url, uploaded_file)
        assert found, (
            f"file {uploaded_file} not found searching for {DISTINCTIVE_TOKEN!r} within 60s"
        )


class TestMockedAsrPlusMockedLlm:
    """A chat turn grounded in a mocked-ASR transcript, answered by a mocked LLM."""

    def test_chat_summary_has_non_empty_grounded_content(
        self, api_session: requests.Session, backend_url: str, uploaded_file: str
    ):
        status = _poll_status(api_session, backend_url, uploaded_file)
        assert status == "completed", f"file did not complete (status={status})"
        # Chat retrieval reads the search index, which is populated by a follow-on
        # task after "completed" — wait for it the same way the search test does,
        # or the grounded turn below has nothing to retrieve and answers empty.
        _wait_for_indexed(api_session, backend_url, uploaded_file)

        headers = {"X-CSRF-Token": api_session.cookies.get("csrf_token")}
        llm_resp = api_session.post(
            f"{backend_url}/api/llm-settings",
            json={
                "name": f"Mock LLM lite-mode test {uploaded_file[:8]}",
                "provider": "custom",
                "model_name": "mock-gpt",
                "base_url": "http://mock-llm:5199/v1",
                "api_key": "mock-key-not-secret",
            },
            headers=headers,
            timeout=30,
        )
        assert llm_resp.status_code == 200, llm_resp.text[:300]
        llm_config = llm_resp.json()

        try:
            conv_resp = api_session.post(
                f"{backend_url}/api/chat/conversations",
                json={
                    "title": f"lite-mode test {uploaded_file[:8]}",
                    # Pinned directly to this test's own LLM config, so the turn
                    # below routes to the mock regardless of whichever config the
                    # shared dev account's global "active provider" pointer
                    # happens to be set to (never mutated by this test).
                    "llm_config_uuid": llm_config["uuid"],
                    "scope": {
                        "file_uuids": [uploaded_file],
                        "collection_uuids": [],
                        "tag_names": [],
                        "speakers": [],
                    },
                },
                timeout=30,
            )
            assert conv_resp.status_code == 201, conv_resp.text[:300]
            conversation_uuid = conv_resp.json()["uuid"]

            try:
                answer_parts: list[str] = []
                citation_frames: list[dict] = []
                other_frames: list[tuple[str, dict]] = []
                response = api_session.post(
                    f"{backend_url}/api/chat/conversations/{conversation_uuid}/messages",
                    json={"content": "What was discussed?"},
                    stream=True,
                    timeout=90,
                    headers={"Accept": "text/event-stream"},
                )
                with response:
                    response.raise_for_status()
                    event_name = None
                    for raw_line in response.iter_lines(decode_unicode=True):
                        if raw_line is None:
                            continue
                        line = raw_line.strip("\r")
                        if line == "":
                            event_name = None
                            continue
                        if line.startswith("event:"):
                            event_name = line[len("event:") :].strip()
                            continue
                        if not line.startswith("data:"):
                            continue
                        import json as _json

                        data_str = line[len("data:") :].strip()
                        data = _json.loads(data_str) if data_str else {}
                        if event_name == "delta":
                            answer_parts.append(data.get("content") or data.get("text") or "")
                        elif event_name == "sources":
                            citation_frames = data.get("citations") or []
                        elif event_name in ("warning", "error"):
                            other_frames.append((event_name, data))

                answer = "".join(answer_parts)
                assert answer.strip(), (
                    f"mock-backed chat turn produced an empty answer; "
                    f"non-delta frames observed: {other_frames}"
                )
                assert citation_frames, "expected at least one offered citation for a grounded turn"
            finally:
                api_session.delete(
                    f"{backend_url}/api/chat/conversations/{conversation_uuid}", timeout=15
                )
        finally:
            with contextlib.suppress(requests.RequestException):
                api_session.delete(
                    f"{backend_url}/api/llm-settings/config/{llm_config['uuid']}",
                    headers=headers,
                    timeout=30,
                )


# NOT YET IMPLEMENTED here (documented gap, not a placeholder test class). A
# live-pipeline negative-path leg (error/malformed/upload-reject scenarios) would
# need either a client-side per-job scenario-selection hook GladiaProvider does
# not have (the mock selects scenario via a ?scenario= query param on the
# request the APP itself sends to POST /v2/transcription, and the provider
# builds that URL from GLADIA_API_BASE_URL with no such passthrough), or
# restarting the mock-asr container mid-module with a different
# MOCK_ASR_SCENARIO — which would break this module's xdist-serialized "ok"
# happy-path tests sharing that same container. The mock's own scenario
# contract IS covered at the unit level, against GladiaProvider directly, in
# backend/tests/unit/test_gladia_provider.py. Tracked as follow-on work rather
# than faked here with an always-skipping test (the audit-tests.py
# `no-assertion` detector correctly rejects that shape).
