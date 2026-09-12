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
    # This module drives the ONE active-ASR-provider setting for its user
    # (UserSetting "active_asr_config_id" is per-user, not per-config) and the
    # pipeline through a single uploaded file at a time. Running two of its tests
    # concurrently under xdist races both, exactly like the SystemSettings-key
    # groups documented in backend/tests/CLAUDE.md.
    #
    # ⚠️ The group serialises this module against ITSELF and can do nothing about
    # the other ~180 tests of the `-m integration` phase running on the other 23
    # workers. That is why the user below is a throwaway rather than the shared
    # admin account — see `lite_mode_user`.
    pytest.mark.xdist_group("lite_mode_mocked_providers"),
]

# A throwaway per-run owner, NOT the shared `admin@example.com`. The active-ASR
# provider is a per-USER setting, so while this module held a mock-Gladia config
# active on the shared account, every *other* integration test that uploaded a
# file as admin was transcribed through it too — and the GPU worker does not carry
# `ASR_ALLOW_PRIVATE_ENDPOINTS=true` (only `backend` and `celery-cloud-asr-worker`
# do, per docker-compose.mock-asr.yml), so the #594 SSRF guard refused the private
# `mock-asr` hostname and failed the file. Measured 2026-09-11: that took out all
# **15** of `tests/test_selective_reprocess.py`'s tests in one `-m integration`
# run, as setup ERRORs reading `The file could not be retrieved` — a failure whose
# every symptom pointed at a file this module never touches. Owning the account
# makes the setting private and the blast radius zero.
#
# Same shape as `tests/fixtures/search_corpus_stack.py`'s `searchqual-` user; the
# prefix is registered in `scripts/cleanup-test-users.py`'s
# ORPHAN_PATTERNS_UNAMBIGUOUS so an interrupted run is swept, not leaked.
_LITE_MODE_USER_PREFIX = "litemode-"
_LITE_MODE_USER_DOMAIN = "@example.invalid"
_LITE_MODE_PASSWORD = "lite-mode-fixture-pw-1"  # noqa: S105 — throwaway test user only


