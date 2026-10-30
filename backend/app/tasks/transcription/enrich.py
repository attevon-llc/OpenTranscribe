"""Background enrichment fan-out for the transcription pipeline.

``enrich_and_dispatch`` runs after the transcript is committed (queued by
``postprocess._finalize``): it dispatches search indexing, ingest artifacts, redaction
detection, summarization, speaker attributes and speaker clustering, then reports every
decision as ONE post-processing plan. Split out of ``postprocess.py`` so the synchronous
finalize stage and the fire-and-forget fan-out change independently.
"""

import logging
import time

from app.core.celery import celery_app
from app.core.enums import PostProcessingState
from app.core.enums import PostProcessingTask
from app.core.queues import CPUPriority
from app.services import post_processing_status
from app.services.privacy.gates import voiceprints_allowed

logger = logging.getLogger(__name__)


@celery_app.task(
    name="transcription.enrich_and_dispatch",
    priority=CPUPriority.SYSTEM,
    max_retries=2,
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    ignore_result=True,
)
def enrich_and_dispatch(
    file_id: int,
    file_uuid: str,
    user_id: int,
    downstream_tasks: list[str] | None = None,
    pipeline_task_id: str | None = None,
) -> None:
    """Fire-and-forget: search indexing + downstream task dispatch, then the plan.

    Runs after the transcript is committed. Every dispatch decision (queued, skipped by a
    setting, failed to queue) is collected and reported as ONE post-processing plan, which
    opens the run the notification card waits on; each task then reports its own outcome.
    A task that is not dispatched at all is absent from the plan, so nothing waits for it.

    ``pipeline_task_id`` is the upstream application task_id; when supplied
    it is propagated into child tasks so their ``_start``/``_end`` benchmark
    markers land in the same ``benchmark:{task_id}`` Redis hash.
    """
    run_at = time.time()
    plan: dict[PostProcessingTask, PostProcessingState] = {
        PostProcessingTask.SEARCH_INDEXING: _index_transcript(
            file_id, file_uuid, user_id, pipeline_task_id=pipeline_task_id
        )
    }

    # Deterministic ingest artifacts (facts / extractive digest / keyphrases). NOT gated
    # on an LLM provider — this is the tier that exists precisely so a deployment without
    # one still has a summary and exact per-file statistics (#403 D6).
    from app.tasks.ingest_artifacts_task import dispatch_file_facts

    plan[PostProcessingTask.FILE_FACTS] = (
        PostProcessingState.QUEUED
        if dispatch_file_facts(file_id, pipeline_task_id=pipeline_task_id)
        else PostProcessingState.FAILED
    )

    # Content redaction detection — gated on the owner's effective config (opt-out by
    # default); detection otherwise happens lazily on first open. Dedicated CPU service;
    # masking applies at read time once spans land.
    plan[PostProcessingTask.REDACTION] = _dispatch_redaction(
        file_id, user_id, pipeline_task_id=pipeline_task_id
    )

    # Downstream tasks — each wrapped individually so one failure
    # doesn't prevent the others from dispatching
    logger.info(f"Dispatching downstream enrichment tasks for file {file_id}")

    try:
        from .core import trigger_automatic_summarization

        plan.update(
            trigger_automatic_summarization(file_id, file_uuid, tasks_to_run=downstream_tasks)
        )
    except Exception as e:
        logger.warning(f"Summarization dispatch failed for file {file_id}: {e}")

    plan.update(_dispatch_speaker_attributes(file_uuid, user_id, downstream_tasks))
    clustering = _dispatch_speaker_clustering(file_uuid, user_id, downstream_tasks, file_id=file_id)
    if clustering is not None:
        plan[PostProcessingTask.SPEAKER_CLUSTERING] = clustering

    ref = post_processing_status.resolve_file(file_id=file_id)
    if ref is not None:
        post_processing_status.report_plan(ref, plan, run_at=run_at)


