"""Trash tests' shared world: a recording external destroy and a trashable file.

Registered in ``conftest.pytest_plugins`` so the ``destroyed`` fixture is visible to every suite.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.services import trash_purge
from app.services import trash_service


@pytest.fixture
def destroyed(monkeypatch, db_session):
    """Record the external destroy, and run the purge's own sessions on the test session."""
    calls: list[str] = []

    def _storage(file_id, artifacts, *_args, **_kwargs):
        calls.append(f"storage:{artifacts.get('storage_path')}")
        return True

    def _opensearch(plan, file_uuid):
        calls.append(f"opensearch:{file_uuid}")
        return []

    monkeypatch.setattr("app.services.file_cleanup_service.delete_file_storage_artifacts", _storage)
    monkeypatch.setattr(
        "app.services.file_cleanup_service._cleanup_opensearch_for_file", _opensearch
    )

    @contextlib.contextmanager
    def _same_session():
        yield db_session
        db_session.commit()

    monkeypatch.setattr(trash_purge, "session_scope", _same_session)
    return calls


def make_file(db, owner, *, status=FileStatus.COMPLETED, completed_days_ago=0) -> MediaFile:
    file_uuid = uuid.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename=f"purge_{file_uuid.hex[:8]}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status=status,
        completed_at=datetime.now(UTC) - timedelta(days=completed_days_ago),
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def trash(db, owner, media_file, *, overdue=False) -> None:
    trash_service.trash_files(db, [media_file], actor=owner, request=None, bypass=None)
    if overdue:
        db.execute(
            text("UPDATE media_file SET purge_after = now() - interval '1 hour' WHERE id = :i"),
            {"i": media_file.id},
        )
        db.commit()


def exists(db, media_file) -> bool:
    db.expire_all()
    return db.get(MediaFile, media_file.id) is not None
