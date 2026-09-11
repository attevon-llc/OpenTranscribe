"""The async export/burn-in workers must re-check quarantine at RUN TIME (#818).

The race under test, precisely: a file is quarantined AFTER an export or a burn-in
render has already been dispatched (its ``file_specs``/``file_id`` already resolved
by the prepare endpoint's permission filter) and BEFORE the worker actually runs.
A dispatch-time-only authorization cannot see a takedown that lands in that gap, so
both ``prepare_bulk_subtitles_task`` (the ZIP) and ``prepare_media_download_task``
(burn-in video + the three audio-extract modes) must re-resolve the quarantine
predicate against Postgres when they execute, not trust what the endpoint already
checked at dispatch time.

Harness shape copied from ``tests/redaction/test_export_redaction_paths.py``'s
``TestBulkExportWorker`` — patch ``session_scope`` to the savepointed test session,
patch ``VideoProcessingService`` with a fake, and run the task via its real Celery
entry point.
"""

from __future__ import annotations

import contextlib
import io
import uuid
import zipfile

import pytest

from app.models.media import MediaFile
from app.models.media import TranscriptSegment

pytestmark = pytest.mark.unit


# Short enough (<42 chars) to survive SRT's line-wrap unbroken — a wrapped marker
# would split across two cue lines and defeat a plain substring check.
ALPHA_TEXT = "ALPHAMARK9f21c stays visible"
BETA_TEXT = "BETAMARK9f21c must never leak"


