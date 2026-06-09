import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_user
from app.db.base import get_db
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.services.minio_service import delete_file

logger = logging.getLogger(__name__)

router = APIRouter()


@router.delete("/{file_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_upload(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Cancel an in-progress upload, or delete a completed file.

    PENDING files owned by the caller get the lightweight cancel cleanup;
    anything else falls through to the full deletion (permission and safety
    checks, storage + search-index cleanup). Both flows share this route —
    this router registers DELETE /{file_uuid} first, so a separate
    full-delete route would be unreachable.
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

    # Get the media file by UUID
    db_file = (
        db.query(MediaFile)
        .filter(
            MediaFile.uuid == file_uuid,
            MediaFile.user_id == current_user.id,
            MediaFile.status == FileStatus.PENDING,
        )
        .first()
    )

    if not db_file:
        # Not a pending upload owned by the caller — full delete path.
        from app.api.endpoints.files.crud import delete_media_file

        delete_media_file(db, file_uuid, current_user)
        return None

    file_id = db_file.id

    try:
        # Delete the file from storage if it was partially uploaded
        if db_file.storage_path:
            try:
                delete_file(str(db_file.storage_path))
                logger.info(f"Deleted partial upload: {db_file.storage_path}")
            except Exception as e:
                logger.error(f"Error deleting file {db_file.storage_path}: {e}")
                # Continue with cleanup even if file deletion fails

        # Delete the database record
        db.delete(db_file)
        db.commit()
        logger.info(f"Cancelled upload for file ID {file_id}")

    except Exception as e:
        db.rollback()
        logger.error(f"Error cancelling upload {file_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to cancel upload",
        ) from e

    return None
