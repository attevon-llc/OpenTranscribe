"""
API endpoints for transcript summarization and related functionality

Provides REST API access to AI-powered summarization features including
summary generation and speaker identification suggestions.

**PostgreSQL is the only store for a summary (#67).** These handlers used to
prefer a copy of ``media_file.summary_data`` held in a separate
``transcript_summaries`` OpenSearch index, falling back to the column when the
cluster was unreachable — two stores for one value, written in the same task, and
a cluster outage silently downgraded to the "fallback" with nothing
distinguishing that from having no summary. The index is retired: grounding for
chat moved to the digest plane inside the v6 ``transcript_chunks`` index
(``doc_type: "digest"``), and the *displayed* summary was always the column.
``POST /files/search`` went with it — it queried only that index.

That removed every OpenSearch hop from this module. The read/release/write phase
split these handlers carried existed **only** to keep those hops off the request
transaction, so it went with them — a ``db.close()`` between two Postgres
statements buys nothing and reads as though something slow happens in between.
The rule it followed still applies to anything reintroducing a slow call here;
see ``backend/app/tasks/CLAUDE.md``.
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
from app.models.media import MediaFile
from app.models.user import User
from app.schemas.summary import SpeakerIdentificationResponse
from app.schemas.summary import SummaryResponse
from app.schemas.summary import SummaryTaskRequest
from app.services.llm_service import is_llm_available
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
        db,
        file_uuid,
        current_user.id,
        is_admin=current_user.is_admin,
        organization_id=ctx.org_id,
        min_permission="editor",
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

    **Source: ``media_file.summary_data``, and only that.** The summarization task
    writes the column and the column is what the file page renders; the retired
    ``transcript_summaries`` index held a byte-for-byte copy that this handler
    used to prefer (#67).

    **Response includes:**
    - Complete BLUF-formatted summary
    - Processing metadata (provider, model, timing)
    """
    media_file = get_file_by_uuid_with_permission(
        db, file_uuid, current_user.id, is_admin=current_user.is_admin, organization_id=ctx.org_id
    )

    if not media_file.summary_data:
        raise HTTPException(
            status_code=404,
            detail="No summary available for this file. Please generate one first.",
        )

    # Flexible structure — a custom prompt may produce any JSON, so nothing is
    # normalized on the way out.
    return SummaryResponse(
        file_id=UUID(str(media_file.uuid)),
        filename=media_file.title or media_file.filename,
        summary_data=_redacted_summary(db, current_user, dict(media_file.summary_data)),
    )


def _redacted_summary(
    db: Session, current_user: User, summary_data: dict[str, Any]
) -> dict[str, Any]:
    """Apply the requesting user's redaction policy to a summary (#465).

    A summary is abstractive, so it restates in the model's own words whatever the
    transcript contained — a phone number the transcript view masks can appear
    verbatim in the BLUF. This surface had no masking at all, which also meant the
    admin ``redaction.force_*`` floor did not reach it.

    **Subject: the requesting user, not the file owner.** This is a *read* surface,
    and the precedent for read surfaces is bulk export (``693a16c1``, issue #85),
    which resolves ``current_user``. The owner is the right subject for *egress*
    decisions — ``redaction/llm_guard.py`` resolves the owner because sending
    their content to a third party is their data leaving — and this is not one.
    The two differ deliberately; inheriting whichever the surrounding code used is
    a documented trap here.

    ``get_file_by_uuid_with_permission`` admits share recipients, so the reader is
    routinely not the owner: a recipient whose own policy masks PII must not get an
    unmasked summary of a recording whose transcript they would see masked.

    Args:
        db: Request-scoped session.
        current_user: The reader whose policy governs.
        summary_data: A COPY of the stored column. Masking is never written back.

    Returns:
        The summary with every string leaf masked, or unchanged when the policy
        does not apply.

    Raises:
        HTTPException: 503 when the policy cannot be resolved or a detector feeding
            an enabled category could not run. Fail closed — the same disposition
            ``files/crud.py`` takes for a transcript it cannot mask.
    """
    from app.services.redaction.config import resolve_effective_config
    from app.services.redaction.summary_redaction import SummaryMaskingUnavailableError
    from app.services.redaction.summary_redaction import mask_summary

    try:
        cfg = resolve_effective_config(db, current_user.id)
    except Exception as e:
        logger.exception("Failed to resolve redaction config; refusing the summary read")
        raise HTTPException(
            status_code=503,
            detail="Redaction policy is temporarily unavailable; summary withheld.",
        ) from e

    try:
        return mask_summary(summary_data, cfg)
    except SummaryMaskingUnavailableError as e:
        logger.warning("Summary masking unavailable; withholding the summary: %s", e)
        raise HTTPException(
            status_code=503,
            detail="Redaction is temporarily unavailable; summary withheld.",
        ) from e


