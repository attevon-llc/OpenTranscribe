"""
Test selective reprocessing — all stages, combinations, and bulk mode.

Exercises the real API endpoints against a completed file in the database.
Run from the repo root with the dev environment running:

    source backend/venv/bin/activate
    pytest backend/tests/test_selective_reprocess.py -v -s

Requires:
    - Dev environment running (./opentr.sh start dev)
    - At least one completed file in the database
"""

import logging
import os
import time
import uuid
from pathlib import Path

import pytest
import requests

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.integration

BASE_URL = "http://localhost:5174/api"
LOGIN_EMAIL = "admin@example.com"
LOGIN_PASSWORD = "password"


@pytest.fixture(scope="module")
def auth_token():
    """Get auth token by logging in (retry through transient rate limiting)."""
    resp: requests.Response | None = None
    for attempt in range(4):
        try:
            resp = requests.post(
                f"{BASE_URL}/auth/login",
                data={"username": LOGIN_EMAIL, "password": LOGIN_PASSWORD},
                timeout=5,
            )
        except requests.ConnectionError:
            pytest.skip("Dev environment not running — skipping integration test")
        if resp is not None and resp.status_code == 200:
            break
        time.sleep(10 * (attempt + 1))
    assert resp is not None and resp.status_code == 200, (
        f"Login failed: {resp.text if resp is not None else 'no response'}"
    )
    token = resp.json().get("access_token")
    assert token, "No access_token in login response"
    return token


@pytest.fixture(scope="module")
def headers(auth_token):
    """Auth headers for API requests."""
    return {"Authorization": f"Bearer {auth_token}"}


#: The committed 10 s / mono / 16 kHz fixture — see `tests/fixtures/media/README.md`.
#: Small, deterministic and GPU-light, so this module runs the same way everywhere.
_SAMPLE_AUDIO = Path(__file__).resolve().parent / "fixtures" / "media" / "sample_short.wav"

#: Local-only override for real-corpus verification (`test_videos/`, the AMI corpus, a long
#: meeting recording). CI runners have no GPU and no such assets, so the committed fixture
#: stays the default and this is opt-in:
#:
#:     REPROCESS_TEST_MEDIA=test_videos/test_ai_video.mp4 pytest tests/test_selective_reprocess.py
#:
#: Mind the VRAM: diarizing a multi-hour file needs far more than the 12 GB on this box —
#: that is what made these tests fail before they used a short clip.
_MEDIA_OVERRIDE_ENV = "REPROCESS_TEST_MEDIA"


def _source_media() -> Path:
    """The clip to upload: the local override when set, else the committed fixture."""
    override = os.environ.get(_MEDIA_OVERRIDE_ENV, "").strip()
    if not override:
        return _SAMPLE_AUDIO
    path = Path(override)
    if not path.is_absolute():
        # Relative paths resolve from the repo root, which is where these are run from.
        path = Path(__file__).resolve().parents[2] / path
    if not path.exists():
        pytest.fail(f"{_MEDIA_OVERRIDE_ENV}={override!r} does not exist (resolved to {path})")
    return path


