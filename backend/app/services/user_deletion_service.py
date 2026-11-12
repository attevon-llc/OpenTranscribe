"""Cascade helpers for permanently deleting a user account and everything it owns."""

import logging
from typing import Any

from sqlalchemy import exists
from sqlalchemy.orm import Session

from app.auth.token_service import token_service
from app.core.exceptions import ConflictError
from app.models.collection import Collection
from app.models.collection import CollectionMember
from app.models.media import Analytics
from app.models.media import Comment
from app.models.media import MediaFile
from app.models.media import Task as TaskModel
from app.models.media import TranscriptSegment
from app.models.prompt import SummaryPrompt
from app.models.speaker import Speaker
from app.models.speaker import SpeakerCollection
from app.models.speaker import SpeakerCollectionMember
from app.models.speaker import SpeakerProfile
from app.models.tag import FileTag
from app.models.tag import Tag
from app.models.user import User
from app.services import account_purge

# No basicConfig here — this module is imported via the API router before
# configure_logging() runs; a default root handler would double every log line.
logger = logging.getLogger(__name__)


def _delete_user_speakers(db: Session, user_id: int) -> None:
    """Delete all speakers for a user. Pure DB — no OpenSearch here (issue #715).

    This used to remove each speaker's OpenSearch embedding itself, one round trip
    per speaker, from inside the caller's open transaction (the
    ``db.begin_nested()`` savepoint :func:`delete_user_account` opens) — holding
    locks, including ``ACCESS SHARE`` on ``media_file`` taken moments later, for as
    long as OpenSearch took to answer. :func:`delete_user_account` now reads every
    speaker UUID via
    ``account_purge.load_account_purge_plans`` **before** this function runs
    and removes the embeddings via ``purge_account_external_copies`` **after** its own
    transaction commits, with no session held — the same phase split
    ``_delete_user_owned_records`` already uses for ``SpeakerProfile`` avatars.

    **The segment detach is not optional.** ``transcript_segment.speaker_id`` is a
    plain FK with ``ON DELETE NO ACTION``, and this runs *before*
    ``_delete_user_media_files`` removes the segments — so deleting a speaker any
    segment still points at raises ``ForeignKeyViolation`` on
    ``transcript_segment_speaker_id_fkey``. Every diarized segment carries a
    ``speaker_id``, which made this the first thing to fail for any account with a
    transcribed file: the endpoint's blanket ``except Exception`` reported it as
    ``500 "User deletion failed"`` and named nothing. It went unnoticed because
    both deletion tests deleted a fixture account that owned no files at all.

    The column is nullable, so detaching is a legitimate transient state; the rows
    are deleted moments later by ``_delete_user_media_files``.

    Args:
        db: Database session
        user_id: ID of the user whose speakers to delete
    """
    speaker_rows = db.query(Speaker.id, Speaker.uuid).filter(Speaker.user_id == user_id).all()
    if not speaker_rows:
        return

    speaker_ids = [row[0] for row in speaker_rows]
    speaker_uuids = [str(row[1]) for row in speaker_rows]
    logger.info(f"Deleting {len(speaker_uuids)} speakers for user {user_id}")

    detached = (
        db.query(TranscriptSegment)
        .filter(TranscriptSegment.speaker_id.in_(speaker_ids))
        .update({TranscriptSegment.speaker_id: None}, synchronize_session=False)
    )
    if detached:
        logger.info(f"Detached {detached} transcript segments from these speakers")

    db.query(Speaker).filter(Speaker.user_id == user_id).delete(synchronize_session=False)
    logger.info("Speakers deleted from DB")


