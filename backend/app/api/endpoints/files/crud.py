import logging
from typing import Any

from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload

from app.core.env import env_bool
from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.models.user import User
from app.schemas.media import MediaFileUpdate
from app.schemas.transcript import TranscriptSegment as TranscriptSegmentSchema
from app.schemas.transcript import TranscriptSegmentUpdate
from app.services.ingest_artifacts.recorded_date_service import set_manual_date
from app.services.opensearch_service import update_transcript_title
from app.services.platform_bypass import PlatformBypass
from app.services.privacy.redact_view import redact_speaker_rows
from app.services.uuid_helpers import get_file_by_uuid_with_permission
from app.utils.db_helpers import owned_in_tenant
from app.utils.speaker_labels import canonical_speaker_label
from app.utils.speaker_labels import canonical_speaker_label_for_row
from app.utils.time_format import format_timestamp_simple as format_timestamp

logger = logging.getLogger(__name__)


def get_media_file_by_uuid(
    db: Session,
    file_uuid: str,
    user_id: int,
    *,
    bypass: PlatformBypass = PlatformBypass.none(),
    organization_id: OrgScope = UNSCOPED,
    min_permission: str = "viewer",
) -> MediaFile:
    """
    Get a media file by UUID and user ID.

    Always delegates to ``get_file_by_uuid_with_permission``: there is no admin
    short-cut around the tenant gate here (the old bare lookup let ``/stream-url``
    and the detail/update/delete routes cross tenants).

    Args:
        db: Database session
        file_uuid: File UUID
        user_id: User ID
        bypass: The request's platform bypass, decided on the loaded file's tenant.
        organization_id: Active org id, None for personal, or UNSCOPED (default,
            legacy = no gate). Threaded from ``ctx.org_id`` by request handlers so
            cross-tenant files 404/403 (default-deny).
        min_permission: Minimum sharing permission level required on the sharing
            path ("viewer", "editor", or "owner"; see
            ``PermissionService.PERMISSION_LEVELS``). Defaults to "viewer",
            preserving prior behavior for read-only call sites. Mutating
            endpoints should pass ``min_permission="editor"``. Does not affect
            direct ownership; a non-viewer level asks the bypass for WRITE access.

    Returns:
        MediaFile object

    Raises:
        HTTPException: If file not found or no permission
    """
    return get_file_by_uuid_with_permission(
        db,
        file_uuid,
        user_id,
        bypass=bypass,
        organization_id=organization_id,
        min_permission=min_permission,
    )


def get_media_file_by_id(
    db: Session,
    file_id: int,
    user_id: int,
    *,
    bypass: PlatformBypass = PlatformBypass.none(),
    organization_id: OrgScope = UNSCOPED,
) -> MediaFile:
    """
    Get a media file by ID and user ID (legacy - internal use only).

    Args:
        db: Database session
        file_id: File ID
        user_id: User ID
        bypass: The request's platform bypass, decided on the loaded file's tenant.
        organization_id: Active tenant (org id, or None for org-less files); a
            non-admin's file outside it is not found. UNSCOPED = no tenant gate.

    Returns:
        MediaFile object

    Raises:
        HTTPException: If file not found
    """
    db_file = db.query(MediaFile).filter(MediaFile.id == file_id).first()
    if db_file is None or not bypass.allows(
        org_id=db_file.organization_id,
        owner_id=db_file.user_id,
        need="read",
        resource_type="media_file",
        resource_uuid=str(db_file.uuid),
    ):
        db_file = (
            db.query(MediaFile)
            .filter(MediaFile.id == file_id)
            .filter(owned_in_tenant(MediaFile, user_id=user_id, organization_id=organization_id))
            .first()
        )

    if not db_file:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media file not found")

    return db_file  # type: ignore[no-any-return]


def _resolve_segment_speaker_name(speaker: Speaker | None) -> str:
    """Resolve a never-null display name for a segment's speaker.

    Delegates to :func:`~app.utils.speaker_labels.canonical_speaker_label` —
    the single home for this resolution — rather than the
    ``display_name or name or "Unknown speaker"`` chain this endpoint used to
    run inline. That chain was a THIRD spelling of "no attribution"
    (lowercase ``"Unknown speaker"``, next to ``file_facts``'s
    ``"Unknown Speaker"`` and the chunk-index writers' bare ``"Unknown"``) and
    never considered a confident LLM/embedding suggestion.

    Args:
        speaker: The segment's linked Speaker, or None.

    Returns:
        A non-empty display name string.
    """
    if speaker is None:
        return canonical_speaker_label(None)
    return canonical_speaker_label_for_row(speaker)


