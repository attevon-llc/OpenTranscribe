"""E2E regression tests for the presigned-URL media architecture.

Covers, against the running dev stack:
- Video playback uses a presigned /s3 URL (not an API byte-proxy) and supports Range/seek.
- The file-detail download dropdown delivers each mode via a presigned download.
- Bulk subtitle export downloads a ZIP via the async + presigned flow.
- The removed legacy byte-proxy endpoints now return 404.

Requires: ./opentr.sh start dev (admin@example.com / password). Creates and deletes
its own media — no pre-existing dev data required.
"""

import os
import time
import uuid

import pytest
import requests
from conftest import COMMITTED_SAMPLE_MEDIA
from conftest import OWNED_MEDIA_PREFIX
from conftest import delete_media_file
from conftest import wait_for_stable_completion

# URLs come from the `base_url` / `backend_url` fixtures in tests/e2e/conftest.py rather than
# module-level constants: a constant is evaluated at import time, so it can never see
# `--base-url` / `--backend-url` and a run aimed at an isolated stack silently drove the LIVE
# stack instead (issue #431).
TEST_ADMIN_EMAIL = os.environ.get("E2E_ADMIN_EMAIL", "admin@example.com")
TEST_ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "password")


def _resolve(url: str, base_url: str, backend_url: str) -> str:
    """Resolve a presigned URL to something fetchable from the test host.

    In dev, presigned URLs are rewritten to a relative /s3/... path proxied by the
    frontend (Vite) to MinIO, so those are fetched against the frontend origin.
    """
    if url.startswith("http"):
        return url
    if url.startswith("/s3"):
        return f"{base_url}{url}"
    return f"{backend_url}{url}"