def _delete_user_owned_records(db: Session, user_id: int) -> None:
    """Delete all user-owned records that are not covered by DB-level CASCADE.

    Cleans up: SpeakerProfile, SpeakerCollection (+ members), Collection (+ members),
    Comment, Task, SummaryPrompt, and Tag records. Must be called BEFORE deleting
    MediaFile rows since some of these tables have FK references to media_file.

    **This list is hand-maintained and its twin is
    ``services/gdpr_erasure_steps._delete_owner_scoped_rows``.** Nothing in the
    application compares the two, and neither is derived from the schema, so a new
    table with a plain ``user_id`` FK breaks account deletion with no code change
    here. ``tests/unit/test_user_deletion_fk_coverage.py`` derives every NO-ACTION
    foreign key into ``user`` / ``media_file`` from the live schema and fails when
    one is not accounted for by *both* paths — add the branch here, or record the
    FK there with a reason.

    Rows recording an action this user took on **somebody else's** row are
    deliberately absent: ``auth_config.created_by``/``updated_by``,
    ``auth_config_audit.changed_by``, ``media_file.quarantined_by`` and
    ``summary_prompt.shared_by`` are not owner-scoped, so no query keyed on
    ``user_id`` can find them. They are ``ON DELETE SET NULL`` at the database
    level instead (``v387``), which is enforced for every deletion path including
    ones that do not exist yet.

    **``SpeakerProfile.avatar_path`` objects are NOT deleted here** (issue #695). This
    function bulk-deletes the ``speaker_profile`` rows below, so by the time it could
    act there is nothing left to read a path off of — the same shape as
    ``_delete_user_media_files`` and object storage. The caller reads every avatar path
    via ``account_purge.load_account_purge_plans`` **before** this function runs
    and destroys the objects via ``purge_account_external_copies`` after the
    transaction commits.

    **Nor are the profiles' OpenSearch embeddings** (issue #715). This used to remove
    each one itself, one round trip per profile, from inside the caller's open
    transaction — the same defect ``_delete_user_speakers`` had. The caller now reads
    every profile UUID via ``load_account_purge_plans`` alongside the avatar paths and
    removes the embeddings via ``purge_account_external_copies`` after the transaction
    commits.
    """
    # Speaker collections and their members
    sc_ids = [
        sc.id
        for sc in db.query(SpeakerCollection.id).filter(SpeakerCollection.user_id == user_id).all()
    ]
    if sc_ids:
        db.query(SpeakerCollectionMember).filter(
            SpeakerCollectionMember.collection_id.in_(sc_ids)
        ).delete(synchronize_session=False)
        db.query(SpeakerCollection).filter(SpeakerCollection.user_id == user_id).delete(
            synchronize_session=False
        )
        logger.info(f"Deleted {len(sc_ids)} speaker collections for user {user_id}")

    # Collections. ORGANIZATION collections belong to the tenant (v422) and hold
    # colleagues' files: they survive, de-attributed (user_id -> NULL). The user's
    # PERSONAL collections and their members go.
    db.query(Collection).filter(
        Collection.user_id == user_id, Collection.organization_id.is_not(None)
    ).update({Collection.user_id: None}, synchronize_session=False)
    col_ids = [c.id for c in db.query(Collection.id).filter(Collection.user_id == user_id).all()]
    if col_ids:
        db.query(CollectionMember).filter(CollectionMember.collection_id.in_(col_ids)).delete(
            synchronize_session=False
        )
        db.query(Collection).filter(Collection.user_id == user_id).delete(synchronize_session=False)
        logger.info(f"Deleted {len(col_ids)} collections for user {user_id}")

    # Speaker profiles. OpenSearch embeddings are NOT removed here — see the
    # docstring; the caller's purge plan already has every UUID.
    profiles_deleted = (
        db.query(SpeakerProfile)
        .filter(SpeakerProfile.user_id == user_id)
        .delete(synchronize_session=False)
    )
    if profiles_deleted:
        logger.info(f"Deleted {profiles_deleted} speaker profiles for user {user_id}")

    # Comments
    comments_deleted = (
        db.query(Comment).filter(Comment.user_id == user_id).delete(synchronize_session=False)
    )
    if comments_deleted:
        logger.info(f"Deleted {comments_deleted} comments for user {user_id}")

    # Background tasks
    tasks_deleted = (
        db.query(TaskModel).filter(TaskModel.user_id == user_id).delete(synchronize_session=False)
    )
    if tasks_deleted:
        logger.info(f"Deleted {tasks_deleted} task records for user {user_id}")

    # Summary prompts
    prompts_deleted = (
        db.query(SummaryPrompt)
        .filter(SummaryPrompt.user_id == user_id)
        .delete(synchronize_session=False)
    )
    if prompts_deleted:
        logger.info(f"Deleted {prompts_deleted} summary prompts for user {user_id}")

    # Tags created by the user (v374). tag.user_id is a plain FK, so it must be
    # cleared before the user row or the delete fails.
    #   * PERSONAL tags are the user's and are deleted. The file_tag pass is belt
    #     and braces: file_tag.tag_id IS ON DELETE CASCADE, so the database would
    #     sweep those rows anyway — including the ones hanging off ANOTHER user's
    #     file, which is the case worth naming.
    #   * ORGANIZATION tags belong to the tenant (v420) and are on colleagues'
    #     files; they survive, de-attributed (user_id → NULL, organization kept).
    # System tags (no owner, no tenant) are shared vocabulary and never touched.
    db.query(Tag).filter(Tag.user_id == user_id, Tag.organization_id.is_not(None)).update(
        {Tag.user_id: None}, synchronize_session=False
    )
    tag_ids = [t.id for t in db.query(Tag.id).filter(Tag.user_id == user_id).all()]
    if tag_ids:
        db.query(FileTag).filter(FileTag.tag_id.in_(tag_ids)).delete(synchronize_session=False)
        db.query(Tag).filter(Tag.user_id == user_id).delete(synchronize_session=False)
        logger.info(f"Deleted {len(tag_ids)} tags for user {user_id}")


