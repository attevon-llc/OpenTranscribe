"""How ``MediaDownloadService`` routes a URL to a protected-media provider, credentials included.

Characterization tests pinned before the rewrite of the protected-media branches
in ``services/media_download_service.py``: provider selection, the fetch-time URL check that
runs before any provider is consulted, and the keyword hand-off of the import's site
credentials from ``process_media_url_sync`` down to the provider. yt-dlp is replaced by a
stand-in that fails the test if it is ever used.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.core.exceptions import InvalidInputError
from app.core.exceptions import UpstreamServiceError
from app.models.media import MediaFile
from app.models.user import User
from app.services import media_download_service as mds
from app.services import protected_media_providers
from app.services.media_download_service import MediaDownloadService
from tests.helpers import stub_public_dns

URL = "https://m.example.com/view?m=vid1"


class _Provider(protected_media_providers.ProtectedMediaProvider):
    """A protected-media provider that records every call."""

    def __init__(self, claims: bool = True, result: Any = None, error: Exception | None = None):
        self.claims = claims
        self.result = result if result is not None else {"id": "vid1"}
        self.error = error
        self.calls: list[tuple[str, tuple, dict]] = []

    def can_handle(self, url, user_id=None):
        self.calls.append(("can_handle", (url,), {"user_id": user_id}))
        return self.claims

    def extract_info(self, url, username=None, password=None, user_id=None):
        self.calls.append(
            (
                "extract_info",
                (url,),
                {"username": username, "password": password, "user_id": user_id},
            )
        )
        if self.error is not None:
            raise self.error
        return self.result

    def download(
        self, url, output_path, progress_callback=None, username=None, password=None, user_id=None
    ):
        self.calls.append(
            (
                "download",
                (url, output_path),
                {
                    "progress_callback": progress_callback,
                    "username": username,
                    "password": password,
                    "user_id": user_id,
                },
            )
        )
        return self.result

    def get_public_auth_config(self, user_id=None):
        return {}


class _Raising(_Provider):
    def can_handle(self, url, user_id=None):
        raise RuntimeError("plugin bug")


@pytest.fixture
def no_ytdlp(monkeypatch):
    def _forbidden(*_a, **_k):
        raise AssertionError("yt-dlp must not be used for a protected URL")

    monkeypatch.setattr(mds.yt_dlp, "YoutubeDL", _forbidden)


def _registry(monkeypatch, providers: list) -> None:
    monkeypatch.setattr(mds, "PROTECTED_MEDIA_PROVIDERS", providers)


# ---------------------------------------------------------------- provider selection


def test_the_first_claiming_provider_wins_and_a_raising_one_is_skipped(monkeypatch, caplog):
    raising, declining, first, second = (
        _Raising(),
        _Provider(claims=False),
        _Provider(),
        _Provider(),
    )
    _registry(monkeypatch, [raising, declining, first, second])

    with caplog.at_level(logging.WARNING, logger="app.services.media_download_service"):
        chosen = MediaDownloadService()._get_protected_provider(URL, user_id=4)

    assert chosen is first
    assert declining.calls == [("can_handle", (URL,), {"user_id": 4})]
    assert second.calls == []
    assert "_Raising" in caplog.text and URL in caplog.text and "plugin bug" in caplog.text


@pytest.mark.parametrize("providers", [[], [_Provider(claims=False)]])
def test_no_claiming_provider_means_none(monkeypatch, providers):
    _registry(monkeypatch, providers)
    assert MediaDownloadService()._get_protected_provider(URL) is None


def test_the_service_keeps_the_registry_it_imported(monkeypatch):
    """Current behaviour, see defect D-10: rebinding the registry module's list is not seen."""
    imported = _Provider()
    _registry(monkeypatch, [imported])
    monkeypatch.setattr(protected_media_providers, "PROTECTED_MEDIA_PROVIDERS", [_Provider()])

    assert MediaDownloadService()._get_protected_provider(URL) is imported


# ---------------------------------------------------------------- extract / download delegation


def test_extract_video_info_hands_a_claimed_url_to_the_provider(monkeypatch, no_ytdlp):
    provider = _Provider(result={"id": "vid1", "extractor": "mediacms"})
    _registry(monkeypatch, [provider])

    info = MediaDownloadService().extract_video_info(
        URL, media_username="u", media_password="p", user_id=7
    )

    assert info is provider.result
    assert provider.calls[-1] == (
        "extract_info",
        (URL,),
        {"username": "u", "password": "p", "user_id": 7},
    )
    assert provider.calls[0] == ("can_handle", (URL,), {"user_id": 7})


def test_a_provider_error_passes_through_extract_video_info(monkeypatch, no_ytdlp):
    error = UpstreamServiceError("MediaCMS login did not return an auth token")
    _registry(monkeypatch, [_Provider(error=error)])

    with pytest.raises(UpstreamServiceError) as exc:
        MediaDownloadService().extract_video_info(URL, user_id=7)

    assert exc.value is error


