"""GDPR Art. 17 erasure reconciliation sweep (issue #442).

The erasure service destroys data and records the request in ``erasure_ledger``.
This task is the other half: it finishes the erasures that could not finish, and it
notices the ones that were undone. Two populations, one pass:

**1. Open entries — the erasure did not complete.**
A file under a legal hold is skipped (Art. 17(3)(e)), and while one exists the
``user`` row cannot be deleted at all (``media_file.user_id`` is a plain ``NO ACTION``
FK). ``_purge_files``' docstring has always claimed "a later hold release re-runs the
idempotent erasure to finish the job" — and nothing did. ``release_file`` cleared the
hold and never called back, so the subject's data was retained indefinitely with
nothing tracking it. This sweep is what makes that sentence true.

Also picked up here: an entry left ``pending`` by a worker that died mid-erasure, and
one left ``deferred``/``error`` by a transient object-store or OpenSearch outage. All
three are the same fix, because the erasure is idempotent.

**2. Completed entries whose subject is alive again — a backup restore.**
Restoring a dump taken before an erasure brings the subject back. The sweep re-checks
each completed entry against the live schema and re-erases anything that is present
again, matching on **both** the id and the UUID: a restore rewinds the id sequence, so
a later account can be issued an id the erased subject once held, and matching on id
alone would erase an innocent bystander.

Why a sweep and not only a hook
-------------------------------
``takedown_service.release_file`` *does* call ``notify_hold_released`` below, so the
common case is prompt. The sweep is what makes it **guaranteed**: a hold can also be
cleared by an admin editing the row, by a restore rolling the flag back, or by a
process that died between clearing the hold and dispatching. A hook alone is an
optimisation with a silent failure mode; a hook plus an idempotent sweep is a
mechanism.

**Dying mid-sweep costs nothing.** A short session decides the work and returns plain
ids; each entry is then handled in its own transaction, and the erasure it runs is
idempotent. So an interrupted pass re-does at most one entry's work on the next tick.
Nothing is marked complete that is not complete — the status is derived from the fresh
summary every time.

That split is also the session-lifetime rule (``app/tasks/CLAUDE.md``): an erasure does
object-storage and OpenSearch deletes, and one session held open across a hundred of
them would hold ``ACCESS SHARE`` for the whole pass and queue any ``ALTER TABLE``,
including an Alembic upgrade, behind it.

Community-edition invariance: the ledger is empty until someone requests an erasure,
so this is two indexed queries returning nothing.
"""

import logging
from typing import TYPE_CHECKING
from typing import Any

from app.core.celery import celery_app
from app.core.constants import UtilityPriority

if TYPE_CHECKING:  # heavy model import kept out of the worker's import path
    from app.models.media import MediaFile

logger = logging.getLogger(__name__)

#: Bound the work per tick. A deployment that restored an old dump could have a large
#: resurrection population; the sweep simply picks up the rest next time.
MAX_OPEN_ENTRIES_PER_RUN = 100

#: Completed entries re-checked for resurrection per tick, newest first. Cheap (one
#: indexed existence probe each), but not unbounded.
MAX_COMPLETED_CHECKS_PER_RUN = 500


def _subject_is_alive(db, entry) -> bool:
    """Is the subject this entry says was erased present in the database again?

    Only answerable for the two scopes that delete the subject row itself:

    - ``user`` — the account row was deleted, so its reappearance is unambiguous.
    - ``organization`` — likewise for the tenant row.

    **``org_member`` deliberately returns False**, and that is a judgement rather than
    an omission. That scope never deletes the ``user`` row, so "does the subject exist"
    is always True and cannot distinguish a restore from normal life. The only
    available signal — "does this member have org-stamped rows again?" — is also what a
    member legitimately uploading new files to the tenant the next day looks like, and
    acting on it would destroy data nobody asked to erase. So org-member entries are
    reported for **manual review** instead (``manual_review`` in the result) and never
    auto-re-erased.

    Both surviving cases match on id AND uuid: a restored dump rewinds the id sequence.
    """
    from app.models.organization import Organization
    from app.models.user import User

    if entry.subject_type == "user":
        if entry.subject_user_id is None or entry.subject_user_uuid is None:
            return False
        return (
            db.query(User.id)
            .filter(User.id == entry.subject_user_id, User.uuid == entry.subject_user_uuid)
            .first()
            is not None
        )
    if entry.subject_type == "organization":
        if entry.subject_organization_id is None or entry.subject_organization_uuid is None:
            return False
        return (
            db.query(Organization.id)
            .filter(
                Organization.id == entry.subject_organization_id,
                Organization.uuid == entry.subject_organization_uuid,
            )
            .first()
            is not None
        )
    return False


def _rerun(db, entry) -> dict[str, Any]:
    """Re-run the erasure this entry describes, updating the entry in place.

    The existing entry is passed down so the retry updates it rather than opening a
    second one — an SLA clock that restarted on every tick would never expire, which is
    precisely the failure mode the ledger exists to prevent.
    """
    from app.services.gdpr_erasure_service import erase_org_member_data
    from app.services.gdpr_erasure_service import erase_organization
    from app.services.gdpr_erasure_service import erase_user

    if entry.subject_type == "user":
        return erase_user(db, int(entry.subject_user_id), ledger_entry=entry)
    if entry.subject_type == "organization":
        return erase_organization(db, int(entry.subject_organization_id), ledger_entry=entry)
    return erase_org_member_data(
        db,
        int(entry.subject_user_id),
        int(entry.subject_organization_id),
        ledger_entry=entry,
    )


