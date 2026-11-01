"""Characterization of the refusals raised by the media-download plane.

These tests pin status, detail, headers and ``str(exc)`` of every refusal that
``media_download_service``, the MediaCMS provider and ``url_validation`` raise, so they pass
unchanged whether the refusal is a ``fastapi.HTTPException`` or the equivalent
``DomainError``. ``str(exc)`` is load-bearing: ``tasks/youtube_processing.py`` stores it in
``last_error_message`` and runs ``categorize_error`` on it, and the ``502`` prefix is what makes a
MediaCMS failure a temporary (retriable) error.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
import requests
import yt_dlp
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.exceptions import InvalidInputError
from app.core.exceptions import OperationFailedError
from app.core.exceptions import UpstreamServiceError
from app.main import app as real_app
from app.services import media_download_service as mds
from app.services.media_download_service import MediaDownloadService
from app.services.protected_media_plugins import mediacms
from app.services.protected_media_plugins.mediacms import MediacmsProvider
from app.utils.error_classification import ErrorCategory
from app.utils.error_classification import categorize_error
from app.utils.url_validation import assert_safe_outbound_url
from tests.helpers import stub_pinned_session
from tests.helpers import stub_public_dns

_MEDIACMS_MODULE = "app.services.protected_media_plugins.mediacms"

_READ_INFO = "Could not read information for that media URL."
_READ_PLAYLIST = "Could not read that playlist."
_BAD_URL = "The provided URL could not be used. It must be a publicly reachable http(s) address."
_PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLabcdefghijklmnop"


def _assert_refusal(exc: BaseException, status: int, detail: str, cls: type[Exception]) -> None:
    """Assert the refusal is the HTTPException of old or the DomainError of new, exactly."""
    assert isinstance(exc, HTTPException | cls)
    assert getattr(exc, "status_code", None) == status
    assert getattr(exc, "detail", None) == detail
    assert getattr(exc, "headers", None) is None
    assert str(exc) == f"{status}: {detail}"
    assert not isinstance(exc, OSError)


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_public_dns(monkeypatch)


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> MediaDownloadService:
    svc = MediaDownloadService()
    # No protected provider: force the yt-dlp branch.
    monkeypatch.setattr(svc, "_get_protected_provider", lambda url, user_id=None: None)
    return svc


def _ydl_raising(exc: Exception) -> type:
    class _Ydl:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def __enter__(self) -> _Ydl:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

        def extract_info(self, *_a: Any, **_k: Any) -> Any:
            raise exc

        def download(self, *_a: Any, **_k: Any) -> Any:
            raise exc

    return _Ydl


# ---------------------------------------------------------------------------
# media_download_service
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raised", [yt_dlp.DownloadError("boom /secret/path"), RuntimeError("x")])
def test_extract_video_info_refuses_with_generic_400(service, monkeypatch, raised):
    monkeypatch.setattr(mds.yt_dlp, "YoutubeDL", _ydl_raising(raised))
    with pytest.raises((HTTPException, InvalidInputError)) as exc:
        service.extract_video_info("https://example.com/v")
    _assert_refusal(exc.value, 400, _READ_INFO, InvalidInputError)
    assert "secret" not in str(exc.value)
    assert exc.value.__cause__ is raised


def test_extract_playlist_info_refuses_with_generic_400(service, monkeypatch):
    raised = RuntimeError("playlist boom")
    monkeypatch.setattr(mds.yt_dlp, "YoutubeDL", _ydl_raising(raised))
    with pytest.raises((HTTPException, InvalidInputError)) as exc:
        service.extract_playlist_info(_PLAYLIST_URL)
    _assert_refusal(exc.value, 400, _READ_PLAYLIST, InvalidInputError)
    assert exc.value.__cause__ is raised


def test_download_video_refuses_an_invalid_url(service, tmp_path):
    with pytest.raises((HTTPException, InvalidInputError)) as exc:
        service.download_video("not a url", str(tmp_path))
    _assert_refusal(
        exc.value, 400, "Invalid URL. Please enter a valid HTTP or HTTPS URL.", InvalidInputError
    )


def test_download_video_download_error_is_400(service, monkeypatch, tmp_path):
    raised = yt_dlp.DownloadError("leaky /etc/cookies")
    monkeypatch.setattr(mds.yt_dlp, "YoutubeDL", _ydl_raising(raised))
    with pytest.raises((HTTPException, InvalidInputError)) as exc:
        service.download_video("https://example.com/v", str(tmp_path))
    _assert_refusal(
        exc.value, 400, "The media could not be downloaded from that URL.", InvalidInputError
    )
    assert exc.value.__cause__ is raised


def test_download_video_unexpected_error_is_500(service, monkeypatch, tmp_path):
    raised = RuntimeError("unexpected")
    monkeypatch.setattr(mds.yt_dlp, "YoutubeDL", _ydl_raising(raised))
    with pytest.raises((HTTPException, OperationFailedError)) as exc:
        service.download_video("https://example.com/v", str(tmp_path))
    _assert_refusal(exc.value, 500, "The media download failed.", OperationFailedError)
    assert exc.value.__cause__ is raised


def test_process_media_url_sync_refuses_an_invalid_url(service):
    with pytest.raises((HTTPException, InvalidInputError)) as exc:
        service.process_media_url_sync("not a url", MagicMock(), MagicMock(), MagicMock())
    _assert_refusal(exc.value, 400, "Invalid media URL", InvalidInputError)


def test_download_and_stage_media_requires_a_video_id(service, monkeypatch):
    monkeypatch.setattr(service, "extract_video_info", lambda *a, **k: {"title": "no id"})
    with pytest.raises((HTTPException, InvalidInputError)) as exc:
        service._download_and_stage_media(url="https://example.com/v", user_id=1, file_id=1)
    _assert_refusal(exc.value, 400, "Could not extract video ID from URL", InvalidInputError)


def test_playlist_sync_refuses_a_non_playlist_url(service):
    with pytest.raises((HTTPException, InvalidInputError)) as exc:
        service.process_youtube_playlist_sync(
            "https://www.youtube.com/watch?v=abc", MagicMock(), MagicMock()
        )
    _assert_refusal(exc.value, 400, "URL is not a valid YouTube playlist", InvalidInputError)


def test_playlist_sync_wraps_an_unexpected_extraction_error(service, monkeypatch):
    raised = RuntimeError("anything")

    def _boom(_url: str) -> dict[str, Any]:
        raise raised

    monkeypatch.setattr(service, "extract_playlist_info", _boom)
    with pytest.raises((HTTPException, InvalidInputError)) as exc:
        service.process_youtube_playlist_sync(_PLAYLIST_URL, MagicMock(), MagicMock())
    _assert_refusal(exc.value, 400, _READ_PLAYLIST, InvalidInputError)
    assert exc.value.__cause__ is raised


def test_playlist_sync_lets_a_refusal_through_unchanged(service, monkeypatch):
    refusal = UpstreamServiceError("upstream said no")

    def _boom(_url: str) -> dict[str, Any]:
        raise refusal

    monkeypatch.setattr(service, "extract_playlist_info", _boom)
    with pytest.raises((HTTPException, UpstreamServiceError)) as exc:
        service.process_youtube_playlist_sync(_PLAYLIST_URL, MagicMock(), MagicMock())
    assert exc.value is refusal


def test_playlist_sync_refuses_an_empty_playlist(service, monkeypatch):
    monkeypatch.setattr(service, "extract_playlist_info", lambda _u: {"video_count": 0})
    with pytest.raises((HTTPException, InvalidInputError)) as exc:
        service.process_youtube_playlist_sync(_PLAYLIST_URL, MagicMock(), MagicMock())
    _assert_refusal(
        exc.value, 400, "Playlist is empty or contains no accessible videos", InvalidInputError
    )


# ---------------------------------------------------------------------------
# MediaCMS provider
# ---------------------------------------------------------------------------


@pytest.fixture
def provider() -> MediacmsProvider:
    return MediacmsProvider()


def _login_ok() -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {"token": "auth-tok"}
    resp.raise_for_status = MagicMock()
    return resp


def _patched(provider: MediacmsProvider):
    return (
        patch.object(provider, "_get_allowed_hosts", return_value={"m.example.com"}),
        patch.object(provider, "_get_verify_ssl_for_host", return_value=True),
        patch.object(provider, "_get_stored_credentials", return_value=(None, None)),
    )


def test_pin_media_url_refuses_a_private_target():
    with pytest.raises((HTTPException, InvalidInputError)) as exc:
        mediacms._pin_media_url("http://169.254.169.254/latest", purpose="test")
    _assert_refusal(exc.value, 400, _BAD_URL, InvalidInputError)


def test_unknown_host_is_400(provider):
    with patch.object(provider, "_get_allowed_hosts", return_value=set()):
        with pytest.raises((HTTPException, InvalidInputError)) as exc:
            provider._get_token_and_base_url("https://evil.example.com/view?m=abc")
    _assert_refusal(
        exc.value, 400, "URL hostname is not in the configured media sources", InvalidInputError
    )


def test_missing_token_is_400(provider):
    with patch.object(provider, "_get_allowed_hosts", return_value={"m.example.com"}):
        with pytest.raises((HTTPException, InvalidInputError)) as exc:
            provider._get_token_and_base_url("https://m.example.com/about")
    _assert_refusal(
        exc.value,
        400,
        "Missing media token (m query param or /api/v1/media/<token>)",
        InvalidInputError,
    )


def test_missing_credentials_is_400(provider):
    with (
        patch.object(
            provider, "_get_token_and_base_url", return_value=("tok", "https://m.example.com")
        ),
        patch.object(provider, "_get_stored_credentials", return_value=(None, None)),
        pytest.raises((HTTPException, InvalidInputError)) as exc,
    ):
        provider._login_and_get_info("https://m.example.com/view?m=tok")
    _assert_refusal(
        exc.value,
        400,
        "Credentials for this media source are not configured. "
        "Either provide credentials in the request or configure "
        "them in Settings > Media Sources.",
        InvalidInputError,
    )


def test_login_without_token_is_502(provider, monkeypatch):
    login = MagicMock()
    login.json.return_value = {}
    session = MagicMock()
    session.post.return_value = login
    stub_pinned_session(monkeypatch, _MEDIACMS_MODULE, session)
    p1, p2, p3 = _patched(provider)
    with p1, p2, p3, pytest.raises((HTTPException, UpstreamServiceError)) as exc:
        provider._login_and_get_info("https://m.example.com/view?m=tok", username="u", password="p")
    _assert_refusal(
        exc.value, 502, "MediaCMS login did not return an auth token", UpstreamServiceError
    )


def test_info_request_error_is_502(provider, monkeypatch):
    session = MagicMock()
    session.post.return_value = _login_ok()
    session.get.side_effect = requests.exceptions.ConnectionError("http://10.0.0.5:8000 refused")
    stub_pinned_session(monkeypatch, _MEDIACMS_MODULE, session)
    p1, p2, p3 = _patched(provider)
    with p1, p2, p3, pytest.raises((HTTPException, UpstreamServiceError)) as exc:
        provider._login_and_get_info("https://m.example.com/view?m=tok", username="u", password="p")
    _assert_refusal(
        exc.value,
        502,
        "Could not fetch media information from the configured media source.",
        UpstreamServiceError,
    )
    assert "10.0.0.5" not in str(exc.value)
    assert isinstance(exc.value.__cause__, requests.exceptions.ConnectionError)


def test_login_leg_refusal_passes_through_the_request_error_wrapper(provider, monkeypatch):
    """A 400 raised inside the try (private login target) must not become a 502."""
    session = MagicMock()
    stub_pinned_session(monkeypatch, _MEDIACMS_MODULE, session)
    with (
        patch.object(
            provider,
            "_get_token_and_base_url",
            return_value=("tok", "https://169.254.169.254"),
        ),
        patch.object(provider, "_get_verify_ssl_for_host", return_value=True),
        patch.object(provider, "_get_stored_credentials", return_value=(None, None)),
        pytest.raises((HTTPException, InvalidInputError)) as exc,
    ):
        provider._login_and_get_info("https://m.example.com/view?m=tok", username="u", password="p")
    _assert_refusal(exc.value, 400, _BAD_URL, InvalidInputError)
    session.post.assert_not_called()


@pytest.mark.parametrize(
    ("info", "detail"),
    [
        ({"title": "no url"}, "MediaCMS media info is missing 'original_media_url'"),
        ({"original_media_url": "http://evil.com/x"}, "MediaCMS returned an invalid media URL"),
        ({"original_media_url": "//evil.com/x"}, "MediaCMS returned an invalid media URL"),
    ],
)
def test_download_refuses_a_bad_media_info(provider, monkeypatch, tmp_path, info, detail):
    info_resp = MagicMock()
    info_resp.json.return_value = info
    session = MagicMock()
    session.post.return_value = _login_ok()
    session.get.return_value = info_resp
    stub_pinned_session(monkeypatch, _MEDIACMS_MODULE, session)
    p1, p2, p3 = _patched(provider)
    with p1, p2, p3, pytest.raises((HTTPException, UpstreamServiceError)) as exc:
        provider.download(
            "https://m.example.com/view?m=v", str(tmp_path), username="u", password="p"
        )
    _assert_refusal(exc.value, 502, detail, UpstreamServiceError)


def test_download_request_error_is_502_and_cleans_up(provider, monkeypatch, tmp_path):
    info_resp = MagicMock()
    info_resp.json.return_value = {"original_media_url": "/media/x.mp4"}
    session = MagicMock()
    session.post.return_value = _login_ok()
    session.get.side_effect = [info_resp, requests.exceptions.ConnectionError("http://10.0.0.5")]
    stub_pinned_session(monkeypatch, _MEDIACMS_MODULE, session)
    p1, p2, p3 = _patched(provider)
    with p1, p2, p3, pytest.raises((HTTPException, UpstreamServiceError)) as exc:
        provider.download(
            "https://m.example.com/view?m=v", str(tmp_path), username="u", password="p"
        )
    _assert_refusal(
        exc.value,
        502,
        "Could not download the media file from the configured media source.",
        UpstreamServiceError,
    )
    assert "10.0.0.5" not in str(exc.value)


# ---------------------------------------------------------------------------
# url_validation
# ---------------------------------------------------------------------------


def test_assert_safe_outbound_url_refuses_with_generic_400():
    with pytest.raises((HTTPException, InvalidInputError)) as exc:
        assert_safe_outbound_url("http://169.254.169.254/", purpose="test")
    _assert_refusal(exc.value, 400, _BAD_URL, InvalidInputError)


# ---------------------------------------------------------------------------
# Stored / classified text: what tasks/youtube_processing.py persists
# ---------------------------------------------------------------------------


def test_stored_text_and_category_of_a_400_refusal():
    with pytest.raises((HTTPException, InvalidInputError)) as exc:
        MediaDownloadService().process_media_url_sync(
            "not a url", MagicMock(), MagicMock(), MagicMock()
        )
    assert str(exc.value) == "400: Invalid media URL"
    assert categorize_error(str(exc.value)) is ErrorCategory.UNKNOWN


def test_stored_text_and_category_of_a_url_validation_refusal():
    with pytest.raises((HTTPException, InvalidInputError)) as exc:
        assert_safe_outbound_url("http://169.254.169.254/", purpose="test")
    assert str(exc.value) == f"400: {_BAD_URL}"
    assert categorize_error(str(exc.value)) is ErrorCategory.UNKNOWN


def test_stored_text_and_category_of_a_500_refusal(service, monkeypatch, tmp_path):
    monkeypatch.setattr(mds.yt_dlp, "YoutubeDL", _ydl_raising(RuntimeError("x")))
    with pytest.raises((HTTPException, OperationFailedError)) as exc:
        service.download_video("https://example.com/v", str(tmp_path))
    assert str(exc.value) == "500: The media download failed."
    assert categorize_error(str(exc.value)) is ErrorCategory.UNKNOWN


def test_mediacms_502_is_a_temporary_service_error(provider, monkeypatch):
    login = MagicMock()
    login.json.return_value = {}
    session = MagicMock()
    session.post.return_value = login
    stub_pinned_session(monkeypatch, _MEDIACMS_MODULE, session)
    p1, p2, p3 = _patched(provider)
    with p1, p2, p3, pytest.raises((HTTPException, UpstreamServiceError)) as exc:
        provider._login_and_get_info("https://m.example.com/view?m=tok", username="u", password="p")
    assert str(exc.value) == "502: MediaCMS login did not return an auth token"
    assert categorize_error(str(exc.value)) is ErrorCategory.TEMPORARY_SERVICE_ERROR


# ---------------------------------------------------------------------------
# url_video_import._extract_video_info over the wire: a MediaCMS 502 stays a 502
# ---------------------------------------------------------------------------


@pytest.fixture
def wire_client(monkeypatch: pytest.MonkeyPatch):
    """Factory: a client whose probe route runs the real ``_extract_video_info`` + MediaCMS."""
    from app.api.endpoints.files.url_video_import import _extract_video_info

    def _make(session: MagicMock, allowed_hosts: set[str]) -> TestClient:
        provider = MediacmsProvider()
        monkeypatch.setattr(provider, "_get_allowed_hosts", lambda user_id=None: allowed_hosts)
        monkeypatch.setattr(provider, "_get_verify_ssl_for_host", lambda *a, **k: True)
        monkeypatch.setattr(provider, "_get_stored_credentials", lambda *a, **k: (None, None))
        monkeypatch.setattr(
            MediaDownloadService,
            "_get_protected_provider",
            lambda self, url, user_id=None: provider,
        )
        stub_pinned_session(monkeypatch, _MEDIACMS_MODULE, session)

        app = FastAPI(exception_handlers=dict(real_app.exception_handlers))

        @app.get("/probe")
        def probe(url: str) -> dict[str, Any]:
            vid, title, _info = _extract_video_info(MediaDownloadService(), url, "u", "p")
            return {"id": vid, "title": title}

        return TestClient(app, raise_server_exceptions=False)

    return _make


def test_endpoint_keeps_a_mediacms_502_a_502(wire_client):
    session = MagicMock()
    session.post.return_value = _login_ok()
    session.get.side_effect = requests.exceptions.ConnectionError("http://10.0.0.5 refused")
    client = wire_client(session, {"m.example.com"})
    r = client.get("/probe", params={"url": "https://m.example.com/view?m=tok"})
    assert r.status_code == 502
    assert r.content == (
        b'{"detail":"Could not fetch media information from the configured media source."}'
    )


def test_endpoint_keeps_a_mediacms_400_a_400(wire_client):
    client = wire_client(MagicMock(), set())
    r = client.get("/probe", params={"url": "https://evil.example.com/view?m=tok"})
    assert r.status_code == 400
    assert r.content == b'{"detail":"URL hostname is not in the configured media sources"}'
