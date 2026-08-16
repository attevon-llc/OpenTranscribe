"""Tests for ``app/tasks/youtube_processing.py`` — YouTube URL/playlist download tasks.

This module owns the two entry points that turn a pasted URL into a downloading
``MediaFile``: ``process_youtube_url_task`` (single video) and
``process_youtube_playlist_task`` (a playlist, which fans out into N calls of the
first). Neither had a test. Three behaviours were read carefully rather than assumed,
because each one is easy to get backwards from the docstrings/comments alone:

1. **``_resolve_download_quality`` is all-or-nothing, not per-field** (L86-L94). The
   guard only skips the DB lookup when ALL THREE of ``video_quality``/``audio_only``/
   ``audio_quality`` are explicit. Passing 2 of 3 still fetches the user's saved DB
   preferences — the third field is filled from the DB, not from
   ``core.constants`` system defaults. Confirmed empirically below rather than assumed
   from the docstring's three-tier description, which doesn't say what happens on a
   *partial* override.

2. **``self.retry()``'s raised exception used to be swallowed by the task's own outer
   ``except Exception`` — fixed.** ``celery.exceptions.Retry`` **is** an ``Exception``
   subclass, and the outer handler around the whole function body used to catch it
   along with everything else, turning "please reschedule me" into an ordinary
   ``{"status": "error", ...}`` return with Celery's tracer never seeing the retry
   request. Fixed by re-raising ``Retry`` explicitly before the generic handler.
   ``test_the_retriable_error_path_actually_retries_up_to_the_cap`` is the regression
   test: it proves the download service is called 4 times (1 initial + 3 retries) and
   the task only reports a permanent failure once the retry cap is genuinely exhausted.

3. **``_dispatch_video_task`` never validates ``media_file.source_url``.** It does
   ``video_url = str(media_file.source_url)`` unconditionally (L609), so a placeholder
   row with ``source_url=None`` dispatches the literal string ``"None"`` as the URL to
   download, and an empty string dispatches ``""``. Both are treated as successful
   dispatch (return ``True``, no error recorded on the row).

Following the characterization-test convention of ``tests/unit/test_recovery_tasks.py``.
"""

from __future__ import annotations

import contextlib
import uuid as uuid_module
from types import SimpleNamespace
from typing import Any

import pytest
from celery.app.task import Task

import app.services.notification_service as notification_service
from app.core.constants import DEFAULT_AUDIO_QUALITY
from app.core.constants import DEFAULT_VIDEO_QUALITY
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.prompt import UserSetting
from app.tasks import youtube_processing
from app.tasks.youtube_processing import _dispatch_video_task
from app.tasks.youtube_processing import _handle_playlist_result
from app.tasks.youtube_processing import _resolve_download_quality
from app.tasks.youtube_processing import process_youtube_url_task


