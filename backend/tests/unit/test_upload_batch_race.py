"""Concurrent creation of an upload batch (parallel ``/files/prepare`` of one multi-file upload).

Two independent sessions on real Postgres: one holds an uncommitted INSERT of the batch row
(so the unique index on ``uuid`` is locked) while a second session runs the get-or-create.
The second must wait, then reuse the committed row instead of raising a UniqueViolation.
"""

from __future__ import annotations

import threading
import time
import uuid as uuid_pkg

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.api.endpoints.files.prepare_upload import get_or_create_upload_batch
from app.api.endpoints.files.prepare_upload import increment_upload_batch_file_count
from app.models.upload_batch import UploadBatch
from tests.conftest import engine
from tests.user_owned_rows import make_user

Session = sessionmaker(bind=engine)


@pytest.fixture
def two_users():
    s = Session()
    a = make_user(s, "batchrace-a")
    b = make_user(s, "batchrace-b")
    ids = (a.id, b.id)
    s.close()
    yield ids
    s = Session()
    s.execute(
        text("DELETE FROM upload_batch WHERE user_id IN (:a, :b)"), {"a": ids[0], "b": ids[1]}
    )
    s.execute(text('DELETE FROM "user" WHERE id IN (:a, :b)'), {"a": ids[0], "b": ids[1]})
    s.commit()
    s.close()


def test_concurrent_create_same_uuid_yields_single_batch(two_users):
    user_id, _ = two_users
    batch_uuid = uuid_pkg.uuid4()

    holder = Session()
    holder.add(UploadBatch(uuid=batch_uuid, user_id=user_id, source="multi_upload", file_count=0))
    holder.flush()  # uncommitted: the second creator's INSERT must wait on this

    result: dict = {}

    def second():
        s = Session()
        try:
            b = get_or_create_upload_batch(s, batch_uuid, user_id)
            increment_upload_batch_file_count(s, b.id)
            s.commit()
            result["id"] = b.id
        except Exception as e:  # noqa: BLE001
            result["error"] = e
            s.rollback()
        finally:
            s.close()

    t = threading.Thread(target=second)
    t.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and "id" not in result and "error" not in result:
        with engine.connect() as c:  # wait until the second insert is blocked on our lock
            blocked = c.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'")
            ).scalar()
        if blocked or "error" in result:
            break
        time.sleep(0.05)
    increment_upload_batch_file_count(
        holder, holder.query(UploadBatch.id).filter_by(uuid=batch_uuid).scalar()
    )
    holder.commit()
    t.join(10)

    assert "error" not in result, result.get("error")
    check = Session()
    rows = check.query(UploadBatch).filter_by(uuid=batch_uuid).all()
    assert len(rows) == 1
    assert rows[0].id == result["id"]
    assert rows[0].file_count == 2  # both increments applied, none lost
    check.close()
    holder.close()


def test_existing_batch_of_another_user_is_rejected(two_users):
    owner, other = two_users
    batch_uuid = uuid_pkg.uuid4()
    s = Session()
    get_or_create_upload_batch(s, batch_uuid, owner)
    s.commit()

    s2 = Session()
    with pytest.raises(HTTPException) as exc:
        get_or_create_upload_batch(s2, batch_uuid, other)
    assert exc.value.status_code == 409
    s2.rollback()
    assert s2.query(UploadBatch).filter_by(uuid=batch_uuid).count() == 1
    s.close()
    s2.close()
