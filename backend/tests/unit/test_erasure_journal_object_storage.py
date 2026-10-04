"""The GDPR erasure journal on object storage (``ERASURE_JOURNAL_BACKEND=object_storage``).

The journal exists to survive a database restore (see ``test_erasure_ledger.py``). On a
deployment whose containers run with a read-only root filesystem and no durable volume,
the file backend had nowhere to write: every append failed with
``[Errno 30] Read-only file system: '<DATA_DIR>/gdpr'`` and every erasure was one a
restore could silently undo. Mounting an ephemeral volume would silence the error and
lose the journal with the pod — the same outcome, now invisible.

Every test here runs with ``DATA_DIR`` pointed at a path that **cannot** be a directory,
so any fallback to the filesystem fails for real; the journal has to be in the bucket.

Two stores: a real S3-compatible server when one is reachable (``SKIP_S3=False``), and an
in-memory stand-in for the three calls the journal makes, so CI (which forces
``SKIP_S3``) still exercises the code path.
"""

from __future__ import annotations

import io
import json
import os
import uuid as uuid_pkg
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.erasure import ErasureLedgerEntry
from app.services import erasure_ledger_service as ledger

_S3_ABSENT = os.environ.get("SKIP_S3", "True").lower() == "true"


class _InMemoryObjectStore:
    """put_object / list_objects / get_object with minio-py's call shapes."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.gets: list[str] = []

    def put_object(self, *, bucket_name, object_name, data, length, content_type=None):
        body = data.read()
        assert len(body) == length
        self.objects[(bucket_name, object_name)] = body

    def list_objects(self, bucket, prefix=None, recursive=False):
        return [
            SimpleNamespace(object_name=name)
            for (b, name) in sorted(self.objects)
            if b == bucket and name.startswith(prefix or "")
        ]

    def get_object(self, bucket, name):
        self.gets.append(name)
        stream = io.BytesIO(self.objects[(bucket, name)])
        stream.release_conn = lambda: None  # type: ignore[attr-defined]
        return stream


def _real_store(bucket: str) -> Any:
    from app.services.minio_service import minio_client

    minio_client.make_bucket(bucket)
    return minio_client


def _drop_real_bucket(client: Any, bucket: str) -> None:
    for obj in client.list_objects(bucket, recursive=True):
        client.remove_object(bucket, obj.object_name)
    client.remove_bucket(bucket)


@pytest.fixture(params=["in_memory", "real_s3"])
def object_journal(request, tmp_path, monkeypatch):
    from app.core.config import settings

    blocker = tmp_path / "read-only-data-dir"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "DATA_DIR", blocker)
    monkeypatch.setattr(settings, "ERASURE_JOURNAL_BACKEND", "object_storage")

    bucket = f"test-erasure-journal-{uuid_pkg.uuid4().hex[:10]}"
    if request.param == "real_s3":
        if _S3_ABSENT:
            pytest.skip("No S3-compatible server reachable (SKIP_S3)")
        client = _real_store(bucket)
    else:
        client = _InMemoryObjectStore()
    monkeypatch.setattr(ledger, "_journal_storage", lambda: (client, bucket))
    try:
        yield SimpleNamespace(client=client, bucket=bucket)
    finally:
        if request.param == "real_s3":
            _drop_real_bucket(client, bucket)


def _mk_user(db, label: str = "journal_os"):
    from app.core.security import get_password_hash
    from app.models.user import User

    user = User(
        email=f"{label}_{uuid_pkg.uuid4().hex[:10]}@example.com",
        full_name=f"{label} user",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _read(store: SimpleNamespace, name: str) -> Any:
    resp = store.client.get_object(store.bucket, name)
    try:
        return json.loads(resp.read().decode("utf-8"))
    finally:
        resp.close()
        resp.release_conn()


def test_the_journal_lands_in_object_storage_when_the_filesystem_is_read_only(
    db_session, object_journal
):
    user = _mk_user(db_session)
    entry = ledger.record_request(
        db_session, subject_type="user", subject_user_id=int(user.id), subject_user_uuid=user.uuid
    )
    assert entry is not None

    record = _read(object_journal, ledger.journal_object_name(str(entry.uuid)))

    assert record["uuid"] == str(entry.uuid)
    assert record["subject_user_id"] == int(user.id)
    assert user.email not in json.dumps(record), "the journal must not copy personal data"
    assert not ledger.journal_path().exists()


def test_restore_reopens_an_entry_the_database_lost_and_is_idempotent(db_session, object_journal):
    """The backup-restore case: the row is gone, the bucket still has the entry."""
    user = _mk_user(db_session)
    entry = ledger.record_request(
        db_session, subject_type="user", subject_user_id=int(user.id), subject_user_uuid=user.uuid
    )
    assert entry is not None
    entry_uuid, requested_at = entry.uuid, entry.requested_at
    db_session.query(ErasureLedgerEntry).filter(ErasureLedgerEntry.uuid == entry_uuid).delete()
    db_session.commit()

    assert ledger.restore_from_journal(db_session) == 1
    back = db_session.query(ErasureLedgerEntry).filter(ErasureLedgerEntry.uuid == entry_uuid).one()
    assert back.status == "pending"
    assert back.requested_at == requested_at, "a restore must not reset the Art. 12(3) clock"

    assert ledger.restore_from_journal(db_session) == 0


def test_entries_the_database_still_has_are_not_fetched(db_session, object_journal):
    """Restore runs every reconciliation tick; known entries must cost no GET."""
    if not isinstance(object_journal.client, _InMemoryObjectStore):
        pytest.skip("GET counting needs the in-memory store")
    user = _mk_user(db_session)
    entry = ledger.record_request(
        db_session, subject_type="user", subject_user_id=int(user.id), subject_user_uuid=user.uuid
    )
    assert entry is not None

    assert ledger.restore_from_journal(db_session) == 0
    assert object_journal.client.gets == []


def test_an_unknown_journal_backend_fails_at_startup():
    """A typo must not silently fall back to a filesystem that cannot be written."""
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError, match="ERASURE_JOURNAL_BACKEND"):
        Settings(ERASURE_JOURNAL_BACKEND="s3")
