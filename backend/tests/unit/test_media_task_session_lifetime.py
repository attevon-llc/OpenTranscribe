# mypy: disable-error-code="arg-type"
# This suite passes structural stand-ins (a fake session, a recording Path) to
# signatures that declare Session/Path. Declared once here rather than as a cast at
# every call site: a cast buries the thing being asserted, and widening a production
# signature to suit a test is worse. Same convention as
# tests/unit/test_proxy_identity_consistency.py.
"""Media download/transcode and expired-file cleanup hold no DB transaction across MinIO, ffmpeg or OpenSearch work.

See ``tests/unit/_session_lifetime_trackers.py`` for the depth-tracking scope stand-in
and the measured incident behind this family of tests.
"""

from app.tasks import media_download as mdl
from tests.unit._session_lifetime_trackers import ScopeTracker
from tests.unit._session_lifetime_trackers import leak
from tests.unit._session_lifetime_trackers import make_transcribed_file


# --------------------------------------------------------------------------- #
# 2. download.prepare_media — MinIO download + full ffmpeg transcode
#
#    The residual this test used to document is gone:
#    ``VideoProcessingService.extract_audio`` / ``process_video_with_subtitles``
#    no longer take a ``Session``. They open their own short sessions for the two
#    reads they need (the filename, and the transcript for the SRT), so the task
#    holds nothing across the transcode.
# --------------------------------------------------------------------------- #
def test_prepare_media_download_transcodes_outside_the_session(
    db_session, normal_user, monkeypatch
):
    tracker = ScopeTracker(db_session)
    media_file, _ = make_transcribed_file(db_session, normal_user, speakers=1)

    monkeypatch.setattr(mdl, "session_scope", tracker.scope)
    monkeypatch.setattr(mdl, "publish_download_event", lambda *a, **kw: None)
    monkeypatch.setattr(mdl, "release_download_prep_guard", lambda *a, **kw: None)
    monkeypatch.setattr(mdl, "MinIOService", lambda *a, **kw: object())

    class _FakeService:
        def __init__(self, minio):
            pass

        def extract_audio(self, *, file_id, original_object_name, audio_format):
            # A MinIO download plus a full ffmpeg transcode: minutes.
            tracker.observe("extract_audio")
            return "cache/key.mp3", "mp3", "audio/mpeg"

        def presigned_download_url(self, cache_key, download_filename, content_type):
            tracker.observe("presign")
            return "http://minio.invalid/key.mp3"

    monkeypatch.setattr(mdl, "VideoProcessingService", _FakeService)

    result = mdl.prepare_media_download_task.apply(
        args=[media_file.id, normal_user.id, "audio_mp3"]
    ).get()

    assert result["status"] == "success", result
    assert tracker.seen["extract_audio"] == 0, leak(tracker, "extract_audio")
    assert tracker.seen["presign"] == 0, leak(tracker, "presign")
    assert tracker.opened == 1, f"expected exactly the read scope, got {tracker.opened}"
    assert tracker.max_depth == 1, "session scopes must not nest"
    assert tracker.depth == 0


def test_video_service_reads_use_their_own_short_sessions(db_session, normal_user, monkeypatch):
    """``VideoProcessingService`` opens (and closes) its own scope per read.

    Without this, "the task holds no session" above would be satisfied by the
    service holding one instead — the leak moved, not removed.
    """
    from app.services import video_processing_service as vps

    tracker = ScopeTracker(db_session)
    media_file, _ = make_transcribed_file(db_session, normal_user, speakers=1)

    monkeypatch.setattr(vps, "session_scope", tracker.scope)
    monkeypatch.setattr("app.services.video_subtitle_burn.session_scope", tracker.scope)
    filename = vps.VideoProcessingService._media_filename(int(media_file.id))

    assert filename == "meeting.mp4"
    assert tracker.opened == 1
    assert tracker.depth == 0, "the filename read left a scope open"

    written: dict = {}

    class _FakeSubtitles:
        @staticmethod
        def generate_srt_content(db, file_id, include_speakers, redaction_cfg=None):
            written["depth_during_render"] = tracker.depth
            return "1\n00:00:00,000 --> 00:00:04,000\nhello there\n"

    monkeypatch.setattr("app.services.video_subtitle_burn.SubtitleService", _FakeSubtitles)

    class _RecordingPath:
        """Reports the open-scope depth at the moment the SRT is written."""

        def write_text(self, content, encoding=None):
            written["depth_during_write"] = tracker.depth
            written["content"] = content

    service = vps.VideoProcessingService.__new__(vps.VideoProcessingService)
    from app.core.redaction_config import EffectiveRedactionConfig

    service._generate_subtitle_file(
        int(media_file.id), _RecordingPath(), True, EffectiveRedactionConfig(enabled=False)
    )

    # The transcript read needs a session; the FILE WRITE that follows must not
    # still be inside it, and neither may the ffmpeg run after that.
    assert written["depth_during_render"] == 1
    assert written["depth_during_write"] == 0, leak(tracker, "srt_write")
    assert "hello there" in written["content"]
    assert tracker.depth == 0
    assert tracker.opened == 2
    assert tracker.max_depth == 1