def assert_no_held_files(db: Session, user_id: int) -> None:
    """Refuse to delete an account that still owns legally-held evidence (issue #689).

    "Held" is the platform's hold OR an organization's (``hold_guard.held_pred``, issue #1276):
    an organization hold on a file is just as much a reason the account cannot be erased.

    ``_delete_user_media_files`` ends in a **bulk** ``query(MediaFile).delete()``. A bulk
    delete emits one statement, loads no instances, and consults no ``legal_hold`` — so
    it does not reach ``file_cleanup_service.purge_media_file`` and inherits none of
    #664's refusal. Deleting a user therefore destroyed their held files, object row and
    all, which is the one failure mode a legal hold exists to prevent, and it is
    irreversible.

    **The whole deletion is refused, not just the held files.** Deleting everything
    *except* them would leave ``media_file`` rows owned by an account that no longer
    exists (``media_file.user_id`` is a plain ``NO ACTION`` FK, so the ``user`` delete
    would then fail anyway, mid-cascade); silently reassigning them to another owner
    would invent a retention policy nobody chose. Refusing matches what the GDPR path
    already does by accident — ``gdpr_erasure_service`` skips held files, and that same
    FK then keeps the account alive — and what ``purge_media_file`` now does on purpose.

    ``is_quarantined`` is deliberately **not** part of this check, mirroring
    ``purge_media_file``. Quarantine is a review state an admin legitimately ends by
    deleting the offending upload — and deleting an abusive *account* is the commonest
    reason to have quarantined its files in the first place, so blocking on it would
    turn a takedown into a shield. Only the unattended retention sweep refuses to touch
    a file under review (``tasks/cleanup._select_expired_files``): it deletes by age, on
    nobody's decision. The scheduled erase after a removal's grace period
    (``removed_member_service``) is unattended too, but it carries out an administrator's
    decision about this account, so it follows this rule, not the sweep's.

    There is deliberately **no override argument**: an argument that skips the guard is
    the guard's own failure mode. The supported path is to lift the hold first —
    ``takedown_service.release_file(..., clear_legal_hold=True)``, exposed as
    ``POST /api/admin/files/{file_uuid}/release`` — and then delete the account.

    Args:
        db: Database session.
        user_id: Internal id of the account being deleted.

    **A comment on somebody else's held file refuses the deletion too** (legal-hold
    freeze): the comment is part of that file's preserved record, and ``comment.user_id``
    is NOT NULL, so it can neither be deleted nor de-attributed while the hold stands.

    Raises:
        ConflictError: 409 ``FILE_UNDER_LEGAL_HOLD``, naming how many files are held,
            when the account owns at least one or commented on one.
    """
    from app.services.hold_guard import HoldRefusedError
    from app.services.hold_guard import held_pred
    from app.services.hold_guard import hold_states
    from app.services.hold_guard import refusal_detail

    held_ids = [
        int(row[0])
        for row in db.query(MediaFile.id).filter(MediaFile.user_id == user_id, held_pred())
    ]
    held_count = len(held_ids)
    # A comment this account wrote on SOMEBODY ELSE's held file is part of that file's
    # preserved record (legal-hold freeze), and ``comment.user_id`` is NOT NULL, so the
    # account cannot go while it exists.
    commented_held_ids = [
        int(row[0])
        for row in db.query(MediaFile.id)
        .filter(
            MediaFile.user_id != user_id,
            held_pred(),
            exists().where(Comment.media_file_id == MediaFile.id, Comment.user_id == user_id),
        )
        .distinct()
    ]
    if not held_count and not commented_held_ids:
        return

    if held_count:
        noun = "file" if held_count == 1 else "files"
        message = (
            f"refused: this account owns {held_count} {noun} under an active legal hold "
            "and cannot be deleted. Release the hold before deleting."
        )
    else:
        noun = "file" if len(commented_held_ids) == 1 else "files"
        message = (
            f"refused: this account commented on {len(commented_held_ids)} {noun} under an "
            "active legal hold, and those comments are preserved with the file. Release the "
            "hold before deleting the account."
        )
    logger.error(f"user deletion: REFUSED to delete user {user_id} — {message}")
    # Who holds them (platform, organization) is told; the organization hold uuids are not:
    # the caller here is a platform admin or the account owner, never the holding organization.
    refusal = HoldRefusedError(hold_states(db, held_ids or commented_held_ids))
    raise ConflictError(
        {
            **refusal_detail(refusal.merged(), reveal_uuids=False, message=message),
            "files_under_legal_hold": held_count,
            "commented_files_under_legal_hold": len(commented_held_ids),
        }
    )


