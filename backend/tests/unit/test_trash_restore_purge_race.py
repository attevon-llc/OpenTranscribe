"""Restore racing the purge: two real sessions, exactly one outcome (issue #1101).

``purge_media_file`` commits before its storage phase, so no row lock can span the destroy.
The trash relies on one invariant instead: purge only takes rows with ``purge_after <=
clock_timestamp()`` and restore only updates rows with ``purge_after > clock_timestamp()``,
each as a single statement that re-evaluates its WHERE after waiting for a row lock.

These tests commit real rows and drive the real ``trash_service.restore_files`` and
``trash_purge.purge_one`` from two threads with their own sessions, pinning the interleaving
with events and ``pg_stat_activity`` rather than timing luck. Everything created is removed
in teardown.
"""

from __future__ import annotations

import threading
import time
import uuid as uuid_pkg
from contextlib import AbstractContextManager
from contextlib import nullcontext
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.services import file_cleanup_service
from app.services import trash_purge
from app.services import trash_service
from tests.conftest import engine
from tests.user_owned_rows import make_user

Session = sessionmaker(bind=engine)


@pytest.fixture
def world(monkeypatch):
    """A committed user and a committed trashed file; both removed afterwards."""
    monkeypatch.setattr(trash_service, "_after_state_change", lambda *_a, **_k: None)
    monkeypatch.setattr(
        file_cleanup_service, "delete_file_storage_artifacts", lambda *_a, **_k: True
    )
    monkeypatch.setattr(file_cleanup_service, "_cleanup_opensearch_for_file", lambda *_a: [])
    monkeypatch.setattr(file_cleanup_service, "_refresh_surviving_centroids", lambda *_a: [])

    s = Session()
    user = make_user(s, "trashrace")
    file_uuid = uuid_pkg.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=user.id,
        filename=f"race_{file_uuid.hex[:8]}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1,
        status=FileStatus.COMPLETED,
    )
    s.add(media_file)
    s.commit()
    ids = {"user": int(user.id), "file": int(media_file.id)}
    trash_service.trash_files(s, [media_file], actor=user, request=None, bypass=None)
    s.close()
    yield ids
    s = Session()
    s.execute(text("DELETE FROM media_file WHERE id = :f"), {"f": ids["file"]})
    s.execute(text('DELETE FROM "user" WHERE id = :u'), {"u": ids["user"]})
    s.commit()
    s.close()


def _set_purge_after(file_id: int, sql_offset: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE media_file SET purge_after = clock_timestamp() + {sql_offset} WHERE id = :f"
            ),
            {"f": file_id},
        )


