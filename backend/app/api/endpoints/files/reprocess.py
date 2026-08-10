import logging
import uuid as uuid_pkg
from decimal import Decimal

from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.services import system_settings_service

logger = logging.getLogger(__name__)


def _fire_gpu_rework_access_gate(media_file: MediaFile) -> None:
    """Cloud-edition access gate for user-triggered GPU rework (issue #262i).

    Stages that re-run GPU work WITHOUT going through
    ``dispatch_transcription_pipeline`` (currently only ``rediarize`` — the
    other selective stages are CPU/LLM work) would otherwise burn GPU for a
    suspended/canceled org. Fire the existing before-dispatch quota seam with a
    ZERO-hours estimate: the transcript was already billed so there is nothing
    to meter (a 0-hour reservation is harmless — nothing to release), but the
    cloud enforcer's access-state check (suspension/cancel/dispute) can still
    block with 402. Community edition: no hooks registered — a no-op. No seam
    signature change (CLOUD_SEAM_VERSION stays 2).

    Raises:
        QuotaExceededError: (HTTP 402) when a registered hook blocks the org.
    """
    from app.tasks.transcription.hooks import DispatchContext
    from app.tasks.transcription.hooks import fire_before_dispatch

    fire_before_dispatch(
        DispatchContext(
            file_id=int(media_file.id),
            file_uuid=str(media_file.uuid),
            user_id=int(media_file.user_id),
            organization_id=int(media_file.organization_id) if media_file.organization_id else None,
            est_audio_hours=Decimal(0),
            # The rediarize Celery id doesn't exist yet — a fresh id scopes the
            # (empty) reservation without colliding with a pipeline run.
            task_id=str(uuid_pkg.uuid4()),
        )
    )


def clear_existing_transcription_data(db: Session, media_file: MediaFile) -> None:
    """
    Clear existing transcription data for reprocessing.

    Args:
        db: Database session
        media_file: MediaFile to clear data for
    """
    try:
        # Clear transcript-related fields that exist on the MediaFile model
        media_file.summary_data = None  # type: ignore[assignment]
        media_file.summary_opensearch_id = None  # type: ignore[assignment]  # Clear OpenSearch summary ID for regeneration
        media_file.summary_status = "pending"  # type: ignore[assignment]  # Reset summary status for regeneration
        media_file.translated_text = None  # type: ignore[assignment]
        media_file.waveform_data = None  # type: ignore[assignment]  # Clear waveform data for regeneration

        # Clear existing transcript segments
        from app.models.media import Analytics
        from app.models.media import Speaker
        from app.models.media import TranscriptSegment

        # Delete existing transcript segments (this will cascade and handle relationships)
        existing_segments = (
            db.query(TranscriptSegment)
            .filter(TranscriptSegment.media_file_id == media_file.id)
            .all()
        )
        for segment in existing_segments:
            db.delete(segment)

        # Clear any existing speaker data (extract UUIDs first, then bulk delete)
        speaker_uuids_to_clean = [
            str(r[0])
            for r in db.query(Speaker.uuid).filter(Speaker.media_file_id == media_file.id).all()
        ]
        db.query(Speaker).filter(Speaker.media_file_id == media_file.id).delete(
            synchronize_session=False
        )

        # Clear any existing analytics data
        existing_analytics = (
            db.query(Analytics).filter(Analytics.media_file_id == media_file.id).first()
        )
        if existing_analytics:
            db.delete(existing_analytics)

        db.commit()
        logger.info(f"Cleared existing transcription data for file {media_file.id}")

        # Remove deleted speakers from all OpenSearch speaker indices (non-fatal)
        if speaker_uuids_to_clean:
            try:
                from app.services.opensearch_service import remove_speaker_embedding

                for uuid in speaker_uuids_to_clean:
                    remove_speaker_embedding(uuid)
                logger.info(
                    f"Removed {len(speaker_uuids_to_clean)} speaker embeddings "
                    f"from OpenSearch for reprocessed file {media_file.id}"
                )
            except Exception as os_err:
                logger.warning(f"OpenSearch speaker cleanup failed (non-fatal): {os_err}")

    except Exception as e:
        logger.exception(f"Error clearing transcription data for file {media_file.id}: {e}")
        db.rollback()
        raise