def test_extract_video_info_does_not_recheck_the_url(monkeypatch, no_ytdlp):
    """Only ``download_video`` re-validates; the plugin pins its own requests."""
    provider = _Provider()
    _registry(monkeypatch, [provider])

    assert MediaDownloadService().extract_video_info("http://10.0.0.5/view?m=x") is provider.result


def test_download_video_hands_a_claimed_url_to_the_provider(monkeypatch, no_ytdlp, tmp_path):
    stub_public_dns(monkeypatch)
    provider = _Provider(result={"file_path": "f", "filename": "f", "info": {}})
    _registry(monkeypatch, [provider])

    def callback(_p, _m):
        return None

    result = MediaDownloadService().download_video(
        URL,
        str(tmp_path),
        progress_callback=callback,
        media_username="u",
        media_password="p",
        video_quality="720p",
        audio_only=True,
        audio_quality="128",
        user_id=7,
    )

    assert result is provider.result
    assert provider.calls[-1] == (
        "download",
        (URL, str(tmp_path)),
        {"progress_callback": callback, "username": "u", "password": "p", "user_id": 7},
    )


@pytest.mark.parametrize("url", ["http://10.0.0.5/view?m=x", "http://169.254.169.254/view?m=x"])
def test_download_video_rechecks_the_url_before_asking_any_provider(monkeypatch, tmp_path, url):
    provider = _Provider()
    _registry(monkeypatch, [provider])

    with pytest.raises(InvalidInputError) as exc:
        MediaDownloadService().download_video(url, str(tmp_path), user_id=7)

    assert (exc.value.status_code, exc.value.detail) == (
        400,
        "Invalid URL. Please enter a valid HTTP or HTTPS URL.",
    )
    assert provider.calls == []


# ---------------------------------------------------------------- the worker-side hand-off


class _StopError(Exception):
    """Ends the staging run once the hand-off under test has been observed."""


def test_staging_forwards_the_credentials_to_extract_and_download(monkeypatch, tmp_path):
    monkeypatch.setattr(mds.settings, "TEMP_DIR", tmp_path)
    service = MediaDownloadService()
    calls: list[tuple[str, tuple, dict]] = []

    def _extract(url, **kw):
        calls.append(("extract", (url,), kw))
        return {"id": "vid1"}

    def _download(url, output_path, **kw):
        calls.append(("download", (url, output_path), kw))
        raise _StopError

    monkeypatch.setattr(service, "extract_video_info", _extract)
    monkeypatch.setattr(service, "download_video", _download)

    def callback(_p, _m):
        return None

    with pytest.raises(_StopError):
        service._download_and_stage_media(
            url=URL,
            user_id=7,
            file_id=11,
            progress_callback=callback,
            media_username="u",
            media_password="p",
            video_quality="720p",
            audio_only=True,
            audio_quality="128",
        )

    assert calls[0] == (
        "extract",
        (URL,),
        {"media_username": "u", "media_password": "p", "user_id": 7},
    )
    assert calls[1][0] == "download" and calls[1][1][0] == URL
    assert calls[1][2] == {
        "progress_callback": callback,
        "media_username": "u",
        "media_password": "p",
        "video_quality": "720p",
        "audio_only": True,
        "audio_quality": "128",
        "user_id": 7,
    }


def test_staging_without_a_video_id_never_downloads(monkeypatch, tmp_path):
    monkeypatch.setattr(mds.settings, "TEMP_DIR", tmp_path)
    service = MediaDownloadService()
    downloads: list[str] = []
    monkeypatch.setattr(service, "extract_video_info", lambda url, **kw: {"title": "t"})
    monkeypatch.setattr(service, "download_video", lambda url, *a, **kw: downloads.append(url))

    with pytest.raises(InvalidInputError) as exc:
        service._download_and_stage_media(url=URL, user_id=7, file_id=11)

    assert (exc.value.status_code, exc.value.detail) == (400, "Could not extract video ID from URL")
    assert downloads == []


def test_process_media_url_sync_forwards_the_credentials_to_staging(monkeypatch):
    stub_public_dns(monkeypatch)
    service = MediaDownloadService()
    seen: list[dict] = []

    def _stage(**kw):
        seen.append(kw)
        raise _StopError

    monkeypatch.setattr(service, "_download_and_stage_media", _stage)

    def callback(_p, _m):
        return None

    db = MagicMock()
    with pytest.raises(_StopError):
        service.process_media_url_sync(
            url=URL,
            db=db,
            user=User(id=7),
            media_file=MediaFile(id=11),
            progress_callback=callback,
            media_username="u",
            media_password="p",
            video_quality="720p",
            audio_only=True,
            audio_quality="128",
            benchmark_task_id="bench-1",
        )

    assert seen == [
        {
            "url": URL,
            "user_id": 7,
            "file_id": 11,
            "progress_callback": callback,
            "media_username": "u",
            "media_password": "p",
            "video_quality": "720p",
            "audio_only": True,
            "audio_quality": "128",
            "benchmark_task_id": "bench-1",
        }
    ]
