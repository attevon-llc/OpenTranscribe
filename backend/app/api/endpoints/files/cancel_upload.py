import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_user
from app.core.exceptions import DomainError
from app.db.base import get_db
from app.models.user import User
from app.services.file_route_queries import get_pending_upload
from app.services.file_route_queries import is_unfinished_upload
from app.services.request_context import RequestContext

logger = logging.getLogger(__name__)

router = APIRouter()


@router.delete("/{file_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_upload(
    file_uuid: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    ctx: RequestContext = Depends(get_current_context),
) -> None:
    """
    Cancel an unfinished upload, or move any other file to the trash.

    Only a genuinely unfinished upload is destroyed at once: a PENDING file owned by the
    caller whose bytes never landed in object storage (a prepared row or an in-progress
    multipart upload) and that has no processing history. Everything else, including a
    PENDING file whose upload completed (queued, or put back by retry, reprocess or
    recovery with its transcript intact), goes to the trash (issue #1101): recoverable
    until its window ends or someone deletes it forever with
    ``DELETE /files/{uuid}/purge``. Both flows share this route: this router registers
    DELETE /{file_uuid} first, so a separate delete route would be unreachable.
    """
    # Reject a malformed UUID up front. Without this, the unparametrized
    # ``MediaFile.uuid == file_uuid`` query below sends the raw string to
    # Postgres, which raises ``invalid input syntax for type uuid`` — an
    # unhandled 500 that also poisons the request's DB transaction. Every
    # other delete entry point (get_by_uuid) maps a bad UUID to 404, so do
    # the same here for parity.
    import uuid as _uuid

    try:
        _uuid.UUID(str(file_uuid))
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        ) from None

    # Get the media file by UUID (tenant-gated: org scope only sees same-org
    # rows; personal scope only org-less rows — mirrors scope_to_context).
    db_file = get_pending_upload(db, file_uuid, user_id=current_user.id, organization_id=ctx.org_id)

    if db_file is not None and not is_unfinished_upload(db, db_file):
        db_file = None

    if not db_file:
        # Not an unfinished upload owned by the caller: move it to the trash.
        from app.api.endpoints.files.crud import delete_media_file

        delete_media_file(
            db,
            file_uuid,
            current_user,
            organization_id=ctx.org_id,
            is_org_admin=ctx.is_org_admin,
            bypass=ctx.bypass,
            request=request,
        )
        return None

    file_id = db_file.id

    # PENDING does not mean "never processed". Retry, reprocess and stuck-file recovery
    # all put a TRANSCRIBED file back to pending with its transcript, OpenSearch
    # documents, voiceprints, thumbnail and playback rendition intact. This branch used
    # to delete only ``storage_path`` and the row, which orphaned every one of those
    # copies — including the verbatim transcript in the search index. The destroy is
    # therefore the canonical one; the only step this branch adds is aborting a
    # browser-side multipart upload, whose parts no other path knows to look for.
    from app.services.content_audit import AuditedResource
    from app.services.content_hold_service import record_hold_refusal
    from app.services.file_cleanup_service import purge_media_file
    from app.services.hold_guard import refusal_detail

    try:
        if db_file.storage_path:
            # A cancelled multipart upload leaves its parts in the bucket, billed until
            # aborted; the upload_id is client state, so they are found by key.
            from app.services.multipart_upload import abort_uploads_for_object

            abort_uploads_for_object(str(db_file.storage_path))

        result = purge_media_file(db, db_file)
        if result.get("refused_legal_hold"):
            record_hold_refusal(
                db,
                None,
                current_user,
                AuditedResource.file(db_file),
                action="cancel_upload",
                held_by=result["hold_state"].held_by,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    **refusal_detail(
                        result["hold_state"],
                        reveal_uuids=ctx.is_org_admin,
                        message=result.get("error"),
                    ),
                    "file_id": str(file_uuid),
                },
            )
        if not result["deleted"]:
            raise RuntimeError(result.get("error") or "purge_media_file failed")
        logger.info(f"Cancelled upload for file ID {file_id}")

    except (HTTPException, DomainError):
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        db.rollback()
        logger.exception(f"Error cancelling upload {file_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to cancel upload",
        ) from e

    return None
