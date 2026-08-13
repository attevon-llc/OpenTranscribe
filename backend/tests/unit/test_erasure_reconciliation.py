"""The GDPR erasure reconciliation sweep (issue #442).

Two failures this suite exists to keep fixed, both of which shipped:

**A legal-hold deferral was permanently forgotten.** ``_purge_files`` skips a file under
an Art. 17(3)(e) hold, and while one exists the ``user`` row cannot be deleted at all
(``media_file.user_id`` is a plain ``NO ACTION`` FK). Its docstring claimed "a later hold
release re-runs the idempotent erasure to finish the job" — and ``release_file`` cleared
the hold and never called back. So the retention became indefinite, and nothing tracked
it. ``test_the_sweep_finishes_the_erasure_once_the_hold_is_released`` is that fix.

**A backup restore resurrected erased subjects.** The sweep re-checks completed entries
against the live schema. ``test_a_resurrected_user_is_re_erased_and_counted`` is that fix
and ``test_an_id_match_with_a_different_uuid_is_not_treated_as_a_resurrection`` is its
control — a restore rewinds the id sequence, so matching on id alone would erase a
bystander who was merely issued a recycled id.

Storage and OpenSearch are patched out; **the relational deletion is not**, because the
`user` row surviving or not is the whole assertion. The sweep's session is bridged onto
the savepoint session (see ``_bridged_sweep``) exactly as ``test_chat_endpoints.py``
does — the task deliberately opens its own scope, which under the savepoint harness
cannot see rows this test has not committed to the outer transaction.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.models.erasure import ErasureLedgerEntry
from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.user import User
from app.services import erasure_ledger_service as ledger


def _mk_user(db, label: str = "sweep", *, role: str = "user") -> User:
    """Create a user. ``is_superuser`` is derived, never set independently.

    ``ck_user_superuser_matches_role`` is ``is_superuser = (role = 'super_admin')``, so
    an ``admin`` with ``is_superuser=True`` is a CheckViolation, not a stronger admin.
    """
    from app.core.security import get_password_hash

    user = User(
        email=f"{label}_{uuid_pkg.uuid4().hex[:10]}@example.com",
        full_name=f"{label} user",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        is_superuser=(role == "super_admin"),
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _mk_org(db, label: str = "sweep") -> Organization:
    org = Organization(
        external_org_id=f"org_{label}_{uuid_pkg.uuid4().hex[:8]}",
        name=f"{label} Org",
        is_active=True,
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _mk_file(db, *, user: User, legal_hold: bool = False, org_id: int | None = None) -> MediaFile:
    fuuid = uuid_pkg.uuid4()
    media = MediaFile(
        uuid=fuuid,
        filename=f"f_{str(fuuid)[:8]}.mp4",
        storage_path=f"media/test/{fuuid}.mp4",
        content_type="video/mp4",
        file_size=1000,
        user_id=user.id,
        organization_id=org_id,
        status="completed",
        legal_hold=legal_hold,
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


@pytest.fixture()
def quiet_stores():
    """Patch object storage / OpenSearch, leaving every relational delete real."""
    with (
        patch(
            "app.services.file_cleanup_service.delete_file_storage_artifacts",
            return_value=True,
        ),
        patch(
            "app.services.file_cleanup_service._cleanup_opensearch_for_file",
            return_value=[],
        ),
        patch("app.services.gdpr_erasure_service._erase_speaker_voiceprints", return_value=0),
        patch("app.services.opensearch_service.remove_profile_embedding", return_value=True),
        patch("app.services.gdpr_erasure_service.audit_logger"),
        patch("app.services.erasure_ledger_service.audit_logger"),
    ):
        yield


@pytest.fixture()
def sweep(db_session, quiet_stores, tmp_path, monkeypatch):
    """Run the real sweep task body against the savepoint session.

    ``session_scope`` is bridged rather than mocked away: the task's own logic — which
    entries it selects, what it re-runs, what it counts — is exactly what is under test.
    ``DATA_DIR`` is redirected so the journal write lands in ``tmp_path``.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    assert str(ledger.journal_path()).startswith(str(tmp_path))

    @contextmanager
    def _test_session_scope():
        yield db_session
        db_session.commit()

    def _run():
        from app.tasks.erasure_reconciliation import erasure_reconciliation_sweep

        with patch("app.db.session_utils.session_scope", _test_session_scope):
            return erasure_reconciliation_sweep.run()

    return _run