def start_reprocessing_task(
    file_uuid: str,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    num_speakers: int | None = None,
    downstream_tasks: list[str] | None = None,
    whisper_model: str | None = None,
    user_id: int | None = None,
    db=None,
    disable_diarization: bool | None = None,
) -> None:
    """
    Start the background reprocessing task.

    The transcription pipeline auto-resolves the correct queue based on the user's
    active ASR provider (cloud-asr for cloud providers, gpu for local).

    Args:
        file_uuid: UUID of the media file to reprocess
        min_speakers: Optional minimum number of speakers for diarization
        max_speakers: Optional maximum number of speakers for diarization
        num_speakers: Optional fixed number of speakers for diarization
        downstream_tasks: Optional list of downstream pipeline stage names to run after transcription
        whisper_model: Optional Whisper model override for this transcription
        user_id: Unused (kept for backward compatibility)
        db: Unused (kept for backward compatibility)
    """
    import os

    if os.environ.get("SKIP_CELERY", "False").lower() != "true":
        # Dispatch 3-stage pipeline chain: CPU preprocess → GPU transcribe → CPU postprocess
        # Queue routing is auto-resolved inside dispatch_transcription_pipeline
        from app.tasks.transcription import dispatch_transcription_pipeline

        dispatch_transcription_pipeline(
            file_uuid=file_uuid,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            num_speakers=num_speakers,
            downstream_tasks=downstream_tasks,
            disable_diarization=disable_diarization,
            whisper_model=whisper_model,
        )
    else:
        logger.info("Skipping Celery task in test environment")


def clear_selective_data(db: Session, media_file: MediaFile, stages: list[str]) -> None:
    """Clear data only for selected pipeline stages."""
    from app.models.media import Analytics
    from app.models.media import Speaker
    from app.models.media import TranscriptSegment

    try:
        if "transcription" in stages:
            # Full transcription clear - use existing function
            clear_existing_transcription_data(db, media_file)
            return  # Already clears everything

        if "rediarize" in stages:
            # Null out speaker_id on segments first to avoid FK constraint violations,
            # then delete speakers. Transcript text/words are preserved.
            db.query(TranscriptSegment).filter(
                TranscriptSegment.media_file_id == media_file.id
            ).update({TranscriptSegment.speaker_id: None}, synchronize_session=False)

            speaker_uuids_to_clean = [
                str(r[0])
                for r in db.query(Speaker.uuid).filter(Speaker.media_file_id == media_file.id).all()
            ]
            db.query(Speaker).filter(Speaker.media_file_id == media_file.id).delete(
                synchronize_session=False
            )
            # Remove deleted speakers from OpenSearch (non-fatal)
            if speaker_uuids_to_clean:
                try:
                    from app.services.opensearch_service import remove_speaker_embedding

                    for uuid in speaker_uuids_to_clean:
                        remove_speaker_embedding(uuid)
                except Exception as os_err:
                    logger.warning(f"OpenSearch speaker cleanup failed (non-fatal): {os_err}")
            # Also clear analytics since speaker stats change
            existing_analytics = (
                db.query(Analytics).filter(Analytics.media_file_id == media_file.id).first()
            )
            if existing_analytics:
                db.delete(existing_analytics)

        if "analytics" in stages:
            existing_analytics = (
                db.query(Analytics).filter(Analytics.media_file_id == media_file.id).first()
            )
            if existing_analytics:
                db.delete(existing_analytics)

        if "speaker_llm" in stages:
            # Reset suggested names on speakers
            existing_speakers = (
                db.query(Speaker).filter(Speaker.media_file_id == media_file.id).all()
            )
            for speaker in existing_speakers:
                speaker.suggested_name = None
                speaker.confidence = None

        if "summarization" in stages:
            media_file.summary_data = None
            media_file.summary_opensearch_id = None
            media_file.summary_status = "pending"

        if "topic_extraction" in stages:
            # Clear AI-generated topic suggestions
            from app.models.topic import TopicSuggestion

            existing_suggestions = (
                db.query(TopicSuggestion)
                .filter(TopicSuggestion.media_file_id == media_file.id)
                .all()
            )
            for suggestion in existing_suggestions:
                db.delete(suggestion)

        db.commit()
        logger.info(f"Cleared selective data for stages {stages} on file {media_file.id}")
    except Exception as e:
        logger.exception(f"Error clearing selective data for file {media_file.id}: {e}")
        db.rollback()
        raise