@pytest.fixture(scope="module")
def completed_file(headers):
    """Upload the committed short fixture and process it, then clean it up.

    This used to reprocess whichever completed file the dev database happened to hold,
    which was wrong three ways:

    * **It mutated real data.** Reprocessing rewrites an existing user's transcript in
      place, which ``backend/tests/CLAUDE.md`` forbids ("E2E must never persist changes
      to dev data").
    * **It could not pass on this hardware.** The shortest *completed* file here is
      8168 s (2 h 16 m); diarizing it exhausts a 12 GB GPU even at ``batch_size=1``, so
      the rediarize/transcription stages OOM'd. The first failure left the file in
      ``error``, and because this fixture is module-scoped every later test in the module
      then failed on the same poisoned file.
    * **It was ambient.** Whether the suite passed depended on what someone had uploaded.

    A 10 s clip removes all three: it is deterministic, it fits in any GPU, and it is
    deleted afterwards so the dev library is unchanged.
    """
    source = _source_media()
    if not source.exists():  # pragma: no cover - the committed fixture is tracked
        pytest.skip(f"missing media fixture {source}")

    mime = "video/mp4" if source.suffix.lower() in {".mp4", ".m4v", ".mov"} else "audio/wav"
    with source.open("rb") as fh:
        resp = requests.post(
            f"{BASE_URL}/files",
            headers=headers,
            files={
                "file": (f"reprocess-{uuid.uuid4().hex[:8]}{source.suffix}", fh, mime),
            },
            timeout=300,
        )
    assert resp.status_code == 200, f"Upload failed: {resp.status_code} {resp.text}"
    uploaded = resp.json()
    file_uuid = uploaded["uuid"]

    try:
        # Transcribing 10 s is quick, but the queue may be busy; this is the initial
        # ingest, not a reprocess, so give it its own generous window.
        assert _wait_for_completed(headers, file_uuid, max_wait=300), (
            f"uploaded fixture {file_uuid} never reached a stable completed state"
        )

        resp = requests.get(f"{BASE_URL}/files/{file_uuid}", headers=headers, timeout=30)
        assert resp.status_code == 200, f"Failed to re-read uploaded file: {resp.text}"
        f = resp.json()
        logger.info(
            f"\nUsing uploaded fixture: {f.get('filename', '?')[:50]}"
            f"\n  UUID: {f['uuid']}"
            f"\n  Duration: {f.get('duration', 0):.1f}s"
            f"\n  Status: {f.get('status')}"
        )
        yield f
    finally:
        # Delete on the way out no matter how the module ended — a cleanup that only runs
        # on the happy path is exactly the one that does not run when a test fails.
        try:
            requests.delete(f"{BASE_URL}/files/{file_uuid}", headers=headers, timeout=30)
        except requests.RequestException:
            logger.warning("could not delete uploaded fixture %s", file_uuid)


#: Statuses a file can never leave on its own. Reaching one means the answer is already
#: decided, so continuing to poll for "completed" only burns the rest of the window.
_TERMINAL_FAILURE_STATUSES = frozenset({"error", "cancelled"})


def _wait_for_completed(headers, file_uuid, max_wait=180):
    """Wait for the file to be STABLY completed.

    A single 'completed' poll is not enough: chained async stages (analytics,
    search indexing) can flip the file back to PROCESSING moments after a
    prior stage reports completed, which races the next reprocess request
    into an INVALID_STATUS rejection. Require two consecutive completed polls.

    Fails FAST on a terminal failure status. Without that, a stage that errors in
    ~50 s still costs the caller its whole window — ``max_wait=600`` at 1-2 s a poll
    is 10-20 minutes — waiting for a "completed" that can never arrive, and then
    reports a bare "File not completed" that says nothing about what went wrong.
    Observed: a rediarize OOM'd on a 61-minute file, and the suite spent the next
    several minutes polling a row that already said ``error``.
    """
    consecutive = 0
    for i in range(max_wait):
        resp = requests.get(f"{BASE_URL}/files/{file_uuid}", headers=headers)
        if resp.status_code == 200:
            body = resp.json()
            status = body.get("status")
            if status in _TERMINAL_FAILURE_STATUSES:
                # `GET /files/{uuid}` carries error_category, not last_error_message; the
                # full message lives on /status-detail, so name the category and point at it
                # rather than reporting an empty string.
                detail = body.get("last_error_message") or body.get("error_category") or "unknown"
                pytest.fail(
                    f"file {file_uuid} reached terminal status {status!r} after {i}s and "
                    f"cannot become completed (error_category={detail}). "
                    f"Full message: GET {BASE_URL}/files/{file_uuid}/status-detail"
                )
            if status == "completed":
                consecutive += 1
                if consecutive >= 2:
                    return True
            else:
                consecutive = 0
            if i % 10 == 0 and i > 0:
                logger.info(f"  Waiting for completion... status={status} ({i}s)")
        time.sleep(2 if consecutive else 1)
    return False


def _reprocess(headers, file_uuid, stages):
    """Send a selective reprocess request."""
    resp = requests.post(
        f"{BASE_URL}/files/{file_uuid}/reprocess",
        headers=headers,
        json={"stages": stages},
    )
    return resp


