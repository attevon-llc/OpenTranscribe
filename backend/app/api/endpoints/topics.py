"""
API endpoints for AI tag and collection suggestions

Simplified endpoints for:
- Getting AI suggestions for a media file
- Extracting/regenerating suggestions
- Applying approved suggestions
- Batch extraction
"""

import logging

from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_user
from app.core.constants import DEFAULT_AUTO_LABEL_CONFIDENCE_THRESHOLD
from app.db.base import get_db
from app.models.topic import TopicSuggestion
from app.models.user import User
from app.schemas.topic import ApplyTopicSuggestionsRequest
from app.schemas.topic import ExtractTopicsRequest
from app.schemas.topic import RetroactiveAutoLabelRequest
from app.schemas.topic import SuggestedCollection
from app.schemas.topic import SuggestedTag
from app.schemas.topic import TopicSuggestionResponse
from app.services.topic_extraction_service import TopicExtractionService
from app.tasks.topic_extraction import extract_topics_task
from app.utils.uuid_helpers import get_file_by_uuid_with_permission

logger = logging.getLogger(__name__)

router = APIRouter()


# =============================================================================
# STATIC ROUTES - Must come before parameterized routes
# =============================================================================


@router.post("/batch-extract", status_code=status.HTTP_202_ACCEPTED)
async def batch_extract_topics(
    file_uuids: list[str] = Body(..., embed=True),
    force_regenerate: bool = Body(False, embed=True),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict[str, str | int | list[str] | None]:
    """
    Extract AI suggestions for multiple files in batch

    Args:
        file_uuids: List of file UUIDs to process
        force_regenerate: Force re-extraction for all files
        db: Database session
        current_user: Authenticated user

    Returns:
        Acknowledgment that batch processing has been triggered
    """
    # Import here to avoid circular imports
    from app.tasks.topic_extraction import batch_extract_topics_task

    # Verify all files exist and belong to user
    verified_uuids = []
    for file_uuid in file_uuids:
        try:
            media_file = get_file_by_uuid_with_permission(
                db, file_uuid, current_user.id, is_admin=current_user.is_admin
            )
            if media_file.transcript_segments:
                verified_uuids.append(file_uuid)
            else:
                logger.warning(f"Skipping file {file_uuid} - no transcript available")
        except HTTPException as e:
            logger.warning(f"Skipping file {file_uuid} - {e.detail}")

    if not verified_uuids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No valid files found for processing",
        )

    # Check if LLM is configured
    extraction_service = TopicExtractionService.create_from_settings(user_id=current_user.id, db=db)

    if not extraction_service:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="LLM provider not configured. Configure LLM settings first.",
        )

    # For multi-file batches, ensure files are linked to an UploadBatch
    # so that downstream batch grouping in extract_topics_task works
    batch_uuid_str: str | None = None
    if len(verified_uuids) >= 2:
        from app.models.media import MediaFile
        from app.models.upload_batch import UploadBatch

        # Check if all files already share a batch
        batch_files = db.query(MediaFile).filter(MediaFile.uuid.in_(verified_uuids)).all()
        batch_ids = {mf.upload_batch_id for mf in batch_files if mf.upload_batch_id}

        if len(batch_ids) != 1:
            # Files don't share a single batch -- create one and link them
            import uuid as uuid_pkg

            batch = UploadBatch(
                uuid=uuid_pkg.uuid4(),
                user_id=current_user.id,
                source="batch_extract",
                file_count=len(batch_files),
            )
            db.add(batch)
            db.flush()

            for mf in batch_files:
                mf.upload_batch_id = batch.id  # type: ignore[assignment]

            db.commit()
            batch_uuid_str = str(batch.uuid)
            logger.info(
                f"Created upload batch {batch.uuid} for {len(batch_files)} files "
                f"via batch_extract_topics"
            )

    # Trigger batch task
    task = batch_extract_topics_task.delay(
        file_uuids=verified_uuids,
        force_regenerate=force_regenerate,
    )

    logger.info(
        f"Triggered batch AI suggestion extraction for {len(verified_uuids)} files, task_id: {task.id}"
    )

    result: dict[str, str | int | list[str] | None] = {
        "message": "Batch AI suggestion extraction started",
        "task_id": task.id,
        "file_count": len(verified_uuids),
        "files": verified_uuids,
    }
    if batch_uuid_str:
        result["upload_batch_id"] = batch_uuid_str

    return result


