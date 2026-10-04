"""``GET /api/files/{uuid}`` must not write speaker rows (#1152).

The detail read decorated each session-attached ``Speaker`` with its computed status by
assigning the mapped ``computed_status`` / ``status_text`` / ``status_color`` /
``resolved_display_name`` columns, then committed. Every poll of a file therefore issued
``UPDATE speaker ...`` for each of its speakers, taking row locks the processing worker was
also taking in its own order — Postgres resolved the cycle with ``DeadlockDetected`` and the
read returned 500.

The tests pin the three things that matter: the read emits no write to ``speaker`` (and, for a
file still processing, no write at all), the response still carries the computed status, and
a read does not wait on a speaker row another transaction holds.
"""

from __future__ import annotations

import threading
import uuid as uuid_pkg
from contextlib import contextmanager

import pytest
from fastapi import status
from sqlalchemy import event
from sqlalchemy import text

from app.api.endpoints.files.crud import get_media_file_detail
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.models.user import User
from tests.conftest import TestingSessionLocal
from tests.conftest import engine
from tests.user_owned_rows import make_user

_WRITE_VERBS = ("INSERT", "UPDATE", "DELETE")


@contextmanager
def captured_writes(session):
    """Every INSERT/UPDATE/DELETE executed on ``session``'s engine while the block runs.

    The engine is taken from the session, not imported: ``tests.conftest`` imported by
    name is a second module object with its own engine, which the request never uses.
    """
    writes: list[str] = []
    engine = session.connection().engine

    def _before(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(_WRITE_VERBS):
            writes.append(statement.strip().split("\n")[0])

    event.listen(engine, "before_cursor_execute", _before)
    try:
        yield writes
    finally:
        event.remove(engine, "before_cursor_execute", _before)


def _seed_file(db, owner_id: int, file_status: str) -> MediaFile:
    media_file = MediaFile(
        user_id=owner_id,
        filename="detail.wav",
        storage_path=f"test/{uuid_pkg.uuid4().hex}.wav",
        file_size=1024,
        content_type="audio/wav",
        status=file_status,
    )
    db.add(media_file)
    db.commit()
    speakers = [
        Speaker(user_id=owner_id, media_file_id=media_file.id, name="SPEAKER_00"),
        Speaker(
            user_id=owner_id,
            media_file_id=media_file.id,
            name="SPEAKER_01",
            display_name="Ada",
            confidence=0.8,
        ),
    ]
    db.add_all(speakers)
    db.commit()
    for i, speaker in enumerate(speakers * 2):
        db.add(
            TranscriptSegment(
                media_file_id=media_file.id,
                speaker_id=speaker.id,
                start_time=float(i),
                end_time=float(i) + 1.0,
                text=f"segment {i}",
            )
        )
    db.commit()
    db.refresh(media_file)
    return media_file


class TestDetailReadEmitsNoSpeakerWrites:
    def test_processing_file_read_writes_nothing(
        self, client, user_token_headers, normal_user, db_session
    ):
        media_file = _seed_file(db_session, normal_user.id, "processing")

        with captured_writes(db_session) as writes:
            response = client.get(f"/api/files/{media_file.uuid}", headers=user_token_headers)

        assert response.status_code == status.HTTP_200_OK, response.text
        assert writes == []

    def test_completed_file_without_analytics_writes_only_analytics(
        self, client, user_token_headers, normal_user, db_session
    ):
        """On-demand analytics is a legitimate write; it must not drag speakers along."""
        media_file = _seed_file(db_session, normal_user.id, "completed")

        with captured_writes(db_session) as writes:
            response = client.get(f"/api/files/{media_file.uuid}", headers=user_token_headers)

        assert response.status_code == status.HTTP_200_OK, response.text
        assert [w for w in writes if "speaker" in w.lower().split("(")[0]] == []
        assert all("analytics" in w for w in writes), writes

    @pytest.mark.parametrize("file_status", ["processing", "completed"])
    def test_response_still_carries_computed_status(
        self, client, user_token_headers, normal_user, db_session, file_status
    ):
        media_file = _seed_file(db_session, normal_user.id, file_status)

        response = client.get(f"/api/files/{media_file.uuid}", headers=user_token_headers)

        assert response.status_code == status.HTTP_200_OK, response.text
        by_name = {s["name"]: s for s in response.json()["speakers"]}
        assert by_name["SPEAKER_00"]["computed_status"] == "unverified"
        assert by_name["SPEAKER_01"]["computed_status"] == "suggested"
        assert by_name["SPEAKER_01"]["resolved_display_name"] == "Ada"
        assert by_name["SPEAKER_00"]["status_text"]
        assert by_name["SPEAKER_00"]["status_color"]
        # Public ids only: the internal integer keys never reach the wire.
        assert by_name["SPEAKER_00"]["media_file_id"] == str(media_file.uuid)
        assert by_name["SPEAKER_00"]["user_id"] == str(normal_user.uuid)

        # Nothing was persisted as a side effect of the read.
        stored = db_session.query(Speaker).filter(Speaker.media_file_id == media_file.id).all()
        db_session.expire_all()
        assert {s.computed_status for s in stored} == {None}


@pytest.fixture
def committed_file():
    """Rows committed for real, so a second connection can lock them."""
    s = TestingSessionLocal()
    user = make_user(s, "detail-lock")
    media_file = _seed_file(s, user.id, "processing")
    ids = (user.id, str(media_file.uuid), media_file.id)
    s.close()
    yield ids
    s = TestingSessionLocal()
    s.execute(text("DELETE FROM transcript_segment WHERE media_file_id = :f"), {"f": ids[2]})
    s.execute(text("DELETE FROM speaker WHERE media_file_id = :f"), {"f": ids[2]})
    s.execute(text("DELETE FROM media_file WHERE id = :f"), {"f": ids[2]})
    s.execute(text('DELETE FROM "user" WHERE id = :u'), {"u": ids[0]})
    s.commit()
    s.close()


def test_read_does_not_wait_on_a_speaker_row_held_by_another_transaction(committed_file):
    """The worker's side of the deadlock: it holds speaker row locks mid-transaction.

    A read that writes speaker rows queues behind that lock (and, with the worker queued
    behind one of the read's own, deadlocks). A read-only read never touches the lock.
    """
    user_id, file_uuid, file_id = committed_file
    worker = engine.connect()
    worker_tx = worker.begin()
    worker.execute(
        text("UPDATE speaker SET suggested_name = suggested_name WHERE media_file_id = :f"),
        {"f": file_id},
    )

    outcome: dict = {}

    def _read():
        db = TestingSessionLocal()
        try:
            db.execute(text("SET lock_timeout = '3s'"))
            user = db.get(User, user_id)
            assert user is not None
            outcome["detail"] = get_media_file_detail(db, file_uuid, user)
        except Exception as e:  # noqa: BLE001 - the failure IS the observation
            outcome["error"] = e
        finally:
            db.close()

    reader = threading.Thread(target=_read)
    try:
        reader.start()
        reader.join(15)
    finally:
        worker_tx.rollback()
        worker.close()
        reader.join(15)

    assert "error" not in outcome, repr(outcome.get("error"))
    assert len(outcome["detail"].speakers) == 2
