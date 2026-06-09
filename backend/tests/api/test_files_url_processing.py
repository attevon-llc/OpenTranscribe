"""Characterization tests for ``files/url_processing.py``.

Covers ``POST /api/files/process-url`` validation paths and the
``GET /api/files/youtube/quota`` endpoint.

IMPORTANT: these tests NEVER hit YouTube/yt-dlp. They submit URLs that fail at
the in-process validation gate (``is_valid_media_url`` — pure regex + SSRF guard)
*before* any network extraction, plus malformed-payload cases. The valid-URL
extraction/dispatch path requires real network access (out of scope here); the
conftest Celery fixture already no-ops dispatch for any path that reaches it.
The ``normalize_media_url`` helper is unit-tested directly (no I/O).
"""

from __future__ import annotations

from fastapi import status

from app.api.endpoints.files.url_processing import normalize_media_url

# ---------------------------------------------------------------------------
# normalize_media_url (pure helper, no I/O)
# ---------------------------------------------------------------------------


def test_normalize_youtube_short_url():
    assert (
        normalize_media_url("https://youtu.be/dQw4w9WgXcQ")
        == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    )


def test_normalize_youtube_watch_url():
    assert (
        normalize_media_url("https://www.youtube.com/watch?v=abc123&t=10s")
        == "https://www.youtube.com/watch?v=abc123"
    )


def test_normalize_youtube_playlist():
    assert (
        normalize_media_url("https://youtube.com/playlist?list=PL123")
        == "https://www.youtube.com/playlist?list=PL123"
    )


def test_normalize_non_youtube_passthrough():
    assert normalize_media_url("  https://vimeo.com/123456789  ") == "https://vimeo.com/123456789"


# ---------------------------------------------------------------------------
# POST /api/files/process-url  (validation, no network)
# ---------------------------------------------------------------------------


def test_process_url_non_http_scheme_400(client, user_token_headers):
    """A non-HTTP(S) URL fails the generic-URL gate before any extraction."""
    response = client.post(
        "/api/files/process-url",
        headers=user_token_headers,
        json={"url": "ftp://example.com/video.mp4"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "Invalid URL. Please enter a valid HTTP or HTTPS URL."


def test_process_url_plain_text_400(client, user_token_headers):
    """A non-URL string is rejected as invalid (no scheme)."""
    response = client.post(
        "/api/files/process-url",
        headers=user_token_headers,
        json={"url": "just some words"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "Invalid URL. Please enter a valid HTTP or HTTPS URL."


def test_process_url_ssrf_localhost_400(client, user_token_headers):
    """An SSRF-unsafe localhost URL is blocked by the safety gate (no fetch)."""
    response = client.post(
        "/api/files/process-url",
        headers=user_token_headers,
        json={"url": "http://localhost:8080/internal"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "Invalid URL. Please enter a valid HTTP or HTTPS URL."


def test_process_url_ssrf_private_ip_400(client, user_token_headers):
    response = client.post(
        "/api/files/process-url",
        headers=user_token_headers,
        json={"url": "http://169.254.169.254/latest/meta-data/"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "Invalid URL. Please enter a valid HTTP or HTTPS URL."


def test_process_url_empty_string_422(client, user_token_headers):
    """``url`` has min_length=1 → empty string is a 422 (validation)."""
    response = client.post("/api/files/process-url", headers=user_token_headers, json={"url": ""})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_process_url_missing_url_422(client, user_token_headers):
    response = client.post("/api/files/process-url", headers=user_token_headers, json={})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_process_url_invalid_video_quality_422(client, user_token_headers):
    """``video_quality`` is validated against an allow-list by a field_validator."""
    response = client.post(
        "/api/files/process-url",
        headers=user_token_headers,
        json={"url": "https://vimeo.com/1", "video_quality": "9001p"},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_process_url_invalid_audio_quality_422(client, user_token_headers):
    response = client.post(
        "/api/files/process-url",
        headers=user_token_headers,
        json={"url": "https://vimeo.com/1", "audio_quality": "ultra"},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_process_url_unauthorized(client):
    response = client.post(
        "/api/files/process-url", json={"url": "https://www.youtube.com/watch?v=abc"}
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET /api/files/youtube/quota
# ---------------------------------------------------------------------------


def test_youtube_quota_owner(client, user_token_headers):
    response = client.get("/api/files/youtube/quota", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    for key in ("hourly_remaining", "daily_remaining", "hourly_limit", "daily_limit"):
        assert key in body


def test_youtube_quota_unauthorized(client):
    response = client.get("/api/files/youtube/quota")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
