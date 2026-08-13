"""The GDPR Art. 17 / Art. 30 erasure ledger (issue #442).

Three things this suite is actually for, in order of how badly getting them wrong would
hurt:

1. **The ledger must not contain the personal data it records the destruction of.**
   A record of "we erased alice@example.com" that contains the address is a copy of the
   thing that was supposed to be destroyed, in a table designed to outlive it. Tested
   at the schema (no free-text column exists), at the database (the ``counters`` CHECK
   rejects a string) and at the journal line.
2. **The status must be derived from the summary, not asserted by the caller.** A
   deferral recorded as ``complete`` is worse than no ledger at all: the sweep skips it
   and the retention becomes permanent while the paperwork says otherwise.
3. **The journal must survive the database.** ``erasure_ledger`` lives in Postgres, so
   restoring a dump taken before an erasure destroys the record of the erasure along
   with the erasure itself. The on-disk journal is the copy that is not in the dump.

``DATA_DIR`` is redirected to ``tmp_path`` in every journal test — the fixture asserts
the redirect took, because a test that silently wrote to the real data volume would pass
while doing something nobody wants.
"""

from __future__ import annotations

import json
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy import String
from sqlalchemy import inspect
from sqlalchemy import text

from app.models.erasure import ErasureLedgerEntry
from app.services import erasure_ledger_service as ledger

#: The only textual columns the table is allowed to have, each backed by a CHECK.
ENUM_COLUMNS = {"subject_type", "status", "actor_kind", "deferred_reason"}