def dispatch_task_by_name(
    stage: str,
    file_uuid: str,
    file_id: int | None = None,
    user_id: int | None = None,
) -> None:
    """Dispatch a single pipeline task by stage name.

    Args:
        stage: Pipeline stage name.
        file_uuid: File UUID string.
        file_id: Internal file ID (required for search_indexing).
        user_id: Owner user ID (required for search_indexing).
    """
    import os

    if os.environ.get("SKIP_CELERY", "False").lower() == "true":
        logger.info("Skipping Celery task in test environment")
        return

    if stage == "search_indexing":
        from app.tasks.search_indexing_task import index_transcript_search_task

        # Resolve file_id and user_id from DB if not provided
        if file_id is None or user_id is None:
            from app.db.session_utils import session_scope
            from app.utils.uuid_helpers import get_file_by_uuid

            with session_scope() as db:
                media_file = get_file_by_uuid(db, file_uuid)
                file_id = media_file.id
                user_id = int(media_file.user_id)

        index_transcript_search_task.delay(file_id=file_id, file_uuid=file_uuid, user_id=user_id)
    elif stage == "analytics":
        from app.tasks.analytics import analyze_transcript_task

        analyze_transcript_task.delay(file_uuid=file_uuid)
    elif stage == "speaker_llm":
        from app.tasks.speaker_tasks import identify_speakers_llm_task

        identify_speakers_llm_task.delay(file_uuid=file_uuid)
    elif stage == "summarization":
        from app.tasks.summarization import summarize_transcript_task

        summarize_transcript_task.delay(file_uuid=file_uuid)
    elif stage == "topic_extraction":
        from app.tasks.topic_extraction import extract_topics_task

        extract_topics_task.delay(file_uuid=file_uuid, force_regenerate=True)
    elif stage == "speaker_clustering":
        if user_id is None:
            from app.db.session_utils import session_scope
            from app.utils.uuid_helpers import get_file_by_uuid

            with session_scope() as db:
                media_file = get_file_by_uuid(db, file_uuid)
                user_id = int(media_file.user_id)

        from app.tasks.speaker_clustering import cluster_speakers_for_file

        cluster_speakers_for_file.delay(file_uuid, user_id)
    else:
        logger.warning(f"Unknown stage '{stage}' for dispatch")


def dispatch_selective_tasks(
    file_uuid: str,
    stages: list[str],
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    num_speakers: int | None = None,
    file_id: int | None = None,
    user_id: int | None = None,
    whisper_model: str | None = None,
) -> None:
    """Dispatch Celery tasks for selected pipeline stages.

    Args:
        file_uuid: File UUID string.
        stages: List of pipeline stage names to dispatch.
        min_speakers: Optional minimum speakers for diarization.
        max_speakers: Optional maximum speakers for diarization.
        num_speakers: Optional fixed speaker count for diarization.
        file_id: Internal file ID (passed to tasks that need it).
        user_id: Owner user ID (passed to tasks that need it).
        whisper_model: Optional Whisper model override for transcription.
    """
    import os

    if os.environ.get("SKIP_CELERY", "False").lower() == "true":
        logger.info("Skipping Celery tasks in test environment")
        return

    if "transcription" in stages:
        # Transcription subsumes rediarize
        downstream = [s for s in stages if s not in ("transcription", "rediarize")]
        start_reprocessing_task(
            file_uuid,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            num_speakers=num_speakers,
            downstream_tasks=downstream if downstream else None,
            whisper_model=whisper_model,
            user_id=user_id,
        )
    elif "rediarize" in stages:
        from app.tasks.rediarize_task import rediarize_task

        other_stages = [s for s in stages if s != "rediarize"]
        rediarize_task.delay(
            file_uuid,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            num_speakers=num_speakers,
            downstream_tasks=other_stages if other_stages else None,
        )
    else:
        # Pure downstream tasks - dispatch each directly
        for stage in stages:
            dispatch_task_by_name(stage, file_uuid, file_id=file_id, user_id=user_id)