# --------------------------------------------------------------------------- #
# 9. cleanup_expired_files — MinIO + OpenSearch deletes, per expired file
# --------------------------------------------------------------------------- #
def test_cleanup_expired_files_purges_each_file_in_its_own_session(
    db_session, normal_user, monkeypatch
):
    """One transaction per file, not one across the whole retention pass."""
    import datetime as dt

    from app.models.media import FileStatus
    from app.services import file_cleanup_service as fcs
    from app.services import system_settings_service as sss
    from app.tasks import cleanup as cl

    tracker = ScopeTracker(db_session)
    monkeypatch.setattr(cl, "session_scope", tracker.scope)
    monkeypatch.setattr(
        sss,
        "get_retention_config",
        lambda db: {
            "retention_enabled": True,
            "retention_days": 1,
            "delete_error_files": False,
            "timezone": "UTC",
            "run_time": "02:00",
            "last_run": None,
        },
    )
    monkeypatch.setattr(sss, "set_setting", lambda db, key, value, desc=None: None)

    old = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
    files = []
    for _ in range(2):
        mf, _speakers = make_transcribed_file(db_session, normal_user, speakers=1)
        mf.status = FileStatus.COMPLETED.value
        mf.completed_at = old
        mf.upload_time = old
        files.append(mf)
    db_session.flush()

    purged: list[int] = []

    def _purge(db, media_file, **_kw):
        # MinIO object deletes + four OpenSearch deletes, per file.
        tracker.observe("auto_delete_media_file")
        purged.append(int(media_file.id))
        db.delete(media_file)
        return {"deleted": True, "file_uuid": str(media_file.uuid), "error": None}

    monkeypatch.setattr(fcs, "auto_delete_media_file", _purge)

    # The dev database this suite runs against holds unrelated aged files, and
    # the point of the test is the SESSION SHAPE, not the candidate query (that
    # has its own test below). Narrow the selection to the two rows this test
    # created — the real selector still runs, inside the real read scope.
    ours = {int(f.id) for f in files}
    real_select = cl._select_expired_files
    monkeypatch.setattr(
        cl,
        "_select_expired_files",
        lambda *a, **kw: [item for item in real_select(*a, **kw) if item[0] in ours],
    )

    result = cl.cleanup_expired_files(force=True)

    assert result == {"status": "completed", "deleted": 2, "failed": 0}, result
    assert sorted(purged) == sorted(int(f.id) for f in files)

    # The purge needs a session (it commits the row delete), so depth 1 is
    # correct here. What must NOT be shared is the scope: each file gets its
    # own, which shows up as a DIFFERENT scope count per call.
    depths = [d for label, d in tracker.observations if label == "auto_delete_media_file"]
    assert depths == [1, 1], tracker.observations
    counts = tracker.opened_at("auto_delete_media_file")
    assert len(set(counts)) == len(counts), (
        "both files were purged inside the SAME session — one transaction is "
        f"spanning the whole retention pass: {tracker.timeline}"
    )
    assert tracker.opened >= 4, f"expected read + 2 purges + write, got {tracker.opened}"
    assert tracker.max_depth == 1, "session scopes must not nest"


def test_cleanup_expired_selection_returns_plain_data(db_session, normal_user, monkeypatch):
    import datetime as dt

    from app.models.media import FileStatus
    from app.tasks import cleanup as cl

    media_file, _ = make_transcribed_file(db_session, normal_user, speakers=1)
    media_file.status = FileStatus.COMPLETED.value
    media_file.completed_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
    db_session.flush()

    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    expired = cl._select_expired_files(
        db_session,
        {"delete_error_files": False},
        cutoff,
        cutoff,
        lambda org_id: None,
    )

    assert (media_file.id, str(media_file.uuid)) in expired
    for file_id, file_uuid in expired:
        assert isinstance(file_id, int)
        assert isinstance(file_uuid, str)
