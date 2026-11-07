"""Trash and restore rewrite the file's index stamp, and a failed stamp converges (#1101).

The trash commit is the truth; the index learns it right after through
``availability_stamp.stamp_files`` (synchronous, ``refresh=True``). A stamp that raises or
leaves documents unstamped never undoes the commit: it dispatches
``search.availability_reconcile`` for those files, and the beat tick covers a failed dispatch.
The stamp is recorded here, not sent: its OpenSearch bodies are pinned in
``tests/unit/test_availability_stamp.py`` and live in
``tests/integration/test_trash_index_lifecycle_opensearch.py``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.models.media import MediaFile
from app.services import trash_service
from app.services.search import availability_stamp


@pytest.fixture
def recorded(monkeypatch) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"stamp": [], "dispatch": []}

    def _stamp(file_ids, value, **_kw):
        calls["stamp"].append((sorted(file_ids), value))
        return availability_stamp.StampOutcome(updated=len(list(file_ids)))

    monkeypatch.setattr(availability_stamp, "stamp_files", _stamp)
    monkeypatch.setattr(
        availability_stamp,
        "dispatch_reconcile",
        lambda ids: calls["dispatch"].append(sorted(ids)),
    )
    return calls


def _file(db, owner) -> MediaFile:
    file_uuid = uuid.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename=f"stamp_{file_uuid.hex[:8]}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1,
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def test_a_trash_stamps_the_files_trashed(normal_user, db_session, recorded):
    media_file = _file(db_session, normal_user)

    trash_service.trash_files(
        db_session, [media_file], actor=normal_user, request=None, bypass=None
    )

    assert recorded["stamp"] == [([media_file.id], "trashed")]
    assert recorded["dispatch"] == []


def test_a_restore_stamps_the_files_available(normal_user, db_session, recorded):
    media_file = _file(db_session, normal_user)
    trash_service.trash_files(
        db_session, [media_file], actor=normal_user, request=None, bypass=None
    )

    restored, _ = trash_service.restore_files(
        db_session, [media_file], actor=normal_user, request=None, bypass=None
    )

    assert [f.id for f in restored] == [media_file.id]
    assert recorded["stamp"][-1] == ([media_file.id], "available")


def test_a_file_already_in_the_trash_is_not_stamped_again(normal_user, db_session, recorded):
    media_file = _file(db_session, normal_user)
    trash_service.trash_files(
        db_session, [media_file], actor=normal_user, request=None, bypass=None
    )
    trash_service.trash_files(
        db_session, [media_file], actor=normal_user, request=None, bypass=None
    )

    assert recorded["stamp"] == [([media_file.id], "trashed")]


def test_a_failed_stamp_keeps_the_trash_and_dispatches_a_reconcile(
    normal_user, db_session, recorded, monkeypatch
):
    media_file = _file(db_session, normal_user)

    def _down(*_a, **_k):
        raise ConnectionError("opensearch down")

    monkeypatch.setattr(availability_stamp, "stamp_files", _down)

    trashed = trash_service.trash_files(
        db_session, [media_file], actor=normal_user, request=None, bypass=None
    )

    db_session.expire_all()
    assert [f.id for f in trashed] == [media_file.id]
    assert db_session.get(MediaFile, media_file.id).purge_after is not None
    assert recorded["dispatch"] == [[media_file.id]]


def test_an_incomplete_stamp_dispatches_a_reconcile(normal_user, db_session, recorded, monkeypatch):
    media_file = _file(db_session, normal_user)
    monkeypatch.setattr(
        availability_stamp,
        "stamp_files",
        lambda *_a, **_k: availability_stamp.StampOutcome(incomplete=["transcript_chunks"]),
    )

    trash_service.trash_files(
        db_session, [media_file], actor=normal_user, request=None, bypass=None
    )

    assert recorded["dispatch"] == [[media_file.id]]
