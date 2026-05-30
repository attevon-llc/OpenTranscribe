# Skip heavy AI imports during testing - speeds up test startup significantly
import logging
import os

logger = logging.getLogger(__name__)

_SKIP_CELERY = os.environ.get("SKIP_CELERY", "").lower() == "true"

if not _SKIP_CELERY:
    # PyTorch 2.6+ compatibility fix - MUST be done BEFORE any ML library imports
    # Patch torch.load to default to weights_only=False for trusted HuggingFace models
    # This must be at the TOP of celery.py because Celery's include= imports task modules
    # which import pyannote/whisperx that cache torch.load at import time
    import torch

    _original_torch_load = torch.load

    def _patched_torch_load(*args, **kwargs):
        # Handle both missing weights_only AND weights_only=None (which PyTorch 2.8 treats as True)
        if kwargs.get("weights_only") is None:
            kwargs["weights_only"] = False
        return _original_torch_load(*args, **kwargs)

    torch.load = _patched_torch_load

    # Note: WhisperX 3.8.1 has native PyAnnote v4 support — no patches needed

# Imports must come after torch.load patch to prevent caching issues
from celery import Celery  # noqa: E402
from celery.schedules import crontab  # noqa: E402
from celery.signals import task_postrun  # noqa: E402
from celery.signals import worker_process_init  # noqa: E402
from celery.signals import worker_ready  # noqa: E402
from kombu import Queue  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.constants import CeleryQueues  # noqa: E402

# Explicit queue declarations — single source of truth.
# With task_create_missing_queues=False, any typo in a queue name will raise
# an error at dispatch time instead of silently creating a phantom queue.
CELERY_QUEUES = tuple(Queue(q) for q in CeleryQueues.ALL)

# Initialize Celery
celery_app = Celery(
    "transcribe_app",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.tasks.transcription",
        "app.tasks.transcription.core",
        "app.tasks.transcription.preprocess",
        "app.tasks.transcription.postprocess",
        "app.tasks.transcription.dispatch",
        "app.tasks.waveform",
        "app.tasks.waveform_generation",
        "app.tasks.summarization",
        "app.tasks.analytics",
        "app.tasks.cleanup",
        "app.tasks.utility",
        "app.tasks.recovery",
        "app.tasks.youtube_processing",
        "app.tasks.media_download",
        "app.tasks.speaker_tasks",
        "app.tasks.speaker_identification_task",
        "app.tasks.speaker_update_task",
        "app.tasks.speaker_embedding_task",
        "app.tasks.speaker_attribute_task",
        "app.tasks.topic_extraction",
        "app.tasks.reindex_task",
        "app.tasks.search_maintenance_task",
        "app.tasks.opensearch_integrity_task",
        "app.tasks.search_indexing_task",
        "app.tasks.thumbnail",
        "app.tasks.thumbnail_migration",
        "app.tasks.embedding_migration_v4",
        "app.tasks.speaker_embedding_migration",
        "app.tasks.baseline_export",
        "app.tasks.rediarize_task",
        "app.tasks.speaker_clustering",
        "app.tasks.auto_labeling",
        "app.tasks.speaker_attribute_migration_task",
        "app.tasks.combined_speaker_analysis_task",
        "app.tasks.speaker_embedding_consistency",
        "app.tasks.embedding_consistency_repair",
    ],
)