def _make_media_file(
    db_session, user, *, source_url: str | None = "https://youtu.be/abc123", title="Video"
) -> MediaFile:
    mf = MediaFile(
        uuid=uuid_module.uuid4(),
        user_id=user.id,
        filename="video.mp4",
        title=title,
        storage_path=f"user_{user.id}/{uuid_module.uuid4()}/video.mp4",
        file_size=1024,
        content_type="video/mp4",
        status=FileStatus.QUEUED,
        source_url=source_url,
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    return mf


# --------------------------------------------------------------------------------------
# 1. _resolve_download_quality — the all-or-nothing DB-fallback guard
# --------------------------------------------------------------------------------------


def test_all_three_explicit_returns_them_unchanged_without_touching_the_db(db_session, normal_user):
    """The one case allowed to skip the DB lookup entirely."""

    class _ExplodingSession:
        def query(self, *_a, **_kw):
            raise AssertionError("all three explicit must not query the DB at all")

    result = _resolve_download_quality(_ExplodingSession(), normal_user.id, "1080p", True, "192")

    assert result == ("1080p", True, "192")


def test_all_three_none_falls_back_to_system_defaults_with_no_saved_prefs(db_session, normal_user):
    result = _resolve_download_quality(db_session, normal_user.id, None, None, None)

    assert result == (DEFAULT_VIDEO_QUALITY, False, DEFAULT_AUDIO_QUALITY)


def test_all_three_none_uses_saved_db_preferences_when_present(db_session, normal_user):
    db_session.add_all(
        [
            UserSetting(
                user_id=normal_user.id, setting_key="download_video_quality", setting_value="720p"
            ),
            UserSetting(
                user_id=normal_user.id, setting_key="download_audio_only", setting_value="true"
            ),
            UserSetting(
                user_id=normal_user.id, setting_key="download_audio_quality", setting_value="128"
            ),
        ]
    )
    db_session.commit()

    result = _resolve_download_quality(db_session, normal_user.id, None, None, None)

    assert result == ("720p", True, "128")


def test_partial_override_still_fetches_db_prefs_for_the_missing_field(db_session, normal_user):
    """CHARACTERIZATION — the all-or-nothing check, empirically confirmed.

    Two of three params are explicit; the third (``audio_only``) is left ``None``.
    The guard at L86 requires ALL THREE to be non-None to skip the DB, so this call
    still issues a DB lookup — and the two explicit values are NOT overwritten by
    whatever the DB holds for them. Only the missing field is filled from the DB.
    """
    db_session.add_all(
        [
            UserSetting(
                user_id=normal_user.id, setting_key="download_video_quality", setting_value="480p"
            ),
            UserSetting(
                user_id=normal_user.id, setting_key="download_audio_only", setting_value="true"
            ),
            UserSetting(
                user_id=normal_user.id, setting_key="download_audio_quality", setting_value="64"
            ),
        ]
    )
    db_session.commit()

    result = _resolve_download_quality(
        db_session,
        normal_user.id,
        "1080p",
        None,
        "192",  # audio_only left None
    )

    # video_quality and audio_quality are the caller's explicit values (NOT the DB's
    # "480p"/"64" — those would only apply to a field actually left None), and
    # audio_only came from the DB despite two of the three params being explicit.
    assert result == ("1080p", True, "192")


def test_partial_override_with_no_saved_prefs_fills_the_gap_from_system_defaults(
    db_session, normal_user
):
    result = _resolve_download_quality(db_session, normal_user.id, "1080p", None, "192")

    assert result == ("1080p", False, "192")


# --------------------------------------------------------------------------------------
# 2. process_youtube_url_task — manual backoff math, and the retry that never retries
# --------------------------------------------------------------------------------------


class _FakeMediaDownloadService:
    def __init__(self, exc: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._exc = exc

    def process_media_url_sync(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        media_file = kwargs["media_file"]
        return media_file


@pytest.fixture
def yt_url_seams(monkeypatch, db_session):
    """Stub every seam ``process_youtube_url_task`` touches besides the DB.

    ``session_scope`` is replaced with something that yields the test's savepointed
    session (twice — the task opens it twice), so writes are visible to assertions
    without a real commit/close cycle. Notifications, the pipeline dispatch, and the
    pre-download jitter sleep are all no-op'd; only ``MediaDownloadService`` is left
    for each test to configure.
    """
    monkeypatch.setattr(youtube_processing.settings, "YOUTUBE_PRE_DOWNLOAD_JITTER_ENABLED", False)
    monkeypatch.setattr(youtube_processing.settings, "YOUTUBE_AUTO_RETRY_ENABLED", True)

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(youtube_processing, "session_scope", _scope)

    notifications: list[dict[str, Any]] = []

    def _notify(**kwargs):
        notifications.append(kwargs)
        return True

    monkeypatch.setattr(youtube_processing, "send_youtube_notification_via_redis", _notify)
    monkeypatch.setattr(youtube_processing, "dispatch_transcription_pipeline", lambda **_kw: None)
    monkeypatch.setattr(youtube_processing, "send_ws_event", lambda *_a, **_kw: None)

    return SimpleNamespace(notifications=notifications)


def test_the_manual_countdown_formula_is_what_self_retry_actually_receives(
    monkeypatch, db_session, normal_user, yt_url_seams
):
    """Pins ``countdown = 30 * (2**retries)`` (L417) as the value passed to ``self.retry``.

    The decorator also sets ``retry_backoff=True``/``retry_backoff_max=600``, but
    passing ``countdown=`` explicitly to ``self.retry`` overrides that automatic
    backoff entirely — Celery only computes its own backoff when ``countdown`` is
    omitted. ``self.retry`` itself is stubbed (real retry scheduling is exercised in
    the next test) purely to observe what it was called with.
    """
    media_file = _make_media_file(db_session, normal_user)
    fake_service = _FakeMediaDownloadService(exc=RuntimeError("Connection timeout"))
    monkeypatch.setattr(youtube_processing, "MediaDownloadService", lambda: fake_service)

    retry_calls: list[dict[str, Any]] = []

    def _fake_retry(self, countdown=None, exc=None, **_kw):
        retry_calls.append({"countdown": countdown, "retries": self.request.retries})
        raise exc

    monkeypatch.setattr(Task, "retry", _fake_retry)

    for retries, expected_countdown in [(0, 30), (1, 60), (2, 120)]:
        retry_calls.clear()
        fake_service.calls.clear()
        process_youtube_url_task.apply(
            args=("https://youtu.be/abc123", normal_user.id, str(media_file.uuid)),
            retries=retries,
        )
        assert retry_calls == [{"countdown": expected_countdown, "retries": retries}]

    assert process_youtube_url_task.max_retries == 3, (
        "the retriable branch's cap, read from the task decorator"
    )


def test_the_retriable_error_path_actually_retries_up_to_the_cap(
    monkeypatch, db_session, normal_user, yt_url_seams
):
    """Regression test for a fixed defect: ``self.retry()``'s exception used to be
    swallowed by this task's own outer ``except Exception`` (L466-472 before the fix),
    so ``celery.exceptions.Retry`` — an ``Exception`` subclass — never escaped this
    function and Celery's tracer never saw the retry request. The fix re-raises
    ``Retry`` explicitly before the generic handler.

    Under eager execution (this test's ``.apply()``), Celery's own retry loop now
    genuinely re-invokes the task on each ``Retry`` it sees: 1 initial attempt + 3
    retries (``max_retries=3``) = 4 total calls to the download service, ending on the
    real terminal path with the ORIGINAL error message — not the
    ``"Retry in Ns: ..."`` string that leaking through the outer handler used to
    produce.
    """
    media_file = _make_media_file(db_session, normal_user)
    fake_service = _FakeMediaDownloadService(exc=RuntimeError("Connection timeout"))
    monkeypatch.setattr(youtube_processing, "MediaDownloadService", lambda: fake_service)

    result = process_youtube_url_task.apply(
        args=("https://youtu.be/abc123", normal_user.id, str(media_file.uuid)),
    ).get()

    assert len(fake_service.calls) == 4, "expected 1 initial attempt + 3 retries"
    assert result == {
        "status": "error",
        "message": "Connection timeout",
        "file_id": media_file.id,
    }


def test_a_terminal_attempt_at_the_retry_cap_writes_the_original_error_and_stops(
    monkeypatch, db_session, normal_user, yt_url_seams
):
    """At ``retries == max_retries`` the retriable branch's own guard (L413) is False.

    This is the real terminal path (distinct from the swallowed-retry defect above):
    no call to ``self.retry`` at all, the ORIGINAL error message is preserved, and the
    row is left in ``ERROR`` with that message recorded.
    """
    media_file = _make_media_file(db_session, normal_user)
    fake_service = _FakeMediaDownloadService(exc=RuntimeError("Connection timeout"))
    monkeypatch.setattr(youtube_processing, "MediaDownloadService", lambda: fake_service)

    retry_calls: list[Any] = []

    def _retry_must_not_be_called(self, **kw):
        retry_calls.append(kw)
        raise AssertionError("self.retry must not be called at retries == max_retries")

    monkeypatch.setattr(Task, "retry", _retry_must_not_be_called)

    result = process_youtube_url_task.apply(
        args=("https://youtu.be/abc123", normal_user.id, str(media_file.uuid)),
        retries=3,
    ).get()

    assert retry_calls == [], "at retries == max_retries, self.retry must never be called"
    assert result == {
        "status": "error",
        "message": "Connection timeout",
        "file_id": media_file.id,
    }
    db_session.refresh(media_file)
    assert media_file.status == FileStatus.ERROR
    assert media_file.last_error_message == "Connection timeout"


# --------------------------------------------------------------------------------------
# 3. _dispatch_video_task / _handle_playlist_result — the unvalidated source_url
# --------------------------------------------------------------------------------------


@pytest.fixture
def dispatch_recorder(monkeypatch):
    calls: list[dict[str, Any]] = []

    def _record(args=None, kwargs=None, countdown=None, **_kw):
        calls.append({"args": args, "kwargs": kwargs, "countdown": countdown})
        return SimpleNamespace(id="fake-task-id")

    monkeypatch.setattr(process_youtube_url_task, "apply_async", _record)
    return calls


def test_a_none_source_url_is_stringified_and_dispatched_as_the_literal_word_none(
    db_session, normal_user, dispatch_recorder
):
    """CHARACTERIZATION — DEFECT: youtube_processing.py L609.

    ``video_url = str(media_file.source_url)`` has no guard for ``None``. The
    downstream Celery task therefore receives the four-character string ``"None"``
    as a URL to download, not an error.
    """
    media_file = _make_media_file(db_session, normal_user, source_url=None)

    dispatched = _dispatch_video_task(media_file, normal_user.id, db_session, countdown=5)

    assert dispatched is True
    assert dispatch_recorder[0]["args"][0] == "None"
    assert dispatch_recorder[0]["countdown"] == 5
    assert media_file.last_error_message is None, "no error was ever recorded for the bad URL"


def test_an_empty_string_source_url_is_dispatched_unchanged(
    db_session, normal_user, dispatch_recorder
):
    media_file = _make_media_file(db_session, normal_user, source_url="")

    dispatched = _dispatch_video_task(media_file, normal_user.id, db_session, countdown=0)

    assert dispatched is True
    assert dispatch_recorder[0]["args"][0] == ""


def test_handle_playlist_result_dispatches_every_video_with_no_source_url_filter(
    monkeypatch, db_session, normal_user, dispatch_recorder
):
    """The fan-out loop (L827-L845) applies no filter before calling ``_dispatch_video_task``.

    A placeholder row with no URL is dispatched exactly like a normal one and counted
    in ``dispatched_count`` / the "queued for download" message — confirming the gap
    identified directly on ``_dispatch_video_task`` also reaches the playlist path
    that is its only production caller.
    """
    monkeypatch.setattr(youtube_processing, "send_ws_event", lambda *_a, **_kw: None)
    monkeypatch.setattr(notification_service, "send_ws_event", lambda *_a, **_kw: None)

    good = _make_media_file(db_session, normal_user, source_url="https://youtu.be/good")
    broken = _make_media_file(db_session, normal_user, source_url=None)

    result = _handle_playlist_result(
        {
            "media_files": [good, broken],
            "playlist_info": {"playlist_title": "Test Playlist", "playlist_id": "PL123"},
            "created_count": 2,
            "skipped_count": 0,
            "total_videos": 2,
        },
        normal_user.id,
        db_session,
    )

    assert result["status"] == "success"
    assert result["created_count"] == 2
    dispatched_urls = [c["args"][0] for c in dispatch_recorder]
    assert dispatched_urls == ["https://youtu.be/good", "None"]