@router.get("/retroactive-auto-label/status")
async def get_retroactive_auto_label_status(
    current_user: User = Depends(get_current_active_user),
) -> dict:
    """Get the current auto-label retroactive apply progress."""
    from app.core.redis import get_redis

    redis = get_redis()
    user_id = current_user.id
    lock_key = f"auto_label_lock:{user_id}"
    progress_key = f"auto_label_progress:{user_id}"

    running = redis.exists(lock_key)
    if not running:
        return {"running": False}

    raw = redis.hgetall(progress_key)
    if not raw:
        return {"running": True, "total": 0, "processed": 0}

    total = int(raw.get(b"total", raw.get("total", 0)))
    processed = int(raw.get(b"processed", raw.get("processed", 0)))
    return {
        "running": True,
        "total": total,
        "processed": processed,
        "tags_applied": int(raw.get(b"tags_applied", raw.get("tags_applied", 0))),
        "collections_applied": int(
            raw.get(b"collections_applied", raw.get("collections_applied", 0))
        ),
        "errors": int(raw.get(b"errors", raw.get("errors", 0))),
    }


@router.post("/retroactive-auto-label", status_code=status.HTTP_202_ACCEPTED)
async def retroactive_auto_label(
    request_data: RetroactiveAutoLabelRequest = Body(
        default=RetroactiveAutoLabelRequest()  # type: ignore[call-arg]  # file_uuids defaults to None
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict:
    """Apply auto-labeling to existing files with pending suggestions."""
    from app.tasks.auto_labeling import retroactive_auto_label_task

    task = retroactive_auto_label_task.delay(
        user_id=current_user.id,
        file_uuids=request_data.file_uuids,
    )

    return {
        "message": "Retroactive auto-labeling started",
        "task_id": task.id,
    }


# =============================================================================
# PARAMETERIZED ROUTES - /{file_uuid}/* patterns
# =============================================================================


@router.post("/{file_uuid}/auto-label")
async def auto_label_single_file(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict:
    """Apply auto-labeling to a single file's pending suggestions."""
    media_file = get_file_by_uuid_with_permission(
        db, file_uuid, current_user.id, is_admin=current_user.is_admin
    )

    from app.services.auto_label_service import AutoLabelService

    service = AutoLabelService(db)
    user_settings = service.get_user_auto_label_settings(current_user.id)

    if not user_settings.get("enabled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Auto-labeling is disabled in your settings",
        )

    suggestion = (
        db.query(TopicSuggestion).filter(TopicSuggestion.media_file_id == media_file.id).first()
    )

    if not suggestion:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No AI suggestions found for this file",
        )

    result = service.auto_apply_suggestions(
        media_file=media_file,
        suggestion=suggestion,
        user_id=current_user.id,
        confidence_threshold=user_settings.get(
            "confidence_threshold", DEFAULT_AUTO_LABEL_CONFIDENCE_THRESHOLD
        ),
        apply_tags=user_settings.get("tags_enabled", True),
        apply_collections=user_settings.get("collections_enabled", True),
    )

    return {
        "message": "Auto-labeling applied",
        "tags_applied": len(result["auto_applied_tags"]),
        "collections_applied": len(result["auto_applied_collections"]),
        "tags_skipped": len(result["skipped_tags"]),
        "collections_skipped": len(result["skipped_collections"]),
    }


@router.get("/{file_uuid}/suggestions", response_model=TopicSuggestionResponse)
async def get_topic_suggestions(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> TopicSuggestionResponse:
    """
    Get AI tag and collection suggestions for a media file

    Args:
        file_uuid: Media file UUID
        db: Database session
        current_user: Authenticated user

    Returns:
        Tag and collection suggestions from AI
    """
    # Get file and verify permission
    media_file = get_file_by_uuid_with_permission(
        db, file_uuid, current_user.id, is_admin=current_user.is_admin
    )
    file_id = media_file.id

    # Get suggestion from PostgreSQL
    suggestion = db.query(TopicSuggestion).filter(TopicSuggestion.media_file_id == file_id).first()

    if not suggestion:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No AI suggestions found for file {file_uuid}. Run extraction first.",
        )

    # Convert JSONB to Pydantic models
    suggested_tags_data = suggestion.suggested_tags if suggestion.suggested_tags is not None else []
    suggested_collections_data = (
        suggestion.suggested_collections if suggestion.suggested_collections is not None else []
    )

    suggested_tags = [SuggestedTag(**tag) for tag in suggested_tags_data]  # type: ignore[arg-type,attr-defined]
    suggested_collections = [
        SuggestedCollection(**coll)
        for coll in suggested_collections_data  # type: ignore[arg-type,attr-defined]
    ]

    # Build response
    from datetime import datetime
    from uuid import UUID

    return TopicSuggestionResponse(
        uuid=UUID(str(suggestion.uuid)),
        media_file_id=UUID(str(media_file.uuid)),
        user_id=UUID(str(current_user.uuid)),
        suggested_tags=suggested_tags,
        suggested_collections=suggested_collections,
        status=str(suggestion.status),
        auto_applied_tags=suggestion.auto_applied_tags or [],
        auto_applied_collections=suggestion.auto_applied_collections or [],
        auto_apply_completed_at=suggestion.auto_apply_completed_at,
        created_at=datetime.fromisoformat(str(suggestion.created_at))
        if isinstance(suggestion.created_at, str)
        else suggestion.created_at,  # type: ignore[arg-type]
    )


@router.post("/{file_uuid}/extract", status_code=status.HTTP_202_ACCEPTED)
async def extract_topics(
    file_uuid: str,
    request_data: ExtractTopicsRequest = Body(default=ExtractTopicsRequest(force_regenerate=False)),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict[str, str]:
    """
    Extract AI suggestions from a transcript (or regenerate existing)

    This endpoint triggers the Celery task for extraction.
    Results will be delivered via WebSocket notification.

    Args:
        file_uuid: Media file UUID
        request_data: Extraction request parameters
        db: Database session
        current_user: Authenticated user

    Returns:
        Acknowledgment that extraction has been triggered
    """
    # Get file and verify permission
    media_file = get_file_by_uuid_with_permission(
        db, file_uuid, current_user.id, is_admin=current_user.is_admin
    )

    # Check if file has transcript
    if not media_file.transcript_segments:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File has no transcript. Complete transcription first.",
        )

    # Check if LLM is configured
    extraction_service = TopicExtractionService.create_from_settings(user_id=current_user.id, db=db)

    if not extraction_service:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="LLM provider not configured. Configure LLM settings first.",
        )

    # Check if suggestion already exists
    existing = (
        db.query(TopicSuggestion).filter(TopicSuggestion.media_file_id == media_file.id).first()
    )

    if existing and not request_data.force_regenerate:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="AI suggestions already exist. Use force_regenerate=true to re-extract.",
        )

    # Trigger Celery task
    task = extract_topics_task.delay(
        file_uuid=file_uuid,
        force_regenerate=request_data.force_regenerate,
    )

    logger.info(f"Triggered AI suggestion extraction for file {file_uuid}, task_id: {task.id}")

    return {
        "message": "AI suggestion extraction started",
        "task_id": task.id,
        "file_uuid": file_uuid,
    }