def _bulk_reprocess(headers, file_uuids, stages):
    """Send a bulk selective reprocess request."""
    resp = requests.post(
        f"{BASE_URL}/files/management/bulk-action",
        headers=headers,
        json={"file_uuids": file_uuids, "action": "reprocess", "stages": stages},
    )
    return resp


# ── Individual stage tests ──────────────────────────────────────────


class TestSingleStages:
    """Test each stage individually — verifies API acceptance and task dispatch."""

    @pytest.mark.parametrize(
        "stage",
        [
            "search_indexing",
            "analytics",
            "speaker_llm",
            "summarization",
            "topic_extraction",
        ],
    )
    def test_non_destructive_stage(self, headers, completed_file, stage):
        """Non-destructive stages dispatch without touching file status."""
        file_uuid = completed_file["uuid"]
        assert _wait_for_completed(headers, file_uuid), "File not in completed state"

        resp = _reprocess(headers, file_uuid, [stage])
        assert resp.status_code == 200, f"[{stage}] HTTP {resp.status_code}: {resp.text}"

        # File should remain completed (non-destructive stage)
        data = resp.json()
        assert data.get("status") == "completed", (
            f"[{stage}] Expected status=completed, got {data.get('status')}"
        )
        logger.info(f"  [{stage}] OK — dispatched, file still completed")

    def test_rediarize_stage(self, headers, completed_file):
        """Rediarize dispatches successfully, file goes to processing."""
        file_uuid = completed_file["uuid"]
        assert _wait_for_completed(headers, file_uuid, max_wait=600), "File not completed"

        resp = _reprocess(headers, file_uuid, ["rediarize"])
        assert resp.status_code == 200, f"[rediarize] HTTP {resp.status_code}: {resp.text}"
        logger.info(f"  [rediarize] OK — dispatched, status={resp.json().get('status')}")

        # Wait for rediarize to complete before next test
        assert _wait_for_completed(headers, file_uuid, max_wait=600), (
            "File did not return to completed after rediarize"
        )

    def test_transcription_stage(self, headers, completed_file):
        """Transcription dispatches successfully, file goes to processing."""
        file_uuid = completed_file["uuid"]
        assert _wait_for_completed(headers, file_uuid, max_wait=600), "File not completed"

        resp = _reprocess(headers, file_uuid, ["transcription"])
        assert resp.status_code == 200, f"[transcription] HTTP {resp.status_code}: {resp.text}"

        data = resp.json()
        logger.info(f"  [transcription] OK — dispatched, status={data.get('status')}")

        # Wait for transcription to complete before other tests
        assert _wait_for_completed(headers, file_uuid, max_wait=600), (
            "File did not return to completed after transcription"
        )


# ── Combination tests ──────────────────────────────────────────


class TestCombinations:
    """Test multi-stage combinations."""

    def test_all_non_destructive(self, headers, completed_file):
        """All 5 non-destructive stages at once."""
        file_uuid = completed_file["uuid"]
        assert _wait_for_completed(headers, file_uuid, max_wait=600)

        stages = [
            "search_indexing",
            "analytics",
            "speaker_llm",
            "summarization",
            "topic_extraction",
        ]
        resp = _reprocess(headers, file_uuid, stages)
        assert resp.status_code == 200, f"All non-destructive: {resp.text}"
        assert resp.json().get("status") == "completed"
        logger.info("  [all non-destructive] OK — 5 stages dispatched")

    def test_rediarize_with_downstream(self, headers, completed_file):
        """Rediarize + analytics + search_indexing."""
        file_uuid = completed_file["uuid"]
        assert _wait_for_completed(headers, file_uuid, max_wait=600)

        stages = ["rediarize", "analytics", "search_indexing"]
        resp = _reprocess(headers, file_uuid, stages)
        assert resp.status_code == 200, f"rediarize+downstream: {resp.text}"
        logger.info("  [rediarize + analytics + search_indexing] OK")
        assert _wait_for_completed(headers, file_uuid, max_wait=600)

    def test_rediarize_with_speaker_llm(self, headers, completed_file):
        """Rediarize + speaker_llm — LLM chains via attribute detection."""
        file_uuid = completed_file["uuid"]
        assert _wait_for_completed(headers, file_uuid, max_wait=600)

        stages = ["rediarize", "speaker_llm"]
        resp = _reprocess(headers, file_uuid, stages)
        assert resp.status_code == 200, f"rediarize+speaker_llm: {resp.text}"
        logger.info("  [rediarize + speaker_llm] OK")
        assert _wait_for_completed(headers, file_uuid, max_wait=600)


