"""Dispatching the Celery work a reprocess starts (full pipeline or selected stages)."""

import logging

from app.core.env import env_bool

logger = logging.getLogger(__name__)


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

    if not env_bool("SKIP_CELERY"):
        # Dispatch 3-stage pipeline chain: CPU preprocess → GPU transcribe → CPU postprocess
        # Queue routing is auto-resolved inside dispatch_transcription_pipeline
        from app.tasks.transcription import dispatch_transcription_pipeline

        dispatch_transcription_pipeline(
            file_uuid=file_uuid,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            num_speakers=num_speakers,
            downstream_tasks=downstream_tasks,
            # False must reach the pipeline as "not asked": the bool is mapped to
            # diarization_source "provider" there, which would override the user's choice.
            disable_diarization=True if disable_diarization else None,
            whisper_model=whisper_model,
        )
    else:
        logger.info("Skipping Celery task in test environment")


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

    if env_bool("SKIP_CELERY"):
        logger.info("Skipping Celery task in test environment")
        return

    if stage == "search_indexing":
        from app.tasks.search_indexing_task import index_transcript_search_task

        # Resolve file_id and user_id from DB if not provided
        if file_id is None or user_id is None:
            from app.db.session_utils import session_scope
            from app.services.uuid_helpers import get_file_by_uuid

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
            from app.services.uuid_helpers import get_file_by_uuid

            with session_scope() as db:
                media_file = get_file_by_uuid(db, file_uuid)
                user_id = int(media_file.user_id)

        from app.tasks.speaker_clustering import cluster_speakers_for_file

        cluster_speakers_for_file.delay(file_uuid, user_id)
    else:
        logger.warning(f"Unknown stage '{stage}' for dispatch")


def _dispatch_manual_rediarize(
    file_uuid: str,
    owner_id: int,
    *,
    min_speakers: int | None,
    max_speakers: int | None,
    num_speakers: int | None,
    downstream_tasks: list[str] | None,
) -> None:
    """Re-run speaker detection with the owner's CURRENT choice (plan decision Q9).

    The post stage runs the plan's detector: pyannote.ai on ``cloud-asr``, otherwise this
    server's engine on ``gpu_preferred_queue()`` (a transcription vendor's own speakers, or
    "off", cannot be re-run after the fact, so they mean this server). A choice that cannot
    run (no pyannote.ai key) fails the task with its fixed sentence rather than silently
    running something else; this server's engine on lite is refused earlier, by
    :func:`refuse_unrunnable_rerun`, and ``post_stage_queue`` refuses it again here.
    """
    from app.core.config import settings
    from app.db.session_utils import session_scope
    from app.services.pipeline_plan import post_stage_engine
    from app.services.pipeline_plan import post_stage_queue
    from app.services.pipeline_plan_inputs import plan_for_user
    from app.tasks.rediarize_task import rediarize_task

    # The file OWNER's choice and credential: the task detects speakers with them.
    with session_scope() as db:
        plan = plan_for_user(db, owner_id)
    engine = post_stage_engine(plan.diarization.engine)
    rediarize_task.apply_async(
        args=[file_uuid],
        kwargs={
            "min_speakers": min_speakers,
            "max_speakers": max_speakers,
            "num_speakers": num_speakers,
            "downstream_tasks": downstream_tasks,
            "engine": engine,
            "diarizer_config_uuid": plan.diarization.config_uuid,
        },
        queue=post_stage_queue(engine, str(settings.DEPLOYMENT_MODE)),
    )


def dispatch_selective_tasks(
    file_uuid: str,
    stages: list[str],
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    num_speakers: int | None = None,
    file_id: int | None = None,
    user_id: int | None = None,
    whisper_model: str | None = None,
    disable_diarization: bool = False,
    owner_id: int | None = None,
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
        disable_diarization: Skip diarization; honoured by the transcription stage only.
        owner_id: The file owner, whose speaker-detection choice a re-diarize runs with.
    """

    if env_bool("SKIP_CELERY"):
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
            disable_diarization=disable_diarization,
        )
    elif "rediarize" in stages:
        other_stages = [s for s in stages if s != "rediarize"]
        if owner_id is None:
            raise ValueError("re-diarizing needs the file owner's id")
        _dispatch_manual_rediarize(
            file_uuid,
            owner_id,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            num_speakers=num_speakers,
            downstream_tasks=other_stages if other_stages else None,
        )
    else:
        # Pure downstream tasks - dispatch each directly
        for stage in stages:
            dispatch_task_by_name(stage, file_uuid, file_id=file_id, user_id=user_id)
