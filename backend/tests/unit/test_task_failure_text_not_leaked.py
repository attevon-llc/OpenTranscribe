"""User-visible failure text from the download and clustering tasks is a fixed sentence (#1365).

Each case makes the work raise an exception carrying a sentinel (a path and a host) and asserts
that nothing pushed to the user, nor returned as the task's message, contains it. The detail
belongs in the log, which these tests do not touch.
"""

from __future__ import annotations

import contextlib
import uuid as uuid_module
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.tasks import media_download
from app.tasks import media_playlist_processing as playlists
from app.tasks import speaker_clustering

SECRET = "/secret/path/cookies.txt on db-internal.local"


def _assert_clean(*texts: object) -> None:
    for text in texts:
        assert "/secret/path" not in str(text)
        assert "db-internal" not in str(text)


@contextlib.contextmanager
def _fake_scope():
    class _Query:
        def filter(self, *_a, **_k):
            return self

        def first(self):
            return (uuid_module.uuid4(), "clip.mp4", "user_7/clip.mp4")

    yield SimpleNamespace(query=lambda *_a, **_k: _Query())


# -- media_playlist_processing -------------------------------------------------------------


@pytest.fixture
def playlist_seams(monkeypatch):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(playlists, "session_scope", _fake_scope)
    monkeypatch.setattr(
        playlists,
        "_send_playlist_notification",
        lambda _u, status, msg, *_a: sent.append((status, msg)),
    )
    monkeypatch.setattr(
        "app.legal.processing_gate.check_processing_allowed", lambda *_a, **_k: None
    )
    return sent


def test_a_playlist_extraction_failure_tells_the_user_a_fixed_sentence(monkeypatch, playlist_seams):
    class _Boom:
        def process_youtube_playlist_sync(self, **_kw):
            raise RuntimeError(SECRET)

    monkeypatch.setattr(playlists, "MediaDownloadService", _Boom)

    result = playlists._process_playlist_with_db("https://example.com/list", 7)

    errors = [m for s, m in playlist_seams if s == "error"]
    assert len(errors) == 1
    _assert_clean(*errors, result["message"])


def test_an_unexpected_playlist_error_tells_the_user_a_fixed_sentence(monkeypatch, playlist_seams):
    def _boom(*_a, **_kw):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(playlists, "_process_playlist_with_db", _boom)

    result = playlists.process_youtube_playlist_task.apply(
        args=("https://example.com/list", 7)
    ).get()

    errors = [m for s, m in playlist_seams if s == "error"]
    assert len(errors) == 1
    _assert_clean(*errors, result["message"])


def test_a_failed_video_dispatch_records_a_fixed_sentence_on_the_file(monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(playlists, "_claim_playlist_dispatch", _boom)
    monkeypatch.setattr(playlists, "update_media_file_status", lambda *_a, **_k: None)
    db = MagicMock(spec=Session)
    media_file = MediaFile(
        id=3, uuid=uuid_module.uuid4(), title="t", source_url="https://x/y", filename="t.mp4"
    )

    assert playlists._dispatch_playlist_video_task(media_file, 7, db) is False

    assert media_file.last_error_message
    _assert_clean(media_file.last_error_message)


# -- media_download ------------------------------------------------------------------------


@pytest.fixture
def download_events(monkeypatch):
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        media_download, "publish_download_event", lambda _u, **kw: events.append(kw)
    )
    monkeypatch.setattr(media_download, "release_download_prep_guard", lambda *_a, **_k: None)
    return events


@pytest.fixture
def download_file_lookup(monkeypatch):
    monkeypatch.setattr(media_download, "session_scope", _fake_scope)
    monkeypatch.setattr(media_download, "exclude_unavailable", lambda q, **_k: q)
    monkeypatch.setattr(media_download, "is_review_admin", lambda *_a: False)


@pytest.fixture
def failing_video_service(monkeypatch):
    def _boom(_minio):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(media_download, "MinIOService", lambda: None)
    monkeypatch.setattr(media_download, "VideoProcessingService", _boom)


@pytest.mark.usefixtures("download_file_lookup", "failing_video_service")
def test_a_failed_download_preparation_publishes_and_returns_no_exception_text(download_events):
    events = download_events

    result = media_download.prepare_media_download_task.apply(args=(1, 7, "audio_mp3")).get()

    assert [e for e in events if e.get("status") == "error"]
    _assert_clean(result["message"], *[e.get("message") for e in events])


def test_a_failed_bulk_export_returns_no_exception_text(monkeypatch):
    monkeypatch.setattr(media_download, "session_scope", _boom_scope)
    monkeypatch.setattr("app.services.download_events.publish_bulk_event", lambda *_a, **_k: None)

    result = media_download.prepare_bulk_subtitles_task.apply(
        args=([[1, "a"]], "srt", False, "job-1"), kwargs={"user_id": 7}
    ).get()

    assert result["status"] == "error"
    _assert_clean(result["message"])


@contextlib.contextmanager
def _boom_scope():
    raise RuntimeError(SECRET)
    yield  # pragma: no cover


# -- speaker_clustering --------------------------------------------------------------------


@pytest.fixture
def clustering_infra(monkeypatch):
    @contextlib.contextmanager
    def _lock(*_a, **_k):
        yield True

    monkeypatch.setattr(speaker_clustering, "session_scope", _fake_scope)
    monkeypatch.setattr("app.utils.task_lock.task_lock_manager.acquire_lock", _lock)
    monkeypatch.setattr("app.tasks.migration_pipeline.cleanup_gpu_memory", lambda: None)


class _Tracker:
    total = 1
    failed: list[str] = []

    def __init__(self, *_a, **_k) -> None:
        pass

    def start(self, **_k) -> None:
        pass

    def fail(self, message: str = "") -> None:
        _Tracker.failed.append(message)


class _FailingService:
    def __init__(self, _db) -> None:
        pass

    def batch_recluster(self, *_a, **_k):
        raise RuntimeError(SECRET)


@pytest.fixture
def clustering_outputs(monkeypatch):
    sent: list[dict[str, Any]] = []
    _Tracker.failed = []
    monkeypatch.setattr(
        speaker_clustering, "send_ws_event", lambda _u, _t, payload, **_k: sent.append(payload)
    )
    monkeypatch.setattr("app.services.progress_tracker.ProgressTracker", _Tracker)
    monkeypatch.setattr(
        "app.services.speaker_clustering_service.SpeakerClusteringService", _FailingService
    )
    return sent


@pytest.mark.usefixtures("clustering_infra")
def test_a_clustering_failure_notifies_the_user_without_the_exception_text(clustering_outputs):
    result = speaker_clustering.recluster_all_speakers.apply(args=(7,), retries=1)

    assert result.failed()
    errors = [p for p in clustering_outputs if p.get("status") == "error"]
    assert len(errors) == 1
    assert len(_Tracker.failed) == 1
    _assert_clean(errors[0]["error"], _Tracker.failed[0])