# NOTE: ``POST /search`` (``POST /api/files/search``, full-text search over
# generated summaries) was removed here. It queried the ``transcript_summaries``
# OpenSearch index and nothing else, and that index is retired (#67) — grounding
# moved to the digest plane in the v6 ``transcript_chunks`` index and the
# displayed summary was always ``media_file.summary_data``. Left mounted over an
# index no longer written it would have answered ``200 {"hits": [], "total": 0}``
# forever, which reads as "no matches" rather than "this feature is gone" — the
# failure mode this repo keeps finding. It had no frontend caller (it was tracked
# as an unverified xfail in ``test_route_has_a_caller.py``); an operator or agent
# calling it now gets a 404. Searching summaries over the PostgreSQL column is a
# real feature, but it is a new implementation rather than a rescue of this one.


# NOTE: A ``GET /analytics`` handler (summary analytics) was removed here. Mounted
# under the ``/files`` prefix it resolved to ``GET /api/files/analytics``, which is
# permanently shadowed by the files router's earlier ``GET /api/files/{file_uuid}``
# (UUID-typed) route → the literal ``analytics`` segment 422'd and the handler never
# ran. It had no frontend caller. Its service method
# (``OpenSearchSummaryService.get_summary_analytics``) was kept at the time for a
# future correctly-mounted endpoint; it aggregated over the now-retired
# ``transcript_summaries`` index and went with it (#67).


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
        db,
        file_uuid,
        current_user.id,
        is_admin=current_user.is_admin,
        organization_id=ctx.org_id,
        min_permission="editor",
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
    Delete a file's summary

    Clears ``summary_data`` — the summary itself — and, on a deployment upgraded
    across #67, the vestigial ``summary_opensearch_id`` pointer into the retired
    ``transcript_summaries`` index. Nothing new ever sets that column; the
    documents it pointed at are swept by file deletion and GDPR erasure
    (``services/file_cleanup_service.py``) until an operator drops the index.
    """
    media_file = get_file_by_uuid_with_permission(
        db,
        file_uuid,
        current_user.id,
        is_admin=current_user.is_admin,
        organization_id=ctx.org_id,
        min_permission="editor",
    )
    file_id = int(media_file.id)
    response_uuid = str(media_file.uuid)

    # ``dict[Any, Any]`` because ``Query.update`` accepts string OR column keys
    # and its parameter type is invariant in the key.
    updates: dict[Any, Any] = {}
    if media_file.summary_data:
        updates["summary_data"] = None
    if media_file.summary_opensearch_id:
        updates["summary_opensearch_id"] = None

    if not updates:
        raise HTTPException(status_code=404, detail="No summary found to delete")

    try:
        db.query(MediaFile).filter(MediaFile.id == file_id).update(
            updates, synchronize_session=False
        )
        db.commit()
    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. Nothing in the block above
        # raises one today, but the broad handler below would report any that
        # appeared as a 500 (issue #431) — and `test_http_exception_passthrough`
        # enforces the guard structurally, not by proving the block is safe.
        raise
    except Exception as e:
        logger.error("Failed to delete summary for file %s: %s", file_id, e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e

    logger.info(f"Deleted summary for file {file_id}")
    return {"message": "Summary deleted successfully", "file_id": response_uuid}