def process_file_reprocess(
    file_uuid: str,
    db: Session,
    current_user: User,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    num_speakers: int | None = None,
    stages: list[str] | None = None,
    whisper_model: str | None = None,
    *,
    organization_id: OrgScope = UNSCOPED,
) -> MediaFile:
    """
    Process file reprocessing request with enhanced error handling.

    Args:
        file_uuid: UUID of the file to reprocess
        db: Database session
        current_user: Current user
        min_speakers: Optional minimum number of speakers for diarization
        max_speakers: Optional maximum number of speakers for diarization
        num_speakers: Optional fixed number of speakers for diarization
        stages: Optional list of pipeline stages to re-run. Empty/None = full reprocess.
        whisper_model: Optional Whisper model override for this transcription.
        organization_id: Active org id, None for personal, or UNSCOPED (legacy).

    Returns:
        Updated MediaFile object

    Raises:
        HTTPException: If file not found or user doesn't have permission
    """
    from app.utils.task_utils import cancel_active_task
    from app.utils.task_utils import reset_file_for_retry
    from app.utils.uuid_helpers import get_file_by_uuid
    from app.utils.uuid_helpers import get_file_by_uuid_with_permission

    try:
        # Get the file (allow admin to reprocess any file)
        is_admin = current_user.is_admin
        if is_admin:
            media_file = get_file_by_uuid(db, file_uuid)
        else:
            media_file = get_file_by_uuid_with_permission(
                db,
                file_uuid,
                current_user.id,
                is_admin=current_user.is_admin,
                organization_id=organization_id,
            )

        file_id = media_file.id  # Get internal ID for task operations

        # Check if file is currently processing
        if media_file.status == FileStatus.PROCESSING and media_file.active_task_id:
            # Cancel active task first
            logger.info(
                f"Cancelling active task {media_file.active_task_id} before reprocessing file {file_id}"
            )
            cancel_active_task(db, int(file_id))

        # Check if file exists in storage
        if not media_file.storage_path:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File storage path not found. Cannot reprocess.",
            )

        # Check retry limits based on system settings (unless admin)
        if not is_admin and not system_settings_service.should_retry_file(
            db, int(media_file.retry_count or 0)
        ):
            config = system_settings_service.get_retry_config(db)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File has reached maximum retry attempts ({config['max_retries']}). Contact admin for help.",
            )

        # Store the user's requested whisper model before dispatching
        if whisper_model:
            media_file.requested_whisper_model = whisper_model  # type: ignore[assignment]
            db.commit()

        logger.info(
            f"Starting reprocessing for file {file_uuid} (id: {file_id}) by user {current_user.email}"
        )

        if stages:
            # Selective pipeline reprocessing
            logger.info(f"Selective reprocessing stages: {stages}")

            # Access gate for GPU rework dispatched OUTSIDE the pipeline
            # (issue #262i). Fired BEFORE clear_selective_data so a blocked
            # org's existing diarization is never destroyed. The transcription
            # stage is gated inside dispatch_transcription_pipeline instead
            # (with a real hours estimate).
            if "rediarize" in stages and "transcription" not in stages:
                _fire_gpu_rework_access_gate(media_file)

            # Only reset file status for transcription stage (full reprocess behavior)
            if "transcription" in stages:
                success = reset_file_for_retry(db, int(file_id), reset_retry_count=False)
                if not success:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Failed to reset file for reprocessing",
                    )

            # Clear data for selected stages
            clear_selective_data(db, media_file, stages)

            # Refresh the file object
            db.refresh(media_file)

            # Dispatch tasks for selected stages
            dispatch_selective_tasks(
                file_uuid,
                stages,
                min_speakers,
                max_speakers,
                num_speakers,
                file_id=file_id,
                user_id=current_user.id,
                whisper_model=whisper_model,
            )

            logger.info(
                f"Selective reprocessing tasks dispatched for file {file_uuid} "
                f"(id: {file_id}, stages: {stages})"
            )
        else:
            # Full reprocess (backward compatible)
            success = reset_file_for_retry(db, int(file_id), reset_retry_count=False)
            if not success:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to reset file for reprocessing",
                )

            # Refresh the file object
            db.refresh(media_file)

            # Start background reprocessing task with speaker parameters
            start_reprocessing_task(
                file_uuid,
                min_speakers,
                max_speakers,
                num_speakers,
                whisper_model=whisper_model,
                user_id=current_user.id,
                db=db,
            )

            logger.info(
                f"Reprocessing task started for file {file_uuid} (id: {file_id}, attempt {media_file.retry_count})"
            )

        return media_file

    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.exception(f"Error processing reprocess request for file {file_uuid}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error processing reprocess request",
        ) from e