def update_media_file(
    db: Session,
    file_uuid: str,
    media_file_update: MediaFileUpdate,
    current_user: User,
    *,
    organization_id: OrgScope = UNSCOPED,
    bypass: PlatformBypass = PlatformBypass.none(),
) -> MediaFile:
    """
    Update a media file's metadata.

    Args:
        db: Database session
        file_uuid: File UUID
        media_file_update: Update data
        current_user: Current user
        organization_id: Active org id, None for personal, or UNSCOPED (legacy).
        bypass: The request's platform bypass (``ctx.bypass``).

    Returns:
        Updated MediaFile object
    """
    db_file = get_media_file_by_uuid(
        db,
        file_uuid,
        current_user.id,
        bypass=bypass,
        organization_id=organization_id,
        min_permission="editor",
    )
    file_id = db_file.id  # Get internal ID for OpenSearch update

    # Track if title was updated for OpenSearch reindexing
    update_data = media_file_update.model_dump(exclude_unset=True)
    title_updated = "title" in update_data and update_data["title"] != db_file.title

    # ``recorded_date`` is POPPED before the generic loop below, not handled after it.
    # A bare ``setattr(db_file, "recorded_date", value)`` writes a date with no source,
    # which ``ck_media_file_recorded_date_provenance`` rejects — so the whole update
    # would fail with an IntegrityError, and the user's title edit would be lost along
    # with their date edit. Routing it through ``set_manual_date`` is what stamps
    # ``source='manual'`` and sets the lock that makes the correction permanent.
    #
    # ``exclude_unset=True`` is load-bearing here: it distinguishes "the client did not
    # mention the date" (leave it alone) from "the client sent null" (clear the manual
    # value and let automatic resolution run again). Without that distinction every
    # rename would silently wipe the user's date.
    if "recorded_date" in update_data:
        set_manual_date(db_file, update_data.pop("recorded_date"))

    # Update fields
    for field, value in update_data.items():
        setattr(db_file, field, value)

    db.commit()
    db.refresh(db_file)

    # Update OpenSearch index if title was changed
    if title_updated:
        new_title = str(db_file.title or db_file.filename)
        try:
            update_transcript_title(str(db_file.uuid), new_title)  # Use UUID not integer ID
        except Exception as e:
            logger.warning(f"Failed to update OpenSearch title for file {file_id}: {e}")

        # ...and the CHUNK plane, which is a separate index write.
        # `update_transcript_title` only touches the full-document transcript index.
        # Chunk documents carry their own `title`, and they are what feeds search
        # result cards and chat citations — so without this a renamed file keeps
        # showing its OLD title in both, permanently. The task existed, was Celery-
        # routed and was documented in two CLAUDE.md files, but nothing ever
        # dispatched it; `test_title_change_queues_the_chunk_rewrite` was asserting
        # a design that had never been wired.
        try:
            from app.tasks.rename_propagation_task import propagate_title_rename

            propagate_title_rename.delay(file_uuid=str(db_file.uuid), new_title=new_title)
        except Exception as exc:  # noqa: BLE001 — dispatch failure must not break the rename
            logger.warning(f"Could not queue chunk title propagation for {db_file.uuid}: {exc}")

    # Invalidate caches so gallery reflects the update
    try:
        from app.services.redis_cache_service import redis_cache

        redis_cache.invalidate_user_files(current_user.id)
    except Exception as e:
        logger.debug(f"Cache invalidation failed (non-critical): {e}")

    return db_file


