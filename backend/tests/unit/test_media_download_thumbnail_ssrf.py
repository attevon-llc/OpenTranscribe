"""Thumbnail-fetch SSRF guard (issue #545-adjacent, this session's audit).

``MediaDownloadService.is_valid_media_url`` already runs ``is_safe_url`` against the
SUBMITTED page URL before yt-dlp ever touches it — but the thumbnail this module later
downloads is yt-dlp's EXTRACTED metadata for that page, a value the submitting page (via
a generic extractor) controls independently of the URL that was validated. This was
fetched with no check at all, the same SSRF shape the LLM context-window probe had
(``llm_context_window.py``) before this session's fix.

A check on the thumbnail URL alone is not enough either: the fetch must go to the address
that was checked (no second DNS answer) and must not follow a redirect to an unchecked
one. The tests intercept at ``HTTPAdapter.send``, the layer every ``requests`` call
reaches, so they observe what would actually be dialled regardless of whether the code
uses ``requests.get`` or a session.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse

import pytest
import requests
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict

from app.services.media_download_service import MediaDownloadService
from tests.helpers import PUBLIC_TEST_ADDRESS
from tests.helpers import stub_public_dns

_THUMB_BYTES = b"\xff\xd8\xff\xe0thumbnail-jpeg"
_METADATA_BYTES = b"instance-metadata-body"


@pytest.fixture
def service() -> MediaDownloadService:
    return MediaDownloadService()


@pytest.fixture
def uploads(monkeypatch) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []

    def fake_upload_file(*, file_content, file_size, object_name, content_type):
        recorded.append(
            {"object_name": object_name, "data": file_content.read(), "size": file_size}
        )

    monkeypatch.setattr("app.services.media_download_service.upload_file", fake_upload_file)
    return recorded


def _response(request: requests.PreparedRequest, status: int, **kw: Any) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp.headers = CaseInsensitiveDict(kw.get("headers", {}))
    resp._content = kw.get("body", b"")
    resp._content_consumed = True
    resp.url = request.url or ""
    resp.request = request
    return resp


def _dialled_address(request: requests.PreparedRequest) -> str:
    """The address the connection would go to: an IP literal as-is, else a fresh lookup."""
    host = urlparse(request.url).hostname or ""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return str(socket.getaddrinfo(host, 443)[0][4][0])


def _intercept(monkeypatch, handler) -> list[requests.PreparedRequest]:
    """Route every ``requests`` send through *handler*; return the list of sent requests."""
    sent: list[requests.PreparedRequest] = []

    def fake_send(self, request, *args, **kwargs):
        # Record, don't raise: the service catches every Exception, so an
        # AssertionError raised here would be swallowed and the test would pass
        # even with the guard removed.
        sent.append(request)
        return handler(request)

    monkeypatch.setattr(HTTPAdapter, "send", fake_send)
    return sent


def _refuse_network(request: requests.PreparedRequest) -> requests.Response:
    raise requests.exceptions.ConnectionError("no real network in a unit test")


class TestThumbnailSsrfGuard:
    def test_a_private_thumbnail_target_is_refused_before_any_http_call(self, service, monkeypatch):
        sent = _intercept(monkeypatch, _refuse_network)

        result = service._download_media_thumbnail_sync(
            {"thumbnail": "http://169.254.169.254/latest/meta-data"}, user_id=1
        )
        assert result is None
        assert sent == []

    def test_a_localhost_thumbnail_target_is_refused(self, service, monkeypatch):
        sent = _intercept(monkeypatch, _refuse_network)

        result = service._download_media_thumbnail_sync(
            {"thumbnail": "http://127.0.0.1:6379/"}, user_id=1
        )
        assert result is None
        assert sent == []

    def test_the_highest_quality_thumbnail_from_a_list_is_also_guarded(self, service, monkeypatch):
        """The checked value isn't always media_info['thumbnail'] — it can come from
        the highest-width entry in media_info['thumbnails'], resolved by
        `_resolve_thumbnail_url`. Both shapes must be checked."""
        sent = _intercept(monkeypatch, _refuse_network)

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
        assert sent == []

    def test_a_redirect_to_a_private_address_is_not_followed(self, service, monkeypatch, uploads):
        stub_public_dns(monkeypatch, domain="thumbs.example.com")

        def handler(request):
            if urlparse(request.url).path == "/vi/abc123/maxresdefault.jpg":
                return _response(
                    request,
                    302,
                    headers={"Location": "http://169.254.169.254/latest/meta-data"},
                )
            return _response(request, 200, body=_METADATA_BYTES)

        sent = _intercept(monkeypatch, handler)

        result = service._download_media_thumbnail_sync(
            {
                "thumbnail": "https://thumbs.example.com/vi/abc123/maxresdefault.jpg",
                "id": "abc123",
                "extractor": "generic",
            },
            user_id=1,
        )

        assert len(sent) == 1
        assert "169.254.169.254" not in {urlparse(r.url).hostname for r in sent}
        assert result is None
        assert uploads == []

    def test_a_hostname_that_resolves_private_after_the_check_is_not_dialled(
        self, service, monkeypatch, uploads
    ):
        """One DNS answer is judged; the connection must go to that answer, not to
        whatever a second lookup of the same name returns."""
        real_getaddrinfo = socket.getaddrinfo
        answers = iter([PUBLIC_TEST_ADDRESS])

        def flipping_getaddrinfo(host, port, *args, **kwargs):
            if str(host).lower().rstrip(".") == "rebind.example.com":
                address = next(answers, "10.0.0.5")
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]
            return real_getaddrinfo(host, port, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", flipping_getaddrinfo)
        dialled: list[str] = []

        def handler(request):
            dialled.append(_dialled_address(request))
            return _response(request, 200, body=_THUMB_BYTES)

        sent = _intercept(monkeypatch, handler)

        service._download_media_thumbnail_sync(
            {"thumbnail": "https://rebind.example.com/t.jpg", "id": "r1", "extractor": "generic"},
            user_id=1,
        )

        assert dialled == [PUBLIC_TEST_ADDRESS]
        assert sent[0].headers["Host"] == "rebind.example.com"

    def test_a_public_thumbnail_still_downloads_and_uploads(self, service, monkeypatch, uploads):
        """Control: the guard doesn't block legitimate thumbnails, only unsafe ones."""
        stub_public_dns(monkeypatch, domain="thumbs.example.com")
        sent = _intercept(monkeypatch, lambda request: _response(request, 200, body=_THUMB_BYTES))

        result = service._download_media_thumbnail_sync(
            {
                "thumbnail": "https://thumbs.example.com/vi/abc123/maxresdefault.jpg",
                "id": "abc123",
                "extractor": "Youtube",
            },
            user_id=1,
        )

        assert result == "user_1/youtube_abc123/thumbnail.jpg"
        assert len(sent) == 1
        assert uploads == [
            {
                "object_name": "user_1/youtube_abc123/thumbnail.jpg",
                "data": _THUMB_BYTES,
                "size": len(_THUMB_BYTES),
            }
        ]
