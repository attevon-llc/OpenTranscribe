import hashlib
import io
import logging
import os

from fastapi import HTTPException
from fastapi import status
from fastapi.responses import StreamingResponse

from app.models.media import MediaFile
from app.services.minio_service import download_file

logger = logging.getLogger(__name__)


def validate_file_exists(db_file: MediaFile) -> None:
    """
    Validate that a file exists and has storage path.

    Args:
        db_file: MediaFile object

    Raises:
        HTTPException: If file doesn't exist or isn't available
    """
    if not db_file:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    if not db_file.storage_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not available")


def get_thumbnail_streaming_response(db_file: MediaFile) -> StreamingResponse:
    """
    Get streaming response for a thumbnail image.

    Supports both WebP and JPEG thumbnails with ETag-based caching.

    Args:
        db_file: MediaFile object

    Returns:
        StreamingResponse for thumbnail image

    Raises:
        HTTPException: If thumbnail doesn't exist or can't be retrieved
    """
    if not db_file.thumbnail_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Thumbnail not available for this file",
        )

    # Detect format from path extension
    thumbnail_path = str(db_file.thumbnail_path)
    if thumbnail_path.endswith(".webp"):
        media_type = "image/webp"
        ext = "webp"
    else:
        media_type = "image/jpeg"
        ext = "jpg"

    # Use private caching for private files (OWASP recommendation)
    is_public = getattr(db_file, "is_public", False)
    cache_control = (
        "public, max-age=86400, must-revalidate" if is_public else "private, no-store, max-age=0"
    )

    if os.environ.get("SKIP_S3", "False").lower() == "true":
        # Mirror production security headers so they remain testable in SKIP_S3 mode.
        return StreamingResponse(
            content=io.BytesIO(b"Mock thumbnail content"),
            media_type=media_type,
            headers={
                "Cache-Control": cache_control,
                "X-Content-Type-Options": "nosniff",
            },
        )

    try:
        # download_file returns a tuple of (BytesIO, content_length, content_type)
        thumbnail_io, content_length, _ = download_file(thumbnail_path)

        # Generate ETag from thumbnail path (changes when thumbnail is regenerated)
        # Using MD5 for ETag generation is safe - it's not used for security purposes
        etag = hashlib.md5(thumbnail_path.encode()).hexdigest()  # noqa: S324  # nosec B324

        return StreamingResponse(
            content=thumbnail_io,
            media_type=media_type,
            headers={
                "Content-Disposition": f'inline; filename="thumbnail.{ext}"',
                "Cache-Control": cache_control,
                "ETag": f'"{etag}"',
                "Content-Length": str(content_length),
                "Vary": "Accept",  # CDN compatibility for content negotiation
                # Security headers (OWASP recommendations)
                "X-Content-Type-Options": "nosniff",  # Prevent MIME type sniffing
            },
        )
    except FileNotFoundError as e:
        # Thumbnail referenced in the DB but missing from storage
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Thumbnail not found in storage",
        ) from e
    except Exception as e:
        logger.exception(f"Error retrieving thumbnail: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving thumbnail: {str(e)}",
        ) from e