def _index_transcript(
    file_id: int, file_uuid: str, user_id: int, pipeline_task_id: str | None = None
) -> PostProcessingState:
    """Dispatch OpenSearch indexing for the completed transcript.

    Phase 2 PR #5: the full-document index used to be built inline on the
    CPU worker here (adding 200-400 ms to the postprocess critical path).
    It now runs inside ``index_transcript_search_task`` on the embedding
    worker together with the chunk-level index, so CPU postprocess can
    return as soon as speaker matching finishes.
    """
    try:
        from app.tasks.search_indexing_task import index_transcript_search_task

        index_transcript_search_task.delay(
            file_id=file_id,
            file_uuid=str(file_uuid),
            user_id=user_id,
            pipeline_task_id=pipeline_task_id,
        )
        logger.info(f"Dispatched search indexing task for file {file_uuid}")
        return PostProcessingState.QUEUED
    except Exception as e:
        logger.warning(f"Failed to dispatch search indexing: {e}")
        return PostProcessingState.FAILED


def _dispatch_redaction(
    file_id: int, user_id: int, pipeline_task_id: str | None = None
) -> PostProcessingState:
    """Dispatch content-redaction detection — ONLY when the owner has redaction enabled
    (or an admin forces it). Redaction is opt-out by default, so we skip the (potentially
    expensive) scan for the common case. If a user enables redaction later, detection is
    dispatched lazily the first time they open the file.

    Consults the FILE's own ``organization_id`` for the tenant floor (issue #982/
    #987) — this pipeline callback carries only ``file_id``/``user_id``, no request
    context, and the file's own tenant is what the floor governs (#988).
    """
    try:
        from app.db.session_utils import session_scope
        from app.models.media import MediaFile
        from app.services.redaction.config import resolve_effective_config

        with session_scope() as db:
            organization_id = (
                db.query(MediaFile.organization_id).filter(MediaFile.id == file_id).scalar()
            )
            cfg = resolve_effective_config(db, user_id, organization_id=organization_id)
        if not cfg.enabled:
            logger.info(f"Redaction off for owner of file {file_id}; skipping detection")
            return PostProcessingState.SKIPPED

        from app.tasks.redaction_task import redaction_detect_task

        redaction_detect_task.delay(
            file_id=file_id,
            user_id=user_id,
            pipeline_task_id=pipeline_task_id,
        )
        logger.info(f"Dispatched redaction detection for file {file_id}")
        return PostProcessingState.QUEUED
    except Exception as e:
        logger.warning(f"Failed to dispatch redaction detection: {e}")
        return PostProcessingState.FAILED


def _dispatch_speaker_attributes(
    file_uuid: str, user_id: int, downstream_tasks: list[str] | None
) -> dict[PostProcessingTask, PostProcessingState]:
    """Dispatch speaker attribute detection and LLM speaker ID (fire-and-forget).

    LLM speaker ID chains from attribute detection (dispatched at the end of
    detect_speaker_attributes_task); with detection off it is queued directly, so the two
    features stay independent (issue #1148). Either way the LLM step's own dispatcher
    reports it, so only the attribute step is returned for the plan.
    """
    try:
        from app.tasks.speaker_attribute_task import dispatch_speaker_attribute_pipeline

        queued = dispatch_speaker_attribute_pipeline(
            str(file_uuid),
            user_id,
            llm_requested=downstream_tasks is not None and "speaker_llm" in downstream_tasks,
        )
    except Exception as e:
        logger.warning(f"Failed to dispatch speaker attribute detection: {e}")
        return {PostProcessingTask.SPEAKER_ATTRIBUTES: PostProcessingState.FAILED}
    state = PostProcessingState.QUEUED if queued == "attributes" else PostProcessingState.SKIPPED
    return {PostProcessingTask.SPEAKER_ATTRIBUTES: state}


def _dispatch_speaker_clustering(
    file_uuid: str, user_id: int, downstream_tasks: list[str] | None, *, file_id: int
) -> PostProcessingState | None:
    """Dispatch speaker clustering (fire-and-forget).

    Returns None when the downstream stage list owns the dispatch (and reports it).
    Clusters are built from voiceprints, so none is queued while the policy forbids them.
    """
    if downstream_tasks is not None and "speaker_clustering" in downstream_tasks:
        return None
    if not voiceprints_allowed(file_id):
        logger.info("Voiceprints are off by policy for file %s; no speaker clustering", file_id)
        return PostProcessingState.SKIPPED
    try:
        from app.tasks.speaker_clustering import cluster_speakers_for_file

        cluster_speakers_for_file.delay(str(file_uuid), user_id)
        logger.info(f"Dispatched speaker clustering for {file_uuid}")
        return PostProcessingState.QUEUED
    except Exception as e:
        logger.warning(f"Failed to dispatch speaker clustering: {e}")
        return PostProcessingState.FAILED