def _delete_user_media_files(db: Session, user_id: int) -> None:
    """Delete all media files and related records for a user.

    **A user owning any file under an active legal hold is REFUSED before anything is
    deleted** (:func:`assert_no_held_files`, issue #689). The check runs
    here as well as in :func:`delete_user_account`, the single caller of the three
    passes, which runs it *first* so that the two earlier passes
    (``_delete_user_owned_records``, ``_delete_user_speakers``) have not already
    destroyed anything by the time the refusal is raised. This call is the backstop a
    future direct caller inherits without having to remember; a new caller should go
    through :func:`delete_user_account` instead.

    **Why the children are deleted by hand here.** ``MediaFile`` declares
    ``cascade="all, delete-orphan"`` on eight relationships, but four of the
    underlying foreign keys — ``transcript_segment``, ``comment``, ``task`` and
    ``analytics`` on ``media_file_id`` — are ``ON DELETE NO ACTION`` in the
    database. The ORM cascade only fires for an *instance* delete
    (``db.delete(file)``, which is what ``file_cleanup_service.purge_media_file``
    and therefore the GDPR path use). This function ends in a **bulk**
    ``query(MediaFile).delete()``, which emits one ``DELETE`` statement and never
    loads the instances, so the ORM cascade is bypassed entirely and the database
    is the only thing left enforcing anything — and for those four it enforces
    *refusal*. Every hand-delete below is therefore load-bearing: remove one and
    this function raises ``ForeignKeyViolation``, which the endpoint's blanket
    ``except Exception`` reports as ``500 "User deletion failed"``.

    ``file_tag``, ``collection_member``, ``speaker`` and ``topic_suggestion`` are the
    other four children; those FKs *are* ``ON DELETE CASCADE``, so the database
    sweeps them whichever delete shape is used. ``file_tag`` is still deleted
    explicitly because ``_delete_user_owned_records`` has already detached the rows
    pointing at this user's tags and the cost of the second pass is nil.

    ``comment`` and ``task`` are **not** covered by the owner-scoped sweep in
    ``_delete_user_owned_records``: a non-owner can comment on a file shared with
    them (``endpoints/comments.create_comment_for_file_nested`` requires viewer+,
    "commenting is collaborative"), so another account's comment on this user's
    file would block the bulk delete. Scope both by ``media_file_id``, not by
    ``user_id``.

    **Object storage is deliberately NOT touched here** (issue #695). The bulk delete
    below is exactly why: it never loads a ``MediaFile`` instance, so there is nothing
    to read a ``storage_path`` off of by the time this function could act on one. The
    caller reads the storage plan with ``account_purge.load_account_purge_plans``
    **before** calling this function, and destroys the objects with
    ``purge_account_external_copies`` **after** its own transaction commits — see
    ``delete_admin_user``'s docstring for the phase split and why.

    Args:
        db: Database session
        user_id: ID of the user whose media files to delete

    Raises:
        ConflictError: 409 when the user owns a file under an active legal hold.
    """
    assert_no_held_files(db, user_id)

    media_ids = [
        row[0] for row in db.query(MediaFile.id).filter(MediaFile.user_id == user_id).all()
    ]
    media_count = len(media_ids)

    if media_count > 0:
        logger.info(f"Found {media_count} media files for user {user_id}")

        # Delete transcript segments for these media files
        segments_deleted = (
            db.query(TranscriptSegment)
            .filter(TranscriptSegment.media_file_id.in_(media_ids))
            .delete(synchronize_session=False)
        )
        if segments_deleted:
            logger.info(f"Deleted {segments_deleted} transcript segments")

        # Delete file_tag records
        file_tags_deleted = (
            db.query(FileTag)
            .filter(FileTag.media_file_id.in_(media_ids))
            .delete(synchronize_session=False)
        )
        if file_tags_deleted:
            logger.info(f"Deleted {file_tags_deleted} file tags")

        # Delete analytics records
        analytics_deleted = (
            db.query(Analytics)
            .filter(Analytics.media_file_id.in_(media_ids))
            .delete(synchronize_session=False)
        )
        if analytics_deleted:
            logger.info(f"Deleted {analytics_deleted} analytics records")

        # Comments on these files, whoever wrote them. `comment.media_file_id` is
        # NO ACTION and commenting is collaborative, so a viewer's comment on a
        # shared file is another account's row that still has to go with the file.
        comments_deleted = (
            db.query(Comment)
            .filter(Comment.media_file_id.in_(media_ids))
            .delete(synchronize_session=False)
        )
        if comments_deleted:
            logger.info(f"Deleted {comments_deleted} comments on these media files")

        # Tasks against these files, whoever owns them. `task.media_file_id` is
        # NO ACTION too; scoping only by `task.user_id` (as the owner-scoped sweep
        # does) leaves any task another account queued against this file behind.
        tasks_deleted = (
            db.query(TaskModel)
            .filter(TaskModel.media_file_id.in_(media_ids))
            .delete(synchronize_session=False)
        )
        if tasks_deleted:
            logger.info(f"Deleted {tasks_deleted} tasks against these media files")

        # Quotes of these recordings in ANY account's chat history (a shared file is
        # cited in other users' conversations). No FK reaches them, so the bulk delete
        # below cannot either.
        from app.services.file_cleanup_service import scrub_chat_citations

        file_uuids = [
            str(row[0])
            for row in db.query(MediaFile.uuid).filter(MediaFile.id.in_(media_ids)).all()
        ]
        scrub_chat_citations(db, file_uuids)

        # Now delete the media files
        db.query(MediaFile).filter(MediaFile.user_id == user_id).delete(synchronize_session=False)
        logger.info(f"Deleted {media_count} media files for user {user_id}")


