"""Post-transcription downstream task dispatch.

Summarization, topic extraction, analytics, and speaker clustering.
"""

import logging

from app.core.enums import PostProcessingState
from app.core.enums import PostProcessingTask
from app.db.session_utils import session_scope
from app.models.media import MediaFile

logger = logging.getLogger(__name__)


def _dispatch_automatic_summary(
    file_id: int, file_uuid: str, collection_prompt_uuid: str | None, llm_configured: bool
) -> PostProcessingState:
    """Check summary disable settings and dispatch if enabled.

    Returns the post-processing state for the plan. With no LLM configured the task is
    still dispatched (it records ``not_configured`` for the file page) but reported
    SKIPPED, since no summary will be produced.
    """
    from app.tasks.summarization import send_summary_notification
    from app.tasks.summarization import summarize_transcript_task
    from app.utils.summary_settings import get_summary_disable_reason

    with session_scope() as db:
        media_file = db.query(MediaFile).filter(MediaFile.uuid == file_uuid).first()
        if not media_file:
            logger.warning(f"File {file_uuid} not found for summary dispatch")
            return PostProcessingState.SKIPPED

        if str(media_file.summary_status) == "disabled":
            logger.info(f"Summary disabled for file {file_id} (per-file flag)")
            send_summary_notification(
                int(media_file.user_id),
                file_id,
                "disabled",
                "AI summary generation is disabled for this file",
                0,
            )
            return PostProcessingState.SKIPPED

        disable_reason = get_summary_disable_reason(db, int(media_file.user_id))
        if disable_reason:
            media_file.summary_status = "disabled"  # type: ignore[assignment]
            db.commit()
            reason_msg = (
                "AI summary generation has been disabled by the system administrator"
                if disable_reason == "system"
                else "AI summary auto-generation is disabled in your settings"
            )
            logger.info(f"Summary disabled for file {file_id} (reason: {disable_reason})")
            send_summary_notification(
                int(media_file.user_id),
                file_id,
                "disabled",
                reason_msg,
                0,
            )
            return PostProcessingState.SKIPPED

    summary_task = summarize_transcript_task.delay(
        file_uuid=file_uuid,
        prompt_uuid=collection_prompt_uuid,
    )
    logger.info(f"Automatic summarization task {summary_task.id} started for file {file_id}")
    return PostProcessingState.QUEUED if llm_configured else PostProcessingState.SKIPPED


def _owner_llm_configured(file_id: int) -> bool:
    """The repo's one answer to "is an LLM configured?" for the file's owner (DB only)."""
    from app.services.llm_service import LLMService

    try:
        with session_scope() as db:
            owner = db.query(MediaFile.user_id).filter(MediaFile.id == file_id).scalar()
            return owner is not None and LLMService.is_configured_for_user(db, int(owner))
    except Exception as e:  # noqa: BLE001 — the plan then waits for the tasks to report
        logger.warning(f"LLM availability check failed for file {file_id}: {e}")
        return True


def _auto_topic_extraction_enabled(file_uuid: str) -> bool:
    """Owner's ``auto_topic_extraction`` switch (user > system default > True)."""
    from app.services.auto_llm_settings import resolve_auto_llm_flags

    with session_scope() as db:
        owner = db.query(MediaFile.user_id).filter(MediaFile.uuid == file_uuid).scalar()
        if owner is None:
            return True
        enabled = resolve_auto_llm_flags(db, int(owner)).topic_extraction
    if not enabled:
        logger.info(f"Automatic topic extraction is off for the owner of {file_uuid}; skipping")
    return enabled


def _get_collection_prompt_uuid(file_id: int) -> str | None:
    """Look up the default summary prompt UUID from the file's first collection (by added_at)."""
    try:
        from app.db.session_utils import session_scope
        from app.models.media import Collection
        from app.models.media import CollectionMember
        from app.models.prompt import SummaryPrompt

        with session_scope() as db:
            result = (
                db.query(SummaryPrompt.uuid)
                .join(Collection, Collection.default_summary_prompt_id == SummaryPrompt.id)
                .join(CollectionMember, CollectionMember.collection_id == Collection.id)
                .filter(
                    CollectionMember.media_file_id == file_id,
                    SummaryPrompt.is_active,
                )
                .order_by(CollectionMember.added_at.asc())
                .first()
            )

            if result:
                logger.info(f"File {file_id} using collection default prompt: {result[0]}")
                return str(result[0])

        return None
    except Exception as e:
        logger.warning(f"Failed to get collection prompt for file {file_id}: {e}")
        return None


