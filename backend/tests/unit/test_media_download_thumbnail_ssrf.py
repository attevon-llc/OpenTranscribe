"""Thumbnail-fetch SSRF guard (issue #545-adjacent, this session's audit).

``MediaDownloadService.is_valid_media_url`` already runs ``is_safe_url`` against the
SUBMITTED page URL before yt-dlp ever touches it — but the thumbnail this module later
downloads is yt-dlp's EXTRACTED metadata for that page, a value the submitting page (via
a generic extractor) controls independently of the URL that was validated. This was
fetched with no check at all, the same SSRF shape the LLM context-window probe had
(``llm_context_window.py``) before this session's fix.
"""

from __future__ import annotations

import pytest

from app.services.media_download_service import MediaDownloadService


@pytest.fixture
def service() -> MediaDownloadService:
    return MediaDownloadService()


class TestThumbnailSsrfGuard:
    def test_a_private_thumbnail_target_is_refused_before_any_http_call(self, service, monkeypatch):
        def explode(*a, **kw):  # pragma: no cover - the assertion is that it never runs
            raise AssertionError("an SSRF-blocked thumbnail URL must never be dialled")

        monkeypatch.setattr("app.services.media_download_service.requests.get", explode)

        result = service._download_media_thumbnail_sync(
            {"thumbnail": "http://169.254.169.254/latest/meta-data"}, user_id=1
        )
        assert result is None

    def test_a_localhost_thumbnail_target_is_refused(self, service, monkeypatch):
        def explode(*a, **kw):  # pragma: no cover
            raise AssertionError("an SSRF-blocked thumbnail URL must never be dialled")

        monkeypatch.setattr("app.services.media_download_service.requests.get", explode)

        result = service._download_media_thumbnail_sync(
            {"thumbnail": "http://127.0.0.1:6379/"}, user_id=1
        )
        assert result is None

    def test_a_public_thumbnail_target_still_reaches_the_fetch(self, service, monkeypatch):
        """Control: the guard doesn't block legitimate thumbnails, only unsafe ones —
        proven by letting the fetch itself fail harmlessly past the guard."""
        calls: list[str] = []

        def fake_get(url, timeout=None):
            calls.append(url)
            raise ConnectionError("no real network in a unit test")

        monkeypatch.setattr("app.services.media_download_service.requests.get", fake_get)

        result = service._download_media_thumbnail_sync(
            {"thumbnail": "https://i.ytimg.com/vi/abc123/maxresdefault.jpg"}, user_id=1
        )
        assert result is None  # the raised ConnectionError is caught and logged
        assert calls == ["https://i.ytimg.com/vi/abc123/maxresdefault.jpg"]

    def test_the_highest_quality_thumbnail_from_a_list_is_also_guarded(self, service, monkeypatch):
        """The vulnerable value isn't always media_info['thumbnail'] — it can come from
        the highest-width entry in media_info['thumbnails'], resolved by
        `_resolve_thumbnail_url`. Both shapes must be checked."""

        def explode(*a, **kw):  # pragma: no cover
            raise AssertionError("an SSRF-blocked thumbnail URL must never be dialled")

        monkeypatch.setattr("app.services.media_download_service.requests.get", explode)

        result = service._download_media_thumbnail_sync(
            {
                "thumbnails": [
                    {"url": "https://i.ytimg.com/vi/abc/default.jpg", "width": 120},
                    {"url": "http://169.254.169.254/latest/meta-data", "width": 9999},
                ]
            },
            user_id=1,
        )
        assert result is None