def _wait_until_due(file_id: int, timeout: float = 10.0) -> None:
    """Poll the database clock until ``purge_after`` has passed for ``file_id``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            due = conn.execute(
                text("SELECT purge_after <= clock_timestamp() FROM media_file WHERE id = :f"),
                {"f": file_id},
            ).scalar()
        if due:
            return
        time.sleep(0.05)
    raise AssertionError("the row never became due")


def _wait_for_a_lock_wait(timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            waiting = conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                )
            ).scalar()
        if waiting:
            return
        time.sleep(0.05)
    raise AssertionError("the second session never waited on the first one's row lock")


def _row(file_id: int) -> tuple[Any, ...] | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT deleted_at, purge_after FROM media_file WHERE id = :f"), {"f": file_id}
        ).first()
    return tuple(row) if row is not None else None


def _restore_in_thread(
    file_id: int,
    user_id: int,
    result: dict,
    *,
    hold_commit: threading.Event | None = None,
    begin_then_wait: threading.Event | None = None,
) -> threading.Thread:
    updated = threading.Event()
    begun = threading.Event()
    result["updated"] = updated
    result["begun"] = begun

    def run() -> None:
        s = Session()
        try:
            if begin_then_wait is not None:
                # Open the transaction now (now() is fixed from here), restore later.
                s.execute(text("SELECT 1"))
                begun.set()
                assert begin_then_wait.wait(15), "the test never let the restore go"
            commit_gate: AbstractContextManager[object] = nullcontext()
            if hold_commit is not None:
                real_commit = s.commit

                def gated_commit() -> None:
                    updated.set()
                    assert hold_commit.wait(15), "the test never released the restore"
                    real_commit()

                commit_gate = patch.object(s, "commit", gated_commit)
            with commit_gate:
                media_file, actor = s.get(MediaFile, file_id), s.get(User, user_id)
                assert media_file is not None and actor is not None
                restored, refused = trash_service.restore_files(
                    s, [media_file], actor=actor, request=None, bypass=None
                )
                result["restored"] = [int(f.id) for f in restored]
                result["refused"] = [int(f.id) for f in refused]
        except Exception as exc:  # surfaced by the assertions
            result["error"] = exc
            s.rollback()
        finally:
            updated.set()
            s.close()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def test_a_restore_that_holds_the_row_wins_over_a_purge_that_becomes_due_meanwhile(world):
    """Restore evaluated while not due, then held its lock past ``purge_after``: the purge
    waits on the lock, re-reads the restored row and skips it."""
    _set_purge_after(world["file"], "interval '1.5 seconds'")
    release = threading.Event()
    restore: dict = {}
    restorer = _restore_in_thread(world["file"], world["user"], restore, hold_commit=release)
    assert restore["updated"].wait(10)
    assert "error" not in restore, restore.get("error")
    _wait_until_due(world["file"])  # due by the clock now, but the restore holds the row

    purge: dict = {}
    purger = threading.Thread(
        target=lambda: purge.update(outcome=trash_purge.purge_one(world["file"], trigger="due"))
    )
    purger.start()
    _wait_for_a_lock_wait()
    release.set()
    restorer.join(15)
    purger.join(15)

    assert "error" not in restore, restore.get("error")
    assert restore["restored"] == [world["file"]]
    assert purge["outcome"][0] == "skipped"
    assert _row(world["file"]) == (None, None), "the restored file was purged anyway"


def test_a_restore_after_the_purge_took_the_row_is_refused(world, monkeypatch):
    """The purge holds the due row mid-destroy; a restore in that window is refused.

    It does not even need the lock: the row fails ``purge_after > clock_timestamp()`` on the
    restore's own snapshot, so the refusal is immediate and the purge finishes undisturbed.
    """
    _set_purge_after(world["file"], "- interval '1 minute'")
    locked, release = threading.Event(), threading.Event()
    real_purge = file_cleanup_service.purge_media_file

    def held_purge(db, media_file, **kw):
        locked.set()
        assert release.wait(15), "the test never released the purge"
        return real_purge(db, media_file, **kw)

    monkeypatch.setattr(file_cleanup_service, "purge_media_file", held_purge)
    purge: dict = {}
    purger = threading.Thread(
        target=lambda: purge.update(outcome=trash_purge.purge_one(world["file"], trigger="due"))
    )
    purger.start()
    assert locked.wait(10), "the purge never took the row"

    restore: dict = {}
    restorer = _restore_in_thread(world["file"], world["user"], restore)
    restorer.join(15)
    assert not restorer.is_alive(), "the restore blocked behind the purge"
    assert _row(world["file"]) is not None, "control: the purge must still be mid-destroy"
    release.set()
    purger.join(15)

    assert "error" not in restore, restore.get("error")
    assert purge["outcome"][0] == "purged"
    assert restore["restored"] == []
    assert restore["refused"] == [world["file"]]
    assert _row(world["file"]) is None


def test_a_restore_whose_transaction_began_before_the_row_was_due_is_still_refused(
    world, monkeypatch
):
    """The ``now()`` trap: ``now()`` is the TRANSACTION's start. A restore request whose
    transaction opened a moment before ``purge_after`` would read the row as not due after the
    purge had taken it and bring back a file whose objects were being destroyed. The trash
    compares with ``clock_timestamp()``, so the restore is refused."""
    _set_purge_after(world["file"], "interval '1 second'")
    go = threading.Event()
    restore: dict = {}
    restorer = _restore_in_thread(world["file"], world["user"], restore, begin_then_wait=go)
    assert restore["begun"].wait(10)
    _wait_until_due(world["file"])  # due by the clock; the restore's now() is still before it

    locked, release = threading.Event(), threading.Event()
    real_purge = file_cleanup_service.purge_media_file

    def held_purge(db, media_file, **kw):
        locked.set()
        assert release.wait(15), "the test never released the purge"
        return real_purge(db, media_file, **kw)

    monkeypatch.setattr(file_cleanup_service, "purge_media_file", held_purge)
    purge: dict = {}
    purger = threading.Thread(
        target=lambda: purge.update(outcome=trash_purge.purge_one(world["file"], trigger="due"))
    )
    purger.start()
    assert locked.wait(10), "the purge never took the row"
    go.set()
    restorer.join(3)
    release.set()
    purger.join(15)
    restorer.join(15)

    assert "error" not in restore, restore.get("error")
    assert purge["outcome"][0] == "purged"
    assert restore["restored"] == [], "a restore brought back a file the purge had taken"
    assert _row(world["file"]) is None