def _make_file(db_session, owner, *, text: str) -> MediaFile:
    media_file = MediaFile(
        uuid=str(uuid.uuid4()),
        filename=f"export818-{uuid.uuid4().hex[:8]}.mp4",
        storage_path="media/test/export818.mp4",
        content_type="video/mp4",
        file_size=1024,
        user_id=owner.id,
        status="completed",
        redaction_status="done",
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    db_session.add(
        TranscriptSegment(
            uuid=str(uuid.uuid4()),
            media_file_id=media_file.id,
            start_time=0.0,
            end_time=3.0,
            text=text,
        )
    )
    db_session.commit()
    return media_file


def _quarantine(db_session, media_file: MediaFile) -> None:
    """Mark a file quarantined without the full admin action — the read side is
    under test here, not `takedown_service.quarantine_file` itself."""
    media_file.is_quarantined = True
    db_session.commit()


def _zip_texts(zip_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return "\n".join(zf.read(name).decode("utf-8") for name in zf.namelist())


class _NoopRedis:
    def setex(self, *args, **kwargs):
        return True


class TestBulkExportQuarantineRace:
    """``prepare_bulk_subtitles_task`` — the batch ZIP export."""

    def _run(self, db_session, monkeypatch, *, file_specs, user_id):
        from app.tasks import media_download as mdl

        @contextlib.contextmanager
        def _scope():
            yield db_session

        uploaded: dict[str, bytes] = {}
        events: list[dict] = []

        class _FakeService:
            cache_bucket = "processed"

            def __init__(self, minio):
                self.minio_service = self

            def upload_bytes(self, bucket, key, data, content_type):
                uploaded["zip"] = data

        monkeypatch.setattr(mdl, "session_scope", _scope)
        monkeypatch.setattr(mdl, "VideoProcessingService", _FakeService)
        monkeypatch.setattr(
            "app.services.download_events.publish_bulk_event",
            lambda job_id, **kw: events.append(kw),
        )
        monkeypatch.setattr(
            "app.services.minio_service.get_presigned_download_url",
            lambda *a, **kw: "http://minio.invalid/x.zip",
        )
        monkeypatch.setattr("app.core.redis.get_redis", lambda: _NoopRedis())

        result = mdl.prepare_bulk_subtitles_task.run(
            file_specs=file_specs,
            subtitle_format="srt",
            include_speakers=True,
            job_id=f"job818-{uuid.uuid4().hex[:8]}",
            user_id=int(user_id),
        )
        return result, uploaded.get("zip"), events

    def test_a_file_taken_down_after_dispatch_is_dropped_from_the_zip(
        self, db_session, normal_user, monkeypatch
    ):
        clean = _make_file(db_session, normal_user, text=ALPHA_TEXT)
        soon_quarantined = _make_file(db_session, normal_user, text=BETA_TEXT)
        # Specs are built here — exactly as the prepare endpoint would have resolved
        # them at dispatch time, BEFORE the takedown below.
        file_specs = [[int(clean.id), "clean"], [int(soon_quarantined.id), "quarantined"]]

        # THE RACE: the takedown lands after dispatch, before the worker runs.
        _quarantine(db_session, soon_quarantined)

        result, zip_bytes, _events = self._run(
            db_session, monkeypatch, file_specs=file_specs, user_id=normal_user.id
        )

        assert result["status"] == "success", result
        assert result["exported"] == 1, result
        assert result["skipped"] == 1, result
        content = _zip_texts(zip_bytes)
        assert ALPHA_TEXT in content, content
        assert BETA_TEXT not in content, (
            f"quarantined file's text leaked into the export:\n{content}"
        )

    def test_the_rest_of_the_batch_still_exports(self, db_session, normal_user, monkeypatch):
        """CONTROL for the test above: the archive isn't just empty."""
        clean = _make_file(db_session, normal_user, text=ALPHA_TEXT)
        soon_quarantined = _make_file(db_session, normal_user, text=BETA_TEXT)
        file_specs = [[int(clean.id), "clean"], [int(soon_quarantined.id), "quarantined"]]

        _quarantine(db_session, soon_quarantined)

        result, zip_bytes, _events = self._run(
            db_session, monkeypatch, file_specs=file_specs, user_id=normal_user.id
        )

        assert result["status"] == "success", result
        content = _zip_texts(zip_bytes)
        assert ALPHA_TEXT in content, "the clean file's own text must still be exported"

    def test_a_batch_of_only_taken_down_files_reports_no_export(
        self, db_session, normal_user, monkeypatch
    ):
        only_file = _make_file(db_session, normal_user, text=BETA_TEXT)
        file_specs = [[int(only_file.id), "quarantined"]]

        _quarantine(db_session, only_file)

        result, zip_bytes, events = self._run(
            db_session, monkeypatch, file_specs=file_specs, user_id=normal_user.id
        )

        assert result["status"] == "error", result
        assert result["skipped"] == 1, result
        assert zip_bytes is None, "nothing should have been uploaded"
        error_events = [e for e in events if e.get("status") == "error"]
        assert error_events, events
        assert error_events[-1]["message"] == "No files could be exported."

    def test_an_admin_export_still_includes_a_quarantined_file(
        self, db_session, admin_user, monkeypatch
    ):
        clean = _make_file(db_session, admin_user, text=ALPHA_TEXT)
        quarantined = _make_file(db_session, admin_user, text=BETA_TEXT)
        file_specs = [[int(clean.id), "clean"], [int(quarantined.id), "quarantined"]]

        _quarantine(db_session, quarantined)

        result, zip_bytes, _events = self._run(
            db_session, monkeypatch, file_specs=file_specs, user_id=admin_user.id
        )

        assert result["status"] == "success", result
        assert result["exported"] == 2, result
        content = _zip_texts(zip_bytes)
        assert ALPHA_TEXT in content and BETA_TEXT in content, content


class TestBurnInAndAudioExtractQuarantineRace:
    """``prepare_media_download_task`` — burn-in video and the three audio modes."""

    def _run(self, db_session, monkeypatch, *, file_id, user_id, mode, service_cls):
        from app.tasks import media_download as mdl

        @contextlib.contextmanager
        def _scope():
            yield db_session

        monkeypatch.setattr(mdl, "session_scope", _scope)
        monkeypatch.setattr(mdl, "MinIOService", lambda *a, **kw: object())
        monkeypatch.setattr(mdl, "publish_download_event", lambda *a, **kw: None)
        monkeypatch.setattr(mdl, "release_download_prep_guard", lambda *a, **kw: None)
        monkeypatch.setattr(mdl, "VideoProcessingService", service_cls)

        return mdl.prepare_media_download_task.apply(args=[file_id, user_id, mode]).get()

    def test_a_burn_in_dispatched_before_the_takedown_is_refused(
        self, db_session, normal_user, monkeypatch
    ):
        media_file = _make_file(db_session, normal_user, text=ALPHA_TEXT)
        _quarantine(db_session, media_file)

        class _ExplodingService:
            def __init__(self, minio):
                pytest.fail("a render was started for a taken-down file")

        result = self._run(
            db_session,
            monkeypatch,
            file_id=int(media_file.id),
            user_id=int(normal_user.id),
            mode="video_subtitles",
            service_cls=_ExplodingService,
        )

        assert result["status"] == "error", result

    def test_an_audio_extract_is_refused_too(self, db_session, normal_user, monkeypatch):
        media_file = _make_file(db_session, normal_user, text=ALPHA_TEXT)
        _quarantine(db_session, media_file)

        class _ExplodingService:
            def __init__(self, minio):
                pytest.fail("an extract was started for a taken-down file")

        result = self._run(
            db_session,
            monkeypatch,
            file_id=int(media_file.id),
            user_id=int(normal_user.id),
            mode="audio_mp3",
            service_cls=_ExplodingService,
        )

        assert result["status"] == "error", result

    def test_a_burn_in_for_a_visible_file_still_runs(self, db_session, normal_user, monkeypatch):
        """CONTROL: a file that was never quarantined must still render."""
        media_file = _make_file(db_session, normal_user, text=ALPHA_TEXT)

        class _WorkingService:
            def __init__(self, minio):
                pass

            def process_video_with_subtitles(self, **kwargs):
                return "cache/video818.mp4"

            def presigned_download_url(self, cache_key, download_filename, content_type):
                return "http://minio.invalid/video818.mp4"

        result = self._run(
            db_session,
            monkeypatch,
            file_id=int(media_file.id),
            user_id=int(normal_user.id),
            mode="video_subtitles",
            service_cls=_WorkingService,
        )

        assert result["status"] == "success", result

    def test_an_admin_burn_in_of_a_quarantined_file_still_runs(
        self, db_session, admin_user, monkeypatch
    ):
        media_file = _make_file(db_session, admin_user, text=ALPHA_TEXT)
        _quarantine(db_session, media_file)

        class _WorkingService:
            def __init__(self, minio):
                pass

            def process_video_with_subtitles(self, **kwargs):
                return "cache/video818-admin.mp4"

            def presigned_download_url(self, cache_key, download_filename, content_type):
                return "http://minio.invalid/video818-admin.mp4"

        result = self._run(
            db_session,
            monkeypatch,
            file_id=int(media_file.id),
            user_id=int(admin_user.id),
            mode="video_subtitles",
            service_cls=_WorkingService,
        )

        assert result["status"] == "success", result
