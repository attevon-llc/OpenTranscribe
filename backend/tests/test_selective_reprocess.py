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
import time

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


@pytest.fixture(scope="module")
def completed_file(headers):
    """Find the shortest completed file in the database."""
    resp = requests.get(
        f"{BASE_URL}/files",
        headers=headers,
        params={"limit": "1", "sort_by": "duration", "sort_order": "asc"},
    )
    assert resp.status_code == 200, f"Failed to fetch files: {resp.text}"

    data = resp.json()
    files = data.get("items", [])
    completed = [f for f in files if f.get("status") == "completed"]
    if not completed:
        # Try fetching more files
        resp2 = requests.get(
            f"{BASE_URL}/files",
            headers=headers,
            params={"limit": "20", "sort_by": "duration", "sort_order": "asc"},
        )
        data2 = resp2.json()
        completed = [f for f in data2.get("items", []) if f.get("status") == "completed"]

    if not completed:
        pytest.skip("No completed files in database — upload and process one first")

    f = completed[0]
    logger.info(
        f"\nUsing file: {f.get('filename', '?')[:50]}"
        f"\n  UUID: {f['uuid']}"
        f"\n  Duration: {f.get('duration', 0):.1f}s"
        f"\n  Status: {f.get('status')}"
    )
    return f


def _wait_for_completed(headers, file_uuid, max_wait=180):
    """Wait for the file to be STABLY completed.

    A single 'completed' poll is not enough: chained async stages (analytics,
    search indexing) can flip the file back to PROCESSING moments after a
    prior stage reports completed, which races the next reprocess request
    into an INVALID_STATUS rejection. Require two consecutive completed polls.
    """
    consecutive = 0
    for i in range(max_wait):
        resp = requests.get(f"{BASE_URL}/files/{file_uuid}", headers=headers)
        if resp.status_code == 200:
            status = resp.json().get("status")
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
