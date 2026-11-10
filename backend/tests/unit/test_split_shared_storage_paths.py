"""``split_shared_storage_paths`` never repoints a held or quarantined recording.

The CLI gives every row that shares another's object key its own copy and repoints the row. A
legal hold or a quarantine freezes the file's record as it stands, so a protected row keeps the
original key (instead of the lowest id) and a second protected row in the same group is left
sharing it and counted. The duplicate scan, the copy and the index build are stood in for: the
test database carries the unique index, so it cannot hold a real shared key.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import text

from app.models.media import MediaFile
from app.scripts import split_shared_storage_paths as cli
from tests.fixtures.tag_world import make_file
from tests.fixtures.tag_world import make_user

SHARED = "user_1/file_1/shared.mp3"


class CopyRecorder:
    def __init__(self) -> None:
        self.copies: list[str] = []

    def copy_object(self, bucket, key, source):
        self.copies.append(key)


@pytest.fixture()
def world(db_session, monkeypatch):
    @contextmanager
    def scope():
        yield db_session

    user = make_user(db_session, "split")
    rows = [make_file(db_session, user) for _ in range(4)]
    for row in rows:
        row.playback_path = f"user_{user.id}/file_{row.id}/playback.mp4"
    db_session.commit()
    ids = [int(r.id) for r in rows]
    recorder = CopyRecorder()
    monkeypatch.setattr("app.db.session_utils.session_scope", scope)
    monkeypatch.setattr("app.services.minio_service.minio_client", recorder)
    monkeypatch.setattr(
        "app.db.object_key_indexes.duplicate_groups", lambda conn, index: [(SHARED, ids)]
    )
    monkeypatch.setattr("app.db.object_key_indexes.ensure_indexes", lambda conn: {})
    return db_session, ids, recorder


def _protect(db, file_id: int, how: str) -> None:
    column = "legal_hold" if how == "legal_hold" else "is_quarantined"
    db.execute(text(f"UPDATE media_file SET {column} = true WHERE id = :i"), {"i": file_id})
    db.commit()


def _paths(db, file_id: int) -> tuple[str, str | None]:
    db.expire_all()
    row = db.get(MediaFile, file_id)
    return str(row.storage_path), row.playback_path


@pytest.mark.parametrize("how", ["legal_hold", "quarantine"])
def test_protected_rows_are_never_repointed_and_are_counted(world, how):
    db, ids, recorder = world
    _protect(db, ids[2], how)
    _protect(db, ids[3], how)
    before = {i: _paths(db, i) for i in ids[2:]}

    dry = cli.split_shared_storage_paths(apply=False)
    report = cli.split_shared_storage_paths(apply=True)

    # ids[2] keeps the original key, ids[3] must keep sharing it, ids[0] and ids[1] are split.
    assert dry["rows_to_split"] == 2 and dry["skipped_protected"] == 1
    assert report["split"] == 2 and report["skipped_protected"] == 1 and report["failed"] == 0
    assert {i: _paths(db, i) for i in ids[2:]} == before, "a protected row was rewritten"
    for file_id in ids[:2]:
        path, playback = _paths(db, file_id)
        assert path.startswith("user_") and f"/file_{file_id}/" in path
        assert playback is None
    assert len(recorder.copies) == 2


def test_without_a_protected_row_the_lowest_id_keeps_the_key(world):
    db, ids, recorder = world
    before = _paths(db, ids[0])

    report = cli.split_shared_storage_paths(apply=True)

    assert report["split"] == 3 and report["skipped_protected"] == 0
    assert _paths(db, ids[0]) == before
    assert all(f"/file_{i}/" in c for i, c in zip(ids[1:], recorder.copies, strict=True))


def test_a_row_held_after_the_plan_was_read_is_left_as_it_stands(world, monkeypatch):
    db, ids, _ = world
    before = _paths(db, ids[1])
    real = cli._protected_ids

    def stale_plan(session, file_ids):
        found = real(session, file_ids)
        _protect(session, ids[1], "legal_hold")  # the hold lands between the plan and the write
        return found

    monkeypatch.setattr(cli, "_protected_ids", stale_plan)
    report = cli.split_shared_storage_paths(apply=True)

    assert _paths(db, ids[1]) == before
    assert report["skipped_protected"] == 1 and report["split"] == 2