@pytest.fixture()
def journal_dir(tmp_path, monkeypatch):
    """Point the erasure journal at a throwaway directory, and prove it moved."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    resolved = ledger.journal_path()
    assert str(resolved).startswith(str(tmp_path)), (
        "the journal is still pointed at the real data volume — this test would write "
        f"to {resolved}"
    )
    return tmp_path


def _mk_user(db, label: str = "ledger"):
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


# --------------------------------------------------------------------- shape


def test_the_ledger_has_no_free_text_column(db_session):
    """Every textual column is a short CHECK-constrained enum. No exceptions.

    This is the strongest form of the no-personal-data rule: there is nowhere to put an
    email even by accident. A future ``subject_email VARCHAR(255)`` fails here, which is
    the point — the mistake this guards against is a well-meaning "make the ledger more
    useful" column, not malice.
    """
    conn = db_session.connection()
    textual = {
        c["name"]
        for c in inspect(conn).get_columns("erasure_ledger")
        if isinstance(c["type"], String)
    }
    assert textual == ENUM_COLUMNS, f"unexpected textual column(s): {textual - ENUM_COLUMNS}"

    bodies = [
        row[0]
        for row in conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'erasure_ledger'::regclass AND contype = 'c'"
            )
        )
    ]
    assert bodies, "the table has no CHECK constraints at all"
    unconstrained = [name for name in ENUM_COLUMNS if not any(name in b for b in bodies)]
    assert not unconstrained, f"textual column(s) with no CHECK behind them: {unconstrained}"


def test_the_counters_check_rejects_a_string_from_the_service_layer_too(db_session):
    """Belt and braces: the DB refuses even if the calling code stops filtering.

    ``tests/unit/test_v389_migration_consistency.py`` asserts the CHECK on raw SQL. This
    one goes through the ORM, which is the path a future caller would actually take.
    """
    from sqlalchemy.exc import IntegrityError

    entry = ErasureLedgerEntry(
        subject_type="user",
        subject_user_id=999_999,
        status="pending",
        actor_kind="system",
        requested_at=datetime.now(UTC),
        sla_due_at=datetime.now(UTC) + timedelta(days=30),
        counters={"target_email": "alice@example.com"},
    )
    db_session.add(entry)
    try:
        with pytest.raises(IntegrityError, match="ck_erasure_ledger_counters_numeric"):
            db_session.commit()
    finally:
        db_session.rollback()


def test_numeric_counters_drops_the_error_list_and_every_string(db_session):
    """The service-layer half of the same rule.

    ``summary["errors"]`` entries carry file UUIDs and raw driver messages, which quote
    storage paths and filenames. Only the COUNT survives. ``db_session`` is requested so
    this runs in the same DB-backed suite as its sibling rather than drifting into a
    module the gate deselects.
    """
    assert db_session is not None
    summary = {
        "subject": "user",
        "media_files_deleted": 3,
        "voiceprints_deleted": 0,
        "complete": False,
        "already_erased": False,
        "errors": [{"file_uuid": "abc", "error": "/data/alice/board-meeting.mp4 missing"}],
        "ledger_uuid": None,
    }

    kept = ledger._numeric_counters(summary)

    assert kept == {"media_files_deleted": 3, "voiceprints_deleted": 0}
    assert "errors" not in kept
    # `complete`/`already_erased` are bools; bool is an int subclass in Python, so a
    # naive isinstance check would have let them through and the CHECK would then have
    # rejected the whole INSERT at runtime.
    assert "complete" not in kept
    assert "already_erased" not in kept


# ------------------------------------------------------------------ lifecycle


def test_record_request_opens_a_pending_entry_with_the_sla_clock_started(db_session):
    """The SLA used to be a constant echoed into a response and measured by nothing."""
    user = _mk_user(db_session)
    before = datetime.now(UTC)

    entry = ledger.record_request(
        db_session,
        subject_type="user",
        subject_user_id=int(user.id),
        subject_user_uuid=user.uuid,
        actor_kind="super_admin",
        actor_user_id=int(user.id),
    )

    assert entry is not None
    assert entry.status == "pending"
    assert entry.attempts == 0
    delta = entry.sla_due_at - entry.requested_at
    assert delta == timedelta(days=ledger.ERASURE_SLA_DAYS)
    assert entry.sla_due_at > before


@pytest.mark.parametrize(
    ("summary_extra", "expected_status", "expected_reason"),
    [
        ({}, "complete", None),
        ({"legal_holds_skipped": 1}, "deferred", "legal_hold"),
        ({"errors": [{"error": "storage unreachable"}]}, "deferred", "error"),
        # A hold wins over a plain error: it is the one with a named legal basis and a
        # different remedy (wait for the hold to lift, not retry the store).
        (
            {"legal_holds_skipped": 2, "errors": [{"error": "x"}, {"error": "y"}]},
            "deferred",
            "legal_hold",
        ),
    ],
)
def test_record_outcome_derives_the_status_from_the_summary(
    db_session, summary_extra, expected_status, expected_reason
):
    """Derived, never passed in — the ledger cannot disagree with what the caller saw.

    A deferral recorded as ``complete`` would be skipped by the sweep forever while the
    compliance record claimed the data was gone.
    """
    user = _mk_user(db_session)
    entry = ledger.record_request(
        db_session, subject_type="user", subject_user_id=int(user.id), subject_user_uuid=user.uuid
    )
    assert entry is not None

    summary = {"media_files_deleted": 2, "legal_holds_skipped": 0, "errors": []}
    summary.update(summary_extra)

    ledger.record_outcome(db_session, entry, summary)

    assert entry.status == expected_status
    assert entry.deferred_reason == expected_reason
    assert entry.attempts == 1
    assert entry.counters["media_files_deleted"] == 2
    if expected_status == "complete":
        assert entry.completed_at is not None
    else:
        assert entry.completed_at is None


def test_open_entries_returns_everything_unfinished_and_completed_entries_does_not(db_session):
    """The two populations the sweep works on must not overlap or leak into each other."""
    user = _mk_user(db_session)
    done = ledger.record_request(
        db_session, subject_type="user", subject_user_id=int(user.id), subject_user_uuid=user.uuid
    )
    deferred = ledger.record_request(
        db_session, subject_type="user", subject_user_id=int(user.id), subject_user_uuid=user.uuid
    )
    assert done is not None and deferred is not None
    ledger.record_outcome(db_session, done, {"errors": [], "legal_holds_skipped": 0})
    ledger.record_outcome(db_session, deferred, {"errors": [], "legal_holds_skipped": 1})

    open_uuids = {e.uuid for e in ledger.open_entries(db_session, limit=500)}
    complete_uuids = {e.uuid for e in ledger.completed_entries(db_session, limit=500)}

    assert deferred.uuid in open_uuids
    assert done.uuid not in open_uuids
    assert done.uuid in complete_uuids
    assert deferred.uuid not in complete_uuids


def test_overdue_entries_finds_an_open_request_past_its_deadline(db_session):
    """Art. 12(3) is one month. An entry past it is a reportable breach, so it must be
    findable — and a COMPLETED entry past the same date must not be, or the count is
    noise nobody will act on."""
    user = _mk_user(db_session)
    stale = ledger.record_request(
        db_session, subject_type="user", subject_user_id=int(user.id), subject_user_uuid=user.uuid
    )
    finished = ledger.record_request(
        db_session, subject_type="user", subject_user_id=int(user.id), subject_user_uuid=user.uuid
    )
    assert stale is not None and finished is not None
    long_ago = datetime.now(UTC) - timedelta(days=1)
    stale.sla_due_at = long_ago
    finished.sla_due_at = long_ago
    ledger.record_outcome(db_session, finished, {"errors": [], "legal_holds_skipped": 0})
    db_session.commit()

    overdue = {e.uuid for e in ledger.overdue_entries(db_session)}

    assert stale.uuid in overdue
    assert finished.uuid not in overdue


# -------------------------------------------------------------------- journal


def test_the_journal_line_carries_surrogate_keys_and_nothing_else(db_session, journal_dir):
    """Same rule as the table, applied to the file that outlives it.

    A journal that quoted the subject's email would be the erasure's own leak, sitting
    on disk precisely because it is designed not to be deleted.
    """
    user = _mk_user(db_session, "journal")
    entry = ledger.record_request(
        db_session, subject_type="user", subject_user_id=int(user.id), subject_user_uuid=user.uuid
    )
    assert entry is not None

    lines = ledger.journal_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, f"expected exactly one journal line, got {len(lines)}"
    record = json.loads(lines[0])

    assert record["uuid"] == str(entry.uuid)
    assert record["subject_user_id"] == int(user.id)
    assert user.email not in lines[0]
    assert str(user.full_name) not in lines[0]
    # Positive control on the exclusion: the line is not empty of content, it just has
    # none of the subject's own data in it.
    assert set(record) == {
        "uuid",
        "subject_type",
        "subject_user_id",
        "subject_user_uuid",
        "subject_organization_id",
        "subject_organization_uuid",
        "status",
        "actor_kind",
        "requested_at",
        "sla_due_at",
    }


def test_restore_from_journal_reopens_an_entry_the_database_lost(db_session, journal_dir):
    """The backup-restore case, simulated exactly: the row is gone, the journal is not.

    Restoring a dump taken before an erasure removes the ledger row along with the
    subject's data. Deleting the row here reproduces that state precisely, and the
    journal — which is on the data volume, not in the dump — is what brings it back.
    """
    user = _mk_user(db_session, "restore")
    entry = ledger.record_request(
        db_session, subject_type="user", subject_user_id=int(user.id), subject_user_uuid=user.uuid
    )
    assert entry is not None
    original_uuid, original_requested = entry.uuid, entry.requested_at
    ledger.record_outcome(db_session, entry, {"errors": [], "legal_holds_skipped": 0})

    db_session.query(ErasureLedgerEntry).filter(ErasureLedgerEntry.uuid == original_uuid).delete()
    db_session.commit()
    assert (
        db_session.query(ErasureLedgerEntry)
        .filter(ErasureLedgerEntry.uuid == original_uuid)
        .first()
        is None
    ), "the pre-condition did not hold — the row was not actually removed"

    restored_count = ledger.restore_from_journal(db_session)

    assert restored_count == 1
    revived = (
        db_session.query(ErasureLedgerEntry)
        .filter(ErasureLedgerEntry.uuid == original_uuid)
        .first()
    )
    assert revived is not None
    # Re-opened as pending so the sweep re-runs the erasure...
    assert revived.status == "pending"
    # ...but the Art. 12(3) clock is NOT reset. A restore must not buy another month.
    assert revived.requested_at == original_requested


def test_restore_from_journal_is_idempotent(db_session, journal_dir):
    """It runs on every sweep tick, so a second pass must add nothing."""
    user = _mk_user(db_session, "idem")
    entry = ledger.record_request(
        db_session, subject_type="user", subject_user_id=int(user.id), subject_user_uuid=user.uuid
    )
    assert entry is not None

    first = ledger.restore_from_journal(db_session)
    second = ledger.restore_from_journal(db_session)

    assert first == 0, "the entry is still in the database; nothing should be restored"
    assert second == 0
    assert (
        db_session.query(ErasureLedgerEntry).filter(ErasureLedgerEntry.uuid == entry.uuid).count()
        == 1
    )


def test_a_corrupt_journal_line_does_not_stop_the_rest(db_session, journal_dir):
    """One bad line must not cost every other subject their re-erasure."""
    user = _mk_user(db_session, "corrupt")
    entry = ledger.record_request(
        db_session, subject_type="user", subject_user_id=int(user.id), subject_user_uuid=user.uuid
    )
    assert entry is not None
    path = ledger.journal_path()
    good_line = path.read_text(encoding="utf-8")
    path.write_text("{not json at all\n" + good_line, encoding="utf-8")

    db_session.query(ErasureLedgerEntry).filter(ErasureLedgerEntry.uuid == entry.uuid).delete()
    db_session.commit()

    assert ledger.restore_from_journal(db_session) == 1


def test_an_unwritable_journal_does_not_abort_the_erasure_record(db_session, tmp_path, monkeypatch):
    """The deletion is the legally binding act; a journal failure must never block it.

    Pointed at a path that cannot be a directory (an existing FILE), so ``mkdir`` raises
    for real rather than being mocked into raising.
    """
    from app.core.config import settings

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "DATA_DIR", blocker)

    user = _mk_user(db_session, "unwritable")
    user_id = int(user.id)
    entry = ledger.record_request(
        db_session, subject_type="user", subject_user_id=user_id, subject_user_uuid=user.uuid
    )

    assert entry is not None, "a journal failure must not prevent the ledger row"
    persisted = (
        db_session.query(ErasureLedgerEntry).filter(ErasureLedgerEntry.uuid == entry.uuid).one()
    )
    assert persisted.status == "pending"
    assert persisted.subject_user_id == user_id
    # The journal really did fail — otherwise this test would pass while proving nothing.
    assert not ledger.journal_path().exists()
