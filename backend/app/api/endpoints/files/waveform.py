"""
Audio waveform generation endpoints.

This module provides endpoints for generating waveform visualization data
from audio and video files for use in the frontend player interface.
"""

import logging
import os
import tempfile
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.models.media import MediaFile
from app.models.user import User
from app.services.minio_service import download_file
from app.tasks.transcription.waveform_generator import WaveformGenerator
from app.tasks.waveform_generation import trigger_waveform_generation

from .crud import get_media_file_by_uuid

logger = logging.getLogger(__name__)

router = APIRouter()


def _validate_media_file_for_waveform(db_file: MediaFile) -> None:
    """
    Validate that a media file is suitable for waveform generation.

    Args:
        db_file: The media file to validate

    Raises:
        HTTPException: If the file is not suitable for waveform generation
    """
    if not db_file.content_type:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File content type unknown",
        )

    is_audio_video = db_file.content_type.startswith("audio/") or db_file.content_type.startswith(
        "video/"
    )

    if not is_audio_video:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File must be audio or video format for waveform generation",
        )

    if db_file.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File is not ready for waveform generation (status: {db_file.status})",
        )


def _get_cached_waveform(db_file: MediaFile, cache_key: str) -> dict | None:
    """
    Check for valid cached waveform data.

    Args:
        db_file: The media file to check
        cache_key: The cache key to look for

    Returns:
        Cached waveform data with file_id added, or None if not found/outdated
    """
    if not db_file.waveform_data or not isinstance(db_file.waveform_data, dict):
        return None

    cached_data = db_file.waveform_data.get(cache_key)
    if not cached_data or not isinstance(cached_data, dict) or not cached_data.get("waveform"):
        return None

    # Check if cached data was generated with old algorithm (missing new fields)
    if "extracted_samples" not in cached_data or "expected_duration" not in cached_data:
        logger.info(f"Cached waveform data for file {db_file.id} is outdated, regenerating")
        return None

    # Return cached data with file_id
    result = cached_data.copy()
    result["file_id"] = str(db_file.uuid)
    result["cached"] = True
    return result  # type: ignore[no-any-return]


def _download_to_temp_file(storage_path: str, temp_file: Any) -> None:
    """
    Download file content from storage to a temporary file.

    Args:
        storage_path: The path in MinIO storage
        temp_file: The temporary file to write to
    """
    file_content_io, _, _ = download_file(storage_path)

    while True:
        chunk = file_content_io.read(8192)
        if not chunk:
            break
        temp_file.write(chunk)


def _cleanup_temp_file(temp_file_path: str) -> None:
    """
    Clean up a temporary file, logging any errors.

    Args:
        temp_file_path: Path to the temporary file to remove
    """
    try:
        if os.path.exists(temp_file_path):
            os.unlink(temp_file_path)
    except Exception as cleanup_error:
        logger.warning(f"Failed to cleanup temp file {temp_file_path}: {cleanup_error}")


def _convert_waveform_to_peaks(waveform_data: list[int], height: int) -> list[int]:
    """
    Convert waveform samples to height-based peaks.

    Args:
        waveform_data: List of waveform samples (0-255 range)
        height: Target height in pixels

    Returns:
        List of peak heights
    """
    peaks = []
    for sample in waveform_data:
        peak_height = int((sample / 255.0) * height)
        peaks.append(peak_height)
    return peaks


def _generate_and_cache_waveform(
    db: Session, db_file: MediaFile, file_id: int, cache_key: str, samples: int
) -> dict:
    """
    Generate waveform data and cache it in the database.

    Args:
        db: Database session
        db_file: The media file record
        file_id: Internal file ID for logging
        cache_key: Cache key for storing the waveform data
        samples: Number of samples to extract

    Returns:
        Waveform data dictionary with file_id and cached flag

    Raises:
        HTTPException: If waveform generation fails
    """
    temp_file_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp") as temp_file:
            _download_to_temp_file(str(db_file.storage_path), temp_file)
            temp_file_path = temp_file.name

        # Extract waveform data using the WaveformGenerator
        waveform_generator = WaveformGenerator()
        waveform_data = waveform_generator._extract_single_waveform(temp_file_path, samples)

        if not waveform_data:
            raise ValueError("Failed to extract waveform data")

        # Cache the waveform data in database
        if not db_file.waveform_data:
            db_file.waveform_data = {}  # type: ignore[assignment]
        db_file.waveform_data[cache_key] = waveform_data.copy()
        db.commit()

        # Add file ID to response
        waveform_data["file_id"] = str(db_file.uuid)
        waveform_data["cached"] = False

        logger.info(
            f"Generated and cached waveform for file {file_id}: {len(waveform_data['waveform'])} samples"
        )

        return waveform_data

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating waveform for file {file_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate waveform: {str(e)}",
        ) from e
    finally:
        if temp_file_path:
            _cleanup_temp_file(temp_file_path)