@pytest.fixture(scope="module")
def lite_mode_user():
    """Create this module's throwaway owning user; delete it on teardown."""
    import uuid as uuid_pkg

    from app.core.security import get_password_hash
    from app.db.base import SessionLocal
    from app.models.user import User

    email = f"{_LITE_MODE_USER_PREFIX}{uuid_pkg.uuid4().hex[:8]}{_LITE_MODE_USER_DOMAIN}"
    db = SessionLocal()
    try:
        user = User(
            email=email,
            full_name="Lite Mode Mocked Providers Fixture",
            hashed_password=get_password_hash(_LITE_MODE_PASSWORD),
            is_active=True,
            is_superuser=False,
            role="user",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        user_id = user.id

        yield {"id": user_id, "email": email, "password": _LITE_MODE_PASSWORD}

        # Backstop only, and deliberately warn-don't-raise: `_delete_file_until_gone`
        # is where the cleanup contract is enforced (it already warns, naming the
        # uuid). Raising here would convert a transient cleanup miss into a
        # module-scope teardown ERROR — trading one intermittent red gate for
        # another, which is the whole thing this file is being fixed for.
        #
        # It purges through the **API**, not by deleting rows. Twelve tables carry an
        # FK onto `media_file` (task, analytics, comment, file_facts,
        # file_pipeline_timing, topic_suggestion, usage_event, …) and a hand-written
        # row-delete order covering four of them raised ForeignKeyViolation on
        # `task_media_file_id_fkey` the first time this fired — and would go stale
        # again at the next migration. `DELETE /files/{uuid}/force` is the production
        # purge and reaches MinIO and the search index too, which no row delete can.
        import os
        import warnings

        from app.models.media import MediaFile

        try:
            leftover = [
                str(row[0])
                for row in db.query(MediaFile.uuid).filter(MediaFile.user_id == user_id).all()
            ]
            if leftover:
                warnings.warn(
                    f"lite-mode fixture user {email} still owned {len(leftover)} media "
                    f"file(s) at module teardown ({leftover[:5]}) — a test's own cleanup "
                    "did not complete; purging them now",
                    stacklevel=2,
                )
                cleanup = requests.Session()
                login = cleanup.post(
                    f"http://localhost:{os.environ.get('BACKEND_PORT', '5174')}/api/auth/token",
                    data={"username": email, "password": _LITE_MODE_PASSWORD},
                    timeout=30,
                )
                if login.status_code == 200:
                    cleanup.headers["X-CSRF-Token"] = cleanup.cookies.get("csrf_token") or ""
                    base = f"http://localhost:{os.environ.get('BACKEND_PORT', '5174')}"
                    for uuid_str in leftover:
                        _delete_file_until_gone(cleanup, base, uuid_str)
                db.expire_all()
            reloaded = db.get(User, user_id)
            if reloaded is not None:
                db.delete(reloaded)
                db.commit()
        except Exception as exc:  # noqa: BLE001 - a backstop must never fail the module
            db.rollback()
            warnings.warn(
                f"lite-mode fixture user {email} could not be torn down ({exc!r}); "
                "scripts/cleanup-test-users.py sweeps the `litemode-` prefix",
                stacklevel=2,
            )
    finally:
        db.close()


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
def api_session(backend_url: str, lite_mode_user: dict) -> requests.Session:
    """An authenticated API session for this module's own user, CSRF-armed."""
    session = requests.Session()
    response = session.post(
        f"{backend_url}/api/auth/token",
        data={"username": lite_mode_user["email"], "password": lite_mode_user["password"]},
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


# One attempt is not enough, and the miss is invisible. The postprocess fan-out
# leaves LLM-backed downstream tasks (summarization, topic_extraction) running past
# the point the test is done with the file, and a delete racing one of those is
# refused — so the single best-effort attempt this used to make silently leaked a
# completed media file, its MinIO object and its search-index entries onto the dev
# stack. Observed once in a full `-m integration` phase (file 571034,
# `topic_extraction` still `in_progress`); the same delete succeeded by hand
# seconds later, which is what makes it a retry problem rather than a permissions
# or state problem. Retries for ~30 s, escalates to /force, then VERIFIES.
_DELETE_SETTLE_TIMEOUT_SECS = 30


def _delete_file_until_gone(
    api_session: requests.Session,
    backend_url: str,
    file_uuid: str,
    timeout_secs: int = _DELETE_SETTLE_TIMEOUT_SECS,
) -> None:
    """Delete an uploaded file and confirm it is actually gone.

    Cleanup must never fail an otherwise-passing test, so this raises nothing — but
    it also must not pretend: a file that outlives its budget is reported through
    ``pytest.fail``'s quieter sibling, a warning carrying the uuid, so the leak is
    attributable instead of being discovered days later as unexplained dev-stack
    residue (``backend/tests/CLAUDE.md``, "a test that reads whatever happens to be
    in the dev stack").
    """
    import time
    import warnings

    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        with contextlib.suppress(requests.RequestException):
            resp = api_session.delete(f"{backend_url}/api/files/{file_uuid}", timeout=30)
            if resp.status_code in (200, 204, 404):
                check = api_session.get(f"{backend_url}/api/files/{file_uuid}", timeout=30)
                if check.status_code == 404:
                    return
            else:
                api_session.delete(f"{backend_url}/api/files/{file_uuid}/force", timeout=30)
        time.sleep(2)

    with contextlib.suppress(requests.RequestException):
        api_session.delete(f"{backend_url}/api/files/{file_uuid}/force", timeout=30)
        if api_session.get(f"{backend_url}/api/files/{file_uuid}", timeout=30).status_code == 404:
            return
    warnings.warn(
        f"lite-mode test file {file_uuid} survived {timeout_secs}s of delete attempts and "
        "is now residue on the dev stack — sweep it with scripts/cleanup-test-data.py",
        stacklevel=2,
    )


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
        _delete_file_until_gone(api_session, backend_url, file_uuid)


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


# Measured on the 2026-09-11 integration gate, from the container logs of the run that
# failed (file 570602 vs 570603, celery-redaction): the FIRST file of a run pays a cold
# Presidio/GLiNER/toxicity model load and its detection scan takes **2,467 ms**, against
# **284 ms** for every later file once the models are warm. 60 s is ~24x that worst case,
# so this cannot fire on a merely slow scan under gate load — it fires on a redaction row
# that is genuinely stranded, which is a defect and must be loud rather than quiet.
REDACTION_SETTLE_TIMEOUT_SECS = 60


def _segments_when_redaction_settled(
    api_session: requests.Session,
    backend_url: str,
    file_uuid: str,
    timeout_secs: int = REDACTION_SETTLE_TIMEOUT_SECS,
) -> list[dict]:
    """Read the transcript, waiting out the redaction scan that withholds it.

    ``status == "completed"`` is NOT a sufficient precondition for reading a transcript,
    and that is the whole of this module's cross-test flakiness. The pipeline marks the
    file COMPLETED and only **then** dispatches redaction detection, while
    ``GET /files/{uuid}/segments`` deliberately withholds the transcript for as long as
    that scan is in flight — answering ``200`` with
    ``{"transcript_segments": [], "redaction_pending": true}``
    (``app/api/endpoints/files/segments.py``, "Withhold the transcript until redaction
    finishes"). A test that polls only the file status therefore races a window it never
    looks at, and reads an empty list that means "not yet", not "this file has no
    segments" — which is exactly how the gate produced
    ``expected 7 canned segments, got 0`` against a file whose 7 segments were already
    committed.

    Waits on the readiness flag the API already publishes rather than on a fixed sleep,
    and fails naming the last observed ``redaction_status`` if it never settles.
    """
    import time

    deadline = time.time() + timeout_secs
    last_status_code: int | None = None
    last_redaction_status: str | None = None
    while time.time() < deadline:
        resp = api_session.get(f"{backend_url}/api/files/{file_uuid}/segments", timeout=30)
        last_status_code = resp.status_code
        if resp.status_code == 200:
            payload = dict(resp.json())
            if not payload.get("redaction_pending"):
                return list(payload["transcript_segments"])
            last_redaction_status = payload.get("redaction_status")
        time.sleep(1)
    raise AssertionError(
        f"transcript for {file_uuid} never became readable within {timeout_secs}s "
        f"(last HTTP status={last_status_code}, redaction_status={last_redaction_status!r}) — "
        "a redaction scan that never settles strands the transcript on every read surface"
    )


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

        segments = _segments_when_redaction_settled(api_session, backend_url, uploaded_file)
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

        segments = _segments_when_redaction_settled(api_session, backend_url, uploaded_file)
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