# ------------------------------------------------------- legal-hold deferral


def test_a_legal_hold_defers_the_erasure_and_the_ledger_records_why(db_session, quiet_stores):
    """The pre-condition for everything below: the hold really does stop the erasure."""
    from app.services.gdpr_erasure_service import erase_user

    user = _mk_user(db_session, "held")
    _mk_file(db_session, user=user, legal_hold=True)
    user_id = int(user.id)

    summary = erase_user(db_session, user_id)

    assert summary["legal_holds_skipped"] == 1
    assert summary["users_deleted"] == 0
    assert db_session.query(User).filter(User.id == user_id).first() is not None
    assert summary["ledger_uuid"] is not None, "the deferral was not recorded anywhere"

    entry = (
        db_session.query(ErasureLedgerEntry)
        .filter(ErasureLedgerEntry.uuid == uuid_pkg.UUID(summary["ledger_uuid"]))
        .first()
    )
    assert entry is not None
    assert entry.status == "deferred"
    assert entry.deferred_reason == "legal_hold"
    assert entry.legal_holds_outstanding == 1


def test_the_sweep_finishes_the_erasure_once_the_hold_is_released(db_session, sweep):
    """**The bug.** Before #442 nothing re-ran the erasure and the account lived forever.

    Note what is asserted: not "the sweep ran", but that the ``user`` row, which the FK
    kept alive through the deferral, is *gone* — and that the ledger now says complete.
    """
    from app.services.gdpr_erasure_service import erase_user

    user = _mk_user(db_session, "released")
    media = _mk_file(db_session, user=user, legal_hold=True)
    user_id = int(user.id)
    erase_user(db_session, user_id)
    assert db_session.query(User).filter(User.id == user_id).first() is not None

    # The hold lifts (what release_file does to the row).
    media.legal_hold = False
    db_session.commit()

    result = sweep()

    assert result["open_retried"] >= 1
    assert db_session.query(User).filter(User.id == user_id).first() is None, (
        "the sweep did not finish the deferred erasure — the account survived the "
        "release of the only thing justifying its retention"
    )
    entry = (
        db_session.query(ErasureLedgerEntry)
        .filter(ErasureLedgerEntry.subject_user_id == user_id)
        .first()
    )
    assert entry is not None
    assert entry.status == "complete"
    assert entry.completed_at is not None


def test_the_sweep_leaves_a_still_held_erasure_deferred(db_session, sweep):
    """The control. A sweep that completed everything it touched would pass the test
    above while destroying the evidence a live legal hold exists to preserve."""
    from app.services.gdpr_erasure_service import erase_user

    user = _mk_user(db_session, "stillheld")
    _mk_file(db_session, user=user, legal_hold=True)
    user_id = int(user.id)
    erase_user(db_session, user_id)

    sweep()

    assert db_session.query(User).filter(User.id == user_id).first() is not None
    entry = (
        db_session.query(ErasureLedgerEntry)
        .filter(ErasureLedgerEntry.subject_user_id == user_id)
        .first()
    )
    assert entry is not None
    assert entry.status == "deferred"
    # Retried, not reset: the SLA clock must keep running while the hold stands.
    assert entry.attempts >= 2


def test_a_retry_updates_the_original_entry_rather_than_opening_a_second(db_session, sweep):
    """A new entry per tick would restart the Art. 12(3) clock, so it could never expire —
    the exact failure the ledger exists to prevent, reintroduced by the fix for it."""
    from app.services.gdpr_erasure_service import erase_user

    user = _mk_user(db_session, "onerow")
    _mk_file(db_session, user=user, legal_hold=True)
    user_id = int(user.id)
    erase_user(db_session, user_id)
    original = (
        db_session.query(ErasureLedgerEntry)
        .filter(ErasureLedgerEntry.subject_user_id == user_id)
        .one()
    )
    original_due = original.sla_due_at

    sweep()
    sweep()

    rows = (
        db_session.query(ErasureLedgerEntry)
        .filter(ErasureLedgerEntry.subject_user_id == user_id)
        .all()
    )
    assert len(rows) == 1, f"the sweep opened {len(rows)} entries for one request"
    assert rows[0].sla_due_at == original_due