def _extract_waveform_from_file(storage_path: str, target_samples: int) -> dict:
    """
    Download a file and extract waveform data from it.

    Args:
        storage_path: The path in MinIO storage
        target_samples: Number of samples to extract

    Returns:
        Waveform data dictionary

    Raises:
        ValueError: If waveform extraction fails
    """
    temp_file_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp") as temp_file:
            _download_to_temp_file(storage_path, temp_file)
            temp_file_path = temp_file.name

        waveform_generator = WaveformGenerator()
        waveform_data = waveform_generator._extract_single_waveform(temp_file_path, target_samples)

        if not waveform_data:
            raise ValueError("Failed to extract waveform data")

        return waveform_data
    finally:
        if temp_file_path:
            _cleanup_temp_file(temp_file_path)


@router.get("/{file_uuid}/waveform")
def get_audio_waveform(
    file_uuid: str,
    samples: int = Query(1000, description="Number of samples to return", ge=100, le=10000),
    refresh_cache: bool = Query(False, description="Force refresh of cached waveform data"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Generate waveform visualization data for an audio or video file.
    Uses caching to improve performance for repeated requests.

    Args:
        file_uuid: UUID of the media file
        samples: Number of waveform samples to return (100-10000)
        refresh_cache: Force refresh of cached waveform data

    Returns:
        JSON response with waveform data and metadata
    """
    # Get the media file and verify user access (tenant-gated via ctx.org_id)
    is_admin = current_user.is_admin
    db_file = get_media_file_by_uuid(
        db, file_uuid, current_user.id, is_admin=bool(is_admin), organization_id=ctx.org_id
    )
    file_id = db_file.id

    # Validate the file is suitable for waveform generation
    _validate_media_file_for_waveform(db_file)

    # Check for cached waveform data (prioritize cached data)
    cache_key = f"waveform_{samples}"
    if not refresh_cache:
        cached_result = _get_cached_waveform(db_file, cache_key)
        if cached_result:
            return cached_result

    # Generate new waveform data
    return _generate_and_cache_waveform(db, db_file, file_id, cache_key, samples)


@router.get("/{file_uuid}/waveform/peaks")
def get_audio_waveform_peaks(
    file_uuid: str,
    width: int = Query(1000, description="Target width in pixels", ge=100, le=10000),
    height: int = Query(100, description="Target height in pixels", ge=50, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Generate waveform peaks data optimized for a specific display size.
    This endpoint provides more detailed control over the waveform visualization.

    Args:
        file_uuid: UUID of the media file
        width: Target display width in pixels
        height: Target display height in pixels

    Returns:
        JSON response with peaks data optimized for display
    """
    # Get the media file and verify user access (tenant-gated via ctx.org_id)
    is_admin = current_user.is_admin
    db_file = get_media_file_by_uuid(
        db, file_uuid, current_user.id, is_admin=bool(is_admin), organization_id=ctx.org_id
    )
    file_id = db_file.id

    # Validate the file is suitable for waveform generation
    _validate_media_file_for_waveform(db_file)

    # Calculate samples based on width (2 samples per pixel for better resolution)
    target_samples = min(width * 2, 4000)

    # Extract waveform data
    try:
        waveform_data = _extract_waveform_from_file(str(db_file.storage_path), target_samples)
    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error(f"Error generating waveform peaks for file {file_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate waveform peaks: {str(e)}",
        ) from e

    # Convert waveform data to height-based peaks
    peaks = _convert_waveform_to_peaks(waveform_data["waveform"], height)

    return {
        "peaks": peaks,
        "duration": waveform_data["duration"],
        "width": width,
        "height": height,
        "samples": len(peaks),
        "sample_rate": waveform_data["sample_rate"],
        "file_id": str(db_file.uuid),
    }


@router.post("/{file_uuid}/waveform/generate")
def generate_waveform_for_file(
    file_uuid: str,
    force_regenerate: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Generate waveform data for a specific file.
    Can be used to create missing waveform data or force regeneration.
    """
    try:
        # Verify user access to the file (tenant-gated via ctx.org_id)
        is_admin = current_user.is_admin
        db_file = get_media_file_by_uuid(
            db, file_uuid, current_user.id, is_admin=bool(is_admin), organization_id=ctx.org_id
        )
        file_id = db_file.id  # Get internal ID for task trigger

        # Check if file is audio/video
        is_audio_video = db_file.content_type.startswith(
            "audio/"
        ) or db_file.content_type.startswith("video/")

        if not is_audio_video:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File must be audio or video format",
            )

        # Trigger waveform generation task for this specific file
        task_id = trigger_waveform_generation(
            file_uuid=file_uuid, skip_existing=not force_regenerate
        )

        action = "regeneration" if force_regenerate else "generation"
        return {
            "success": True,
            "message": f"Waveform {action} started for file {file_id}",
            "task_id": task_id,
            "file_id": str(db_file.uuid),  # Use UUID for frontend
            "force_regenerate": force_regenerate,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error triggering waveform generation for file {file_uuid}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start waveform generation: {str(e)}",
        ) from e


@router.post("/waveforms/generate")
def generate_waveforms_for_files(
    force_regenerate: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Generate waveform data for all eligible files that don't have it.
    Only admin users can trigger this operation.
    """
    # Check if user is admin
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can trigger bulk waveform generation",
        )

    try:
        from app.models.media import FileStatus

        # Get count of files that would be processed
        query = (
            db.query(MediaFile)
            .filter(MediaFile.status == FileStatus.COMPLETED)
            .filter(MediaFile.content_type.like("audio/%") | MediaFile.content_type.like("video/%"))
        )

        if not force_regenerate:
            query = query.filter(MediaFile.waveform_data.is_(None))

        files_to_process = query.all()

        if not files_to_process:
            return {
                "success": True,
                "message": "No files need waveform generation",
                "files_to_process": 0,
                "task_id": None,
            }

        # Trigger the background task
        task_id = trigger_waveform_generation(file_uuid=None, skip_existing=not force_regenerate)  # type: ignore[arg-type]

        logger.info(
            f"User {current_user.id} triggered waveform generation for {len(files_to_process)} files"
        )

        return {
            "success": True,
            "message": f"Waveform generation started for {len(files_to_process)} files",
            "files_to_process": len(files_to_process),
            "task_id": task_id,
            "force_regenerate": force_regenerate,
        }

    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.exception(f"Error triggering waveform generation: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start waveform generation: {str(e)}",
        ) from e


@router.get("/waveforms/status")
def get_waveform_status(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_active_user)
):
    """
    Get status of waveform data across all files.
    Shows how many files have/don't have waveform data.
    """
    # Check if user is admin
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can view waveform status",
        )

    try:
        # Get all counts in a single query (replaces 3 separate queries)
        from sqlalchemy import func

        from app.models.media import FileStatus

        is_media = MediaFile.content_type.like("audio/%") | MediaFile.content_type.like("video/%")
        counts = (
            db.query(
                func.count().label("total"),
                func.count().filter(MediaFile.status == FileStatus.COMPLETED).label("completed"),
                func.count()
                .filter(
                    MediaFile.status == FileStatus.COMPLETED,
                    MediaFile.waveform_data.isnot(None),
                )
                .label("with_waveforms"),
            )
            .filter(is_media)
            .one()
        )

        total_media_files = counts.total
        completed_media_files = counts.completed
        files_with_waveforms = counts.with_waveforms
        files_without_waveforms = completed_media_files - files_with_waveforms

        return {
            "total_media_files": total_media_files,
            "completed_media_files": completed_media_files,
            "files_with_waveforms": files_with_waveforms,
            "files_without_waveforms": files_without_waveforms,
            "waveform_coverage_percentage": round(
                (files_with_waveforms / completed_media_files * 100)
                if completed_media_files > 0
                else 0,
                1,
            ),
        }

    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.exception(f"Error getting waveform status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get waveform status: {str(e)}",
        ) from e
