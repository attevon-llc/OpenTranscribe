"""Inference runtime for speaker attribute (gender) detection.

Concurrency bound, model lifetime and the parallel fetch-and-classify loop used by
``detect_speaker_attributes_task`` (``tasks/speaker_attribute_task.py``), which owns the
task, its gates, tracking and persistence.

Memory (issue #1066): at most ``SPEAKER_ATTRIBUTE_MAX_CONCURRENCY`` detections run at once per
worker host, and a CPU worker drops the model once the last detection finishes.
"""

import contextlib
import logging
import os
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

from app.services.audio_segment_utils import extract_audio_segment_np

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CONCURRENCY = 2


def _max_concurrency() -> int:
    """``SPEAKER_ATTRIBUTE_MAX_CONCURRENCY``: detections allowed at once per worker host.

    Each costs ~0.7 GB for the model plus ~0.4 GB of activations at the default clip cap,
    and every prefork child shares the container's memory limit. 0 disables the bound.
    """
    raw = os.environ.get("SPEAKER_ATTRIBUTE_MAX_CONCURRENCY", "").strip()
    if not raw:
        return _DEFAULT_MAX_CONCURRENCY
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("SPEAKER_ATTRIBUTE_MAX_CONCURRENCY=%r is not an integer; using default", raw)
        return _DEFAULT_MAX_CONCURRENCY


def _is_gpu_worker() -> bool:
    return os.environ.get("PRELOAD_GPU_MODELS", "").lower() == "true"


_model_users_lock = threading.Lock()
_model_users = 0


@contextlib.contextmanager
def _model_in_use() -> Iterator[None]:
    """Count this process's detections; the last one out on a CPU worker frees the model.

    Counted rather than released unconditionally because a ``--pool=threads`` worker runs
    several detections in one process against the one cached model. The release happens
    under the lock, so a detection that has registered itself never sees its model
    released underneath it.
    """
    global _model_users
    with _model_users_lock:
        _model_users += 1
    try:
        yield
    finally:
        with _model_users_lock:
            _model_users -= 1
            if _model_users == 0 and not _is_gpu_worker():
                from app.services.speaker_attribute_service import release_cached_attribute_service

                release_cached_attribute_service()


_MODEL_LOAD_TIMEOUT_SECONDS = 60


def _load_models_with_timeout(service, timeout: float = _MODEL_LOAD_TIMEOUT_SECONDS) -> None:
    """Load the gender-detection model with a bounded wait.

    ``Wav2Vec2*.from_pretrained`` makes a HuggingFace Hub metadata request before
    falling back to the local cache, and — unlike the ffmpeg segment fetches below,
    which are bounded both by their own ``subprocess.run(timeout=30)`` and by the
    pool's per-future ``result(timeout=30)`` — that call carries no wall-clock bound
    of its own. On a degraded or blocked network it can sit doing nothing (0% CPU)
    for the rest of the task's budget, indistinguishable from a task that isn't
    running at all until the Celery hard ``time_limit`` kills it.

    Delegates to :func:`app.utils.hf_hub_offline.load_with_timeout` — the same
    one-shot-thread-pool idiom used below and by every other HuggingFace Hub load
    site in this codebase, kept as one implementation rather than four.
    """
    from app.utils.hf_hub_offline import load_with_timeout

    load_with_timeout(
        service.load_models,
        timeout=timeout,
        label="Gender-detection model load",
    )


def _run_gender_inference_parallel(
    audio_source: str,
    work_items: list[tuple[int, dict]],
    service,
) -> tuple[dict[int, dict[str, float]], dict[int, int]]:
    """Run gender inference on segments fetched in parallel.

    Returns (speaker_probs, speaker_clip_counts) dicts.
    """
    speaker_probs: dict[int, dict[str, float]] = {}
    speaker_clip_counts: dict[int, int] = {}

    # Not a `with` block: its __exit__ calls shutdown(wait=True), so one fetch thread that
    # never returns would hang the task forever — the per-future timeout below bounds the
    # wait, not the work. Shut down without waiting instead, and let the threads die daemon.
    pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="attr-ffmpeg")
    try:
        futures = []
        for speaker_id, seg in work_items:
            duration = seg["end"] - seg["start"]
            fut = pool.submit(extract_audio_segment_np, audio_source, seg["start"], duration)
            futures.append((speaker_id, fut))

        for speaker_id, fut in futures:
            try:
                audio_np = fut.result(timeout=30)
            except Exception as e:
                logger.debug("Segment fetch failed for speaker %s: %s", speaker_id, e)
                continue

            if audio_np is None or len(audio_np) < 16000:
                continue

            gender, confidence = service._run_inference(audio_np)

            if speaker_id not in speaker_probs:
                speaker_probs[speaker_id] = {"male": 0.0, "female": 0.0}
                speaker_clip_counts[speaker_id] = 0
            speaker_probs[speaker_id][gender] += confidence
            speaker_clip_counts[speaker_id] += 1
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    return speaker_probs, speaker_clip_counts