# Configure Celery
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,  # One task at a time for GPU tasks
    worker_send_task_events=True,  # Enable real-time task events for Flower
    task_send_sent_event=True,  # Fire event when task is dispatched to queue
    result_expires=86400,  # Expire results after 24h (prevent Redis bloat)
    # Enable Redis priority queues: lower number = higher priority (runs first).
    # Priorities are PER-QUEUE — GPUPriority.X is independent of CPUPriority.X.
    # Named constants defined in app.core.constants: GPUPriority, CPUPriority, etc.
    # GPU queue:  0=speaker-reassign  1=embed-extract  3=transcription  4=rediarize
    #             5=recluster  7=admin-migration-batches
    # CPU queue:  2=pipeline-critical  4=user-triggered  5=system  6=admin  8=maintenance
    # NLP queue:  3=user-triggered  5=auto-pipeline  7=admin-batch  9=background
    # Download:   3=single-url  6=playlist
    # Embedding:  2=pipeline-critical
    # Utility:    1=emergency  3=operational  5=routine  7=background  9=dev-tools
    task_queues=CELERY_QUEUES,
    task_create_missing_queues=False,  # Catch queue name typos at dispatch time
    broker_transport_options={
        "priority_steps": list(range(10)),
        "queue_order_strategy": "priority",
    },
    task_routes={
        # GPU Queue - GPU-intensive AI tasks (concurrency=1, requires GPU)
        # See priority comment above for priority scheme
        "transcription.process_file": {"queue": CeleryQueues.GPU},
        # Pipeline chain tasks (3-stage: CPU preprocess → GPU transcribe → CPU postprocess)
        # NOTE: "transcription.gpu_transcribe" is intentionally NOT listed here so
        # dispatch.py can route it to either "gpu" or "cloud-asr" at call time.
        # NOTE: "transcription.cpu_transcribe" is also intentionally NOT listed here
        # so dispatch.py can route it to "cpu-transcribe" at call time.
        "transcription.preprocess": {"queue": CeleryQueues.CPU},
        "transcription.postprocess": {"queue": CeleryQueues.CPU},
        "transcription.enrich_and_dispatch": {"queue": CeleryQueues.CPU},
        "transcription.pipeline_error": {"queue": CeleryQueues.UTILITY},
        "rediarize": {"queue": CeleryQueues.GPU},
        "update_speaker_embedding_on_reassignment": {"queue": CeleryQueues.GPU},
        "extract_v4_embeddings": {"queue": CeleryQueues.GPU},
        "extract_v4_embeddings_batch": {"queue": CeleryQueues.GPU},
        "speaker.recluster_all": {"queue": CeleryQueues.GPU},
        "speaker.cluster_for_file": {"queue": CeleryQueues.CPU},
        # Download Queue - Network I/O tasks (concurrency=3, no GPU)
        "download.media_url": {"queue": CeleryQueues.DOWNLOAD},
        "download.media_playlist": {"queue": CeleryQueues.DOWNLOAD},
        "download.prepare_media": {"queue": CeleryQueues.DOWNLOAD},
        # CPU Queue - CPU-intensive parallel tasks (concurrency=8, no GPU)
        "media.generate_waveform": {"queue": CeleryQueues.CPU},
        "media.generate_waveform_data": {"queue": CeleryQueues.CPU},
        "analytics.analyze_transcript": {"queue": CeleryQueues.CPU},
        "detect_speaker_attributes": {"queue": CeleryQueues.CPU},
        "migrate_speaker_attributes": {"queue": CeleryQueues.CPU},
        "detect_speaker_attributes_batch": {"queue": CeleryQueues.GPU},
        "analyze_speakers_combined_batch": {"queue": CeleryQueues.GPU},
        "migrate_speakers_combined": {"queue": CeleryQueues.CPU},
        "system.update_gpu_stats": {"queue": CeleryQueues.CPU},
        "migrate_speaker_embeddings_to_v4": {"queue": CeleryQueues.CPU},
        "migration.normalize_embeddings": {"queue": CeleryQueues.CPU},
        "generate_thumbnail": {"queue": CeleryQueues.CPU},
        "migrate_thumbnails_to_webp": {"queue": CeleryQueues.CPU},
        "reindex_transcripts": {"queue": CeleryQueues.CPU},
        "reindex_batch": {"queue": CeleryQueues.CPU},
        "search_index_maintenance": {"queue": CeleryQueues.CPU},
        "opensearch_orphan_cleanup": {"queue": CeleryQueues.CPU},
        "speaker_embedding_consistency_check": {"queue": CeleryQueues.CPU},
        "speaker_embedding_consistency_repair_batch": {"queue": CeleryQueues.GPU},
        "process_speaker_update_background": {"queue": CeleryQueues.CPU},
        "extract_speaker_embeddings": {"queue": CeleryQueues.GPU},
        # NLP Queue - LLM API calls (concurrency=4, no GPU needed)
        "ai.generate_summary": {"queue": CeleryQueues.NLP},
        "ai.identify_speakers": {"queue": CeleryQueues.NLP},
        "ai.extract_topics": {"queue": CeleryQueues.NLP},
        "ai.extract_topics_batch": {"queue": CeleryQueues.NLP},
        "ai.group_batch_files": {"queue": CeleryQueues.NLP},
        "ai.retroactive_auto_label": {"queue": CeleryQueues.NLP},
        "ai.auto_label_batch": {"queue": CeleryQueues.NLP},
        # Embedding Queue - Search indexing with embedding model (concurrency=1)
        "index_transcript_search": {"queue": CeleryQueues.EMBEDDING},
        # Access index updates are lightweight OpenSearch writes (no GPU/embedding needed)
        "update_file_access_index": {"queue": CeleryQueues.UTILITY},
        # Utility Queue - Lightweight maintenance tasks (concurrency=8)
        "system.startup_recovery": {"queue": CeleryQueues.UTILITY},
        "system.recover_user_files": {"queue": CeleryQueues.UTILITY},
        "system.health_check": {"queue": CeleryQueues.UTILITY},
        "cleanup_expired_files": {"queue": CeleryQueues.UTILITY},
        "cleanup.run_periodic_cleanup": {"queue": CeleryQueues.UTILITY},
        "cleanup.deep_cleanup": {"queue": CeleryQueues.UTILITY},
        "cleanup.health_check": {"queue": CeleryQueues.UTILITY},
        "cleanup.emergency_recovery": {"queue": CeleryQueues.UTILITY},
        "cleanup.scratch_janitor": {"queue": CeleryQueues.CPU},
        "cleanup.orphan_upload_sweeper": {"queue": CeleryQueues.UTILITY},
        "check_migration_status": {"queue": CeleryQueues.UTILITY},
        "finalize_v4_migration": {"queue": CeleryQueues.UTILITY},
        "export_transcript_baseline": {"queue": CeleryQueues.UTILITY},
        "compare_transcript_baseline": {"queue": CeleryQueues.UTILITY},
    },
    # Configure beat schedule for periodic tasks
    beat_schedule={
        "periodic-health-check": {
            "task": "system.health_check",
            "schedule": crontab(minute="*/10"),  # Run every 10 minutes
            "options": {"queue": "utility", "priority": 3},  # UtilityPriority.OPERATIONAL
        },
        "search-index-maintenance": {
            "task": "search_index_maintenance",
            "schedule": crontab(minute=0, hour="*/6"),  # Every 6 hours
            "options": {"queue": "cpu", "priority": 8},  # CPUPriority.MAINTENANCE
        },
        "opensearch-orphan-cleanup": {
            "task": "opensearch_orphan_cleanup",
            "schedule": crontab(minute=0, hour="3,9,15,21"),  # Every 6h, offset from maintenance
            "options": {"queue": "cpu", "priority": 8},  # CPUPriority.MAINTENANCE
        },
        "embedding-consistency-check": {
            "task": "speaker_embedding_consistency_check",
            "schedule": crontab(minute="*/10"),  # Every 10 minutes
            "options": {"queue": "cpu", "priority": 8},  # CPUPriority.MAINTENANCE
        },
        "gpu-stats-update": {
            "task": "system.update_gpu_stats",
            "schedule": crontab(minute="*/5"),  # Run every 5 minutes
            "options": {"queue": "cpu", "priority": 5},  # CPUPriority.SYSTEM
        },
        "cleanup-expired-files": {
            "task": "cleanup_expired_files",
            "schedule": crontab(minute=0),  # Every hour on the hour
            "options": {"queue": "utility", "priority": 5},  # UtilityPriority.ROUTINE
        },
        "scratch-janitor": {
            "task": "cleanup.scratch_janitor",
            "schedule": crontab(minute=15),  # Hourly at :15, offset from cleanup/maintenance
            "options": {"queue": "cpu", "priority": 5},  # CPUPriority.SYSTEM
        },
        "orphan-upload-sweeper": {
            "task": "cleanup.orphan_upload_sweeper",
            # Every 15 minutes, offset from the hourly cleanup tasks. PENDING
            # rows older than 30 min are the signal we never heard back from
            # the client / presigned PUT.
            "schedule": crontab(minute="5,20,35,50"),
            "options": {"queue": "utility", "priority": 5},  # UtilityPriority.ROUTINE
        },
    },
)


