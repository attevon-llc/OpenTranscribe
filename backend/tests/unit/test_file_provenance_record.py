"""``record_stage``: one atomic write per step, never a read-modify-write (issue #1229).

The summary, topic and speaker-name tasks finish concurrently for the same file. The
concurrency test makes the race deterministic instead of hoping for it: the first writer
holds its row lock uncommitted, the next writers are started and the test waits until
Postgres reports them blocked on that lock, then commits. A writer that read the document
before writing (read-modify-write) computes its update from the stale NULL and erases the
first step; the single ``jsonb_set`` statement is re-evaluated against the committed row.

These tests need the ``processing_provenance`` column, i.e. a database migrated to v435.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from sqlalchemy import text

from app.db.base import SessionLocal
from app.models.media import MediaFile
from app.models.user import User
from app.services.file_provenance import record_stage


def _document(db, file_id: int):
    return db.execute(
        text("SELECT processing_provenance FROM media_file WHERE id = :i"), {"i": file_id}
    ).scalar()


@pytest.fixture
def committed_file():
    """A user and a media file COMMITTED, so separate connections can contend on the row."""
    session = SessionLocal()
    user = User(
        email=f"provenance-{uuid.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        full_name="Provenance Test",
        role="user",
        auth_type="local",
    )
    session.add(user)
    session.commit()
    media = MediaFile(
        filename="provenance.wav",
        storage_path=f"x/{uuid.uuid4().hex}.wav",
        file_size=1,
        content_type="audio/wav",
        status="completed",
        user_id=user.id,
    )
    session.add(media)
    session.commit()
    file_id, user_id = media.id, user.id
    session.close()
    try:
        yield file_id
    finally:
        cleanup = SessionLocal()
        cleanup.execute(text("DELETE FROM media_file WHERE id = :i"), {"i": file_id})
        cleanup.execute(text('DELETE FROM "user" WHERE id = :i'), {"i": user_id})
        cleanup.commit()
        cleanup.close()


def _wait_until_blocked(pids: list[int], timeout: float = 15.0) -> None:
    """Until every backend in ``pids`` is waiting on a lock (proves the interleaving)."""
    probe = SessionLocal()
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            waiting = probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE pid = ANY(:pids) AND wait_event_type = 'Lock'"
                ),
                {"pids": pids},
            ).scalar()
            probe.rollback()
            if waiting == len(pids):
                return
            time.sleep(0.05)
        raise AssertionError(f"writers {pids} never blocked on the row lock")
    finally:
        probe.close()


def test_concurrent_stages_all_survive(committed_file):
    file_id = committed_file
    first = SessionLocal()
    pids: list[int] = []
    pid_lock = threading.Lock()
    connected = [threading.Event(), threading.Event()]
    errors: list[BaseException] = []

    def writer(stage: str, model: str, ready: threading.Event) -> None:
        session = SessionLocal()
        try:
            with pid_lock:
                pids.append(int(session.execute(text("SELECT pg_backend_pid()")).scalar_one()))
            ready.set()
            assert record_stage(session, file_id, stage, provider="vllm", model=model)
            session.commit()
        except BaseException as exc:  # surfaced on the main thread below
            errors.append(exc)
        finally:
            session.close()

    try:
        assert record_stage(first, file_id, "summary", provider="vllm", model="m-summary")
        threads = [
            threading.Thread(target=writer, args=("topics", "m-topics", connected[0])),
            threading.Thread(target=writer, args=("speaker_names", "m-names", connected[1])),
        ]
        for thread in threads:
            thread.start()
        assert all(event.wait(timeout=15) for event in connected), errors
        _wait_until_blocked(list(pids))
        first.commit()
        for thread in threads:
            thread.join(timeout=30)
        assert not errors, errors
    finally:
        first.close()

    check = SessionLocal()
    try:
        steps = _document(check, file_id)["steps"]
    finally:
        check.close()
    assert {name: entry["model"] for name, entry in steps.items()} == {
        "summary": "m-summary",
        "topics": "m-topics",
        "speaker_names": "m-names",
    }


def _make_file(db_session) -> int:
    user = User(
        email=f"provenance-{uuid.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        full_name="Provenance Test",
        role="user",
        auth_type="local",
    )
    db_session.add(user)
    db_session.flush()
    media = MediaFile(
        filename="provenance.wav",
        storage_path=f"x/{uuid.uuid4().hex}.wav",
        file_size=1,
        content_type="audio/wav",
        status="completed",
        user_id=user.id,
    )
    db_session.add(media)
    db_session.flush()
    return int(media.id)


def test_recording_a_stage_again_replaces_only_that_stage(db_session):
    file_id = _make_file(db_session)
    assert record_stage(db_session, file_id, "summary", provider="openai", model="a")
    assert record_stage(db_session, file_id, "topics", provider="openai", model="t")
    assert record_stage(db_session, file_id, "summary", provider="vllm", model="b", where="server")

    document = _document(db_session, file_id)
    assert document["v"] == 1
    assert set(document["steps"]) == {"summary", "topics"}
    summary = document["steps"]["summary"]
    assert (summary["provider"], summary["model"], summary["where"]) == ("vllm", "b", "server")
    assert "completed_at" in summary
    assert document["steps"]["topics"]["model"] == "t"


def test_a_new_run_drops_the_previous_runs_steps(db_session):
    file_id = _make_file(db_session)
    record_stage(db_session, file_id, "summary", provider="openai", model="old")
    record_stage(db_session, file_id, "voiceprints", provider="native", where="server")
    assert record_stage(
        db_session, file_id, "transcription", new_run=True, provider="local", where="server"
    )
    assert set(_document(db_session, file_id)["steps"]) == {"transcription"}


def test_secrets_and_urls_are_refused(db_session):
    file_id = _make_file(db_session)
    with pytest.raises(TypeError, match="base_url"):
        record_stage(db_session, file_id, "summary", provider="vllm", base_url="http://h:8000")
    with pytest.raises(TypeError, match="api_key"):
        record_stage(db_session, file_id, "summary", api_key="sk-1")
    with pytest.raises(ValueError, match="unknown provenance stage"):
        record_stage(db_session, file_id, "billing", provider="vllm")
    # A URL-shaped model name is data, not a programming error: it is dropped, not stored.
    assert not record_stage(db_session, file_id, "summary", model="http://h:8000/v1")
    assert _document(db_session, file_id) is None


def test_a_failed_write_does_not_poison_the_stage_transaction(db_session, monkeypatch):
    """Provenance is best-effort: the stage's own write must still commit."""
    from sqlalchemy.exc import ProgrammingError

    from app.services import file_provenance

    file_id = _make_file(db_session)
    monkeypatch.setattr(file_provenance, "_MERGE_SQL", text("UPDATE no_such_table SET x = 1"))
    assert not record_stage(db_session, file_id, "summary", provider="vllm")

    # The session is still usable: the savepoint absorbed the failure.
    db_session.execute(
        text("UPDATE media_file SET title = 'still-writable' WHERE id = :i"), {"i": file_id}
    )
    assert (
        db_session.execute(
            text("SELECT title FROM media_file WHERE id = :i"), {"i": file_id}
        ).scalar()
        == "still-writable"
    )
    with pytest.raises(ProgrammingError):
        db_session.execute(text("UPDATE no_such_table SET x = 1"))


def test_the_post_stage_can_record_how_it_ran(db_session):
    """The facts the plan-driven post stage (WP3) knows: execution and merge granularity."""
    file_id = _make_file(db_session)
    assert record_stage(
        db_session,
        file_id,
        "diarization",
        provider="pyannote.ai",
        model="precision-3",
        where="cloud",
        engine="pyannote_ai",
        execution="post_stage_cloud",
        merge="segment",
    )
    entry = _document(db_session, file_id)["steps"]["diarization"]
    assert (entry["execution"], entry["merge"]) == ("post_stage_cloud", "segment")
    assert not record_stage(db_session, file_id, "diarization", merge="sentence")
