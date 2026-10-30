"""The relational and object-storage steps of a GDPR erasure, and its outcome rules.

``gdpr_erasure_service`` imports each of these by name and calls them through its own
module globals (tests patch them there), in the order its ``erase_*`` functions define:
files (skipping legal holds) first, then the owner-scoped rows, then the avatar objects
after the commit. ``_resolve_outcome`` is the one place an erasure is judged complete.
"""

from typing import Any

from sqlalchemy.orm import Session

from app.auth.audit_events import AuditOutcome
from app.models.chat import ChatConversation
from app.models.collection import Collection
from app.models.media import Comment
from app.models.media import MediaFile
from app.models.media import Task
from app.models.speaker import SpeakerCollection
from app.models.speaker import SpeakerProfile
from app.models.tag import FileTag
from app.models.tag import Tag
from app.services.file_cleanup_service import purge_media_file
from app.services.gdpr_voiceprint_erasure import _erase_profile_embedding


def _delete_profiles(
    db: Session, profiles: list[SpeakerProfile], summary: dict[str, Any]
) -> list[str]:
    """Delete speaker profiles (embedding first) and return their avatar object keys.

    The avatar is an uploaded photo of the person the profile names — personal data in
    object storage that no row cascade reaches. Its key is read here, while the row
    still exists, and handed back so the caller can delete the objects with
    :func:`_erase_avatar_objects` AFTER its transaction commits (the three-phase
    session rule: no object-storage round trips inside an open transaction). Every
    erasure scope used to delete the rows and leave the photos in the bucket.
    """
    avatar_paths: list[str] = []
    for profile in profiles:
        _erase_profile_embedding(str(profile.uuid), summary["errors"])
        if profile.avatar_path:
            avatar_paths.append(str(profile.avatar_path))
        db.delete(profile)
        summary["speaker_profiles_deleted"] += 1
    return avatar_paths


def _erase_avatar_objects(avatar_paths: list[str], errors: list[dict[str, Any]]) -> None:
    """Delete profile avatar objects. **Takes no session**; call after the commit.

    A failure is recorded (so the erasure audits PARTIAL), never raised. The entry
    names the failure class only: ``summary`` is an API response body, and a storage
    key or driver message is not an authored fact (#914).
    """
    from app.services.minio_service import delete_file

    for path in avatar_paths:
        try:
            delete_file(path)
        except Exception as e:  # noqa: BLE001 — record, don't raise; one avatar never blocks the rest
            errors.append(
                {"stage": "avatar", "error": f"avatar delete failed ({type(e).__name__})"}
            )


def _purge_files(db: Session, files: list[MediaFile], summary: dict[str, Any]) -> None:
    """Run the canonical per-file destroy for every file, accumulating counters.

    Files under an active legal hold (the platform's, or an organization's:
    ``hold_guard.hold_states``) are SKIPPED, not destroyed: GDPR
    Art. 17(3)(e) exempts data retained for legal claims, and the takedown
    flow's evidence-preservation guarantee rests on the DB state (S3
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
    from app.services.hold_guard import hold_states

    held = hold_states(db, [int(f.id) for f in files])
    for media_file in files:
        if int(media_file.id) in held:
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


def _delete_owner_scoped_rows(db: Session, user_id: int, summary: dict[str, Any]) -> list[str]:
    """Delete a single user's profile/collection rows that survive file cleanup.

    Returns the deleted profiles' avatar object keys, for :func:`_erase_avatar_objects`
    to remove once this function's commit has closed the transaction.

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
    ``services/user_deletion_service._delete_user_owned_records``.** The two are independent
    and nothing in the application compares them;
    ``tests/unit/test_user_deletion_fk_coverage.py`` derives the FKs from the live
    schema and requires both paths to account for each one.
    """
    profiles = db.query(SpeakerProfile).filter(SpeakerProfile.user_id == user_id).all()
    avatar_paths = _delete_profiles(db, profiles, summary)

    # Organization collections are the tenant's and hold colleagues' files (v422):
    # they survive, de-attributed. Personal collections go with the account.
    db.query(Collection).filter(
        Collection.user_id == user_id, Collection.organization_id.is_not(None)
    ).update({Collection.user_id: None}, synchronize_session=False)

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

    # Organization tags are the tenant's vocabulary and sit on colleagues' files
    # (v420): they survive, de-attributed. Personal tags go with the account.
    db.query(Tag).filter(Tag.user_id == user_id, Tag.organization_id.is_not(None)).update(
        {Tag.user_id: None}, synchronize_session=False
    )
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
    return avatar_paths


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