# Signal handlers for proper database connection management
@worker_process_init.connect
def init_worker_process(**kwargs):
    """Initialize worker process - register HF token and dispose connections."""
    import os
    import warnings

    # Suppress benign warnings from AI libraries (community models, library compat)
    warnings.filterwarnings("ignore", message=".*loss function.*", category=UserWarning)
    warnings.filterwarnings("ignore", message=".*not writable.*", category=UserWarning)
    warnings.filterwarnings("ignore", module="pytorch_lightning")
    warnings.filterwarnings("ignore", module="lightning_fabric")

    # Register HuggingFace token for gated model access (e.g., pyannote)
    # Skip in offline mode — models are pre-downloaded and no network is available
    hf_token = os.getenv("HUGGINGFACE_TOKEN")
    if hf_token and os.getenv("HF_HUB_OFFLINE") != "1":
        try:
            from huggingface_hub import login

            login(token=hf_token, add_to_git_credential=False)
            logger.info("HuggingFace token registered for gated model access")
        except Exception as e:
            logger.warning(f"Failed to register HuggingFace token: {e}")

    from app.db.base import engine

    engine.dispose()


@worker_ready.connect
def preload_models(**kwargs):
    """Preload AI models at worker startup.

    GPU workers: Load Whisper + PyAnnote into VRAM (shared across threads).
    CPU-transcribe workers: Load lightweight Whisper model into RAM.
    Other CPU workers: No model preloading needed.
    """
    import os

    # GPU model preloading — ONLY on GPU workers (PRELOAD_GPU_MODELS=true).
    # Other workers (cpu-processor, search-indexer, etc.) must NOT load models,
    # even if CUDA is available, to avoid wasting 15+ GB of GPU memory.
    # Set via docker-compose.yml environment for gpu worker containers only.
    is_gpu_worker = os.environ.get("PRELOAD_GPU_MODELS", "").lower() == "true"

    try:
        if is_gpu_worker:
            from app.transcription.config import TranscriptionConfig

            config = TranscriptionConfig.from_environment()
            if config.device == "cuda":
                import torch

                from app.transcription.model_manager import ModelManager

                ModelManager.get_instance().ensure_models_loaded(config)

                # Enable TF32 AFTER model loading. PyAnnote's fix_reproducibility()
                # disables TF32 during Pipeline.from_pretrained(). Re-enabling here
                # gives Whisper ~15-20% speedup on Ampere+ GPUs (RTX 3000+, A-series).
                # pipeline.py also re-enables after each diarization run.
                torch.backends.cuda.matmul.allow_tf32 = True
                torch.backends.cudnn.allow_tf32 = True
                logger.info("TF32 enabled for Tensor Core acceleration")

                # Pin the model name so subsequent tasks use the loaded model,
                # even if the admin changes the DB setting before restarting.
                TranscriptionConfig.pin_model(config.model_name)

                logger.info(
                    "GPU models preloaded and pinned "
                    f"(model={config.model_name}, "
                    f"concurrent_requests={config.concurrent_requests})"
                )
        else:
            logger.info("Skipping GPU model preload (PRELOAD_GPU_MODELS not set)")
    except Exception as e:
        logger.debug(f"GPU model preloading skipped: {e}")

    # CPU lightweight model preloading
    if os.getenv("PRELOAD_CPU_WHISPER", "").lower() == "true":
        try:
            from app.transcription.config import TranscriptionConfig as CpuTranscriptionConfig

            cpu_config = CpuTranscriptionConfig.for_cpu_lightweight()
            logger.info(
                f"Preloading CPU lightweight model '{cpu_config.model_name}' "
                f"(compute_type={cpu_config.compute_type})..."
            )
            from faster_whisper import WhisperModel

            # Load model to warm the cache — subsequent loads are instant
            _model = WhisperModel(
                cpu_config.model_name,
                device="cpu",
                compute_type="int8",
            )
            del _model
            logger.info(f"CPU lightweight model '{cpu_config.model_name}' preloaded successfully")
        except Exception as e:
            logger.warning(f"CPU lightweight model preloading failed: {e}")

    # Validate that all registered tasks have explicit queue routes
    _validate_task_routes()


