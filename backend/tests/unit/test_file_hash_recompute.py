"""``file_hash_recompute.backfill_document_file_hashes`` — the one-off backfill of
``document.file_hash`` for rows written before it was populated at ingest time.

Mirrors ``imohash_recompute.py``'s three-phase shape (read -> MinIO work with no
session open -> write), but scoped to ``file_hash IS NULL`` rows only — unlike
that task, this one does not invalidate already-populated rows.

The task function is called directly, bypassing the Celery broker, the same
way this repo tests every other task body (``test_document_parse_task.py``'s
own docstring explains the pattern). ``compute_from_minio`` is monkeypatched
rather than requiring the live MinIO stack: what this suite asserts is the
DB-side orchestration (which rows are eligible, the NULL-only scope, the
per-row failure isolation, the self-rescheduling cursor, the completion flag)
— not the fingerprint algorithm itself, which ``imohash_service`` and its own
tests already cover.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import uuid as uuid_pkg

import pytest

from app.core.enums import FileStatus
from app.models.document import Document
from app.tasks.file_hash_recompute import BACKFILL_FLAG_KEY
from app.tasks.file_hash_recompute import backfill_document_file_hashes

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _patched_session_scope(monkeypatch, db_session):
    """The task opens its own `session_scope()` for each of its three phases
    (by design — see `app/tasks/CLAUDE.md`'s session-lifetime rule), which a
    savepoint-isolated `db_session` cannot see under READ COMMITTED unless
    redirected onto the same connection. Same fix `test_document_parse_task.py`
    documents for the identical trap.
    """

    @contextlib.contextmanager
    def fake_scope():
        yield db_session
        db_session.commit()

    monkeypatch.setattr("app.tasks.file_hash_recompute.session_scope", fake_scope)


@pytest.fixture(autouse=True)
def _no_real_dispatch(monkeypatch):
    """Never let a self-reschedule reach a real broker — capture it instead."""
    calls: list[dict] = []
    monkeypatch.setattr(
        backfill_document_file_hashes,
        "apply_async",
        lambda kwargs=None, **kw: calls.append(kwargs or {}),
    )
    return calls


def _make_document(
    db, user, *, file_hash=None, status=FileStatus.COMPLETED, storage_path=None
) -> Document:
    doc = Document(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename="report.pdf",
        storage_path=storage_path or f"documents/{uuid_pkg.uuid4()}.pdf",
        file_size=2048,
        content_type="application/pdf",
        file_hash=file_hash,
        status=status,
        created_at=dt.datetime.now(dt.UTC),
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def _get_setting(db, key: str) -> str | None:
    from app.models.system_settings import SystemSettings

    row = db.query(SystemSettings).filter(SystemSettings.key == key).first()
    return row.value if row else None


def test_backfills_a_null_file_hash_row(db_session, normal_user, monkeypatch):
    doc = _make_document(db_session, normal_user, file_hash=None)

    calls: list[str] = []

    def fake_compute(object_name, size=None):
        calls.append(object_name)
        return "abc123fingerprint"

    monkeypatch.setattr("app.tasks.file_hash_recompute.compute_from_minio", fake_compute)

    summary = backfill_document_file_hashes(batch_size=100)

    assert summary["documents_backfilled"] == 1
    assert calls == [doc.storage_path]
    db_session.refresh(doc)
    assert doc.file_hash == "abc123fingerprint"


def test_a_row_with_an_existing_file_hash_is_never_touched(db_session, normal_user, monkeypatch):
    """The core distinction from `imohash_recompute.py`: this task fills the
    GAP only. A row already carrying a fingerprint was written correctly by
    the current ingest code path and must not be recomputed or overwritten."""
    already_hashed = _make_document(db_session, normal_user, file_hash="already-set")

    calls: list[str] = []

    def fake_compute(object_name, size=None):
        calls.append(object_name)
        return "should-never-be-written"

    monkeypatch.setattr("app.tasks.file_hash_recompute.compute_from_minio", fake_compute)

    summary = backfill_document_file_hashes(batch_size=100)

    assert summary["documents_found"] == 0
    assert calls == []
    db_session.refresh(already_hashed)
    assert already_hashed.file_hash == "already-set"


def test_error_status_rows_are_skipped(db_session, normal_user, monkeypatch):
    _make_document(db_session, normal_user, file_hash=None, status=FileStatus.ERROR)

    calls: list[str] = []

    def fake_compute(object_name, size=None):
        calls.append(object_name)
        return "x"

    monkeypatch.setattr("app.tasks.file_hash_recompute.compute_from_minio", fake_compute)

    summary = backfill_document_file_hashes(batch_size=100)

    assert summary["documents_found"] == 0
    assert calls == []


def test_a_missing_or_unreadable_object_is_skipped_not_an_error(
    db_session, normal_user, monkeypatch
):
    """`compute_from_minio` returns None (its documented contract on error or a
    missing object) — the row stays unhashed but the batch does not raise."""
    doc = _make_document(db_session, normal_user, file_hash=None)

    monkeypatch.setattr(
        "app.tasks.file_hash_recompute.compute_from_minio", lambda object_name, size=None: None
    )

    summary = backfill_document_file_hashes(batch_size=100)

    assert summary["documents_skipped"] == 1
    assert summary["documents_failed"] == 0
    db_session.refresh(doc)
    assert doc.file_hash is None


def test_one_failing_document_does_not_abort_the_batch(db_session, normal_user, monkeypatch):
    """One bad file must not prevent its siblings in the same batch from
    being fingerprinted — same guarantee `imohash_recompute.py` gives."""
    bad = _make_document(db_session, normal_user, file_hash=None, storage_path="documents/bad.pdf")
    good = _make_document(
        db_session, normal_user, file_hash=None, storage_path="documents/good.pdf"
    )

    def fake_compute(object_name, size=None):
        if object_name == "documents/bad.pdf":
            raise RuntimeError("simulated MinIO read failure")
        return "good-fingerprint"

    monkeypatch.setattr("app.tasks.file_hash_recompute.compute_from_minio", fake_compute)

    summary = backfill_document_file_hashes(batch_size=100)

    assert summary["documents_failed"] == 1
    assert summary["documents_backfilled"] == 1
    db_session.refresh(bad)
    db_session.refresh(good)
    assert bad.file_hash is None
    assert good.file_hash == "good-fingerprint"


def test_self_reschedules_when_more_rows_remain(
    db_session, normal_user, monkeypatch, _no_real_dispatch
):
    _make_document(db_session, normal_user, file_hash=None, storage_path="documents/one.pdf")
    _make_document(db_session, normal_user, file_hash=None, storage_path="documents/two.pdf")

    monkeypatch.setattr(
        "app.tasks.file_hash_recompute.compute_from_minio",
        lambda object_name, size=None: f"hash-{object_name}",
    )

    summary = backfill_document_file_hashes(batch_size=1)

    assert summary["has_more"] is True
    assert len(_no_real_dispatch) == 1
    assert _no_real_dispatch[0]["after_id"] == summary["last_id"]
    assert _no_real_dispatch[0]["batch_size"] == 1
    # The completion flag must NOT be set yet — one batch remains.
    assert _get_setting(db_session, BACKFILL_FLAG_KEY) is None


def test_sets_completion_flag_when_every_eligible_row_is_processed(
    db_session, normal_user, monkeypatch
):
    _make_document(db_session, normal_user, file_hash=None)

    monkeypatch.setattr(
        "app.tasks.file_hash_recompute.compute_from_minio",
        lambda object_name, size=None: "fingerprint",
    )

    summary = backfill_document_file_hashes(batch_size=100)

    assert summary["has_more"] is False
    assert _get_setting(db_session, BACKFILL_FLAG_KEY) == "true"


def test_no_eligible_rows_still_sets_the_completion_flag(db_session, normal_user):
    """An empty library (or one where every row already has a hash) is a
    legitimate "done" state, not a no-op that leaves the flag unset forever."""
    _make_document(db_session, normal_user, file_hash="already-set")

    summary = backfill_document_file_hashes(batch_size=100)

    assert summary["documents_found"] == 0
    assert summary["has_more"] is False
    assert _get_setting(db_session, BACKFILL_FLAG_KEY) == "true"
