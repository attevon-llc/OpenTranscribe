"""Celery queue topology, task priorities and queue-routing helpers.

``CeleryQueues`` is the single source of truth for queue names; the routing
functions read the deployment environment at call time.
"""

import os as _os

# =============================================================================
# Celery Task Queue Priorities
# =============================================================================
# Priorities are PER-QUEUE and independent of each other.
# A GPUPriority.INTERACTIVE=0 has no relation to CPUPriority.PIPELINE_CRITICAL=2.
# Scale: 0 (highest — runs first) to 9 (lowest — runs last).
# Configured via broker_transport_options: priority_steps=list(range(10)).


class CeleryQueues:
    """All valid Celery queue names — single source of truth.

    Use these constants instead of raw strings to prevent typo-based phantom queues.
    Celery is configured with task_create_missing_queues=False, so any queue name
    not declared here will raise an error at dispatch time.
    """

    GPU = "gpu"
    DOWNLOAD = "download"
    CPU = "cpu"
    NLP = "nlp"
    EMBEDDING = "embedding"
    UTILITY = "utility"
    CLOUD_ASR = "cloud-asr"  # Dynamic: cloud ASR providers (CPU worker consumes)
    CPU_TRANSCRIBE = "cpu-transcribe"  # Dynamic: lightweight CPU transcription
    # Phase 4: transcription-only GPU worker (celery-worker-gpu-transcribe, gpu-split
    # profile). The pipeline plan routes here only for inline local speaker detection with
    # gpu_split_enabled() true and a live consumer (issue #703; dispatch._plan_for_dispatch).
    GPU_TRANSCRIBE = "gpu-transcribe"
    GPU_DIARIZE = "gpu-diarize"  # Phase 4: diarization-only GPU worker
    REDACTION = "redaction"  # Content redaction detection (dedicated CPU service)
    DEFAULT = "celery"  # Celery default queue (NLP worker consumes as fallback)

    ALL: list[str] = [
        GPU,
        DOWNLOAD,
        CPU,
        NLP,
        EMBEDDING,
        UTILITY,
        CLOUD_ASR,
        CPU_TRANSCRIBE,
        GPU_TRANSCRIBE,
        GPU_DIARIZE,
        REDACTION,
        DEFAULT,
    ]


def gpu_preferred_queue(deployment_mode: str | None = None) -> str:
    """Queue for work that PREFERS a GPU but produces a correct result without one.

    ``gpu`` in a full deployment; ``cpu`` in lite. ``docker-compose.lite.yml``
    scales BOTH ``gpu``-consuming workers (``celery-worker`` and
    ``celery-worker-gpu-scaled``) to ``replicas: 0``, so a task published to
    ``gpu`` there lands in a queue with no consumer, forever, with no error
    anywhere and the API having already answered 200 (issue #865). Nine tasks did
    exactly that; the user-visible shape was a speaker reassignment reporting
    success while the cross-file voiceprint update never happened.

    Routing is decided by the PUBLISHER, and ``docker-compose.lite.yml`` sets
    ``DEPLOYMENT_MODE=lite`` on **every** service in the deployment — including
    beat, flower and each worker — so keying on it covers every process that can
    publish one of these tasks. Read at call time, never cached, so a test can
    flip the mode per-case (the same contract as :func:`gpu_split_enabled`).

    The reroute costs only speed: the lite image ships the whole CPU inference
    stack these tasks need (``torch==...+cpu``, ``torchaudio``, ``pyannote.audio``,
    ``onnxruntime`` — see ``requirements-lite.txt``, which keeps them deliberately,
    per issue #660), and a lite deployment has no faster option by definition. It
    is the same argument issue #584 already accepted for
    ``extract_speaker_embeddings``, generalised to the rest of the family.

    ⚠️ **Not for work that REQUIRES a GPU to be correct.** Local Whisper
    (``transcription.gpu_transcribe`` on ``gpu``) stays where it is: lite ships no
    local ASR and the pipeline plan refuses it at dispatch (``local_asr_unavailable``),
    so rerouting it would replace a clear refusal with a different failure.

    ⚠️ Deliberately **not** a live consumer probe. ``inspect().active_queues()``
    (as in ``tasks/transcription/dispatch.py``) costs a broker broadcast of up to
    2 s, and that file's own docstring justifies it only because it sits behind a
    rare operator opt-in — whereas these tasks dispatch from interactive request
    handlers like ``PUT /segments/{uuid}/speaker``. More importantly the two
    conditions differ: a *deployment* with no GPU worker by design is permanent
    and knowable for free, while a full deployment whose GPU worker is briefly
    down is an outage, where queueing until it returns is the CORRECT behaviour
    and a silent downgrade to CPU would be the regression.

    Args:
        deployment_mode: Override for the ambient ``DEPLOYMENT_MODE`` (tests).

    Returns:
        ``CeleryQueues.CPU`` in lite, otherwise ``CeleryQueues.GPU``.
    """
    from app.core.config import settings

    mode = deployment_mode if deployment_mode is not None else settings.DEPLOYMENT_MODE
    return CeleryQueues.CPU if mode.strip().lower() == "lite" else CeleryQueues.GPU


