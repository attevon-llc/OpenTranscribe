import logging
from typing import Any

from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from kombu.exceptions import KombuError
from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from app.core.env import env_bool
from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.models.user import User
from app.schemas.media import MediaFileUpdate
from app.schemas.transcript import TranscriptSegment as TranscriptSegmentSchema
from app.schemas.transcript import TranscriptSegmentUpdate
from app.services.file_availability import is_unavailable
from app.services.file_route_queries import get_file_by_id
from app.services.file_route_queries import get_owned_file_in_tenant
from app.services.file_route_queries import get_segment_with_speaker
from app.services.ingest_artifacts.recorded_date_service import set_manual_date
from app.services.opensearch_service import update_transcript_title
from app.services.opensearch_service.client import CLUSTER_UNAVAILABLE_ERRORS
from app.services.platform_bypass import PlatformBypass
from app.services.privacy.redact_view import redact_speaker_rows
from app.services.uuid_helpers import get_file_by_uuid_with_permission
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
    db_file = get_file_by_id(db, file_id)
    platform_reached = db_file is not None and bypass.allows(
        org_id=db_file.organization_id,
        owner_id=db_file.user_id,
        need="read",
        resource_type="media_file",
        resource_uuid=str(db_file.uuid),
    )
    if not platform_reached:
        db_file = get_owned_file_in_tenant(
            db, file_id, user_id=user_id, organization_id=organization_id
        )

    # Same availability rule as the uuid choke point: trashed is not found for anyone,
    # quarantined is not found unless the platform role reached the row.
    if not db_file or is_unavailable(db_file, platform_reached=platform_reached):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media file not found")

    return db_file


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

    # Every field here is part of the held record (title, filename, date, language, ...).
    from app.api.hold_refusal import refuse_if_frozen

    refuse_if_frozen(db, int(file_id), actor=current_user, action="file_update")

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
        except CLUSTER_UNAVAILABLE_ERRORS as e:
            logger.warning(
                f"Failed to update OpenSearch title for file {file_id}: {e}", exc_info=True
            )

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
        except (KombuError, OSError) as exc:  # dispatch failure must not break the rename
            logger.warning(
                f"Could not queue chunk title propagation for {db_file.uuid}: {exc}",
                exc_info=True,
            )

    # Invalidate caches so gallery reflects the update
    try:
        from app.services.redis_cache_service import redis_cache

        redis_cache.invalidate_user_files(current_user.id)
    except RedisError as e:
        logger.warning(f"Cache invalidation failed (non-critical): {e}", exc_info=True)

    return db_file


def delete_media_file(
    db: Session,
    file_uuid: str,
    current_user: User,
    *,
    organization_id: OrgScope = UNSCOPED,
    is_org_admin: bool = False,
    bypass: PlatformBypass = PlatformBypass.none(),
    request: Request | None = None,
) -> None:
    """Move a media file to the trash (issue #1101); nothing is destroyed here.

    Delete is its own right, not "editor" (issue #1103): the owner, an ``org:admin`` of the
    file's organization, or a platform admin (``get_deletable_file``). A file with a task in
    flight is no longer refused: trash is reversible, so ``trash_service`` cancels the task
    and trashes the file. The destroy happens only from the trash, when its window ends or on
    an explicit "delete forever" (``DELETE /files/{uuid}/purge``).

    Args:
        db: Database session
        file_uuid: File UUID
        current_user: Current user
        organization_id: Active org id, None for personal, or UNSCOPED (legacy).
        is_org_admin: Caller is ``org:admin`` of ``organization_id``
            (``ctx.is_org_admin``); lets an org admin delete members' files.
        bypass: The request's platform bypass (``ctx.bypass``).
        request: The HTTP request, for the ``file.trash`` audit event (None is allowed).

    Raises:
        HTTPException: 409 ``FILE_UNDER_LEGAL_HOLD`` when the file is held.
    """
    from app.services.content_audit import AuditedResource
    from app.services.content_hold_service import record_hold_refusal
    from app.services.delete_permissions import get_deletable_file
    from app.services.hold_guard import HoldRefusedError
    from app.services.hold_guard import hold_state
    from app.services.hold_guard import refusal_detail
    from app.services.trash_service import trash_files

    db_file = get_deletable_file(
        db,
        file_uuid,
        current_user,
        organization_id=organization_id,
        is_org_admin=is_org_admin,
        bypass=bypass,
    )
    try:
        trash_files(db, [db_file], actor=current_user, request=request, bypass=bypass)
    except HoldRefusedError:
        # Read back from the database, never from the exception: the row is still locked by
        # this transaction, so the state is the one that refused it.
        state = hold_state(db, int(db_file.id))
        record_hold_refusal(
            db,
            request,
            current_user,
            AuditedResource.file(db_file),
            action="delete",
            held_by=state.held_by,
        )
        # A hold is a deliberate, actionable refusal: the holder releases it first. The body
        # says WHO holds the file; only an organization admin is given the hold uuids.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                **refusal_detail(state, reveal_uuids=is_org_admin),
                "file_id": str(db_file.uuid),
            },
        ) from None


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
        request: The HTTP request, for the ``content.hold.refused`` event written when the
            file is under a legal hold. None is allowed.

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

    # A held file's transcript is preserved as it was (legal-hold freeze): refused, audited.
    from app.api.hold_refusal import refuse_if_frozen

    refuse_if_frozen(db, int(file_id), actor=current_user, action="segment_edit", request=request)

    # Find the specific segment by UUID with speaker eagerly loaded
    segment = get_segment_with_speaker(db, segment_uuid, file_id)

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
        uuid=segment.uuid,
        media_file_id=db_file.uuid,
        start_time=float(segment.start_time),
        end_time=float(segment.end_time),
        text=str(segment.text),
        speaker_id=segment.speaker.uuid if segment.speaker else None,
        speaker=segment.speaker,
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
