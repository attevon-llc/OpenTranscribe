"""GDPR / right-to-erasure service — edition-neutral core capability.

The legacy ``user.deleted`` / ``organization.deleted`` paths only *deactivated*
(set ``is_active = False``), which is a data **freeze**, not the **erasure** GDPR
Art. 17 requires. This module provides the real cascade: object storage, the
relational rows, AND — critically — the **OpenSearch voiceprint indices**, which
hold biometric data (speaker embeddings) and are NOT reachable by a Postgres
``ON DELETE CASCADE``.

What gets erased
----------------
For a **user** (``erase_user``) or an **organization** (``erase_organization``):

1. **Object storage (S3 / MinIO):** every media file's original, thumbnail, and
   regenerable derived cache — via the canonical per-file destroy
   ``purge_media_file`` (the same single source of truth every delete path uses).
2. **Relational rows:** media files, transcript segments, speakers, speaker
   profiles, collections, comments, tags, prompts, user settings, etc. Per-file
   children go via ``purge_media_file``'s CASCADE; remaining owner-scoped rows
   (speaker profiles, collections) are deleted explicitly; for a *user* erasure
   the ``user`` row itself is finally deleted, whose FK CASCADEs sweep the rest.
3. **OpenSearch voiceprint / biometric indices:** a ``delete_by_query`` on the
   speaker indices (v3 + v4 + alias) scoped by ``user_id`` (user erasure) or
   ``organization_id`` (org erasure) removes any speaker/profile embedding doc
   not already swept by the per-file cleanup. This is the biometric-data
   guarantee the per-file path alone cannot make for orphaned docs.

The ledger (issue #442)
-----------------------
Every call opens a row in ``erasure_ledger`` **before** it destroys anything and
closes it afterwards (``services/erasure_ledger_service``). That row is what makes
three otherwise-invisible things work, and none of them existed before:

* **Art. 30 demonstrability.** The erasure used to leave a summary dict and a log
  line. The ledger entry is the durable record, and its ``uuid`` comes back in
  ``summary["ledger_uuid"]`` as a receipt.
* **Deferred work is finished.** A legal hold makes the erasure ``deferred``, and
  ``tasks/erasure_reconciliation`` re-runs it — on a schedule, and immediately when
  ``takedown_service.release_file`` lifts the hold. Before, the deferral was simply
  forgotten and the retention became permanent.
* **A restore cannot quietly undo an erasure.** The same sweep re-checks completed
  entries against the live schema and re-erases a subject that a restored dump
  brought back.

**What the ledger must never hold is the data it records the destruction of** —
see ``models/erasure``'s docstring. The table has no free-text column at all, and
this module passes it surrogate keys and integer counters only, never
``user.email``, never a filename, and never ``summary["errors"]``.

SLA
---
**30-day completion** (GDPR Art. 12(3) "without undue delay and in any event
within one month"). This service runs the erasure synchronously and is the
callable the cloud ``user.deleted`` / ``organization.deleted`` webhooks invoke;
the webhook handler is responsible only for *enqueueing* the call within the
window. The deadline is stamped on the ledger entry as ``sla_due_at``, so it is a
value something can be measured against rather than a constant echoed into a
response. The operation is **idempotent** (re-running on an already-erased subject
is a no-op that returns zeroed counters) and writes an ``admin.user.delete``
audit event on completion so the erasure itself is on the compliance trail.

Partial erasure
---------------
An erasure that could not destroy every copy is reported as one. One failing
store never aborts the others — the relational and storage deletions are the
legally-binding ones and must proceed — but every failure lands in
``summary["errors"]``, ``summary["complete"]`` goes False, and the audit event
is ``PARTIAL`` instead of ``SUCCESS``. This covers the object store, the
transcript document, the ``transcript_chunks`` RAG index, summaries, and the
voiceprint indices alike. Reporting a partial erasure as complete is worse than
a loud failure: the DB rows that identify *what* to re-delete are already gone.

Community-edition invariance
----------------------------
``erase_organization`` is only reachable with orgs present; in the
self-host/community edition ``organization_id`` is NULL everywhere, so the
org path never triggers. ``erase_user`` works in both editions (the speaker
``delete_by_query`` simply matches the user's personal docs).
"""

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.models.chat import ChatConversation
from app.models.custom_vocabulary import CustomVocabulary
from app.models.erasure import ErasureLedgerEntry
from app.models.media import Collection
from app.models.media import Comment
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import SpeakerCollection
from app.models.media import SpeakerProfile
from app.models.media import Tag
from app.models.media import Task
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.prompt import SummaryPrompt
from app.models.prompt import UserSetting
from app.models.user import User
from app.models.watch_source import WatchSource
from app.services import erasure_ledger_service as ledger
from app.services.file_cleanup_service import purge_media_file

