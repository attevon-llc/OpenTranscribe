"""A seeded file carries what a pipeline run records: provenance, a task row, a thumbnail (#628).

Before, File details read "Not recorded (processed before this was tracked)", File status had
no ``transcription`` task, and the gallery had no thumbnails until a backfill ran. Real rows
on the savepoint-isolated ``db_session``; only the Celery task boundary is faked.
"""

from __future__ import annotations

from types import SimpleNamespace

from fixtures.speaker_cleanup_seams import make_file
from sqlalchemy import select

from app.models.media import Task
from app.scripts.demo_seed import processing
from app.services.file_provenance_view import build_processing_steps


def test_the_transcription_step_is_recorded_and_not_reported_as_untracked(db_session, normal_user):
    media_file = make_file(db_session, normal_user)
    db_session.commit()

    processing.record_import_processing(db_session, media_file.id, normal_user.id)
    db_session.refresh(media_file)

    first = build_processing_steps(media_file)[0]
    assert (first.step, first.status, first.recorded, first.where) == (
        "transcription",
        "done",
        "full",
        "server",
    )
    assert {s.status for s in build_processing_steps(media_file)[1:]} == {"not_run"}


def test_a_completed_transcription_task_is_listed_for_the_file(db_session, normal_user):
    media_file = make_file(db_session, normal_user)
    db_session.commit()

    processing.record_import_processing(db_session, media_file.id, normal_user.id)

    rows = db_session.scalars(select(Task).where(Task.media_file_id == media_file.id)).all()
    assert [(t.task_type, t.status) for t in rows] == [("transcription", "completed")]
    db_session.refresh(media_file)
    assert media_file.active_task_id is None


def test_a_video_goes_through_the_production_thumbnail_task(monkeypatch):
    seen: list[dict] = []

    def apply(kwargs):
        seen.append(kwargs)
        return SimpleNamespace(result={"success": True})

    from app.tasks import thumbnail

    monkeypatch.setattr(thumbnail.generate_thumbnail_task, "apply", apply)

    assert processing.generate_import_thumbnail(5, 7, "user_7/file_5/x.mp4", "video/mp4")
    assert seen == [{"file_id": 5, "user_id": 7, "storage_path": "user_7/file_5/x.mp4"}]


def test_a_failed_thumbnail_is_reported_and_audio_is_skipped(monkeypatch):
    from app.tasks import thumbnail

    monkeypatch.setattr(
        thumbnail.generate_thumbnail_task,
        "apply",
        lambda kwargs: SimpleNamespace(result={"success": False}),
    )

    assert not processing.generate_import_thumbnail(5, 7, "p.mp4", "video/mp4")
    assert processing.generate_import_thumbnail(5, 7, "p.mp3", "audio/mpeg")


def test_a_seeded_item_is_finished_in_the_order_a_pipeline_run_ends(monkeypatch):
    from contextlib import contextmanager

    from app.db import base
    from app.scripts.corpus_injection import injector
    from app.scripts.demo_seed import __main__ as seed

    calls: list[tuple] = []

    @contextmanager
    def session():
        yield "db"

    monkeypatch.setattr(base, "SessionLocal", session)
    monkeypatch.setattr(
        processing, "record_import_processing", lambda _db, *ids: calls.append(("record", *ids))
    )

    def thumbnail(*args):
        calls.append(("thumb", *args))
        return True

    monkeypatch.setattr(processing, "generate_import_thumbnail", thumbnail)
    monkeypatch.setattr(injector, "dispatch_indexing", lambda *_a, **_k: calls.append(("index",)))
    monkeypatch.setattr(injector, "dispatch_redaction", lambda *_a, **_k: calls.append(("redact",)))

    seed._finish_item("x", SimpleNamespace(media_file_id=5), 7, "p.mp4", "video/mp4")

    assert calls == [
        ("record", 5, 7),
        ("thumb", 5, 7, "p.mp4", "video/mp4"),
        ("index",),
        ("redact",),
    ]


def test_a_missing_thumbnail_stops_the_seed(monkeypatch):
    from contextlib import contextmanager

    import pytest

    from app.db import base
    from app.scripts.demo_seed import __main__ as seed

    @contextmanager
    def session():
        yield "db"

    monkeypatch.setattr(base, "SessionLocal", session)
    monkeypatch.setattr(processing, "record_import_processing", lambda *_a: None)
    monkeypatch.setattr(processing, "generate_import_thumbnail", lambda *_a: False)

    with pytest.raises(RuntimeError, match="x: no thumbnail"):
        seed._finish_item("x", SimpleNamespace(media_file_id=5), 7, "p.mp4", "video/mp4")
