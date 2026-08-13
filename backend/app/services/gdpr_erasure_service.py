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

SLA
---
**30-day completion** (GDPR Art. 12(3) "without undue delay and in any event
within one month"). This service runs the erasure synchronously and is the
callable the cloud ``user.deleted`` / ``organization.deleted`` webhooks invoke;
the webhook handler is responsible only for *enqueueing* the call within the
window. The operation is **idempotent** (re-running on an already-erased subject
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

import contextlib
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.models.chat import ChatConversation
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
from app.models.user import User
from app.services.file_cleanup_service import purge_media_file

logger = logging.getLogger(__name__)

# GDPR Art. 12(3): erasure must complete within one month of the request.
ERASURE_SLA_DAYS = 30


def _erase_speaker_voiceprints(
    *,
    user_id: int | None = None,
    organization_id: int | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> int:
    """Delete speaker/profile embedding docs (biometric data) from OpenSearch.

    Scopes by ``user_id`` and/or ``organization_id`` (both given = the
    intersection, i.e. one member's docs within one org) and runs a
    ``delete_by_query`` across the v3 + v4 + alias speaker indices.
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

        indices = {get_speaker_index(), get_speaker_index_v3(), get_speaker_index_v4()}
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


def _purge_files(db: Session, files: list[MediaFile], summary: dict[str, Any]) -> None:
    """Run the canonical per-file destroy for every file, accumulating counters.

    Files under an active legal hold are SKIPPED, not destroyed: GDPR
    Art. 17(3)(e) exempts data retained for legal claims, and the takedown
    flow's evidence-preservation guarantee rests on the DB flag (S3
    object-lock is best-effort only). Skips are reported in the summary so
    the erasure is auditable as partial, and a later hold release re-runs the
    idempotent erasure to finish the job.

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
        with contextlib.suppress(Exception):
            from app.services.opensearch_service import remove_profile_embedding

            remove_profile_embedding(str(profile.uuid))
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
        "voiceprints_deleted": 0,
        "users_deleted": 0,
        "legal_holds_skipped": 0,
        "errors": [],
        "complete": True,
        "sla_days": ERASURE_SLA_DAYS,
        "already_erased": False,
    }


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
    erased within :data:`ERASURE_SLA_DAYS` days of the request.
    """
    summary = _new_summary("user", user_id)

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        summary["already_erased"] = True
        logger.info(f"erase_user: user {user_id} not present — idempotent no-op")
        return summary

    email = str(user.email)

    files = db.query(MediaFile).filter(MediaFile.user_id == user_id).all()
    _purge_files(db, files, summary)
    _delete_owner_scoped_rows(db, user_id, summary)

    summary["voiceprints_deleted"] = _erase_speaker_voiceprints(
        user_id=user_id, errors=summary["errors"]
    )

    if summary["legal_holds_skipped"]:
        logger.warning(
            f"erase_user({user_id}): {summary['legal_holds_skipped']} file(s) under "
            "legal hold were preserved; user row retained until holds release."
        )
    else:
        try:
            db.delete(user)
            db.commit()
            summary["users_deleted"] = 1
        except Exception as e:  # noqa: BLE001 — record, don't raise
            db.rollback()
            summary["errors"].append({"user_id": user_id, "error": str(e)})
            logger.error(f"erase_user: failed to delete user row {user_id}: {e}")

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
    logger.info(f"erase_user({user_id}) complete: {summary}")
    return summary


def erase_org_member_data(
    db: Session,
    user_id: int,
    org_id: int,
    *,
    actor_user_id: int | None = None,
    actor_email: str | None = None,
) -> dict[str, Any]:
    """Erase ONE member's data WITHIN ONE organization (org-admin scope).

    The org-admin variant of erasure: destroys only the target's rows stamped
    with ``org_id`` — org media files, org-scoped speaker profiles/collections,
    and the (user, org) voiceprint docs. The target's personal-scope data,
    other orgs' data, and the ``user`` row itself are untouched: an org admin
    has authority over their tenant's data, never over the person's account.
    Full account erasure remains :func:`erase_user` (data subject / platform
    super-admin only).

    Idempotent and never raises; legal-hold files are skipped and reported.
    """
    summary = _new_summary("org_member", user_id)
    summary["organization_id"] = org_id

    user = db.query(User).filter(User.id == user_id).first()
    email = str(user.email) if user else None

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
        with contextlib.suppress(Exception):
            from app.services.opensearch_service import remove_profile_embedding

            remove_profile_embedding(str(profile.uuid))
        db.delete(profile)
        summary["speaker_profiles_deleted"] += 1

    for model, key in (
        (SpeakerCollection, "speaker_collections_deleted"),
        (Collection, "collections_deleted"),
        # Only this member's conversations stamped with THIS org — their
        # personal-scope chats stay, exactly like their personal files.
        (ChatConversation, "chat_conversations_deleted"),
    ):
        rows = (
            db.query(model).filter(model.user_id == user_id, model.organization_id == org_id).all()
        )
        for row in rows:
            db.delete(row)
            summary[key] += 1
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
    logger.info(f"erase_org_member_data(user={user_id}, org={org_id}) complete: {summary}")
    return summary


def erase_organization(
    db: Session,
    org_id: int,
    *,
    actor_user_id: int | None = None,
    actor_email: str | None = None,
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
        return summary

    org_name = str(org.name)

    files = db.query(MediaFile).filter(MediaFile.organization_id == org_id).all()
    _purge_files(db, files, summary)

    # Org-scoped speaker profiles (clear their profile embedding first), then
    # the org's collections — across ALL members of the org.
    profiles = db.query(SpeakerProfile).filter(SpeakerProfile.organization_id == org_id).all()
    for profile in profiles:
        with contextlib.suppress(Exception):
            from app.services.opensearch_service import remove_profile_embedding

            remove_profile_embedding(str(profile.uuid))
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
    try:
        db.delete(org)
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        summary["errors"].append({"org_id": org_id, "error": str(e)})
        logger.error(f"erase_organization: failed to delete org row {org_id}: {e}")

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
    logger.info(f"erase_organization({org_id}) complete: {summary}")
    return summary