# Speaker re-clustering runs its similarity matrix on CUDA only once a tenant
# partition has at least this many speakers (benchmarked: larger sets are faster on
# the GPU; smaller ones pay a ~1.4 GB CUDA context for nothing). Read by BOTH
# SpeakerClusteringService._compute_similarity_groups (device choice) and
# speaker_clustering_queue() (queue choice) so the two cannot drift (issue #1083).
# Both read it as a module attribute at call time, never via `from ... import`.
SPEAKER_CLUSTERING_GPU_MIN_SPEAKERS = 500


def speaker_clustering_queue(largest_partition: int, deployment_mode: str | None = None) -> str:
    """Queue for a speaker re-cluster whose largest tenant partition has this many speakers.

    Below the GPU threshold the clustering math runs on CPU wherever it lands, so
    publishing it to ``gpu`` only added latency and, on a deployment whose GPU
    workers scale to zero, forced a GPU cold start for an interactive click
    (issue #1083). At or above it the work is routed like any other GPU-preferred
    task (``gpu`` in a full deployment, ``cpu`` in lite).

    The dispatcher passes the queue explicitly; the static ``task_routes`` entry
    (``GPU_PREFERRED_TASKS`` in ``core/celery.py``) is only the fallback for a
    publisher that does not.
    """
    if largest_partition >= SPEAKER_CLUSTERING_GPU_MIN_SPEAKERS:
        return gpu_preferred_queue(deployment_mode)
    return CeleryQueues.CPU


def gpu_split_enabled() -> bool:
    """Whether THIS process's environment ASKS FOR the gpu-split topology.

    Read at call time (never cached) so a test can flip ``ENGINE_GPU_SPLIT`` per-case,
    and because Celery worker processes never re-import this module after startup.

    ⚠️ This is a REQUEST, not a guarantee a consumer exists. ``ENGINE_GPU_SPLIT`` is
    documented in ``.env.example`` as an ordinary operator knob and reaches every
    container via ``env_file: .env`` regardless of which Compose overlay is loaded —
    ``docker-compose.gpu-split.yml`` additionally hardcodes it to ``true`` on the
    dispatching services, but that overlay is loaded only under ``--with-gpu-split``,
    while ``env_file`` is not. So an operator who sets ``ENGINE_GPU_SPLIT=true`` in
    ``.env`` directly (no ``--with-gpu-split``) makes this function return True with
    ``celery-worker-gpu-transcribe`` never started to drain the queue it names.
    Before this branch the flag was inert for dispatch, so that combination is a
    regression, not a pre-existing risk.

    Do NOT route on this value alone. ``dispatch.py::_plan_for_dispatch`` also
    verifies a live consumer is bound to ``CeleryQueues.GPU_TRANSCRIBE`` (via Celery
    control ``inspect().active_queues()``) before honouring it, and falls back to the
    always-staffed ``'gpu'`` queue otherwise — that check, not this function, is what
    keeps the failure direction "no split", never "queued on a dead queue" (issue
    #703, amended by the corroboration this docstring used to (wrongly) claim this
    function alone provided).
    """
    return _os.environ.get("ENGINE_GPU_SPLIT", "false").lower() == "true"