def delete_media_file(
    db: Session,
    file_uuid: str,
    current_user: User,
    force: bool = False,
    *,
    organization_id: OrgScope = UNSCOPED,
    is_org_admin: bool = False,
    bypass: PlatformBypass = PlatformBypass.none(),
) -> None:
    """
    Delete a media file and all associated data with safety checks.

    Args:
        db: Database session
        file_uuid: File UUID
        current_user: Current user
        force: Force deletion even if processing is active (admin only)
        organization_id: Active org id, None for personal, or UNSCOPED (legacy).
        is_org_admin: Caller is ``org:admin`` of ``organization_id``
            (``ctx.is_org_admin``); lets an org admin delete members' files.
        bypass: The request's platform bypass (``ctx.bypass``).
    """
    from app.services.delete_permissions import get_deletable_file
    from app.utils.file_retry import is_file_safe_to_delete
    from app.utils.task_cancel import cancel_active_task

    # Capability flags offered back to the client in the 409 below: whether the platform
    # role could cancel or force this delete (class P, not a content-access decision).
    is_admin = current_user.is_admin
    # Delete is its own right, not "editor" (issue #1103): owner, org admin of
    # the file's organization, or platform admin.
    db_file = get_deletable_file(
        db,
        file_uuid,
        current_user,
        organization_id=organization_id,
        is_org_admin=is_org_admin,
        bypass=bypass,
    )
    file_id = db_file.id  # Get internal ID for task operations

    # Check if file is safe to delete
    is_safe, reason = is_file_safe_to_delete(db, file_id)

    if not is_safe and not force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "FILE_NOT_SAFE_TO_DELETE",
                "message": f"Cannot delete file: {reason}",
                "file_id": str(db_file.uuid),  # Use UUID for frontend
                "active_task_id": db_file.active_task_id,
                "status": db_file.status,
                "options": {
                    "cancel_and_delete": is_admin,
                    "wait_for_completion": True,
                    "force_delete": is_admin and db_file.force_delete_eligible,
                },
            },
        )

    # If force deletion and file has active task, cancel it first
    if force and db_file.active_task_id:
        logger.info(
            f"Force deleting file {file_id}, cancelling active task {db_file.active_task_id}"
        )
        cancel_active_task(db, file_id)
        # Refresh the file object
        db.refresh(db_file)

    # Delegate the actual destroy to the single canonical implementation so the
    # interactive, bulk, retention, and orphan-cleanup paths all behave identically.
    from app.services.content_audit import AuditedResource
    from app.services.content_hold_service import record_hold_refusal
    from app.services.file_cleanup_service import purge_media_file
    from app.services.hold_guard import refusal_detail

    result = purge_media_file(db, db_file)
    if result.get("refused_legal_hold"):
        record_hold_refusal(
            db,
            None,
            current_user,
            AuditedResource.file(db_file),
            action="delete",
            held_by=result["hold_state"].held_by,
        )
        # A legal hold is a deliberate, actionable refusal, not a server fault:
        # the admin has to release the hold first. Reporting it as a 500 told the
        # caller the delete had broken rather than that it had been declined. The body says
        # WHO holds the file; only an organization admin is given the hold uuids to release.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                **refusal_detail(
                    result["hold_state"], reveal_uuids=is_org_admin, message=result.get("error")
                ),
                "file_id": str(db_file.uuid),
            },
        )
    if not result["deleted"]:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete file from database: {result.get('error')}",
        )