@pytest.fixture(scope="module")
def session(backend_url: str):
    """Log in once per module (avoids auth rate-limiting from per-test logins)."""
    resp = requests.post(
        f"{backend_url}/api/auth/token",
        data={"username": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        pytest.skip(f"Cannot authenticate against dev stack (HTTP {resp.status_code})")
    return resp.json()["access_token"]


@pytest.fixture(scope="module")
def completed_file(session, backend_url: str):
    """A completed media file this module OWNS for its whole lifetime.

    Module-scoped on purpose: ~8 tests share one upload, and re-uploading per test
    would cost a real ASR run each time. It cannot depend on the function-scoped
    `owned_media_factory`, so it inlines the same contract from conftest's plain
    helpers.

    Previously this scavenged the first `completed` file out of the account. Under
    the parallel suite that intermittently grabbed another worker's ephemeral
    `e2e-owned-*` upload, which that worker then deleted mid-test — MinIO NoSuchKey
    / 404 on a file that had worked seconds earlier.
    """
    if not COMMITTED_SAMPLE_MEDIA.exists():  # pragma: no cover - the fixture is tracked
        pytest.skip(f"missing media fixture {COMMITTED_SAMPLE_MEDIA}")
    headers = {"Authorization": f"Bearer {session}"}
    name = f"{OWNED_MEDIA_PREFIX}{uuid.uuid4().hex[:8]}{COMMITTED_SAMPLE_MEDIA.suffix}"
    with COMMITTED_SAMPLE_MEDIA.open("rb") as fh:
        resp = requests.post(
            f"{backend_url}/api/files",
            headers=headers,
            files={"file": (name, fh, "audio/wav")},
            timeout=300,
        )
    assert resp.status_code == 200, f"Upload failed: {resp.status_code} {resp.text[:300]}"
    file_uuid = str(resp.json()["uuid"])
    try:
        status = wait_for_stable_completion(backend_url, session, file_uuid)
        assert status == "completed", f"uploaded fixture never completed (status={status})"
        detail = requests.get(f"{backend_url}/api/files/{file_uuid}", headers=headers, timeout=30)
        assert detail.status_code == 200, detail.text[:300]
        yield detail.json(), session
    finally:
        delete_media_file(backend_url, session, file_uuid)


class TestPresignedPlayback:
    def test_stream_url_is_presigned_and_supports_range(
        self, completed_file, base_url: str, backend_url: str
    ):
        f, token = completed_file
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(
            f"{backend_url}/api/files/{f['uuid']}/stream-url?media_type=video",
            headers=headers,
            timeout=30,
        )
        assert resp.status_code == 200
        url = resp.json()["url"]
        # Presigned, browser-reachable URL — served directly from object storage.
        assert "/s3/" in url or url.startswith("http")

        # Range request straight to the presigned URL must yield 206 (enables seek).
        # Was `in (206, 200)` with the content-range check gated behind `if == 206` — a
        # wrong status (or a 200 that silently dropped Range support) skipped the only
        # check that mattered. The docstring's own contract is 206; pin it unconditionally.
        ranged = requests.get(
            _resolve(url, base_url, backend_url),
            headers={"Range": "bytes=0-1023"},
            timeout=30,
        )
        assert ranged.status_code == 206, ranged.text[:300]
        assert "content-range" in {k.lower() for k in ranged.headers}


class TestRemovedLegacyEndpoints:
    @pytest.mark.parametrize(
        "suffix", ["video", "simple-video", "content", "download", "download-with-token"]
    )
    def test_legacy_routes_return_404(self, completed_file, suffix, backend_url: str):
        f, token = completed_file
        resp = requests.get(
            f"{backend_url}/api/files/{f['uuid']}/{suffix}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        assert resp.status_code == 404


class TestDownloadDropdown:
    def test_prepare_download_audio_mp3_yields_presigned_url(
        self, completed_file, base_url: str, backend_url: str
    ):
        """Audio mp3 download resolves to a presigned URL (immediately or via SSE)."""
        f, token = completed_file
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.post(
            f"{backend_url}/api/files/{f['uuid']}/prepare-download?mode=audio_mp3",
            headers=headers,
            timeout=30,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in ("ready", "processing")

        if body["status"] == "ready":
            url = body["url"]
        else:
            # Consume the SSE stream until ready (worker finishes ffmpeg extract).
            url = _await_sse_ready(
                f"{backend_url}/api/files/{f['uuid']}/download-stream?mode=audio_mp3", token
            )
        assert url
        # A Range request against MinIO's presigned URL reliably yields 206 (verified
        # against the live dev stack) — same contract as the stream-url test above.
        # Was `in (200, 206)`, which also accepted a 200 that silently dropped Range
        # support and, being an `in` on two 2xx values, never actually excluded a 500.
        head = requests.get(
            _resolve(url, base_url, backend_url), headers={"Range": "bytes=0-1"}, timeout=60
        )
        assert head.status_code == 206, head.text[:300]


class TestBulkExport:
    def test_bulk_export_delivers_presigned_zip(
        self, completed_file, base_url: str, backend_url: str
    ):
        f, token = completed_file
        headers = {"Authorization": f"Bearer {token}"}
        prep = requests.post(
            f"{backend_url}/api/files/bulk-export/prepare",
            json={"file_uuids": [f["uuid"]], "subtitle_format": "srt", "include_speakers": True},
            headers=headers,
            timeout=30,
        )
        assert prep.status_code == 200
        job_id = prep.json()["job_id"]

        url = _await_sse_ready(f"{backend_url}/api/files/bulk-export-stream?job={job_id}", token)
        assert url
        zip_resp = requests.get(_resolve(url, base_url, backend_url), timeout=60)
        assert zip_resp.status_code == 200
        assert zip_resp.headers.get("content-type", "").startswith("application/")
        assert zip_resp.content[:2] == b"PK"  # ZIP magic bytes


def _await_sse_ready(stream_url: str, token: str, timeout: float = 90.0) -> str | None:
    """Read an SSE stream until a `ready` event arrives; return its url."""
    import json

    deadline = time.time() + timeout
    with requests.get(
        stream_url,
        headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
        stream=True,
        timeout=timeout,
    ) as r:
        assert r.status_code == 200
        event = None
        for raw in r.iter_lines(decode_unicode=True):
            if time.time() > deadline:
                break
            if raw is None:
                continue
            line = raw.strip()
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                payload = line.split(":", 1)[1].strip()
                if event == "ready":
                    url = json.loads(payload).get("url")
                    return str(url) if url else None
                if event == "error":
                    return None
    return None
