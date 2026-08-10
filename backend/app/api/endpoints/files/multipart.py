"""Part-signing endpoint for browser-side multipart uploads (issue #327).

``/files/prepare`` creates the multipart upload and hands out the first batch of
part URLs; ``/files/complete`` assembles them. This is the middle of that flow:
the browser asks for the next batch of signed part URLs, and — when resuming an
interrupted upload — for the parts the bucket already holds.

Batching is what keeps part URLs inside ``PRESIGNED_URL_MAX_SECONDS``. A 15 GB
upload can outlive a 6 h clamp; eight 64 MiB parts cannot, so each batch is
signed just before it is needed instead of minting one long-lived URL per part
up front.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.models.media import MediaFile
from app.models.user import User
from app.services import multipart_upload

logger = logging.getLogger(__name__)

router = APIRouter()

#: Refuse to sign more than one batch per call. The batch size is the server's
#: decision (``multipart_upload.PART_URL_BATCH``); a client asking for hundreds of
#: URLs at once is either buggy or trying to mint long-lived credentials wholesale.
MAX_PARTS_PER_REQUEST = 32


class PartUrlRequest(BaseModel):
    """Payload for POST /files/multipart/parts."""

    file_id: str = Field(..., description="UUID of the MediaFile from /prepare")
    upload_id: str = Field(..., description="Multipart upload_id from /prepare")
    part_numbers: list[int] = Field(
        default_factory=list, description="1-based part numbers to sign (one batch)"
    )
    include_uploaded: bool = Field(
        False, description="Also return the parts already stored — used when resuming"
    )


def _load_pending_file(db: Session, file_id: str, user: User) -> MediaFile:
    """Resolve the caller's prepared MediaFile or raise 404.

    Ownership is the whole authorization story here: a signed part URL writes
    into the object key of that row, so handing one out for someone else's row
    would let a caller overwrite another user's media.
    """
    db_file = (
        db.query(MediaFile).filter(MediaFile.uuid == file_id, MediaFile.user_id == user.id).first()
    )
    if not db_file or not db_file.storage_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No prepared upload {file_id} for this user",
        )
    return db_file  # type: ignore[return-value]


@router.post("/multipart/parts", response_model=dict[str, Any])
async def sign_upload_parts(
    request: PartUrlRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Sign the next batch of part URLs, optionally listing parts already stored."""
    db_file = _load_pending_file(db, request.file_id, current_user)
    object_name = str(db_file.storage_path)

    wanted = sorted({n for n in request.part_numbers if n >= 1})
    if len(wanted) > MAX_PARTS_PER_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"At most {MAX_PARTS_PER_REQUEST} part URLs may be signed per request",
        )

    urls: dict[int, str] = {}
    expires_in = 0
    if wanted:
        try:
            urls, expires_in = await run_in_threadpool(
                multipart_upload.presign_parts, object_name, request.upload_id, wanted
            )
        except Exception as e:
            logger.warning(f"Could not sign parts for {object_name}: {e}")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Multipart upload is no longer available; restart the upload",
            ) from e

    response: dict[str, Any] = {
        "urls": {str(number): url for number, url in urls.items()},
        "expires_in": expires_in,
    }

    if request.include_uploaded:
        try:
            response["uploaded_parts"] = await run_in_threadpool(
                multipart_upload.list_uploaded_parts, object_name, request.upload_id
            )
        except Exception as e:
            # A vanished upload cannot be resumed; say so instead of returning an
            # empty list, which the client would read as "nothing uploaded yet"
            # and silently re-send every part into a dead upload_id.
            logger.warning(f"Could not list parts for {object_name}: {e}")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Multipart upload is no longer available; restart the upload",
            ) from e

    return response