def _validate_task_routes():
    """Log warnings for tasks missing from task_routes.

    Runs once at worker startup. Tasks not in task_routes and without a
    decorator-level queue= will silently go to the default 'celery' queue,
    which may not be the intended behavior.
    """
    # Tasks intentionally excluded from task_routes (dynamically routed at call time)
    intentionally_unrouted = {
        "transcription.gpu_transcribe",  # Routed to "gpu" or "cloud-asr" by dispatch.py
        "transcription.cpu_transcribe",  # Routed to "cpu-transcribe" by dispatch.py
    }

    routed_names = set(celery_app.conf.task_routes.keys())
    unrouted = []

    for name in celery_app.tasks:
        if name.startswith("celery."):
            continue
        if name in intentionally_unrouted:
            continue
        if name not in routed_names:
            unrouted.append(name)

    if unrouted:
        for name in sorted(unrouted):
            logger.warning(
                f"Task '{name}' has no task_routes entry — will go to default 'celery' queue"
            )
    else:
        logger.info(
            f"Task route validation passed: {len(routed_names)} routes, "
            f"{len(intentionally_unrouted)} intentionally dynamic"
        )


@task_postrun.connect
def close_session_after_task(**kwargs):
    """Close database connections after each task to prevent stale connections."""
    from app.db.base import engine

    engine.dispose()