logger = logging.getLogger(__name__)

# GDPR Art. 12(3): erasure must complete within one month of the request. Re-exported
# from the ledger service, which stamps every entry's `sla_due_at` with it — two
# independent copies of the deadline is how the reported SLA and the measured one drift.
ERASURE_SLA_DAYS = ledger.ERASURE_SLA_DAYS


def _erase_speaker_voiceprints(
    *,
    user_id: int | None = None,
    organization_id: int | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> int:
    """Delete speaker/profile embedding docs (biometric data) from OpenSearch.

    Scopes by ``user_id`` and/or ``organization_id`` (both given = the
    intersection, i.e. one member's docs within one org) and runs a
    ``delete_by_query`` across the v3 + v4 + alias speaker indices **and the
    legacy ``speakers_v3_backup``**. The backup index is not optional
    housekeeping: ``opensearch_service/indices._restore_v3_from_backup``
    reindexes it into ``speakers_v3`` whenever v3 is found empty, so on a small
    deployment erasing its last subject, the next ``ensure_indices_exist()``
    put their biometric embeddings straight back. Legacy installs only — the
    index does not exist on a fresh one, and ``indices.exists`` skips it.
    Best-effort: a down/absent OpenSearch never blocks the relational +
    storage erasure (those are the legally-binding deletions; this catches
    orphaned biometric docs).

    Args:
        user_id: Scope the deletion to one user.
        organization_id: Scope the deletion to one organization.
        errors: The caller's ``summary["errors"]`` list. Failures are appended
            here so the erasure is audited as PARTIAL rather than SUCCESS —
            biometric data surviving an Art. 17 request must not be recorded as
            a completed erasure.

    Returns the number of voiceprint documents deleted (0 if OpenSearch is
    unavailable).
    """
    if user_id is None and organization_id is None:
        return 0

    def _record(reason: str) -> None:
        if errors is not None:
            errors.append(
                {
                    "stage": "voiceprints",
                    "user_id": user_id,
                    "org_id": organization_id,
                    "error": reason,
                }
            )

    deleted = 0
    try:
        from app.core.constants import get_speaker_index
        from app.core.constants import get_speaker_index_v3
        from app.core.constants import get_speaker_index_v3_backup
        from app.core.constants import get_speaker_index_v4
        from app.services.opensearch_service import opensearch_client

        if opensearch_client is None:
            logger.warning("OpenSearch unavailable — skipping voiceprint erasure (orphan docs)")
            _record("OpenSearch client unavailable")
            return 0

        terms = []
        if user_id is not None:
            terms.append({"term": {"user_id": user_id}})
        if organization_id is not None:
            terms.append({"term": {"organization_id": organization_id}})
        body = {"query": {"bool": {"filter": terms}}}

        indices = {
            get_speaker_index(),
            get_speaker_index_v3(),
            get_speaker_index_v4(),
            get_speaker_index_v3_backup(),
        }
        for idx in indices:
            try:
                if not opensearch_client.indices.exists(index=idx):
                    continue
                resp = opensearch_client.delete_by_query(
                    index=idx,
                    body=body,
                    refresh=True,
                    conflicts="proceed",
                )
                deleted += int(resp.get("deleted", 0))
                # delete_by_query reports per-document failures in the BODY
                # rather than raising, so a partial sweep of biometric docs
                # would otherwise look identical to a complete one.
                failures = (resp or {}).get("failures") or []
                if failures:
                    _record(f"index {idx}: {len(failures)} document(s) failed to delete")
            except Exception as idx_err:  # noqa: BLE001 — best-effort per index
                logger.warning(f"Voiceprint erasure failed on index {idx}: {idx_err}")
                _record(f"index {idx}: {idx_err}")
    except Exception as e:  # noqa: BLE001 — OpenSearch optional
        logger.warning(f"Voiceprint erasure skipped (OpenSearch error): {e}")
        _record(str(e))

    if deleted:
        logger.info(
            f"Erased {deleted} voiceprint doc(s) for "
            f"{'org ' + str(organization_id) if organization_id else 'user ' + str(user_id)}"
        )
    return deleted


def _erase_profile_embedding(profile_uuid: str, errors: list[dict[str, Any]]) -> None:
    """Remove one speaker profile's embedding doc, recording a failure. Never raises.

    This used to be ``contextlib.suppress(Exception)`` at all three call sites, which is
    the same defect already fixed for the transcript document: a biometric embedding
    that could not be deleted made the erasure report SUCCESS, and the DB row naming
    which document to retry was destroyed in the same pass. Best-effort is right;
    silent is not.
    """
    try:
        from app.services.opensearch_service import remove_profile_embedding

        remove_profile_embedding(profile_uuid)
    except Exception as e:  # noqa: BLE001 — record, never abort the relational delete
        logger.warning(f"Profile embedding removal failed for {profile_uuid}: {e}")
        errors.append({"stage": "profile_embedding", "profile_uuid": profile_uuid, "error": str(e)})


def _purge_files(db: Session, files: list[MediaFile], summary: dict[str, Any]) -> None:
    """Run the canonical per-file destroy for every file, accumulating counters.

    Files under an active legal hold are SKIPPED, not destroyed: GDPR
    Art. 17(3)(e) exempts data retained for legal claims, and the takedown
    flow's evidence-preservation guarantee rests on the DB flag (S3
    object-lock is best-effort only). Skips are reported in the summary so
    the erasure is auditable as partial, and the deferral is recorded in the
    **erasure ledger** (``models/erasure``) so a later hold release re-runs the
    idempotent erasure to finish the job. That last clause used to be a claim this
    docstring made and nothing implemented — ``takedown_service.release_file``
    cleared the hold and never called back, so the deferral was permanent
    (issue #442). ``tasks/erasure_reconciliation`` is what makes it true.

    ``purge_media_file``'s ``residual_errors`` are surfaced for the same reason:
    a deleted DB row with the transcript, its RAG chunks or the media object
    still in place is a partial erasure, and reading ``deleted: True`` alone
    reported it as a complete one.
    """
    for media_file in files:
        if bool(media_file.legal_hold):
            summary["legal_holds_skipped"] += 1
            summary["errors"].append(
                {
                    "file_uuid": str(media_file.uuid),
                    "error": "skipped: active legal hold (GDPR Art. 17(3)(e))",
                }
            )
            continue
        result = purge_media_file(db, media_file)
        summary["errors"].extend(result.get("residual_errors") or [])
        if result.get("deleted"):
            summary["media_files_deleted"] += 1
        else:
            summary["errors"].append(
                {"file_uuid": result.get("file_uuid"), "error": result.get("error")}
            )


def _delete_owner_scoped_rows(db: Session, user_id: int, summary: dict[str, Any]) -> None:
    """Delete a single user's profile/collection rows that survive file cleanup.

    ``purge_media_file`` deliberately preserves ``SpeakerProfile`` (and never
    touches empty ``Collection`` shells), so erasure must remove them
    explicitly. Their profile embeddings are cleared from OpenSearch first.

    Tags owned by the subject go too (v374): tag names are user-authored free
    text, i.e. personal data, and ``tag.user_id`` is a plain FK that would block
    the user-row delete. Their ``file_tag`` rows are detached first — one may
    hang off another user's file — while system tags (``user_id IS NULL``) are
    shared vocabulary and are left alone.

    ``Comment`` and ``Task`` are here for the same reason as ``Tag`` and not for
    the reason you would guess. ``purge_media_file`` deletes each of the subject's
    files as an ORM *instance*, so ``MediaFile``'s ``delete-orphan`` cascade takes
    the comments and tasks **on those files** with it. What it cannot reach is a
    row the subject created on somebody *else's* file: commenting is collaborative
    (a viewer on a shared file may comment), and both ``comment.user_id`` and
    ``task.user_id`` are NOT NULL with ``ON DELETE NO ACTION``. One such comment
    made the whole erasure fail at ``db.delete(user)`` — recorded as an error in
    the summary, i.e. an Art. 17 request that silently did not complete.

    **This list is hand-maintained and its twin is
    ``api/endpoints/admin._delete_user_owned_records``.** The two are independent
    and nothing in the application compares them;
    ``tests/unit/test_user_deletion_fk_coverage.py`` derives the FKs from the live
    schema and requires both paths to account for each one.
    """
    profiles = db.query(SpeakerProfile).filter(SpeakerProfile.user_id == user_id).all()
    for profile in profiles:
        _erase_profile_embedding(str(profile.uuid), summary["errors"])
        db.delete(profile)
        summary["speaker_profiles_deleted"] += 1

    for model, key in (
        (SpeakerCollection, "speaker_collections_deleted"),
        (Collection, "collections_deleted"),
        # Chat threads quote transcript content back to the user, so they must
        # be erased even when a legal hold retains the user row and its files.
        (ChatConversation, "chat_conversations_deleted"),
    ):
        rows = db.query(model).filter(model.user_id == user_id).all()
        for row in rows:
            db.delete(row)
            summary[key] += 1

    tag_ids = [t.id for t in db.query(Tag.id).filter(Tag.user_id == user_id).all()]
    if tag_ids:
        db.query(FileTag).filter(FileTag.tag_id.in_(tag_ids)).delete(synchronize_session=False)
        db.query(Tag).filter(Tag.user_id == user_id).delete(synchronize_session=False)
        summary["tags_deleted"] = len(tag_ids)

    # Whatever the subject wrote on OTHER people's files. The rows on their own
    # files are already gone via purge_media_file's ORM cascade; these are the
    # ones no per-file pass can see, and both FKs are NOT NULL / NO ACTION, so
    # leaving them turns `db.delete(user)` into a foreign-key violation.
    summary["comments_deleted"] = (
        db.query(Comment).filter(Comment.user_id == user_id).delete(synchronize_session=False)
    )
    summary["tasks_deleted"] = (
        db.query(Task).filter(Task.user_id == user_id).delete(synchronize_session=False)
    )

    db.commit()


def _new_summary(subject: str, subject_id: int) -> dict[str, Any]:
    return {
        "subject": subject,
        "subject_id": subject_id,
        "media_files_deleted": 0,
        "speaker_profiles_deleted": 0,
        "speaker_collections_deleted": 0,
        "collections_deleted": 0,
        "tags_deleted": 0,
        "comments_deleted": 0,
        "tasks_deleted": 0,
        "chat_conversations_deleted": 0,
        # Only the org-member path fills these: the account-wide path reaches the same
        # rows through the `user` row's FK CASCADE, so they legitimately stay 0 there.
        "prompts_deleted": 0,
        "user_settings_deleted": 0,
        "custom_vocabularies_deleted": 0,
        "watch_sources_deleted": 0,
        "voiceprints_deleted": 0,
        "users_deleted": 0,
        "legal_holds_skipped": 0,
        "errors": [],
        "complete": True,
        "sla_days": ERASURE_SLA_DAYS,
        "already_erased": False,
        # The receipt. A caller that scripts against the API gets a reference it can
        # quote back — and, more to the point, the erasure now has an identity that
        # outlives the request, which is what Art. 30 demonstrability needs.
        "ledger_uuid": None,
    }


def _actor_kind(actor_user_id: int | None, *, staff_role: str) -> str:
    """Classify the caller for the ledger without recording who they are.

    ``None`` means the cloud ``user.deleted`` / ``organization.deleted`` webhook, i.e.
    the data subject deleted their own IdP account. Anything else is staff, and which
    kind is decided by the endpoint that called us (the two entry points are
    super-admin and org-admin scoped respectively).
    """
    return staff_role if actor_user_id is not None else "data_subject"


def _resolve_outcome(summary: dict[str, Any]) -> AuditOutcome:
    """Set ``summary["complete"]`` and pick the audit outcome from the errors.

    The single place that decides whether an Art. 17 erasure completed. An
    erasure that could not destroy every copy of the subject's data — a legal
    hold, an unreachable object store, an OpenSearch outage that left the
    transcript or its RAG chunks indexed — is PARTIAL, never SUCCESS. The
    endpoints return this summary verbatim, so ``complete`` is also the
    machine-readable answer for a caller scripting against the API (which gets
    HTTP 200 either way).

    Args:
        summary: The erasure summary, whose ``errors`` list has been fully
            populated by this point.

    Returns:
        ``AuditOutcome.SUCCESS`` when nothing survived, ``PARTIAL`` otherwise.
    """
    complete = not summary["errors"]
    summary["complete"] = complete
    return AuditOutcome.SUCCESS if complete else AuditOutcome.PARTIAL


def erase_user(
    db: Session,
    user_id: int,
    *,
    actor_user_id: int | None = None,
    actor_email: str | None = None,
    ledger_entry: ErasureLedgerEntry | None = None,
) -> dict[str, Any]:
    """Permanently erase ALL of a user's personal data (GDPR Art. 17).

    Account-wide: cascades object storage, OpenSearch transcript + voiceprint
    docs, and the relational rows across EVERY scope the user owns (personal
    and all orgs), then deletes the ``user`` row (FK CASCADEs sweep settings,
    comments, MFA, refresh tokens, memberships, etc.). Because the blast
    radius crosses tenant boundaries, this callable is reserved for the data
    subject's own deletion flow (the cloud ``user.deleted`` webhook — the user
    deleted their IdP account) and platform super-admins. Org admins get the
    org-scoped :func:`erase_org_member_data` instead.

    ``actor_*`` identifies WHO invoked the erasure for the audit trail
    (``None`` = the data subject via the deletion webhook). Idempotent: a
    missing user returns ``already_erased=True`` with zeroed counters. Never
    raises — errors are collected in ``summary["errors"]``, and anything left in
    that list makes ``summary["complete"]`` False and audits PARTIAL. Files under
    an active legal hold are skipped (Art. 17(3)(e)) and reported. SLA: data
    erased within :data:`ERASURE_SLA_DAYS` days of the request, measured against
    the ledger entry's ``sla_due_at``.

    ``ledger_entry`` is passed by ``tasks/erasure_reconciliation`` when it retries a
    deferred erasure, so the retry UPDATES the original entry instead of opening a
    second one — an SLA clock that restarted on every sweep tick would never expire.
    A first-time caller leaves it ``None`` and one is opened here.

    **The ledger row is committed before anything is destroyed.** A worker that dies
    part-way leaves a ``pending`` entry the sweep picks up; recording only on the way
    out would leave the crash — the case that most needs a record — with none.
    """
    summary = _new_summary("user", user_id)

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        summary["already_erased"] = True
        logger.info(f"erase_user: user {user_id} not present — idempotent no-op")
        if ledger_entry is not None:
            ledger.record_outcome(db, ledger_entry, summary)
            summary["ledger_uuid"] = str(ledger_entry.uuid)
        return summary

    email = str(user.email)

    if ledger_entry is None:
        ledger_entry = ledger.record_request(
            db,
            subject_type="user",
            subject_user_id=user_id,
            subject_user_uuid=user.uuid,
            actor_kind=_actor_kind(actor_user_id, staff_role="super_admin"),
            actor_user_id=actor_user_id,
        )

    files = db.query(MediaFile).filter(MediaFile.user_id == user_id).all()
    _purge_files(db, files, summary)
    _delete_owner_scoped_rows(db, user_id, summary)

    summary["voiceprints_deleted"] = _erase_speaker_voiceprints(
        user_id=user_id, errors=summary["errors"]
    )

    if summary["legal_holds_skipped"]:
        # The account row survives because it HAS to: ``media_file.user_id`` is a plain
        # NO ACTION foreign key, so `DELETE FROM "user"` raises while a held file
        # exists. Art. 17(3)(e) justifies retaining the FILE; retaining the account
        # (credentials, MFA secrets, tokens) is a side effect of the FK, not of the
        # exemption — see the ledger note in this module's docstring for what is and
        # is not covered. The ledger entry below is what makes it temporary.
        logger.warning(
            f"erase_user({user_id}): {summary['legal_holds_skipped']} file(s) under "
            "legal hold were preserved; user row retained until holds release."
        )
    else:
        ledger_entry_id = ledger_entry.id if ledger_entry is not None else None
        try:
            db.delete(user)
            db.commit()
            summary["users_deleted"] = 1
        except Exception as e:  # noqa: BLE001 — record, don't raise
            db.rollback()
            summary["errors"].append({"user_id": user_id, "error": str(e)})
            logger.error(f"erase_user: failed to delete user row {user_id}: {e}")
            # db.rollback() expires every object the session was tracking, including
            # `ledger_entry` — opened and COMMITTED independently by record_request
            # before anything was destroyed, so this rollback cannot have undone it,
            # but the in-memory instance is now stale. Without re-fetching it,
            # `ledger.record_outcome` below raises ObjectDeletedError on its first
            # attribute read, turning a PARTIAL erasure into an unhandled exception —
            # breaking this function's own "never raises" docstring promise, exactly
            # the class of bug this module exists to not have (see its own "Partial
            # erasure" section).
            if ledger_entry_id is not None:
                ledger_entry = (
                    db.query(ErasureLedgerEntry)
                    .filter(ErasureLedgerEntry.id == ledger_entry_id)
                    .first()
                )

    audit_logger.log(
        event_type=AuditEventType.ADMIN_USER_DELETE,
        outcome=_resolve_outcome(summary),
        user_id=user_id,
        username=email,
        details={
            "action": "gdpr_erasure",
            # Both sides named explicitly. The top-level ``user_id``/``username``
            # are the TARGET here (this record survives the account, so it has to
            # carry who was erased) while the org-scoped twin below keys them on
            # the ACTOR — so a reader cannot infer either side from position
            # alone. ``actor_email`` falls back to "data-subject-webhook" ONLY for
            # the genuine self-service path; a caller that omits it for a
            # staff-initiated erasure misattributes the act to the data subject.
            "target_user_id": user_id,
            "target_email": email,
            "actor_user_id": actor_user_id,
            "actor_email": actor_email or "data-subject-webhook",
            **{
                k: summary[k]
                for k in (
                    "media_files_deleted",
                    "speaker_profiles_deleted",
                    "voiceprints_deleted",
                    "users_deleted",
                    "legal_holds_skipped",
                )
            },
        },
    )
    ledger.record_outcome(db, ledger_entry, summary)
    if ledger_entry is not None:
        summary["ledger_uuid"] = str(ledger_entry.uuid)
    logger.info(f"erase_user({user_id}) complete: {summary}")
    return summary


def erase_org_member_data(
    db: Session,
    user_id: int,
    org_id: int,
    *,
    actor_user_id: int | None = None,
    actor_email: str | None = None,
    ledger_entry: ErasureLedgerEntry | None = None,
) -> dict[str, Any]:
    """Erase ONE member's data WITHIN ONE organization (org-admin scope).

    The org-admin variant of erasure: destroys only the target's rows stamped
    with ``org_id`` — org media files, org-scoped speaker profiles/collections,
    prompts, settings, vocabulary and watch sources, the comments and tasks they
    authored on the tenant's files, and the (user, org) voiceprint docs. The
    target's personal-scope data, other orgs' data, and the ``user`` row itself
    are untouched: an org admin has authority over their tenant's data, never
    over the person's account. Full account erasure remains :func:`erase_user`
    (data subject / platform super-admin only).

    Four of those row types were missing until issue #442 — ``SummaryPrompt``,
    ``UserSetting``, ``CustomVocabulary`` and ``WatchSource`` all carry an
    ``organization_id`` and were swept by the account-wide path but not by this
    one. ``WatchSource`` is the one that mattered: it stores **SMB/S3
    credentials**, so an org-scoped erasure left the tenant holding the erased
    member's encrypted secrets.

    Idempotent and never raises; legal-hold files are skipped and reported, and
    the request is recorded in the erasure ledger (``ledger_entry`` is passed by
    the reconciliation sweep when retrying — see :func:`erase_user`).
    """
    summary = _new_summary("org_member", user_id)
    summary["organization_id"] = org_id

    user = db.query(User).filter(User.id == user_id).first()
    email = str(user.email) if user else None
    org = db.query(Organization).filter(Organization.id == org_id).first()

    if ledger_entry is None:
        ledger_entry = ledger.record_request(
            db,
            subject_type="org_member",
            subject_user_id=user_id,
            subject_user_uuid=user.uuid if user else None,
            subject_organization_id=org_id,
            subject_organization_uuid=org.uuid if org else None,
            actor_kind=_actor_kind(actor_user_id, staff_role="org_admin"),
            actor_user_id=actor_user_id,
        )

    files = (
        db.query(MediaFile)
        .filter(MediaFile.user_id == user_id, MediaFile.organization_id == org_id)
        .all()
    )
    _purge_files(db, files, summary)

    profiles = (
        db.query(SpeakerProfile)
        .filter(SpeakerProfile.user_id == user_id, SpeakerProfile.organization_id == org_id)
        .all()
    )
    for profile in profiles:
        _erase_profile_embedding(str(profile.uuid), summary["errors"])
        db.delete(profile)
        summary["speaker_profiles_deleted"] += 1

    for model, key in (
        (SpeakerCollection, "speaker_collections_deleted"),
        (Collection, "collections_deleted"),
        # Only this member's conversations stamped with THIS org — their
        # personal-scope chats stay, exactly like their personal files.
        (ChatConversation, "chat_conversations_deleted"),
        # Added in #442. All four are org-stamped and were only ever reached by the
        # account-wide path's FK CASCADE off the `user` row — which this path never
        # deletes, by design. WatchSource carries the encrypted SMB/S3 credentials,
        # SummaryPrompt and CustomVocabulary are user-authored free text, and
        # UserSetting is a key/value store the user fills in.
        (SummaryPrompt, "prompts_deleted"),
        (UserSetting, "user_settings_deleted"),
        (CustomVocabulary, "custom_vocabularies_deleted"),
        (WatchSource, "watch_sources_deleted"),
    ):
        rows = (
            db.query(model).filter(model.user_id == user_id, model.organization_id == org_id).all()
        )
        for row in rows:
            db.delete(row)
            summary[key] += 1

    # Comments and tasks the member authored on the TENANT's files. Neither table has
    # an `organization_id` of its own, so the tenant boundary is the file's — a
    # subquery, not a column. Rows on the member's own org files are already gone with
    # those files (MediaFile's delete-orphan cascade); what is left is what they wrote
    # on other members' org files, which no per-file pass can see.
    org_file_ids = db.query(MediaFile.id).filter(MediaFile.organization_id == org_id).subquery()
    summary["comments_deleted"] = (
        db.query(Comment)
        .filter(Comment.user_id == user_id, Comment.media_file_id.in_(org_file_ids.select()))
        .delete(synchronize_session=False)
    )
    summary["tasks_deleted"] = (
        db.query(Task)
        .filter(Task.user_id == user_id, Task.media_file_id.in_(org_file_ids.select()))
        .delete(synchronize_session=False)
    )
    db.commit()

    summary["voiceprints_deleted"] = _erase_speaker_voiceprints(
        user_id=user_id, organization_id=org_id, errors=summary["errors"]
    )

    audit_logger.log(
        event_type=AuditEventType.ADMIN_USER_DELETE,
        # Audit as the ACTING org admin (a member — visible in the org's audit
        # read); the erased member is carried in details. Org-stamped (#262a).
        user_id=actor_user_id,
        username=actor_email,
        organization_id=org_id,
        outcome=_resolve_outcome(summary),
        details={
            "action": "gdpr_erasure_org_member",
            "organization_id": org_id,
            "target_user_id": user_id,
            "target_email": email,
            **{
                k: summary[k]
                for k in (
                    "media_files_deleted",
                    "speaker_profiles_deleted",
                    "voiceprints_deleted",
                    "legal_holds_skipped",
                )
            },
        },
    )
    ledger.record_outcome(db, ledger_entry, summary)
    if ledger_entry is not None:
        summary["ledger_uuid"] = str(ledger_entry.uuid)
    logger.info(f"erase_org_member_data(user={user_id}, org={org_id}) complete: {summary}")
    return summary


def erase_organization(
    db: Session,
    org_id: int,
    *,
    actor_user_id: int | None = None,
    actor_email: str | None = None,
    ledger_entry: ErasureLedgerEntry | None = None,
) -> dict[str, Any]:
    """Permanently erase an organization's data and all member-owned org data.

    Erases every org-stamped media file, the org's voiceprint docs (by
    ``organization_id``), then each member's org-scoped profiles/collections,
    and finally deletes the ``organization`` row (membership rows CASCADE).

    Member **users are NOT deleted** — a user may belong to multiple orgs and
    retains a personal account; only the org's data and the membership links go.
    The cloud ``organization.deleted`` webhook invokes this (``actor_*`` =
    ``None``); the org-admin endpoint passes the acting admin for the audit
    trail. Idempotent and never raises; legal-hold files are skipped and
    reported. SLA: :data:`ERASURE_SLA_DAYS` days.
    """
    summary = _new_summary("organization", org_id)

    org = db.query(Organization).filter(Organization.id == org_id).first()
    if org is None:
        summary["already_erased"] = True
        logger.info(f"erase_organization: org {org_id} not present — idempotent no-op")
        if ledger_entry is not None:
            ledger.record_outcome(db, ledger_entry, summary)
            summary["ledger_uuid"] = str(ledger_entry.uuid)
        return summary

    org_name = str(org.name)

    if ledger_entry is None:
        ledger_entry = ledger.record_request(
            db,
            subject_type="organization",
            subject_organization_id=org_id,
            subject_organization_uuid=org.uuid,
            actor_kind=_actor_kind(actor_user_id, staff_role="org_admin"),
            actor_user_id=actor_user_id,
        )

    files = db.query(MediaFile).filter(MediaFile.organization_id == org_id).all()
    _purge_files(db, files, summary)

    # Every document stamped with this org, across ALL members (v399/lane C3/C4).
    # Org-scoped speaker profiles (clear their profile embedding first), then
    # the org's collections — across ALL members of the org.
    profiles = db.query(SpeakerProfile).filter(SpeakerProfile.organization_id == org_id).all()
    for profile in profiles:
        _erase_profile_embedding(str(profile.uuid), summary["errors"])
        db.delete(profile)
        summary["speaker_profiles_deleted"] += 1

    speaker_collections = (
        db.query(SpeakerCollection).filter(SpeakerCollection.organization_id == org_id).all()
    )
    for sc in speaker_collections:
        db.delete(sc)
        summary["speaker_collections_deleted"] += 1

    collections = db.query(Collection).filter(Collection.organization_id == org_id).all()
    for coll in collections:
        db.delete(coll)
        summary["collections_deleted"] += 1
    db.commit()

    summary["voiceprints_deleted"] = _erase_speaker_voiceprints(
        organization_id=org_id, errors=summary["errors"]
    )

    # Drop the org row (organization_membership rows CASCADE on delete).
    member_count = (
        db.query(OrganizationMembership)
        .filter(OrganizationMembership.organization_id == org_id)
        .count()
    )
    ledger_entry_id = ledger_entry.id if ledger_entry is not None else None
    try:
        db.delete(org)
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        summary["errors"].append({"org_id": org_id, "error": str(e)})
        logger.error(f"erase_organization: failed to delete org row {org_id}: {e}")
        # See the identical comment in erase_user: db.rollback() expires every object
        # in the session, including ledger_entry — committed independently by
        # record_request before anything was destroyed, so this rollback cannot have
        # undone the row, only the in-memory instance. A held document (v399) or a
        # held media file both reach this branch, and until this re-fetch,
        # ledger.record_outcome below raised ObjectDeletedError on its first
        # attribute read every time — an unhandled exception from a function whose
        # docstring promises it never raises.
        if ledger_entry_id is not None:
            ledger_entry = (
                db.query(ErasureLedgerEntry)
                .filter(ErasureLedgerEntry.id == ledger_entry_id)
                .first()
            )

    summary["memberships_removed"] = member_count

    audit_logger.log(
        event_type=AuditEventType.ADMIN_USER_DELETE,
        # Audit as the ACTING admin when invoked from the org-admin endpoint
        # (a member user-id — visible in the org-scoped audit read); None for
        # the data-controller webhook path. Org-stamped (#262a) — the org row
        # is gone but the event stays attributed to the erased tenant's id.
        user_id=actor_user_id,
        username=actor_email,
        organization_id=org_id,
        outcome=_resolve_outcome(summary),
        details={
            "action": "gdpr_erasure_organization",
            "organization_id": org_id,
            "organization_name": org_name,
            "memberships_removed": member_count,
            "actor_user_id": actor_user_id,
            **{
                k: summary[k]
                for k in (
                    "media_files_deleted",
                    "speaker_profiles_deleted",
                    "voiceprints_deleted",
                    "legal_holds_skipped",
                )
            },
        },
    )
    ledger.record_outcome(db, ledger_entry, summary)
    if ledger_entry is not None:
        summary["ledger_uuid"] = str(ledger_entry.uuid)
    logger.info(f"erase_organization({org_id}) complete: {summary}")
    return summary