@router.post("/{file_uuid}/apply", response_model=dict)
async def apply_topic_suggestions(
    file_uuid: str,
    request_data: ApplyTopicSuggestionsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict[str, int | str]:
    """
    Apply user-approved AI suggestions

    This endpoint tracks what the user has accepted for analytics.
    The actual tag/collection creation happens in the frontend.

    Args:
        file_uuid: Media file UUID
        request_data: User's acceptance decisions
        db: Database session
        current_user: Authenticated user

    Returns:
        Summary of actions taken
    """
    # Get file and verify permission
    media_file = get_file_by_uuid_with_permission(
        db, file_uuid, current_user.id, is_admin=current_user.is_admin
    )
    file_id = media_file.id

    # Get suggestion
    suggestion = db.query(TopicSuggestion).filter(TopicSuggestion.media_file_id == file_id).first()

    if not suggestion:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No AI suggestions found for file {file_uuid}",
        )

    # Track user decisions
    extraction_service = TopicExtractionService(db)
    success = extraction_service.apply_suggestions(
        suggestion_id=suggestion.id,
        accepted_collections=request_data.accepted_collections,
        accepted_tags=request_data.accepted_tags,
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to track suggestions",
        )

    return {
        "message": "Suggestions tracked successfully",
        "collections_added": len(request_data.accepted_collections),
        "tags_added": len(request_data.accepted_tags),
    }


@router.delete("/{file_uuid}/suggestions", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss_topic_suggestions(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> None:
    """
    Dismiss/reject AI suggestions

    Updates suggestion status to 'rejected' but keeps the data for
    future analysis and improvement.

    Args:
        file_uuid: Media file UUID
        db: Database session
        current_user: Authenticated user
    """
    # Get file and verify permission
    media_file = get_file_by_uuid_with_permission(
        db, file_uuid, current_user.id, is_admin=current_user.is_admin
    )
    file_id = media_file.id

    # Get suggestion
    suggestion = db.query(TopicSuggestion).filter(TopicSuggestion.media_file_id == file_id).first()

    if not suggestion:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No AI suggestions found for file {file_uuid}",
        )

    # Update status
    suggestion.status = "rejected"  # type: ignore[assignment]
    suggestion.user_decisions = {"rejected": True}  # type: ignore[assignment]
    db.commit()

    logger.info(f"Dismissed AI suggestions for file {file_uuid}")