# Import for automatic summarization, speaker identification, and analytics
def trigger_automatic_summarization(
    file_id: int, file_uuid: str, tasks_to_run: list[str] | None = None
) -> dict[PostProcessingTask, PostProcessingState]:
    """Trigger automatic summarization, speaker identification, and analytics after transcription completes.

    Note: In the default (full) flow, LLM speaker identification is dispatched by
    detect_speaker_attributes_task after gender detection completes, ensuring gender
    data is available to the LLM. When tasks_to_run explicitly includes 'speaker_llm',
    it is dispatched directly for selective reprocessing.

    Args:
        file_id: Internal file ID
        file_uuid: File UUID string
        tasks_to_run: Optional list of specific stages to run. None = run all tasks.
            Valid values: 'analytics', 'speaker_llm', 'summarization',
            'topic_extraction', 'search_indexing', 'speaker_clustering'

    Returns:
        The post-processing state of every task this decided on (queued, or skipped by a
        setting or a missing LLM). A task it did not get to is absent, so nothing waits
        for it.
    """
    decisions: dict[PostProcessingTask, PostProcessingState] = {}
    try:
        # Analytics computation
        if tasks_to_run is None or "analytics" in tasks_to_run:
            from app.tasks.analytics import analyze_transcript_task

            analytics_task = analyze_transcript_task.delay(file_uuid=file_uuid)
            decisions[PostProcessingTask.ANALYTICS] = PostProcessingState.QUEUED
            logger.info(
                f"Automatic analytics computation task {analytics_task.id} started for file {file_id}"
            )

        # Speaker LLM identification: always chained from detect_speaker_attributes_task
        # (dispatched by _dispatch_speaker_attributes in postprocess). Gender detection
        # runs first, then chains to LLM speaker ID to ensure gender/age context is
        # available. No direct dispatch needed here — would cause double dispatch.

        # Note: search_indexing is dispatched by the postprocess stage (always runs
        # during transcription). No need to dispatch it here to avoid double dispatch.

        # Look up collection default prompt for this file
        collection_prompt_uuid = _get_collection_prompt_uuid(file_id)

        llm_configured = _owner_llm_configured(file_id)

        # Summarization
        if tasks_to_run is None or "summarization" in tasks_to_run:
            decisions[PostProcessingTask.SUMMARY] = _dispatch_automatic_summary(
                file_id, file_uuid, collection_prompt_uuid, llm_configured
            )

        # Topic extraction
        # An explicit stage list is a manual request, so only the automatic path
        # (tasks_to_run is None) honours the user's opt-out (issue #1148).
        if "topic_extraction" in (tasks_to_run or []) or (
            tasks_to_run is None and _auto_topic_extraction_enabled(file_uuid)
        ):
            from app.tasks.topic_extraction import extract_topics_task

            topic_task = extract_topics_task.delay(file_uuid=file_uuid, force_regenerate=False)
            logger.info(
                f"Automatic topic extraction task {topic_task.id} started for file {file_id}"
            )
            decisions[PostProcessingTask.TOPICS] = (
                PostProcessingState.QUEUED if llm_configured else PostProcessingState.SKIPPED
            )
        elif tasks_to_run is None:
            decisions[PostProcessingTask.TOPICS] = PostProcessingState.SKIPPED

        # Speaker clustering (selective reprocessing only)
        _clustering_stages = {"speaker_clustering"}
        if tasks_to_run is not None and _clustering_stages & set(tasks_to_run):
            try:
                from app.db.session_utils import session_scope
                from app.models.media import MediaFile

                with session_scope() as _db:
                    _mf = _db.query(MediaFile).filter(MediaFile.uuid == file_uuid).first()
                    if not _mf:
                        logger.warning(f"File {file_uuid} not found for clustering dispatch")
                    else:
                        _uid = int(_mf.user_id)

                        if "speaker_clustering" in tasks_to_run:
                            try:
                                from app.tasks.speaker_clustering import cluster_speakers_for_file

                                cluster_speakers_for_file.delay(file_uuid, _uid)
                                decisions[PostProcessingTask.SPEAKER_CLUSTERING] = (
                                    PostProcessingState.QUEUED
                                )
                                logger.info(
                                    f"Selective speaker clustering dispatched for file {file_id}"
                                )
                            except Exception as sc_err:
                                logger.warning(f"Failed to dispatch speaker clustering: {sc_err}")
                                decisions[PostProcessingTask.SPEAKER_CLUSTERING] = (
                                    PostProcessingState.FAILED
                                )

            except Exception as e:
                logger.warning(f"Failed to look up file for clustering dispatch: {e}")
    except Exception as e:
        logger.warning(f"Failed to start automatic tasks for file {file_id}: {e}")
    return decisions
