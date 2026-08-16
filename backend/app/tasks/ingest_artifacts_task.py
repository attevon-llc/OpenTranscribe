"""Celery task: build the deterministic ingest artifacts for a completed transcript.

Runs on the **nlp** queue. That queue's docstring says "LLM API calls", and this task
makes none — it is here because the nlp worker is the CPU-only, concurrency-4 pool that
already owns post-transcription enrichment, and because putting it on `cpu` would queue
it behind the pipeline-critical preprocess/postprocess stages it must not delay. It loads
no model and holds no GPU.

Dispatched fire-and-forget from ``transcription/postprocess.enrich_and_dispatch``, in
parallel with search indexing, so it is off the critical path: the file is already
COMPLETED and visible to the user before this runs.

**No LLM gate here, deliberately.** Every other enrichment task on this queue returns
early when no provider is configured. This one must not — #403 **D6** makes the no-LLM
deployment first class, and the Stage 2 gate is *100% of transcribed files get facts and a
digest with ``LLM_PROVIDER`` empty*.
"""

from __future__ import annotations

import logging

from app.core.celery import celery_app
from app.core.constants import NLPPriority
from app.db.session_utils import session_scope
from app.services.ingest_artifacts.recorded_date_service import resolve_for_file
from app.services.ingest_artifacts.service import generate_file_artifacts
from app.utils import benchmark_timing

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="artifacts.generate_file_facts",
    priority=NLPPriority.AUTO_PIPELINE,
    max_retries=2,
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    ignore_result=True,
)
def generate_file_facts_task(
    self,
    file_id: int,
    force: bool = False,
    pipeline_task_id: str | None = None,
) -> dict[str, object]:
    """Generate ``file_facts`` for one media file.

    Args:
        file_id: ``MediaFile.id``.
        force: Regenerate even when the source fingerprint is unchanged.
        pipeline_task_id: Upstream application task id, so the benchmark markers land in
            the same ``benchmark:{task_id}`` hash as the rest of the pipeline. Both marks
            are no-ops unless ``ENABLE_BENCHMARK_TIMING`` is on.

    Returns:
        ``{"status": ..., "file_id": ..., "sections": ..., "digest_words": ...}``.
        ``status`` is ``"skipped"`` when the file has no segments — a real outcome, not a
        failure, and one the caller must not treat as an error. ``recorded_date_source`` is
        present either way.
    """
    benchmark_timing.mark(pipeline_task_id, "file_facts_start")
    try:
        with session_scope() as db:
            # BEFORE the no-segments early return, and that ordering is the point: a file
            # whose transcription produced nothing still has a filename and a container
            # stamp, and those are two of the three sources. Resolving after the return
            # would leave every failed or empty transcript permanently undated.
            resolution = resolve_for_file(db, file_id)
            date_source = resolution.source.value if resolution else None

            row = generate_file_artifacts(db, file_id, force=force)
            if row is None:
                return {
                    "status": "skipped",
                    "file_id": file_id,
                    "reason": "no_segments",
                    "recorded_date_source": date_source,
                }
            # Annotated: the literal's mixed value types infer as a narrow union,
            # and dict is invariant, so returning it from a dict[str, object]
            # function fails without this.
            result: dict[str, object] = {
                "recorded_date_source": date_source,
                "status": "success",
                "file_id": file_id,
                "sections": int(row.section_count),
                "digest_words": int(row.digest_word_count),
                "generation_ms": int(row.generation_ms or 0),
            }
        return result
    finally:
        benchmark_timing.mark(pipeline_task_id, "file_facts_end")


def dispatch_file_facts(file_id: int, pipeline_task_id: str | None = None) -> None:
    """Fire-and-forget dispatch, contained.

    Wrapped because it is called from the enrichment fan-out: a broker hiccup must cost
    the digest, not the whole enrichment pass.
    """
    try:
        generate_file_facts_task.delay(file_id=file_id, pipeline_task_id=pipeline_task_id)
        logger.info("Dispatched file_facts generation for file %s", file_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to dispatch file_facts generation for file %s: %s", file_id, exc)