def hand_over_org_content(db: Session, user_id: int) -> None:
    """Re-own the account's organization content before the destroy passes (issue #1104).

    For every organization the account is a member of and owns content in, an administrator
    takes it over (:mod:`app.services.org_content_transfer`); personal rows are untouched and go
    with the account. A no-op in the community edition, where nobody has a membership. Runs
    after the held-file refusal, so a refusal never follows a partial hand-over.

    Raises:
        ConflictError: 409 ``no_transfer_target`` when an organization holds the account's
            content and has no other active administrator to take it.
    """
    from app.models.organization import OrganizationMembership
    from app.services import org_content_transfer

    org_ids = [
        int(r[0])
        for r in db.query(OrganizationMembership.organization_id).filter(
            OrganizationMembership.user_id == user_id
        )
    ]
    for org_id in org_ids:
        if not org_content_transfer.has_org_content(db, org_id, user_id):
            continue
        try:
            org_content_transfer.transfer_org_content(
                db,
                organization_id=org_id,
                from_user_id=user_id,
                reason="account_deleted",
            )
        except org_content_transfer.TransferError as error:
            # Only no_transfer_target is reachable: no explicit target is passed and org_id came
            # from an existing membership. Anything else is a bug, not a caller's 409.
            if error.code != "no_transfer_target":
                raise
            raise ConflictError(
                {
                    "code": "no_transfer_target",
                    "message": (
                        "An organization still holds this account's content and has no other "
                        "active administrator to take it over. Add another administrator first."
                    ),
                }
            ) from error