class GPUPriority:
    """GPU queue (concurrency=1). Controls what runs next when the worker is free.
    Interactive UI actions must preempt queued long-running jobs.
    """

    INTERACTIVE = 0  # User action awaiting instant feedback (~5s), e.g. speaker drag
    NEAR_REALTIME = 1  # User action with response in <30s, e.g. manual embedding re-extract
    # An automatic retry of a transcription interrupted by the infrastructure (worker lost,
    # OOM, a lost connection). Ahead of USER_IMPORT so a requeued file keeps its place in
    # front of submissions that arrived after it, instead of going to the back of the line.
    TRANSCRIPTION_RETRY = 2
    USER_IMPORT = 3  # User-submitted transcription/import (~5-60min)
    USER_REDIARIZ = 4  # User-triggered re-diarization of an existing file (~5-30min)
    USER_RECLUSTER = 5  # User-triggered full speaker re-clustering (~5-15min)
    ADMIN_MIGRATION = 7  # Admin bulk migration batches (~1-5min/batch); yields to user work


class CPUPriority:
    """CPU queue (concurrency=8). Controls ordering when all workers are busy."""

    # The CPU stages of an automatically retried transcription; see
    # GPUPriority.TRANSCRIPTION_RETRY for why a retry runs ahead of new submissions.
    PIPELINE_RETRY = 1
    PIPELINE_CRITICAL = 2  # Completes the import pipeline the user is watching
    #                        e.g. waveform, thumbnail, post-transcription clustering
    USER_TRIGGERED = 4  # Explicit user action outside the import pipeline
    SYSTEM = 5  # System monitoring and stats collection
    ADMIN_BATCH = 6  # Admin bulk operations and data migrations
    MAINTENANCE = 8  # Scheduled background maintenance (search, cleanup)


class NLPPriority:
    """NLP queue (concurrency=4). LLM API calls and AI enrichment tasks."""

    USER_TRIGGERED = 3  # User explicitly requested (summarize, identify speakers)
    AUTO_PIPELINE = 5  # Automatically triggered after transcription completes
    ADMIN_BATCH = 7  # Admin batch operations
    BACKGROUND = 9  # Background retroactive enrichment (no user waiting)


class DownloadPriority:
    """Download queue (concurrency=3). Network I/O for media downloads."""

    SINGLE_URL = 3  # Single URL — user watching progress bar
    PLAYLIST = 6  # Playlist — bulk download, less urgent per individual item


class EmbeddingPriority:
    """Embedding queue (concurrency=1). OpenSearch neural indexing.
    Single-worker queue — priority controls backlog ordering.
    """

    # A retried (or sweep re-dispatched) index run: ahead of first attempts so a file that
    # already waited out a failure is not queued behind a backlog of new work.
    PIPELINE_RETRY = 1
    PIPELINE_CRITICAL = 2  # Post-import indexing — makes new content searchable


class UtilityPriority:
    """Utility queue (concurrency=8). Lightweight maintenance and system tasks."""

    EMERGENCY = 1  # System recovery, critical operations
    OPERATIONAL = 3  # Health checks, monitoring
    ROUTINE = 5  # Periodic cleanup, access tracking
    BACKGROUND = 7  # Migration finalization, status checks
    DEV_TOOLS = 9  # Development and testing utilities (baseline export etc.)


class RedactionPriority:
    """Redaction queue (dedicated CPU service). Content-moderation detection.

    Priority controls backlog ordering within the redaction worker pool.
    """

    PIPELINE_AUTO = 4  # Auto-dispatched after a transcript completes
    USER_TRIGGERED = 3  # User re-ran detection (e.g. after editing a segment)
    ADMIN_BACKFILL = 7  # Admin bulk re-index (model upgrade / first rollout)


# =============================================================================
# Cloud ASR rate-limit retry policy (transcribe_gpu_task's ASRRateLimitedError handler)
# =============================================================================
# Separate from the task's autoretry_for=(ConnectionError, TimeoutError) policy — that one's
# retry_backoff_max=30/max_retries=1 are tuned for a GPU-path connection blip, not a vendor
# throttle response, which needs a longer backoff ceiling and more attempts. See
# app/services/asr/errors.py and app/tasks/transcription/core.py.

CLOUD_ASR_RETRY_BASE = 15  # seconds — first retry delay when the vendor gave no Retry-After
CLOUD_ASR_RETRY_MAX = 300  # seconds — backoff ceiling (5 min)
CLOUD_ASR_MAX_RETRIES = 5  # attempts, distinct from the GPU path's max_retries=1