def test_releasing_a_legal_hold_dispatches_the_sweep(db_session):
    """The prompt path. The scheduled sweep is the guarantee; this is the latency fix.

    Asserted on the dispatch AND on the row's state, so a hook that fired while failing
    to clear the hold could not pass.
    """
    from app.services.takedown_service import release_file

    admin = _mk_user(db_session, "admin", role="admin")
    owner = _mk_user(db_session, "owner")
    media = _mk_file(db_session, user=owner, legal_hold=True)
    media.is_quarantined = True
    db_session.commit()

    with (
        patch("app.tasks.erasure_reconciliation.erasure_reconciliation_sweep.delay") as dispatch,
        patch("app.services.takedown_service.audit_logger"),
        patch("app.services.takedown_service._notify_owner_release"),
        patch("app.services.minio_service.set_object_legal_hold", return_value=True),
    ):
        release_file(db_session, media, admin=admin, clear_legal_hold=True)

    assert dispatch.call_count == 1
    db_session.refresh(media)
    assert bool(media.legal_hold) is False


def test_releasing_without_clearing_the_hold_does_not_dispatch(db_session):
    """The control for the hook: a release that KEEPS the hold has unblocked nothing."""
    from app.services.takedown_service import release_file

    admin = _mk_user(db_session, "admin2", role="admin")
    owner = _mk_user(db_session, "owner2")
    media = _mk_file(db_session, user=owner, legal_hold=True)
    media.is_quarantined = True
    db_session.commit()

    with (
        patch("app.tasks.erasure_reconciliation.erasure_reconciliation_sweep.delay") as dispatch,
        patch("app.services.takedown_service.audit_logger"),
        patch("app.services.takedown_service._notify_owner_release"),
    ):
        release_file(db_session, media, admin=admin, clear_legal_hold=False)

    assert dispatch.call_count == 0
    db_session.refresh(media)
    assert bool(media.legal_hold) is True


# ---------------------------------------------------------- restore / resurrection


def test_a_resurrected_user_is_re_erased_and_counted(db_session, sweep):
    """A restore replayed the subject back. The completed entry must notice and act.

    Built the way a restore actually leaves the database: the ledger says the erasure
    completed, and the ``user`` row (id AND uuid) is present again.
    """
    user = _mk_user(db_session, "revenant")
    user_id, user_uuid = int(user.id), user.uuid
    entry = ledger.record_request(
        db_session, subject_type="user", subject_user_id=user_id, subject_user_uuid=user_uuid
    )
    assert entry is not None
    ledger.record_outcome(db_session, entry, {"errors": [], "legal_holds_skipped": 0})
    assert entry.status == "complete"

    result = sweep()

    assert result["resurrections_reerased"] == 1
    assert db_session.query(User).filter(User.id == user_id).first() is None
    db_session.refresh(entry)
    assert entry.resurrections_detected == 1
    assert entry.last_resurrection_at is not None


def test_an_id_match_with_a_different_uuid_is_not_treated_as_a_resurrection(db_session, sweep):
    """The bystander control, and the reason the UUID is stored at all.

    A restored dump rewinds the id sequence, so a NEW account can later be issued an id
    an erased subject once held. Matching on id alone would erase that person's data on
    the next sweep, which is worse than the bug being fixed.
    """
    bystander = _mk_user(db_session, "bystander")
    entry = ledger.record_request(
        db_session,
        subject_type="user",
        subject_user_id=int(bystander.id),
        subject_user_uuid=uuid_pkg.uuid4(),  # the ERASED subject's uuid, not this one's
    )
    assert entry is not None
    ledger.record_outcome(db_session, entry, {"errors": [], "legal_holds_skipped": 0})
    bystander_id = int(bystander.id)

    result = sweep()

    assert result["resurrections_reerased"] == 0
    assert db_session.query(User).filter(User.id == bystander_id).first() is not None
    db_session.refresh(entry)
    assert entry.resurrections_detected == 0