def delete_user_account(db: Session, user: User) -> list[dict[str, Any]]:
    """Delete an account and everything it owns, then destroy its external copies.

    The one cascade both ``DELETE /admin/users/{uuid}`` and ``DELETE /users/{uuid}``
    run. Authorization, the self/last-super_admin checks and the audit record stay in
    the handlers; this owns only the sequence, which had drifted between two copies
    (only the admin one rolled a failed pass back).

    1. Refuse the whole deletion if any owned file is legally held, before the first
       pass, so a refusal never follows a partial destroy (issue #689).
    2. Phase 0: read the storage/OpenSearch plan while the rows still exist. Postgres
       reads only (issue #695).
    3. Phase 1: the three bulk passes and the ``user`` delete inside one savepoint, so
       a failure in any pass leaves the session as it was.
    4. Commit.
    5. Phase 2: object storage and OpenSearch, with NO transaction held. The rows are
       gone, so a failure here is not retryable: it is returned, never swallowed.

    Args:
        db: Database session; committed by this function.
        user: The account to delete.

    Returns:
        Per-object failures from ``account_purge.purge_account_external_copies``;
        empty when every external copy was confirmed gone.

    Raises:
        ConflictError: 409 when an owned file is under a legal hold.
    """
    user_id = int(user.id)
    user_uuid = str(user.uuid)

    assert_no_held_files(db, user_id)
    hand_over_org_content(db, user_id)

    purge_plan = account_purge.load_account_purge_plans(db, user_id)

    savepoint = db.begin_nested()
    try:
        _delete_user_owned_records(db, user_id)
        _delete_user_speakers(db, user_id)
        _delete_user_media_files(db, user_id)
        # No email: the audit record carries the identity; app logs outlive erasure.
        logger.info("Deleting user with ID %s", user_id)
        db.delete(user)
        savepoint.commit()
    except Exception:
        savepoint.rollback()
        raise
    db.commit()
    # A deleted account's open sockets would otherwise outlive it.
    token_service.stamp_user_revocation_epoch(user_uuid)

    return account_purge.purge_account_external_copies(purge_plan)
