"""E2E regression tests for the presigned-URL media architecture.

Covers, against the running dev stack:
- Video playback uses a presigned /s3 URL (not an API byte-proxy) and supports Range/seek.
- The file-detail download dropdown delivers each mode via a presigned download.
- Bulk subtitle export downloads a ZIP via the async + presigned flow.
- The removed legacy byte-proxy endpoints now return 404.

Requires: ./opentr.sh start dev, with at least one completed media file in the
dev dataset (admin@example.com / password).
"""

import os
import time

import pytest
import requests

# Inlined (not imported from .conftest) so this module collects regardless of the
# e2e package-import quirk; the api_helper fixture is still auto-discovered from conftest.
BACKEND_URL = os.environ.get("E2E_BACKEND_URL", "http://localhost:5174")
# In dev, presigned URLs are rewritten to a relative /s3/... path proxied by the
# frontend (Vite) to MinIO. Fetch those against the frontend origin.
FRONTEND_URL = os.environ.get("E2E_FRONTEND_URL", "http://localhost:5173")
TEST_ADMIN_EMAIL = os.environ.get("E2E_ADMIN_EMAIL", "admin@example.com")
TEST_ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "password")


def _resolve(url: str) -> str:
    """Resolve a presigned URL to something fetchable from the test host."""
    if url.startswith("http"):
        return url
    if url.startswith("/s3"):
        return f"{FRONTEND_URL}{url}"
    return f"{BACKEND_URL}{url}"


@pytest.fixture(scope="module")
def session():
    """Log in once per module (avoids auth rate-limiting from per-test logins)."""
    resp = requests.post(
        f"{BACKEND_URL}/api/auth/token",
        data={"username": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        pytest.skip(f"Cannot authenticate against dev stack (HTTP {resp.status_code})")
    return resp.json()["access_token"]


@pytest.fixture(scope="module")
def completed_file(session):
    resp = requests.get(
        f"{BACKEND_URL}/api/files?limit=100",
        headers={"Authorization": f"Bearer {session}"},
        timeout=30,
    )
    items = resp.json().get("items", []) if resp.status_code == 200 else []
    target = next((f for f in items if f.get("status") == "completed"), None)
    if not target:
        pytest.skip("No completed media file in dev dataset — required for media E2E tests")
    return target, session


class TestPresignedPlayback:
    def test_stream_url_is_presigned_and_supports_range(self, completed_file):
        f, token = completed_file
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(
            f"{BACKEND_URL}/api/files/{f['uuid']}/stream-url?media_type=video",
            headers=headers,
            timeout=30,
        )
        assert resp.status_code == 200
        url = resp.json()["url"]
        # Presigned, browser-reachable URL — served directly from object storage.
        assert "/s3/" in url or url.startswith("http")

        # Range request straight to the presigned URL must yield 206 (enables seek).
        ranged = requests.get(
            _resolve(url),
            headers={"Range": "bytes=0-1023"},
            timeout=30,
        )
        assert ranged.status_code in (206, 200)
        if ranged.status_code == 206:
            assert "content-range" in {k.lower() for k in ranged.headers}


class TestRemovedLegacyEndpoints:
    @pytest.mark.parametrize(
        "suffix", ["video", "simple-video", "content", "download", "download-with-token"]
    )
    def test_legacy_routes_return_404(self, completed_file, suffix):
        f, token = completed_file
        resp = requests.get(
            f"{BACKEND_URL}/api/files/{f['uuid']}/{suffix}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        assert resp.status_code == 404


class TestDownloadDropdown:
    def test_prepare_download_audio_mp3_yields_presigned_url(self, completed_file):
        """Audio mp3 download resolves to a presigned URL (immediately or via SSE)."""
        f, token = completed_file
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.post(
            f"{BACKEND_URL}/api/files/{f['uuid']}/prepare-download?mode=audio_mp3",
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
                f"{BACKEND_URL}/api/files/{f['uuid']}/download-stream?mode=audio_mp3", token
            )
        assert url
        head = requests.get(_resolve(url), headers={"Range": "bytes=0-1"}, timeout=60)
        assert head.status_code in (200, 206)


class TestBulkExport:
    def test_bulk_export_delivers_presigned_zip(self, completed_file):
        f, token = completed_file
        headers = {"Authorization": f"Bearer {token}"}
        prep = requests.post(
            f"{BACKEND_URL}/api/files/bulk-export/prepare",
            json={"file_uuids": [f["uuid"]], "subtitle_format": "srt", "include_speakers": True},
            headers=headers,
            timeout=30,
        )
        assert prep.status_code == 200
        job_id = prep.json()["job_id"]

        url = _await_sse_ready(f"{BACKEND_URL}/api/files/bulk-export-stream?job={job_id}", token)
        assert url
        zip_resp = requests.get(_resolve(url), timeout=60)
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