def update_single_transcript_segment(
    db: Session,
    file_uuid: str,
    segment_uuid: str,
    segment_update: TranscriptSegmentUpdate,
    current_user: User,
    *,
    organization_id: OrgScope = UNSCOPED,
    bypass: PlatformBypass = PlatformBypass.none(),
    request: Request | None = None,
) -> TranscriptSegmentSchema:
    """
    Update a single transcript segment for a media file.

    Args:
        db: Database session
        file_uuid: File UUID
        segment_uuid: Segment UUID
        segment_update: Segment update data
        current_user: Current user
        organization_id: Active org id, None for personal, or UNSCOPED (legacy).
        bypass: The request's platform bypass (``ctx.bypass``).
        request: The HTTP request, for the audit event written when the file is under a legal
            hold (issue #1307). None is allowed.

    Returns:
        Updated TranscriptSegmentSchema object with all formatted fields
    """
    db_file = get_media_file_by_uuid(
        db,
        file_uuid,
        current_user.id,
        bypass=bypass,
        organization_id=organization_id,
        min_permission="editor",
    )
    file_id = db_file.id  # Get internal ID for segment query

    # Find the specific segment by UUID with speaker eagerly loaded
    segment = (
        db.query(TranscriptSegment)
        .options(joinedload(TranscriptSegment.speaker))
        .filter(
            TranscriptSegment.uuid == segment_uuid,
            TranscriptSegment.media_file_id == file_id,
        )
        .first()
    )

    if not segment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Transcript segment not found"
        )

    # Update fields. Explicit allow-list rather than a blind setattr loop over
    # whatever the schema happens to declare — `TranscriptSegmentUpdate` is the wire
    # contract, not a safe-to-apply set of ORM attribute names (issue #722: an earlier
    # blind loop let a client rewrite the segment's primary key, and separately crash
    # the request by sending a UUID into the integer `speaker_id` FK).
    mutable_segment_fields = ("start_time", "end_time", "text")
    update_fields = segment_update.model_dump(exclude_unset=True)
    text_changed = "text" in update_fields and update_fields["text"] != segment.text
    for field in mutable_segment_fields:
        if field in update_fields:
            setattr(segment, field, update_fields[field])

    # If the text changed, re-run redaction detection for THIS segment only so the
    # edited text never bypasses masking. The API process preloads no ML detectors,
    # so this is also where a detector is most likely to be unavailable — and this
    # path WRITES, so a swallowed failure would be cached as "clean" forever rather
    # than costing one request. `redetect_edited_segment` owns that decision: it
    # marks the file stale and queues a worker re-scan instead of persisting a
    # result nobody could examine. It never raises for a detector fault, so a
    # broken detector cannot turn a transcript edit into a 500.
    if text_changed:
        from app.services.redaction.service import RedactionService

        RedactionService.redetect_edited_segment(db, db_file, segment)

    db.commit()
    db.refresh(segment)

    # Editing a held file is allowed, but it is audited (issue #1307). After the commit, so a
    # rolled-back edit leaves no event; writes nothing for a file nobody holds.
    from app.services.content_hold_service import record_edit_on_held_file

    record_edit_on_held_file(
        db,
        request,
        current_user,
        db_file,
        kind="segment_edit",
        segment_uuid=str(segment.uuid),
        fields=[f for f in mutable_segment_fields if f in update_fields],
        bypass=bypass,
    )

    # The text is what search/RAG chunk documents are built from — a stale
    # index otherwise serves the pre-edit wording indefinitely (issue #666).
    # Debounced per file so editing many segments in a row queues one
    # re-index, not one per PUT.
    if text_changed:
        from app.services.search.reindex_dispatch import dispatch_transcript_reindex

        dispatch_transcript_reindex(
            file_id=db_file.id, file_uuid=str(db_file.uuid), user_id=int(db_file.user_id)
        )

        # Word counts and talk time are derived from segment text, so the stored
        # analytics row is stale the moment the text changes. The SPA's post-save GET
        # /analytics reads that row, and _get_or_compute_analytics only computes when
        # it is ABSENT — so without this the UI silently renders pre-edit numbers with
        # no error (issue #890). Mirrors what the speaker-change path already does at
        # transcript_segments.py:188. Placed after the segment commit above:
        # refresh_analytics deletes+recomputes, so running it before that commit would
        # recompute against the pre-edit text.
        from app.services.analytics_service import AnalyticsService

        if not AnalyticsService.refresh_analytics(db, db_file.id):
            logger.warning("Analytics refresh failed after text edit on file %s", db_file.uuid)

    if segment.speaker is not None:
        redact_speaker_rows(db, [segment.speaker])

    # Manually construct Pydantic response with all required fields
    return TranscriptSegmentSchema(
        uuid=segment.uuid,  # type: ignore[arg-type]
        media_file_id=db_file.uuid,  # type: ignore[arg-type]
        start_time=float(segment.start_time),
        end_time=float(segment.end_time),
        text=str(segment.text),
        speaker_id=segment.speaker.uuid if segment.speaker else None,
        speaker=segment.speaker,  # type: ignore[arg-type]  # ORM Speaker coerced via from_attributes
        formatted_timestamp=format_timestamp(float(segment.start_time)),
        display_timestamp=format_timestamp(float(segment.start_time)),
        speaker_label=(segment.speaker.name if segment.speaker else None),
        resolved_speaker_name=_resolve_segment_speaker_name(segment.speaker),
    )


def get_stream_url_info(
    db: Session,
    file_uuid: str,
    current_user: User,
    *,
    organization_id: OrgScope = UNSCOPED,
    bypass: PlatformBypass = PlatformBypass.none(),
) -> dict[str, Any]:
    """
    Get streaming URL information for a media file.

    Args:
        db: Database session
        file_uuid: File UUID
        current_user: Current user
        organization_id: Active org id, None for personal, or UNSCOPED (legacy).
        bypass: The request's platform bypass (``ctx.bypass``).

    Returns:
        Dictionary with URL and content type information
    """
    db_file = get_media_file_by_uuid(
        db, file_uuid, current_user.id, bypass=bypass, organization_id=organization_id
    )

    # Skip S3 operations in test environment
    if env_bool("SKIP_S3"):
        logger.info("Returning mock URL in test environment")
        return {
            "url": f"/api/files/{db_file.uuid}/content",
            "content_type": db_file.content_type,
        }

    # Return the URL to our video endpoint
    return {
        "url": f"/api/files/{db_file.uuid}/video",  # Video endpoint
        "content_type": db_file.content_type,
        "requires_auth": False,  # No auth required for video endpoint
    }