@celery_app.task(
    bind=True,
    name="gdpr.erasure_reconcile",
    priority=UtilityPriority.BACKGROUND,
)
def erasure_reconciliation_sweep(self) -> dict:  # noqa: ARG001 — bind=True signature
    """Finish deferred erasures and re-erase resurrected subjects.

    Returns:
        Counters for the pass: how many entries were re-opened from the on-disk
        journal (non-zero means the database was rolled back past an erasure), how
        many open entries were retried and how many of those completed, how many
        resurrections were found and re-erased, how many org-member entries need
        manual review, and how many entries are past their Art. 12(3) deadline
        (``overdue`` — a reportable breach, not a housekeeping number).
    """
    from app.db.session_utils import session_scope
    from app.models.erasure import ErasureLedgerEntry
    from app.services import erasure_ledger_service as ledger

    retried = completed = resurrected = failures = 0
    manual_review = 0

    # Phase 1 — a SHORT session that decides the work and returns plain ids.
    # An erasure does object-storage and OpenSearch deletes, and holding one session
    # open across all of them would take ACCESS SHARE on every table it touched for the
    # whole pass, queueing any ALTER TABLE (i.e. an Alembic upgrade) behind it. See
    # app/tasks/CLAUDE.md — this package's single most repeated defect. ORM instances
    # must not escape either: a detached row can lazy-load and silently reopen a
    # transaction, which reintroduces the bug invisibly.
    with session_scope() as db:
        # FIRST: re-open any entry the database has lost but the on-disk journal still
        # has. That is the signature of a restore from a dump taken BEFORE an erasure —
        # subject rows back, ledger row gone — and it is the one case an in-database
        # ledger cannot detect about itself.
        restored = ledger.restore_from_journal(db)
        open_ids = [int(e.id) for e in ledger.open_entries(db, limit=MAX_OPEN_ENTRIES_PER_RUN)]
        completed_ids = [
            int(e.id) for e in ledger.completed_entries(db, limit=MAX_COMPLETED_CHECKS_PER_RUN)
        ]

    # Phase 2 — one session per entry. Dying mid-sweep therefore loses at most one
    # entry's progress, and the erasure it runs is idempotent, so the next tick redoes
    # exactly that entry and nothing else.
    for entry_id in open_ids:
        retried += 1
        try:
            with session_scope() as db:
                entry = db.get(ErasureLedgerEntry, entry_id)
                if entry is None:
                    continue
                summary = _rerun(db, entry)
                if summary.get("complete") and not summary.get("legal_holds_skipped"):
                    completed += 1
        except Exception as e:  # noqa: BLE001 — one bad entry must not stop the pass
            failures += 1
            logger.error("Erasure reconciliation failed for ledger entry %s: %s", entry_id, e)

    for entry_id in completed_ids:
        try:
            with session_scope() as db:
                entry = db.get(ErasureLedgerEntry, entry_id)
                if entry is None or entry.status != "complete":
                    continue
                if entry.subject_type == "org_member":
                    manual_review += 1
                    continue
                if not _subject_is_alive(db, entry):
                    continue
                resurrected += 1
                ledger.record_resurrection(db, entry)
                _rerun(db, entry)
        except Exception as e:  # noqa: BLE001
            failures += 1
            logger.error("Re-erasure after resurrection failed for entry %s: %s", entry_id, e)

    with session_scope() as db:
        overdue = len(ledger.overdue_entries(db))

    if resurrected:
        logger.error(
            "GDPR: %d erased subject(s) were present again and have been re-erased — a "
            "restore replayed data past an Art. 17 erasure",
            resurrected,
        )
    if overdue:
        logger.error(
            "GDPR: %d erasure request(s) are past the Art. 12(3) one-month deadline",
            overdue,
        )

    return {
        "status": "ok",
        "journal_restored": restored,
        "open_retried": retried,
        "completed": completed,
        "resurrections_reerased": resurrected,
        "org_member_manual_review": manual_review,
        "failures": failures,
        "overdue": overdue,
        "truncated": retried >= MAX_OPEN_ENTRIES_PER_RUN,
    }


def notify_hold_released(file: "MediaFile") -> None:
    """Nudge the sweep after a legal hold is lifted. Never raises, never blocks.

    Called by ``takedown_service.release_file``. It dispatches the sweep rather than
    running an erasure inline for two reasons: the release is an admin HTTP request and
    an erasure is object-storage-and-OpenSearch work, and the sweep already knows how to
    decide *which* entries are now finishable — this function deliberately does not
    re-implement that decision.

    Dispatch failing is survivable by design: the scheduled sweep runs anyway, so a
    broker outage delays the completion rather than losing it. That is exactly why the
    hook is not the mechanism.
    """
    try:
        if not getattr(file, "user_id", None):
            return
        erasure_reconciliation_sweep.delay()
        logger.info(
            "Legal hold released on file %s — erasure reconciliation dispatched",
            getattr(file, "uuid", "?"),
        )
    except Exception as e:  # noqa: BLE001 — the release must never fail on this
        logger.warning("Could not dispatch erasure reconciliation after hold release: %s", e)