def test_an_org_member_entry_is_flagged_for_review_and_never_auto_re_erased(db_session, sweep):
    """The judgement call, pinned so it cannot be "tidied up" into a data-loss bug.

    An org-member erasure never deletes the ``user`` row, so "is the subject present"
    is always True and cannot distinguish a restore from normal life. The only other
    signal — "does this member have org rows again?" — is also what a member legitimately
    uploading to the tenant the next day looks like. So these are reported for manual
    review, and the member's later work is left alone.
    """
    org = _mk_org(db_session)
    member = _mk_user(db_session, "member")
    entry = ledger.record_request(
        db_session,
        subject_type="org_member",
        subject_user_id=int(member.id),
        subject_user_uuid=member.uuid,
        subject_organization_id=int(org.id),
        subject_organization_uuid=org.uuid,
    )
    assert entry is not None
    ledger.record_outcome(db_session, entry, {"errors": [], "legal_holds_skipped": 0})
    # Work the member did AFTER the erasure — a restore-detector that fired on "has org
    # rows" would destroy this.
    later = _mk_file(db_session, user=member, org_id=int(org.id))
    later_id = int(later.id)

    result = sweep()

    assert result["org_member_manual_review"] >= 1
    assert result["resurrections_reerased"] == 0
    assert db_session.query(MediaFile).filter(MediaFile.id == later_id).first() is not None


def test_the_sweep_reopens_an_entry_the_database_lost_and_re_erases(db_session, sweep):
    """End to end for the restore case an in-database ledger cannot detect about itself.

    Dump taken before the erasure → subject's rows are back AND the ledger row that
    recorded the erasure is gone. Only the on-disk journal, which is not in the dump,
    still knows. This is the single test that covers ledger + journal + sweep together.

    Note which branch does the work: the journal re-opens the entry as ``pending``, so
    it is finished by the OPEN pass, not by the resurrection check (that one only sees
    entries the database still records as ``complete``). Asserting on
    ``resurrections_reerased`` here would be asserting on the wrong mechanism.
    """
    from app.services.gdpr_erasure_service import erase_user

    user = _mk_user(db_session, "rollback")
    user_id, user_uuid = int(user.id), user.uuid
    erase_user(db_session, user_id)
    assert db_session.query(User).filter(User.id == user_id).first() is None

    # The restore: the ledger row disappears, the subject comes back with the same
    # surrogate keys (the dump predates both).
    db_session.query(ErasureLedgerEntry).filter(
        ErasureLedgerEntry.subject_user_id == user_id
    ).delete()
    db_session.add(
        User(
            id=user_id,
            uuid=user_uuid,
            email=f"rollback_{uuid_pkg.uuid4().hex[:10]}@example.com",
            hashed_password="x",
            is_active=True,
            is_superuser=False,
            role="user",
            auth_type="local",
        )
    )
    db_session.commit()
    assert db_session.query(User).filter(User.id == user_id).first() is not None

    result = sweep()

    assert result["journal_restored"] == 1
    assert result["open_retried"] >= 1
    assert db_session.query(User).filter(User.id == user_id).first() is None, (
        "the restored subject survived the sweep — a backup restore silently undid an "
        "Art. 17 erasure"
    )


def test_the_sweep_is_a_no_op_on_an_empty_ledger(db_session, sweep):
    """Community-edition invariance: a deployment that never erased anything.

    Also the guard against a sweep whose counters are computed from something other
    than the ledger — every number must be zero when there is nothing to do.
    """
    db_session.query(ErasureLedgerEntry).delete()
    db_session.commit()

    result = sweep()

    assert result["status"] == "ok"
    assert result["journal_restored"] == 0
    assert result["open_retried"] == 0
    assert result["resurrections_reerased"] == 0
    assert result["failures"] == 0