# ── Bulk mode tests ──────────────────────────────────────────


class TestBulkMode:
    """Test bulk selective reprocessing via the management endpoint."""

    def test_bulk_analytics(self, headers, completed_file):
        """Bulk reprocess single file — analytics only."""
        file_uuid = completed_file["uuid"]
        assert _wait_for_completed(headers, file_uuid, max_wait=600)

        resp = _bulk_reprocess(headers, [file_uuid], ["analytics"])
        assert resp.status_code == 200, f"Bulk analytics: {resp.text}"
        results = resp.json()
        assert isinstance(results, list) and len(results) == 1
        assert results[0]["success"], f"Bulk result: {results[0]}"
        logger.info(f"  [bulk analytics] OK — {results[0]['message']}")

    def test_bulk_multiple_stages(self, headers, completed_file):
        """Bulk reprocess with multiple non-destructive stages."""
        file_uuid = completed_file["uuid"]
        assert _wait_for_completed(headers, file_uuid, max_wait=600)

        resp = _bulk_reprocess(
            headers,
            [file_uuid],
            ["analytics", "search_indexing", "summarization"],
        )
        assert resp.status_code == 200, f"Bulk multi: {resp.text}"
        results = resp.json()
        assert results[0]["success"], f"Bulk result: {results[0]}"
        logger.info(f"  [bulk multi-stage] OK — {results[0]['message']}")

    def test_bulk_empty_stages_is_full_reprocess(self, headers, completed_file):
        """Empty stages = full reprocess (backward compatible)."""
        file_uuid = completed_file["uuid"]
        assert _wait_for_completed(headers, file_uuid, max_wait=600)

        resp = _bulk_reprocess(headers, [file_uuid], [])
        assert resp.status_code == 200, f"Bulk full: {resp.text}"
        results = resp.json()
        assert results[0]["success"], f"Bulk result: {results[0]}"
        logger.info(f"  [bulk full reprocess] OK — {results[0]['message']}")
        # Wait for full reprocess to complete
        assert _wait_for_completed(headers, file_uuid, max_wait=600)


# ── Validation tests ──────────────────────────────────────────


class TestValidation:
    """Edge cases and error handling."""

    def test_invalid_uuid_returns_error(self, headers):
        """Nonexistent UUID should return 404."""
        resp = _reprocess(headers, "00000000-0000-0000-0000-000000000000", ["analytics"])
        assert resp.status_code == 404, (
            resp.text
        )  # the docstring says 404; accepting 400 too made it unfalsifiable

    def test_transcription_and_rediarize_together(self, headers, completed_file):
        """Both core stages — transcription subsumes rediarize."""
        file_uuid = completed_file["uuid"]
        assert _wait_for_completed(headers, file_uuid, max_wait=600)

        resp = _reprocess(headers, file_uuid, ["transcription", "rediarize"])
        assert resp.status_code == 200, f"Both core stages: {resp.text}"
        logger.info("  [transcription + rediarize] OK — transcription subsumes")
        assert _wait_for_completed(headers, file_uuid, max_wait=600)


# ── Quick smoke test ──────────────────────────────────────────


def test_smoke_all_non_destructive(headers, completed_file):
    """Smoke: fire every non-destructive stage one at a time, verify API accepts all."""
    file_uuid = completed_file["uuid"]
    stages = ["search_indexing", "analytics", "speaker_llm", "summarization", "topic_extraction"]

    results = {}
    for stage in stages:
        assert _wait_for_completed(headers, file_uuid, max_wait=60), f"File not ready for {stage}"
        resp = _reprocess(headers, file_uuid, [stage])
        ok = resp.status_code == 200
        results[stage] = "OK" if ok else f"FAIL {resp.status_code}: {resp.text[:80]}"

    logger.info("\n=== Smoke Test Results ===")
    for stage, result in results.items():
        logger.info(f"  {stage:20s} → {result}")

    failures = {s: r for s, r in results.items() if not r.startswith("OK")}
    assert not failures, f"Failed stages: {failures}"
