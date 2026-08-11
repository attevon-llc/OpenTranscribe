"""
API endpoints for transcript summarization and related functionality

Provides REST API access to AI-powered summarization features including
summary generation, search, analytics, and speaker identification suggestions.
"""

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Path
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.models.user import User
from app.schemas.summary import SpeakerIdentificationResponse
from app.schemas.summary import SummaryResponse
from app.schemas.summary import SummarySearchRequest
from app.schemas.summary import SummarySearchResponse
from app.schemas.summary import SummaryTaskRequest
from app.services.llm_service import is_llm_available
from app.services.opensearch_summary_service import OpenSearchSummaryService
from app.tasks.speaker_tasks import identify_speakers_llm_task
from app.tasks.summarization import summarize_transcript_task
from app.utils.uuid_helpers import get_file_by_uuid_with_permission

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/{file_uuid}/summarize", response_model=dict[str, Any])
async def trigger_summarization(
    file_uuid: str = Path(..., description="UUID of the media file to summarize"),
    request: SummaryTaskRequest = SummaryTaskRequest(),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
    db: Session = Depends(get_db),
):
    """
    Trigger AI-powered summarization for a transcript

    This endpoint starts a background task to generate a comprehensive BLUF-format
    summary using your configured LLM provider (vLLM, OpenAI, Ollama, etc.).

    **Requirements:**
    - File must belong to the authenticated user
    - Transcript must be completed and available
    - Speaker embedding matching should be completed for best results

    **Process:**
    1. Validates file ownership and transcript availability
    2. Starts background Celery task for LLM summarization
    3. Returns task ID for progress tracking

    **Summary Format:**
    - **BLUF**: Bottom Line Up Front executive summary
    - **Speaker Analysis**: Talk time, key points, contributions
    - **Content Sections**: Time-based topic breakdown
    - **Action Items**: Assigned tasks with priorities and due dates
    - **Key Decisions**: Concrete decisions made
    - **Follow-up Items**: Future discussion points
    """
    # Verify file exists and belongs to user (tenant-gated via ctx.org_id)
    media_file = get_file_by_uuid_with_permission(
        db, file_uuid, current_user.id, is_admin=current_user.is_admin, organization_id=ctx.org_id
    )
    file_id = media_file.id

    # Check if file has completed transcription
    if not media_file.transcript_segments:
        raise HTTPException(
            status_code=400,
            detail="File must have completed transcription before summarization",
        )

    # Check system-wide disable (non-admin users blocked)
    if getattr(current_user, "role", None) != "admin":
        from app.utils.summary_settings import is_summary_enabled_system

        if not is_summary_enabled_system(db):
            raise HTTPException(
                status_code=423,
                detail=(
                    "AI summary generation has been disabled by the system "
                    "administrator. Contact your admin to re-enable."
                ),
            )

    # Per-file disabled status: manual trigger resets it (explicit user intent)
    if str(media_file.summary_status) == "disabled":
        media_file.summary_status = "pending"  # type: ignore[assignment]
        db.commit()
        logger.info(
            f"User manually triggered summary for file {file_id}; "
            f"resetting per-file disabled status"
        )

    # Check LLM availability before starting the task
    llm_available = await is_llm_available(user_id=current_user.id)
    if not llm_available:
        raise HTTPException(
            status_code=503,
            detail="AI summarization is currently unavailable. Please configure an LLM provider or check your server configuration. You can still access all transcription features.",
        )

    try:
        # Start summarization task
        task = summarize_transcript_task.delay(
            file_uuid=file_uuid,
            force_regenerate=request.force_regenerate,
            prompt_uuid=request.prompt_uuid,
        )

        logger.info(
            f"Started summarization task {task.id} for file {file_id} (force_regenerate={request.force_regenerate})"
        )

        # Send queued notification immediately
        from app.tasks.summarization import send_summary_notification

        send_summary_notification(
            current_user.id,
            file_id,
            "queued",
            f"AI summary {'regeneration' if request.force_regenerate else 'generation'} has been queued for processing",
        )

        # Get LLM provider info for response
        from app.core.config import settings

        provider = settings.LLM_PROVIDER or "default"
        # Get model name based on provider
        model_map = {
            "vllm": settings.VLLM_MODEL_NAME,
            "openai": settings.OPENAI_MODEL_NAME,
            "ollama": settings.OLLAMA_MODEL_NAME,
            "anthropic": settings.ANTHROPIC_MODEL_NAME,
            "openrouter": settings.OPENAI_MODEL_NAME,  # OpenRouter uses OpenAI-compatible API
        }
        model = model_map.get(provider.lower(), "default") if provider else "default"

        return {
            "message": "Summarization task started",
            "task_id": task.id,
            "file_id": str(media_file.uuid),  # Use UUID for frontend
            "provider": provider,
            "model": model,
        }

    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error(
            "Failed to start summarization task for file %s: %s", file_id, e, exc_info=True
        )
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.get("/{file_uuid}/summary", response_model=SummaryResponse)
def get_file_summary(
    file_uuid: str = Path(..., description="UUID of the media file"),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
    db: Session = Depends(get_db),
):
    """
    Retrieve the latest AI-generated summary for a file

    Returns the most recent structured summary with BLUF format,
    including speaker analysis, action items, decisions, and more.

    **Data Sources:**
    - Primary: OpenSearch (full structured data)
    - Fallback: PostgreSQL (basic summary text)

    **Response includes:**
    - Complete BLUF-formatted summary
    - Processing metadata (provider, model, timing)
    - Search-optimized content for highlighting
    """
    # Verify file exists and belongs to user (tenant-gated via ctx.org_id)
    media_file = get_file_by_uuid_with_permission(
        db, file_uuid, current_user.id, is_admin=current_user.is_admin, organization_id=ctx.org_id
    )
    file_id = media_file.id

    try:
        # Try to get structured summary from OpenSearch
        summary_service = OpenSearchSummaryService()
        opensearch_result = summary_service.get_summary_by_file_id(file_id, current_user.id)

        if opensearch_result and opensearch_result.get("summary_data"):
            # Return flexible summary structure - no field normalization needed
            # The summary can have any structure from custom AI prompts
            summary_data = opensearch_result.get("summary_data", {})

            return SummaryResponse(
                file_id=UUID(str(media_file.uuid)),
                filename=media_file.title or media_file.filename,
                summary_data=summary_data,
                source="opensearch",
                document_id=opensearch_result.get("document_id"),
                created_at=opensearch_result.get("created_at"),
                updated_at=opensearch_result.get("updated_at"),
            )

        # Fallback: Try to get from PostgreSQL if OpenSearch failed
        if media_file.summary_data:
            logger.info(f"Returning summary from PostgreSQL for file {file_id}")
            return SummaryResponse(
                file_id=UUID(str(media_file.uuid)),
                filename=media_file.title or media_file.filename,
                summary_data=dict(media_file.summary_data),
                source="postgresql",
            )

        # No summary available
        raise HTTPException(
            status_code=404,
            detail="No summary available for this file. Please generate one first.",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to retrieve summary for file %s: %s", file_id, e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.post("/search", response_model=SummarySearchResponse)
def search_summaries(
    search_request: SummarySearchRequest,
    current_user: User = Depends(get_current_active_user),
):
    """
    Search across all user summaries with advanced filtering

    Performs full-text search across summary content with support for:
    - Text queries with fuzzy matching
    - Speaker filtering
    - Date range filtering
    - Action item status filtering
    - Content highlighting

    **Search Features:**
    - **Multi-field search**: BLUF, content, decisions, action items
    - **Fuzzy matching**: Handles typos and variations
    - **Highlighting**: Shows matched content with <mark> tags
    - **Pagination**: Efficient large result handling
    - **Sorting**: Relevance or date-based ordering

    **Use Cases:**
    - "Find meetings where John discussed the budget"
    - "Show all action items assigned to Sarah"
    - "Search for decisions made about the product launch"
    """
    try:
        summary_service = OpenSearchSummaryService()

        # Build search parameters
        search_params = {
            "text": search_request.query,
            "speakers": search_request.speakers,
            "date_from": search_request.date_from.isoformat() if search_request.date_from else None,
            "date_to": search_request.date_to.isoformat() if search_request.date_to else None,
            "has_pending_actions": search_request.has_pending_actions,
        }

        # Execute search
        results = summary_service.search_summaries(
            query=search_params,
            user_id=current_user.id,
            size=search_request.size,
            from_=search_request.offset,
        )

        return SummarySearchResponse(
            hits=results["hits"],
            total=results["total"],
            max_score=results.get("max_score"),
            query=search_request.query,
            filters=search_params,
        )

    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Summary search failed for user %s: %s", current_user.id, e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e


# NOTE: A ``GET /analytics`` handler (summary analytics) was removed here. Mounted
# under the ``/files`` prefix it resolved to ``GET /api/files/analytics``, which is
# permanently shadowed by the files router's earlier ``GET /api/files/{file_uuid}``
# (UUID-typed) route → the literal ``analytics`` segment 422'd and the handler never
# ran. It had no frontend caller. The underlying
# ``OpenSearchSummaryService.get_summary_analytics`` service method is retained for
# any future, correctly-mounted endpoint.


@router.post("/{file_uuid}/identify-speakers", response_model=SpeakerIdentificationResponse)
async def identify_speakers(
    file_uuid: str = Path(..., description="UUID of the media file"),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
    db: Session = Depends(get_db),
):
    """
    Generate LLM-based speaker identification suggestions

    Uses AI analysis to provide speaker identification suggestions based on:
    - Content analysis and expertise areas
    - Role indicators and decision-making patterns
    - Speech patterns and language usage
    - Context clues and references

    **Important Notes:**
    - Suggestions are NOT automatically applied
    - Users must manually review and approve suggestions
    - Confidence scores indicate reliability
    - Cross-references with known speaker profiles

    **Analysis Process:**
    1. Analyzes conversation content for role indicators
    2. Compares against known speaker profiles
    3. Generates confidence-scored predictions
    4. Provides reasoning and evidence for each suggestion

    **Use Cases:**
    - Help identify speakers in large meetings
    - Suggest matches with existing speaker profiles
    - Provide context clues for manual identification
    """
    # Verify file exists and belongs to user (tenant-gated via ctx.org_id)
    media_file = get_file_by_uuid_with_permission(
        db, file_uuid, current_user.id, is_admin=current_user.is_admin, organization_id=ctx.org_id
    )
    file_id = media_file.id

    # Check if file has speakers to identify
    if not media_file.speakers:
        raise HTTPException(status_code=400, detail="No speakers found in this file to identify")

    # Check LLM availability before starting the task
    llm_available = await is_llm_available(user_id=current_user.id)
    if not llm_available:
        raise HTTPException(
            status_code=503,
            detail="AI speaker identification is currently unavailable. Please configure an LLM provider or check your server configuration. You can still manually update speaker names.",
        )

    try:
        # Start speaker identification task
        task = identify_speakers_llm_task.delay(file_uuid=file_uuid)

        logger.info(f"Started speaker identification task {task.id} for file {file_id}")

        return SpeakerIdentificationResponse(
            message="Speaker identification task started",
            task_id=task.id,
            file_id=UUID(str(media_file.uuid)),
            speaker_count=len(media_file.speakers),
        )

    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error(
            "Failed to start speaker identification for file %s: %s", file_id, e, exc_info=True
        )
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.delete("/{file_uuid}/summary")
def delete_summary(
    file_uuid: str = Path(..., description="UUID of the media file"),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
    db: Session = Depends(get_db),
):
    """
    Delete a file's summary from both OpenSearch and PostgreSQL

    Removes all summary data associated with a file, including:
    - OpenSearch structured summary document
    - PostgreSQL summary text
    - OpenSearch document reference

    **Cleanup includes:**
    - Primary OpenSearch summary document
    - PostgreSQL summary text field
    - Document ID references
    - Search index entries
    """
    # Verify file exists and belongs to user (tenant-gated via ctx.org_id)
    media_file = get_file_by_uuid_with_permission(
        db, file_uuid, current_user.id, is_admin=current_user.is_admin, organization_id=ctx.org_id
    )
    file_id = media_file.id

    try:
        summary_service = OpenSearchSummaryService()
        deleted = False

        # Delete from OpenSearch if document ID exists
        if hasattr(media_file, "summary_opensearch_id") and media_file.summary_opensearch_id:
            opensearch_deleted = summary_service.delete_summary(
                str(media_file.summary_opensearch_id)
            )
            if opensearch_deleted:
                media_file.summary_opensearch_id = None  # type: ignore[assignment]
                deleted = True

        # Clear PostgreSQL summary
        if media_file.summary_data:
            media_file.summary_data = None  # type: ignore[assignment]
            deleted = True

        if deleted:
            db.commit()
            logger.info(f"Deleted summary for file {file_id}")
            return {"message": "Summary deleted successfully", "file_id": str(media_file.uuid)}
        else:
            raise HTTPException(status_code=404, detail="No summary found to delete")

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to delete summary for file %s: %s", file_id, e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e
